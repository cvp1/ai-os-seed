#!/usr/bin/env python3
"""memory-mesh shared core — config, event IO, identity, registry, fold logic.

Everything deterministic lives here so emit.py / fold.py / replay.py stay thin
CLIs and the selftest can exercise the real logic. NO model calls anywhere in
this file, ever (SPEC invariant 5): same logs in, same views out, on every
host, under any LLM vendor.

Layout (events repo, default ~/memory-events — separate from this code repo):
    events/<host>.ndjson      single-writer append-only log (SPEC invariant 1)
    _archive/                 rotated segments (still fetched — same repo)
    views/<audience>/         materialized: INDEX.md, CONFLICTS.md, DENIALS.md
    state/                    last-seen refs (FF guard), alert edge state
    view.version              sha256 of folded state — the staleness contract
"""
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
MESH_ROOT = Path(os.environ.get("MESH_ROOT", os.path.expanduser("~/memory-events")))


def _host():
    """Fleet slug, not hostname. Resolution: env → persisted host file →
    hostname. This box is the cautionary tale: hostname 'imac', fleet slug
    '{{REDACTED}}' — the identity fork that once cost a nine-day silent
    heartbeat gap. install.sh persists the slug once; nothing guesses."""
    if os.environ.get("MESH_HOST"):
        return os.environ["MESH_HOST"]
    f = Path(os.path.expanduser("~/.config/memory-mesh/host"))
    if f.exists():
        return f.read_text().strip()
    return socket.gethostname().split(".")[0].lower()


HOST = _host()

KINDS = {"assert", "correct", "lesson", "denial", "retract",
         "propose-correct", "update-pointer"}
POLARITIES = {"exists", "absent", "n/a"}
LINEAGES = {"operator-direct", "contains-untrusted"}
AUDIENCES = {"operator", "family", "shared"}
CONFIDENCES = {"operator-stated", "verified-live", "inferred"}

# Audience visibility: which event audiences each view folds in.
VIEW_INCLUDES = {"operator": {"operator", "shared"}, "family": {"family", "shared"}}

INDEX_BUDGET = 20_480          # bytes, per SPEC (generated index hard cap)
MAX_EVENT_BYTES = 8_192        # bound every loop and output (Principle 8)
MAX_LINE_SUSPECT_SKEW = 300    # seconds into the future before ts is SUSPECT


# ── config ────────────────────────────────────────────────────────────────────
def _load_toml(path):
    """tomllib on 3.11+; a 20-line fallback for older interpreters ({{REDACTED}}
    ships system Python 3.9). The fallback handles exactly the two shapes our
    files use — [[array-of-tables]] and flat `key = "value"` — nothing more,
    ON PURPOSE: if a config grows past that, this parser fails loudly instead
    of half-reading it."""
    text = Path(path).read_text()
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass
    out, cur = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            name = line[2:-2].strip()
            cur = {}
            out.setdefault(name, []).append(cur)
        elif line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            cur = out.setdefault(name, {})
        elif "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            if not (v.startswith('"') and v.endswith('"')):
                raise ValueError(f"toml fallback: only string values supported: {raw!r}")
            (cur if cur is not None else out)[k] = v[1:-1]
        else:
            raise ValueError(f"toml fallback: unparseable line {raw!r}")
    return out


def peers():
    """[(host, ssh_alias)] for every mesh host that isn't us — from mesh.toml.
    An absent/empty [[hosts]] list is SOLO MODE, not an error: the mesh is
    fully functional on one machine (no nudges, no --sync) until peers are
    enrolled (seed recipients start here — cc-seed ENROLL.md)."""
    cfg = _load_toml(CODE_DIR / "mesh.toml")
    return [(h["name"], h["ssh"]) for h in (cfg.get("hosts") or [])
            if h["name"] != HOST]


# ── event identity & IO ───────────────────────────────────────────────────────
def event_id(host, session, ts, content, kind="", subject=""):
    """Content-derived id = idempotent producer (SPEC: Kafka-gap table row 2).
    A retried append produces the same id; the fold dedups.

    v1.2 (Grok post-impl review): kind + subject joined the basis — without
    them an assert and a retract carrying the same content in the same second
    collided, and the fold silently ate the second as a 'retry'. Safe change:
    ids are STORED in events, never recomputed at fold, so existing
    supersedes references are unaffected; only new events derive this way."""
    raw = "|".join((host, session, ts, kind, subject, content))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_event(kind, subject, content, *, session, polarity="n/a", home=None,
               lineage="operator-direct", audience="operator",
               confidence="inferred", supersedes=None, pin=False, ts=None):
    ts = ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = {"id": event_id(HOST, session, ts, content, kind, subject), "ts": ts, "host": HOST,
          "session": session, "kind": kind, "subject": subject,
          "polarity": polarity, "content": content, "home": home,
          "lineage": lineage, "audience": audience, "confidence": confidence,
          "supersedes": supersedes, "sig": None}
    if pin:
        ev["pin"] = True
    problems = validate_event(ev)
    if problems:
        raise ValueError("invalid event: " + "; ".join(problems))
    line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
    if len(line.encode()) > MAX_EVENT_BYTES:
        raise ValueError(f"event exceeds {MAX_EVENT_BYTES}B — split it or point at a doc")
    return ev, line


def validate_event(ev):
    p = []
    for k in ("id", "ts", "host", "session", "kind", "subject", "content"):
        if not ev.get(k):
            p.append(f"missing {k}")
    if ev.get("kind") not in KINDS:
        p.append(f"bad kind {ev.get('kind')!r}")
    if ev.get("polarity", "n/a") not in POLARITIES:
        p.append(f"bad polarity {ev.get('polarity')!r}")
    if ev.get("lineage") not in LINEAGES:
        p.append(f"bad lineage {ev.get('lineage')!r}")
    if ev.get("audience") not in AUDIENCES:
        p.append(f"bad audience {ev.get('audience')!r}")
    if ev.get("confidence") not in CONFIDENCES:
        p.append(f"bad confidence {ev.get('confidence')!r}")
    if ev.get("kind") == "assert" and not ev.get("home"):
        # One home per fact: an assertion without a home is a fact-copy trying
        # to be born. The home pointer is what keeps memory out of the truth
        # business (FLEET.md seam rule 1).
        p.append("assert requires home (one home per fact)")
    return p


# ── signatures (v1.3) ────────────────────────────────────────────────────────
# Reuses the fleet's existing human-signature machinery: ssh-keygen -Y over
# Ed25519 keys in ~/.key/signing/, verified against cc-handoff/allowed_signers
# — ONE signer registry for the fleet (one home per fact applies to keys too).
#
# The namespace is 'memory-mesh', deliberately NOT cc-handoff's: a signature
# over a signed task file must never verify as an event signature. Different
# namespace = different signed blob = no cross-protocol replay.
SIG_NAMESPACE = "memory-mesh"
# Env overrides exist for DRILLS ONLY: the real key is passphrase-protected in
# the fscrypt vault, which is the actual gate — an agent cannot sign, by
# construction, because it cannot supply Craig's passphrase. Drills need a
# throwaway keypair to exercise the verify path end-to-end.
def _signers_file():
    """One signer registry for the fleet, but its checkout path differs by
    host ({{REDACTED}}/cvptp: ~/Github/CC/cc-handoff; {{REDACTED}}: ~/cc-handoff).
    A host that can't find it treats every signature as unverified — which
    silently forked view.version fleet-wide (found 2026-07-28). Probe the
    known homes; env override wins (drills)."""
    if os.environ.get("MESH_ALLOWED_SIGNERS"):
        return Path(os.environ["MESH_ALLOWED_SIGNERS"])
    cands = [Path(os.path.expanduser(p)) for p in
             ("~/Github/CC/cc-handoff/allowed_signers",
              "~/cc-handoff/allowed_signers")]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


ALLOWED_SIGNERS = _signers_file()
# Signer identity + key are per-operator (seed recipients set MESH_SIGNER /
# MESH_SIGNING_KEY; the id must match a line in allowed_signers).
SIGNER = os.environ.get("MESH_SIGNER", "craig@fleet")
SIGNING_KEYS = {SIGNER: Path(os.environ.get(
    "MESH_SIGNING_KEY", os.path.expanduser("~/.key/signing/craig2_ed25519")))}


def canonical_bytes(ev):
    """The exact bytes a signature covers: the event minus its own signature
    and minus fold-local underscore fields, keys sorted. Deterministic across
    hosts and Python versions — a signature made here verifies everywhere."""
    payload = {k: v for k, v in ev.items()
               if k not in ("sig", "signer") and not k.startswith("_")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def verify_sig(ev, allowed=None):
    """True iff ev carries a signature that verifies for its claimed signer
    against the registry. Degrades toward safety: a malformed/absent registry
    or a broken ssh-keygen yields False (unsigned), never a free pass."""
    allowed = Path(allowed or ALLOWED_SIGNERS)
    if not ev.get("sig") or not ev.get("signer") or not allowed.exists():
        return False
    import tempfile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".sig", delete=False) as f:
            f.write(ev["sig"])
            sigpath = f.name
        r = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(allowed),
             "-I", ev["signer"], "-n", SIG_NAMESPACE, "-s", sigpath],
            input=canonical_bytes(ev), capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(sigpath)
        except OSError:
            pass


def sign_event(ev, signer="craig@fleet"):
    """Attach a detached SSH signature over canonical_bytes(ev).

    TIME-BOXED AUTHORITY (v1.4). The key is passphrase-protected per KEYS.md,
    which made v1.3 signing require Craig to type a passphrase for every
    single event — so in practice nothing would ever be signed and the
    "unsigned contradicts signed" rule would stay permanently dormant. A
    guardrail nobody can afford to use is worse than none: it creates
    confidence without protection.

    So signing prefers the ssh-agent path: Craig runs
        ssh-add -t 8h ~/.key/signing/craig2_ed25519
    ONCE (entering the passphrase once), and for that TTL this signs through
    the agent using only the PUBLIC half. Authority is delegated for a bounded
    window and expires by itself — instead of being either always-on
    (passphrase-less key on disk) or never-usable (passphrase per event).

    The passphrase still gates everything: no agent load, no signing. An agent
    with the key absent falls back to the private key on disk, which prompts —
    correct behaviour when Craig IS at a terminal.
    """
    key = SIGNING_KEYS.get(signer)
    if key is None:
        raise ValueError(f"unknown signer {signer!r} (known: {sorted(SIGNING_KEYS)})")
    pub = Path(str(key) + ".pub")
    attempts = []
    if pub.exists() and os.environ.get("SSH_AUTH_SOCK"):
        attempts.append(("agent", pub))     # bounded-window authority
    if key.exists():
        attempts.append(("key file", key))  # interactive fallback
    if not attempts:
        raise RuntimeError(f"no signing material for {signer}: {key} absent — "
                           f"is ~/.key unlocked? (keyvault/unlock.sh)")
    errs = []
    for how, path in attempts:
        # -U is required on the agent path: it tells ssh-keygen -f is a PUBLIC
        # key and the private half lives in the agent. Without it, ssh-keygen
        # tries to load the .pub as a private key and falls back to prompting.
        agent_flag = ["-U"] if how == "agent" else []
        r = subprocess.run(["ssh-keygen", "-Y", "sign", *agent_flag,
                            "-f", str(path), "-n", SIG_NAMESPACE, "-"],
                           input=canonical_bytes(ev), capture_output=True, timeout=60)
        if r.returncode == 0:
            ev["signer"] = signer
            ev["sig"] = r.stdout.decode()
            ev["_signed_via"] = how
            return ev
        errs.append(f"{how}: {r.stderr.decode().strip()[:120]}")
    raise RuntimeError(
        "signing failed — " + " | ".join(errs) +
        "\nhint: load the key into the agent once for a bounded window:\n"
        "      ssh-add -t 8h ~/.key/signing/craig2_ed25519")


# ── subject registry ─────────────────────────────────────────────────────────
_SUBJECT_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9._:-]+$")


def load_registry():
    return _load_toml(CODE_DIR / "subjects.toml")


def subject_problem(subject, registry):
    """None if the subject parses against the registry, else a reason.
    Unregistered shapes get PARKED as UNNORMALIZED (not rejected at emit —
    the producer warns, the fold parks; naming drift must be visible, and a
    hard emit-reject would just push sessions to lie about the class)."""
    if not _SUBJECT_RE.match(subject):
        return f"subject {subject!r} not class/entity shaped"
    cls = subject.split("/", 1)[0]
    if cls not in registry.get("classes", {}):
        return f"unregistered subject class {cls!r}"
    return None


# ── git helpers ──────────────────────────────────────────────────────────────
def git(*args, cwd=None, check=True, timeout=60):
    r = subprocess.run(["git", "-C", str(cwd or MESH_ROOT), *args],
                       capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def head_blob(path, cwd=None):
    """Read a file AS COMMITTED (SPEC invariant 3) — the hash chain guards
    what the fold consumes; the working tree is the surface the 0-byte
    incident corrupted while reporting clean."""
    r = subprocess.run(["git", "-C", str(cwd or MESH_ROOT), "show", f"HEAD:{path}"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def committed_log_paths(cwd=None):
    out = git("ls-tree", "-r", "--name-only", "HEAD", cwd=cwd, check=False)
    return [p for p in out.splitlines()
            if (p.startswith("events/") or p.startswith("_archive/"))
            and p.endswith(".ndjson")]


def append_event_line(line, log=None):
    """Append one event line to this host's log, healing a torn tail first.

    A crash mid-write can leave a half-line with no trailing newline (drill
    3's scenario). A blind append would CONCATENATE the next event onto that
    fragment — destroying a good event to preserve a dead one (found by
    drill 7 running after drill 3, 2026-07-28). If the last byte isn't \\n,
    lead with one: the torn fragment stays its own held-out line and the new
    event lands clean."""
    log = log or MESH_ROOT / "events" / f"{HOST}.ndjson"
    log.parent.mkdir(parents=True, exist_ok=True)
    lead = ""
    if log.exists() and log.stat().st_size:
        with open(log, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                lead = "\n"
    with open(log, "a", encoding="utf-8") as f:
        f.write(lead + line + "\n")
    return log


def unsuperseded_ids(subject, events=None):
    """Ids of every committed event on `subject` not yet superseded (and not
    denial/retract). Deliberately WIDER than fold['live']: the lesson dedup
    serves only the latest revision, but a supersede that covered only the
    latest would resurrect the older revision on the next fold. Shared by
    emit.py --supersedes-live-on and home_watch.py."""
    if events is None:
        events, _ = read_all_events()
    sup = set()
    for e in events:
        raw = e.get("supersedes")
        for s in ([raw] if isinstance(raw, str) else (raw or [])):
            sup.add(s)
    return sorted(e["id"] for e in events
                  if e["subject"] == subject and e["id"] not in sup
                  and e["kind"] not in ("denial", "retract"))


# ── fold core (pure: events in → views out) ──────────────────────────────────
def read_all_events(cwd=None):
    """All committed events from all logs. Returns (events, problems).
    Dedup by id (idempotence); host↔filename validation (invariant 1)."""
    seen, events, problems = set(), [], []
    now = datetime.datetime.now(datetime.timezone.utc)
    for path in committed_log_paths(cwd):
        owner = Path(path).stem.split(".")[0]
        blob = head_blob(path, cwd=cwd)
        if blob is None:
            continue
        for n, line in enumerate(blob.splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{path}:{n} unparseable — held out")
                continue
            if ev.get("host") != owner:
                problems.append(f"{path}:{n} host {ev.get('host')!r} != file owner "
                                f"{owner!r} — single-writer violation, held out")
                continue
            if ev["id"] in seen:
                continue                      # idempotent replay of a retry
            seen.add(ev["id"])
            ev["_line"] = n                   # per-writer offset (Kafka offset)
            ev["_suspect"] = False
            try:
                ets = datetime.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                if (ets - now).total_seconds() > MAX_LINE_SUSPECT_SKEW:
                    ev["_suspect"] = True     # ordering hint only — never truth
            except ValueError:
                ev["_suspect"] = True
            events.append(ev)
    events.sort(key=lambda e: (e["ts"], e["host"], e["_line"]))
    return events, problems


def fold_events(events, registry):
    """The deterministic rule pass. Returns a dict of fold results.
    Resolution is explicit (supersedes by id) — never temporal (invariant 4)."""
    by_id = {e["id"]: e for e in events}
    superseded, dangling = set(), []
    for e in events:
        # v1.3: supersedes accepts a LIST — a two-sided park took two
        # resolution events before, which made the common case (both claims
        # wrong, one correction) awkward enough to discourage resolving.
        raw = e.get("supersedes")
        for s in ([raw] if isinstance(raw, str) else (raw or [])):
            if s in by_id:
                superseded.add(s)
            else:
                dangling.append((e["id"], s))   # compaction-bug tripwire
    # Signature verification is part of the fold, not the producer: every host
    # independently re-verifies, so a forged 'sig' string is caught everywhere
    # rather than trusted because it arrived looking signed.
    for e in events:
        e["_signed"] = verify_sig(e) if e.get("sig") else False
        if e.get("sig") and not e["_signed"]:
            e["_badsig"] = True
    # retract is excluded from serving: its whole job is the supersedes edge
    # it carries — a retraction that RENDERED would resurrect the content it
    # exists to remove (caught live on the first real retract, 2026-07-27).
    live = [e for e in events if e["id"] not in superseded
            and e["kind"] not in ("denial", "propose-correct", "retract")]

    unnormalized = [e for e in live if subject_problem(e["subject"], registry)]
    normalized = [e for e in live if not subject_problem(e["subject"], registry)]

    # Contradiction rules (SPEC fold step 3) — deterministic, no model.
    parked, alarms = {}, []
    by_subject = {}
    for e in normalized:
        if e["kind"] in ("assert", "correct"):
            by_subject.setdefault(e["subject"], []).append(e)
    for subj, evs in sorted(by_subject.items()):
        pols = {e["polarity"] for e in evs if e["polarity"] != "n/a"}
        if {"exists", "absent"} <= pols:
            parked[subj] = evs + parked.get(subj, [])
            continue
        asserts = [e for e in evs if e["kind"] == "assert"]
        if len({e["content"] for e in asserts}) > 1:
            parked[subj] = evs
            continue
        # LIVE as of v1.3 (was dormant while nothing signed): an operator-
        # signed claim is truth; anything unsigned disagreeing with it parks
        # AND alarms — that is the poisoned-session tripwire.
        signed = [e for e in evs if e.get("_signed")]
        if signed and len({e["content"] for e in evs}) > 1:
            parked[subj] = evs
            alarms.append(f"unsigned event contradicts SIGNED truth on {subj}")
    for e in events:
        if e.get("_badsig"):
            alarms.append(f"event {e['id']} carries a signature that DOES NOT "
                          f"verify (claimed signer {e.get('signer')!r}) — forged "
                          f"or key rotated; treated as unsigned")
    for eid, missing in dangling:
        alarms.append(f"event {eid} supersedes missing {missing} — compaction bug?")

    # Lessons obey invariant 4 like everything else (Grok review 4, priority
    # 1 — this replaced a latest-wins temporal pick that contradicted the
    # constitution): a revision supersedes its predecessors explicitly (the
    # producer resolves the chain), so >1 live lesson with DIFFERING content
    # on one subject is a real race → park it. Identical restatements are not
    # a conflict — collapse to the earliest and hold the copies out.
    dup_lessons = set()
    lessons_by_subject = {}
    for e in normalized:
        if e["kind"] == "lesson":
            lessons_by_subject.setdefault(e["subject"], []).append(e)
    for subj, evs in sorted(lessons_by_subject.items()):
        if len({e["content"] for e in evs}) > 1:
            parked[subj] = evs + parked.get(subj, [])
        elif len(evs) > 1:
            dup_lessons |= {e["id"] for e in evs[1:]}

    parked_ids = {e["id"] for evs in parked.values() for e in evs}
    servable = [e for e in normalized
                if e["id"] not in parked_ids and e["id"] not in dup_lessons]
    denials = [e for e in events if e["kind"] == "denial"]
    proposals = [e for e in events if e["kind"] == "propose-correct"
                 and e["id"] not in superseded]
    return {"live": servable, "parked": parked, "unnormalized": unnormalized,
            "denials": denials, "proposals": proposals, "alarms": alarms,
            "total": len(events)}


def score_for_index(e, correction_counts, session_breadth):
    """Eviction priority (SPEC): pinned → signed → correction-history →
    breadth (distinct sessions per subject — raw recall counts are gameable)
    → recency last. Higher tuple sorts first."""
    return (1 if e.get("pin") else 0,
            1 if e.get("_signed") else 0,
            correction_counts.get(e["subject"], 0),
            session_breadth.get(e["subject"], 0),
            e["ts"])


def ranked_index(fold, audience):
    """One audience's live events in eviction-priority order (see
    score_for_index) — shared by the mesh INDEX view and the harness
    MEMORY.md renderer so both serve the identical ranking."""
    inc = VIEW_INCLUDES[audience]
    vis = [e for e in fold["live"] if e["audience"] in inc]
    correction_counts, session_breadth = {}, {}
    for e in vis:
        if e["kind"] == "correct":
            correction_counts[e["subject"]] = correction_counts.get(e["subject"], 0) + 1
        session_breadth.setdefault(e["subject"], set()).add(e["session"])
    session_breadth = {k: len(v) for k, v in session_breadth.items()}
    return sorted(vis, key=lambda e: score_for_index(
        e, correction_counts, session_breadth), reverse=True)


def index_row(e):
    return (f"- [{e['subject']}] {e['content'][:200]}"
            f"{' → ' + e['home'] if e.get('home') else ''}"
            f" ({e['ts'][:10]}{', SUSPECT-ts' if e['_suspect'] else ''})")


def budgeted_rows(ranked, head_lines):
    """head_lines + one row per event, hard-capped at INDEX_BUDGET bytes with
    the overflow noted (bound every output — Principle 8)."""
    lines = list(head_lines)
    used = sum(len(l) + 1 for l in lines)
    shown = 0
    for e in ranked:
        row = index_row(e)
        if used + len(row) + 1 > INDEX_BUDGET:
            lines.append(f"… {len(ranked) - shown} more demoted by budget "
                         f"({INDEX_BUDGET}B cap) — recall reaches them")
            break
        lines.append(row)
        used += len(row) + 1
        shown += 1
    return lines


def render_views(fold, audience):
    """Materialize one audience's views. Pure string-building."""
    inc = VIEW_INCLUDES[audience]
    ranked = ranked_index(fold, audience)

    lines = budgeted_rows(ranked, [
        f"# INDEX ({audience}) — GENERATED by memory-mesh fold; never hand-edit",
        f"# fold of {fold['total']} events; {len(fold['parked'])} subject(s) parked", ""])

    conflicts = [f"# CONFLICTS ({audience}) — parked subjects; served only as UNRESOLVED", ""]
    for subj, evs in sorted(fold["parked"].items()):
        vis_evs = [e for e in evs if e["audience"] in inc]
        if not vis_evs:
            continue
        conflicts.append(f"## {subj}")
        for e in sorted(vis_evs, key=lambda x: (x["ts"], x["host"], x["_line"])):
            conflicts.append(f"- {e['ts']} {e['host']}/{e['session'][:8]} "
                             f"[{e['kind']}/{e['polarity']}] {e['content'][:160]} "
                             f"(id {e['id']})")
        conflicts.append("- RESOLVE: emit kind=correct with supersedes=<losing ids>")
        conflicts.append("")
    for e in fold["unnormalized"]:
        if e["audience"] in inc:
            conflicts.append(f"- UNNORMALIZED subject {e['subject']!r} "
                             f"(id {e['id']}) — fix subjects.toml or re-emit")

    denials = [f"# DENIALS ({audience}) — blocked writes, metadata only; review or force-accept", ""]
    for e in fold["denials"]:
        if e["audience"] in inc:
            denials.append(f"- {e['ts']} {e['host']}/{e['session'][:8]}: {e['content'][:200]}")
    return {"INDEX.md": "\n".join(lines) + "\n",
            "CONFLICTS.md": "\n".join(conflicts) + "\n",
            "DENIALS.md": "\n".join(denials) + "\n"}


# ── harness MEMORY.md (cutover phase 7) ──────────────────────────────────────
# The Claude Code harness force-feeds <store>/MEMORY.md into every session.
# Post-cutover that file is GENERATED here from the folded corpus. The flip is
# per-host OPT-IN via a `.mesh-generated` marker in the store: a host whose
# curated index has not been backfilled into the mesh keeps its hand-built
# MEMORY.md untouched until it backfills and opts in ({{REDACTED}}/cvptp).

def store_dir():
    """This workspace's auto-memory store path. The workspace root is
    wherever memory-mesh/ lives (CODE_DIR's parent) — true for the CC tree
    and for any seed recipient's chosen root — and the harness keys the
    store by that path with / → - (e.g. {{HOME}}/Github/CC →
    -home-x-Github-CC)."""
    return (Path.home() / ".claude" / "projects"
            / str(CODE_DIR.parent).replace("/", "-") / "memory")


def harness_store():
    """The store, or None if this host hasn't opted into fold-generated
    MEMORY.md (the .mesh-generated marker is the per-host opt-in)."""
    store = store_dir()
    return store if (store / ".mesh-generated").exists() else None


def ondemand_slugs(store):
    """Slugs deliberately held OUT of the always-on index
    (store/_index-exclude.txt — the store's standing two-tier decision)."""
    f = store / "_index-exclude.txt"
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.add(s[:-3] if s.endswith(".md") else s)
    return out


def render_harness_memory(fold, store):
    """The harness-loaded MEMORY.md: the operator INDEX minus on-demand slugs,
    plus a compact appendix naming what /recall can reach — so flipping to
    generated never silently hides the on-demand tier from sessions."""
    def lesson_slug(e):
        return (e["subject"].split("/", 1)[1]
                if e["subject"].startswith("lesson/") else None)
    exclude = ondemand_slugs(store)
    ranked = [e for e in ranked_index(fold, "operator")
              if lesson_slug(e) not in exclude]
    lines = budgeted_rows(ranked, [
        "# MEMORY — GENERATED by memory-mesh fold; never hand-edit "
        "(overwritten within minutes)",
        f"# fold of {fold['total']} events; {len(fold['parked'])} subject(s) "
        "parked (see ~/memory-events/views/operator/CONFLICTS.md)", ""])
    if exclude:
        lines += ["", "## On-demand memories — not always-loaded; /recall reaches them", ""]
        row = ""
        for slug in sorted(exclude):
            if row and len(row) + len(slug) + 3 > 100:
                lines.append(row)
                row = ""
            row = f"{row} · {slug}" if row else slug
        if row:
            lines.append(row)
    return "\n".join(lines) + "\n"


def write_harness_memory(fold):
    """Atomically regenerate <store>/MEMORY.md if this host has opted in.
    Returns the path written, else None. Never raises — a broken store must
    not fail the fold (the mesh views above are the durable output)."""
    store = harness_store()
    if store is None:
        return None
    try:
        text = render_harness_memory(fold, store)
        tmp = store / f"MEMORY.md.tmp.{os.getpid()}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, store / "MEMORY.md")
        return store / "MEMORY.md"
    except Exception as e:  # noqa: BLE001
        print(f"fold: harness MEMORY.md write failed (views intact): {e}",
              file=sys.stderr)
        return None


def view_version(fold):
    """sha256 of folded state — identical on every host for identical logs
    (drill 5), and the staleness contract for the future PreToolUse hook."""
    reg_hash = hashlib.sha256((CODE_DIR / "subjects.toml").read_bytes()).hexdigest()[:16]
    basis = json.dumps(
        {"registry": reg_hash,
         "signed": sorted(e["id"] for e in fold["live"] if e.get("_signed")),
         "live": sorted(e["id"] for e in fold["live"]),
         "parked": {k: sorted(e["id"] for e in v) for k, v in fold["parked"].items()},
         "unnormalized": sorted(e["id"] for e in fold["unnormalized"])},
        sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()
