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
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
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
         "propose-correct", "update-pointer", "pin"}
POLARITIES = {"exists", "absent", "n/a"}
LINEAGES = {"operator-direct", "contains-untrusted"}
AUDIENCES = {"operator", "family", "shared"}
CONFIDENCES = {"operator-stated", "verified-live", "inferred"}

# Audience visibility: which event audiences each view folds in.
VIEW_INCLUDES = {"operator": {"operator", "shared"}, "family": {"family", "shared"}}

INDEX_BUDGET = 20_480          # bytes, per SPEC — the MESH's own views only.
                               # NOT an authority over the harness MEMORY.md:
                               # that artifact answers to the loader's ceilings
                               # below, measured on the COMPOSED file.

# --- the consumer's law (the delivery ceilings) ------------------------------
# The agent harness injects the WHOLE of MEMORY.md into every session and
# SILENTLY TRUNCATES past EITHER of these (bytes observed live 2026-06-16 at
# 27.1 KB -> "only part loaded"; the line axis is the harness's documented
# "first 200 lines OR first 25 KB, whichever comes first").
#
# These are the ONLY numbers the delivery gate may use, and they are measured
# on the fully assembled file — never on a section. A bound that measures a
# subsection is not Principle 8; it is Principle 8's costume. Reviewed
# 2026-07-29 (reviews/2026-07-29-grok-index-budget-review.md): the previous
# design capped the index rows at INDEX_BUDGET and then appended an unmetered
# on-demand appendix, so the "bounded" writer shipped 25,973 B every 5 minutes
# and every session lost the tail. 24_986 - 20_480 left 4,506 B of residual for
# the rest of the file; the appendix alone was 6,078 B. The cushion was already
# false when the constant was picked, because nobody measured the composition.
LOADER_BYTE_CEILING = 24_986
LOADER_LINE_CEILING = 200
# Share of the ceiling the PINNED tier may hold before the fold alarms. See the
# bound in fold_events: a majority, chosen as a regime boundary rather than a
# tuned constant — pins are uncontested residency, so past half the ceiling the
# merit ranking is no longer deciding what Craig sees.
PIN_DOMINANCE_SHARE = 0.5
# What we publish to: headroom below the ceiling, so a memory written mid-session
# cannot cross the cliff before the next fold re-renders.
DELIVERY_BYTES = 24_200
DELIVERY_LINES = 190

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

    THE PASSPHRASE IS THE SIGNATURE (v1.5, 2026-07-30). A signature here means
    "Craig personally attests", and SPEC §3 spends that meaning immediately:
    a signed event outranks any unsigned one, and "an agent cannot promote"
    is stated as a property of this key being passphrase-gated. So the
    passphrase prompt is not friction in front of the mechanism — it IS the
    mechanism. Nothing an agent can do unattended may produce a signature.

    v1.4 delegated this to ssh-agent (`ssh-add -t 8h`) on the premise that
    per-event passphrases made signing so painful nothing would ever be
    signed. That premise was never true of the built system: `sign_event` has
    exactly ONE caller (sign.py, the operator's own tool), no scheduled job
    invokes it, and in the mesh's whole life 3 of 157 events are signed — all
    operator `correct` acts. There was no frequency problem to solve. What
    the delegation did buy was real: while the agent held the key, ANY local
    process could mint Craig's authority — in the mesh (forging operator
    truth, the ASI06 memory-poisoning path this design exists to close) and,
    because cc-handoff's sign_task.py points at the same key file, on the
    fleet bus, where it silently authorized a production deploy on 2026-07-30
    with no human in the loop.

    So: key file only. No agent, ever. If there is no terminal to prompt at,
    signing FAILS — loudly and by design. An unsigned event is a known,
    handled state (it simply carries no operator authority); a signature
    produced without Craig is an unhandled one.
    """
    key = SIGNING_KEYS.get(signer)
    if key is None:
        raise ValueError(f"unknown signer {signer!r} (known: {sorted(SIGNING_KEYS)})")
    if not key.exists():
        raise RuntimeError(f"no signing material for {signer}: {key} absent — "
                           f"is ~/.key unlocked? (keyvault/unlock.sh)")
    # SSH_AUTH_SOCK is stripped, not merely unused: ssh-keygen -Y sign will
    # reach for a loaded agent identity on its own when the private key can't
    # be read non-interactively. Leaving the socket visible would leave the
    # agent path open by accident — the exact way v1.4's delegation outlived
    # the decision to end it.
    env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key),
                        "-n", SIG_NAMESPACE, "-"],
                       input=canonical_bytes(ev), capture_output=True,
                       timeout=300, env=env)
    if r.returncode != 0:
        raise RuntimeError(
            "signing failed: " + r.stderr.decode().strip()[:200] +
            "\nSigning is deliberately interactive — it needs Craig at a "
            "terminal to enter the key passphrase. Do not load this key into "
            "ssh-agent to work around this: that hands every local process "
            "Craig's authority (see this function's docstring).")
    ev["signer"] = signer
    ev["sig"] = r.stdout.decode()
    ev["_signed_via"] = "key file"
    return ev


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


# ── concurrency ──────────────────────────────────────────────────────────────
# SPEC invariant 1 calls events/<host>.ndjson a SINGLE-WRITER log, and means one
# writer per HOST — the parallelism the design was built for is across hosts.
# Several agents in one shell on one host are several writers to one file.
#
# Measured 2026-07-28, 5 concurrent emits on {{REDACTED}}: all 5 event lines
# landed intact (one write() under O_APPEND, events ~800B, far under PIPE_BUF),
# but 2 of 5 GIT COMMITS failed on index.lock. Nothing was lost only because a
# later agent's commit swept up the earlier lines — accidental recovery, and it
# does not cover the LAST writer. An uncommitted event is worse than it looks:
# read_all_events() loads from head_blob(), i.e. committed state only, so the
# event never folds into MEMORY.md and never reaches a peer. It sits on disk,
# inert, until something else happens to commit.
LOCK_PATH = MESH_ROOT / ".mesh.lock"
LOCK_WAIT = 30          # seconds to wait for the lock before failing loudly


@contextlib.contextmanager
def repo_lock(timeout=LOCK_WAIT):
    """Serialize the append+add+commit critical section across processes.

    Held across the WHOLE sequence, not per git call: locking each call
    individually still lets two agents interleave between add and commit.

    Advisory (flock) — which is sufficient because every writer is our code.
    Fails loudly on timeout rather than proceeding unserialized: a caller that
    silently skipped the lock would reintroduce exactly the race this closes.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"mesh: could not acquire {LOCK_PATH} within {timeout}s — "
                        "another writer is stuck; not proceeding unserialized")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ── git helpers ──────────────────────────────────────────────────────────────
_LOCK_ERR = ("index.lock", "unable to create", "cannot lock ref", "ref lock")


def git(*args, cwd=None, check=True, timeout=60, retries=6):
    """Run git, retrying transient LOCK contention with bounded backoff.

    Defence in depth behind repo_lock(): the fold, home_watch and a human shell
    also touch this repo and do not take our lock. Only lock-shaped failures
    retry — a real error (bad ref, conflict) must fail on the first try rather
    than being sat on for a second.
    """
    delay = 0.05
    for attempt in range(retries + 1):
        r = subprocess.run(["git", "-C", str(cwd or MESH_ROOT), *args],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        err = (r.stderr or "").lower()
        if attempt < retries and any(m in err for m in _LOCK_ERR):
            time.sleep(delay)
            delay = min(delay * 2, 1.0)
            continue
        if check:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return r.stdout
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

    # LINEAGE QUARANTINE (built 2026-07-30) — the second half of the Story 029
    # gate, which had been shipped write-side only.
    #
    # `contains-untrusted` was validated at emit and then IGNORED here, so a
    # lesson distilled from ingested/untrusted content rendered into the
    # always-on index exactly like an operator-stated one. Found by reading the
    # live store: the very first untrusted-lineage event ever written
    # (lesson/derived-threshold-is-still-invented, 2026-07-30T15:14:30Z) was
    # sitting in MEMORY.md, load-bearing, while the write-guard hook's docstring
    # and SPEC.md both claimed such facts "land in QUARANTINE.md, never
    # MEMORY.md, until Craig promotes them". A control asserted by nothing had
    # become false without anyone editing it.
    #
    # Held out BEFORE the contradiction pass, not after, and that ordering is the
    # security property: if an untrusted event could park a subject it disagreed
    # with, a single crafted page would be able to silence real doctrine by
    # contradicting it — memory poisoning by denial of service rather than by
    # substitution. It cannot reach the rule pass at all.
    #
    # Two promotion routes, both requiring the operator's passphrase-gated key
    # (sign.py; an agent cannot sign by construction):
    #   1. sign.py --promote <id>  — emits a signed `correct` superseding it.
    #   2. a signature on the event itself — Craig vouching for it in place.
    # An unknown or absent lineage quarantines and alarms: the field is required
    # at emit, so a live event missing it means the log was written by something
    # that is not this code, and the safe reading of that is "do not serve".
    alarms = []
    quarantined, keep = [], []
    for e in live:
        lin = e.get("lineage")
        if lin == "operator-direct":
            keep.append(e)
        elif lin == "contains-untrusted":
            (keep if e.get("_signed") else quarantined).append(e)
        else:
            quarantined.append(e)
            alarms.append(
                f"event {e['id']} carries lineage {lin!r}, which is not one of "
                f"{sorted(LINEAGES)} — quarantined, not served")
    live = keep

    # ── PIN OVERLAY (2026-07-31) ─────────────────────────────────────────────
    # Residency in the always-on tier is decided by score_for_index, whose first
    # key is `pin`. Until now that flag could only be set AT EMIT (`emit.py
    # --pin`), which meant an already-written memory could never become pinned:
    # the store had 2 pins out of 166 events and neither was a hard boundary, so
    # rules where THE PROMPT IS THE MECHANISM (no-auto-MFA, never-announce-
    # session-endings) held their always-on slots by luck of ranking. A busy
    # incident week could evict the OTP rule and nothing would say so.
    #
    # The obvious fix — re-emit the lesson with --pin — fails three different
    # ways depending on how it is spelled. All three measured 2026-07-31 and
    # frozen in drill 12; none of them is a hypothetical:
    #   * identical content, no supersede → the dup-lesson rule below collapses
    #     to the EARLIEST copy and holds the rest out. The pinned twin is
    #     discarded and the pin silently does nothing.
    #   * differing content → the subject PARKS. The boundary leaves the served
    #     index entirely, which for an MFA rule is worse than never pinning it.
    #   * identical content WITH a supersede — what emit.py actually does, since
    #     it auto-supersedes lessons — pins successfully and forges the record.
    #     The replacement carries a new id and today's `ts`, so a 07-28 lesson
    #     renders as learned today. The date exists so a reader can weigh
    #     recency ("recalled memories are point-in-time"); overwriting it to buy
    #     a sort key corrupts the one signal the row exists to carry, and every
    #     pinned row would claim to be new on the day someone ran the backfill.
    #
    # So a pin is an OVERLAY, not a rewrite: a `pin` event names a subject and
    # never renders. The lesson keeps its own id, content, timestamp and
    # correction history; only its residency changes. Unpinning needs no new
    # verb — a `retract` superseding the pin event drops it out of `live` here,
    # and the overlay is gone on the next fold.
    pins = [e for e in live if e["kind"] == "pin"]
    live = [e for e in live if e["kind"] != "pin"]

    unnormalized = [e for e in live if subject_problem(e["subject"], registry)]
    normalized = [e for e in live if not subject_problem(e["subject"], registry)]

    # Contradiction rules (SPEC fold step 3) — deterministic, no model.
    parked = {}
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

    # The signed-truth tripwire, ACROSS kinds (2026-07-30, found by drill 10).
    # The in-bucket rule above only ever compared assert/correct events with each
    # other, and the lesson rule only lessons with lessons — so a signed `correct`
    # and an unsigned `lesson` telling different stories about the same subject
    # both served, silently, side by side. In this store that is not an edge case:
    # 123 of 141 events are lessons, so the tripwire was blind to the dominant
    # kind, and the promotion path (sign.py emits `correct`) lands exactly there.
    # Whatever the operator signed is truth; anything live disagreeing with it
    # parks and alarms, regardless of which kind each one is.
    for subj in sorted({e["subject"] for e in normalized}):
        evs = [e for e in normalized if e["subject"] == subj]
        if not any(e.get("_signed") for e in evs):
            continue
        if len({e["content"] for e in evs}) > 1 and subj not in parked:
            parked[subj] = evs
            alarms.append(f"unsigned event contradicts SIGNED truth on {subj} "
                          f"(across kinds: "
                          f"{', '.join(sorted({e['kind'] for e in evs}))})")

    parked_ids = {e["id"] for evs in parked.values() for e in evs}
    servable = [e for e in normalized
                if e["id"] not in parked_ids and e["id"] not in dup_lessons]

    # Apply the pin overlay (see PIN OVERLAY above) against the FINAL served
    # set, because that is the only set where "did this pin do anything?" has an
    # answer. A pin naming a subject nobody serves — a typo, or a lesson that has
    # since been retracted or parked — is dangling, and dangling MUST alarm: a
    # pin that silently protects nothing is indistinguishable from a pin that
    # works, and the whole point of pinning a boundary is that its absence is
    # never silent. Same reasoning as the dangling-supersedes tripwire above.
    pinned_subjects = {e["subject"] for e in pins}
    for e in servable:
        if e["subject"] in pinned_subjects:
            e["_pin"] = True
    for subj in sorted(pinned_subjects - {e["subject"] for e in servable}):
        alarms.append(f"PIN on {subj} protects nothing — no live event has that "
                      f"subject (typo, or the target was retracted/parked)")

    # Bound the pinned tier (Principle 8). Pinned rows are UNCONTESTED
    # residency: they never lose a slot, so every pin is a permanent withdrawal
    # from the budget the other ~112 rows compete over. Past a point the ranking
    # stops ranking and the index is just the pin list — and because an agent
    # can emit a pin, that is also the amplifier shape for memory poisoning
    # ([[improve-loop-poisoning-surface]]): pinning does not make content
    # trusted (lineage quarantine runs first, above) but it does buy a poisoned
    # operator-direct lesson a permanent front-page slot at real doctrine's
    # expense. The bound is a MAJORITY, not a tuned number — at >50% the pinned
    # set outweighs everything merit-ranked, which is a regime change rather
    # than a threshold anyone had to measure or invent.
    pinned_bytes = sum(len(index_row(e).encode("utf-8")) + 1 for e in servable
                       if (e.get("_pin") or e.get("pin"))
                       and e["audience"] in VIEW_INCLUDES["operator"])
    if pinned_bytes > LOADER_BYTE_CEILING * PIN_DOMINANCE_SHARE:
        alarms.append(
            f"pinned rows are {pinned_bytes} B of the {LOADER_BYTE_CEILING} B "
            f"loader ceiling (>{PIN_DOMINANCE_SHARE:.0%}) — the pinned tier now "
            f"outweighs the merit-ranked one; eviction order is decorative")
    # A quarantined claim cannot park a served subject (see above), but it can
    # still SAY that it disagrees with one. That is the poisoned-source tripwire:
    # untrusted content arriving with a different story about a fact we already
    # serve is the shape MemGhost produces, and it is worth a look even though
    # nothing was overwritten.
    served_content = {}
    for e in servable:
        served_content.setdefault(e["subject"], set()).add(e["content"])
    for e in quarantined:
        other = served_content.get(e["subject"])
        if other and e["content"] not in other:
            alarms.append(f"QUARANTINED event {e['id']} disagrees with served "
                          f"content on {e['subject']} — untrusted source "
                          f"contradicting live memory; review QUARANTINE.md")

    denials = [e for e in events if e["kind"] == "denial"]
    proposals = [e for e in events if e["kind"] == "propose-correct"
                 and e["id"] not in superseded]
    return {"live": servable, "parked": parked, "unnormalized": unnormalized,
            "quarantined": quarantined, "denials": denials,
            "proposals": proposals, "alarms": alarms, "total": len(events)}


def score_for_index(e, correction_counts, session_breadth):
    """Eviction priority (SPEC): pinned → signed → correction-history →
    breadth (distinct sessions per subject — raw recall counts are gameable)
    → recency last. Higher tuple sorts first."""
    # `pin` is the emit-time flag on the event itself; `_pin` is the overlay a
    # later `pin` event applies to an already-written memory. Same tier — the
    # two differ only in when the operator decided, which is not a ranking fact.
    return (1 if (e.get("pin") or e.get("_pin")) else 0,
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
    """One index line: subject, content, optional home pointer, and a date.

    The date is rendered MM-DD, not YYYY-MM-DD. It exists so a reader can weigh
    recency ("recalled memories are point-in-time — verify before asserting"), and
    month-day carries that; the year does not, because a doctrine index that
    reaches back years is a different problem than a stale row. Full precision
    lives in the event (`ts`) and in the memory's own home file — this is a view,
    not the fact. Measured 2026-07-29: 6 B x 113 rows, the only byte saving
    available that costs no coverage, since the subject slug is a load-bearing
    pointer and row prose is already tight (median content 117 chars).
    """
    return (f"- [{e['subject']}] {e['content'][:200]}"
            f"{' → ' + e['home'] if e.get('home') else ''}"
            # .get, not [] — `_suspect` is stamped by read_all_events, so a
            # caller holding events built any other way (fold_events is public
            # and the pin-dominance bound calls this) would otherwise take a
            # KeyError from a RENDERER while trying to measure a bound.
            f" ({e['ts'][5:10]}{', SUSPECT-ts' if e.get('_suspect') else ''})")


def line_bytes(s):
    """UTF-8 bytes this line costs in a "\\n".join(), newline included.

    len() counts CHARACTERS. Every budget here is in BYTES, and these indexes
    are full of em-dashes and arrows (3 bytes each), so char-counting silently
    under-measures — 19,615 chars was 19,895 bytes when this was found.
    """
    return len(s.encode("utf-8")) + 1


def budgeted_rows(ranked, head_lines, cap=INDEX_BUDGET, line_cap=None):
    """head_lines + one row per event, hard-capped at `cap` BYTES and, when
    given, `line_cap` LINES — with the overflow noted (bound every output —
    Principle 8).

    The demotion note is RESERVED for up front rather than appended after the
    break: the old version appended it past the cap check, so the "bounded"
    section could exceed its own budget by exactly the width of the line that
    announced the bound.
    """
    lines = list(head_lines)
    used = sum(line_bytes(l) for l in lines)
    # Worst-case width of the note, reserved before the first row is admitted.
    reserve = line_bytes(f"… {len(ranked)} more demoted by budget "
                         f"({cap}B cap) — recall reaches them")
    shown = 0
    for e in ranked:
        row = index_row(e)
        over_bytes = used + line_bytes(row) + reserve > cap
        over_lines = line_cap is not None and len(lines) + 2 > line_cap
        if over_bytes or over_lines:
            break
        lines.append(row)
        used += line_bytes(row)
        shown += 1
    if shown < len(ranked):
        lines.append(f"… {len(ranked) - shown} more demoted by budget "
                     f"({cap}B cap) — recall reaches them")
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

    quar = [f"# QUARANTINE ({audience}) — untrusted-lineage facts, NOT served",
            "#",
            "# These were written by a session working on ingested or otherwise",
            "# untrusted content (lineage: contains-untrusted). They are held out of",
            "# INDEX.md and out of the harness MEMORY.md, and they cannot park or",
            "# contradict a served fact. Promote with the operator's key:",
            "#",
            "#     memory-mesh/sign.py --promote <id>",
            "#",
            "# or drop one by emitting a retract that supersedes it. Doing nothing is",
            "# a valid outcome — an unpromoted lesson simply never becomes doctrine.",
            ""]
    for e in sorted(fold.get("quarantined", []),
                    key=lambda x: (x["ts"], x["host"], x["_line"])):
        if e["audience"] not in inc:
            continue
        quar.append(f"## {e['subject']}  (id {e['id']})")
        quar.append(f"- {e['ts']} {e['host']}/{e['session'][:8]} "
                    f"[{e['kind']}/{e['confidence']}]"
                    f"{' → ' + e['home'] if e.get('home') else ''}")
        quar.append(f"- {e['content'][:400]}")
        quar.append("")

    return {"INDEX.md": "\n".join(lines) + "\n",
            "CONFLICTS.md": "\n".join(conflicts) + "\n",
            "DENIALS.md": "\n".join(denials) + "\n",
            "QUARANTINE.md": "\n".join(quar) + "\n"}


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


DEFAULT_MESH_ROOT = Path(os.path.expanduser("~/memory-events"))


def harness_store():
    """The store, or None if this host hasn't opted into fold-generated
    MEMORY.md (the .mesh-generated marker is the per-host opt-in).

    SANDBOX GUARD. store_dir() derives from where this CODE lives, not from
    MESH_ROOT — so a fold run against a throwaway event log (the drills, any
    replay or sandbox) still resolved to the OPERATOR's real store and
    published that sandbox's fold as the live always-on memory. Observed
    2026-07-29: after a drill run, MEMORY.md was 22 events of `conc-0` /
    `pre-rebase` test fixtures instead of 110 real memories. The 5-minutely
    fold repairs it, so it never persisted — but any session starting inside
    that window loaded test fixtures as its standing context, silently.

    A sandbox must never be able to write the operator's brain. If MESH_ROOT
    has been pointed somewhere other than the real log, this is not the run
    that owns MEMORY.md.
    """
    if MESH_ROOT.resolve() != DEFAULT_MESH_ROOT.resolve():
        return None
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


ONDEMAND_HEADING = "## On-demand memories — not always-loaded; /recall reaches them"


def delivery_breach(text, byte_cap=LOADER_BYTE_CEILING,
                    line_cap=LOADER_LINE_CEILING):
    """Reasons `text` violates the CONSUMER's limits; [] means it loads whole.

    Measured on the fully assembled document, in UTF-8 bytes AND lines. This is
    the one predicate the write gate consults — no section, no proxy.
    """
    nbytes = len(text.encode("utf-8"))
    nlines = len(text.splitlines())
    out = []
    if nbytes > byte_cap:
        out.append(f"{nbytes} B exceeds the {byte_cap} B loader ceiling "
                   f"(+{nbytes - byte_cap})")
    if nlines > line_cap:
        out.append(f"{nlines} lines exceeds the {line_cap}-line loader ceiling "
                   f"(+{nlines - line_cap})")
    return out


def _slug_rows(slugs, width=100):
    """Pack slugs into ' · '-joined rows no wider than `width` characters."""
    rows, row = [], ""
    for slug in slugs:
        if row and len(row) + len(slug) + 3 > width:
            rows.append(row)
            row = ""
        row = f"{row} · {slug}" if row else slug
    if row:
        rows.append(row)
    return rows


def _assemble_harness_memory(head, ranked, n_rows, slugs, n_slugs):
    """One candidate document: `n_rows` index rows and `n_slugs` named slugs.

    The on-demand tier ALWAYS keeps an existence stub. That appendix exists so
    flipping to a generated index never silently hides the second tier; a fit
    path that drops it to nothing would re-create the very bug it was added to
    fix, and would render "no data" as though it were "no such tier"
    ([[no-data-must-not-render-as-positive-data]]).
    """
    lines = list(head)
    lines += [index_row(e) for e in ranked[:n_rows]]
    if n_rows < len(ranked):
        lines.append(f"… {len(ranked) - n_rows} more demoted by budget "
                     f"— /recall reaches them")
    if slugs:
        lines += ["", ONDEMAND_HEADING, ""]
        lines += _slug_rows(slugs[:n_slugs])
        if n_slugs < len(slugs):
            lines.append(
                f"… and {len(slugs) - n_slugs} more not listed "
                f"({len(slugs)} on-demand total) — /recall reaches them"
                if n_slugs else
                f"{len(slugs)} on-demand memories — not listed here; "
                f"/recall reaches them")
    return "\n".join(lines) + "\n"


def fit_harness_memory(head, ranked, slugs, byte_cap=DELIVERY_BYTES,
                       line_cap=DELIVERY_LINES):
    """Compose the harness index so the FULLY ASSEMBLED file fits the consumer.

    Degradation order, delivery-preserving:
      1. header + the on-demand existence stub — never dropped;
      2. ranked index rows;
      3. the named slug list, as meat between those poles.
    Slugs go first because they are pointers /recall reaches by name anyway,
    while an index row is the only always-on trace of its memory.

    Returns (text, report). The loop is bounded up front (Principle 8): each
    pass strictly decreases n_slugs or n_rows.
    """
    n_rows, n_slugs = len(ranked), len(slugs)
    for _ in range(len(ranked) + len(slugs) + 2):
        text = _assemble_harness_memory(head, ranked, n_rows, slugs, n_slugs)
        if not delivery_breach(text, byte_cap, line_cap):
            return text, {"ok": True, "rows": n_rows, "rows_total": len(ranked),
                          "slugs": n_slugs, "slugs_total": len(slugs),
                          "bytes": len(text.encode("utf-8")),
                          "lines": len(text.splitlines())}
        if n_slugs:
            n_slugs = max(0, n_slugs - max(1, n_slugs // 8))
        elif n_rows:
            n_rows -= 1
        else:
            break
    # Even the minimum does not fit: publish the safe minimum and say so LOUDLY
    # rather than a plausible-looking file the loader will amputate.
    text = _assemble_harness_memory(head, ranked, 0, slugs, 0)
    return text, {"ok": False, "rows": 0, "rows_total": len(ranked),
                  "slugs": 0, "slugs_total": len(slugs),
                  "bytes": len(text.encode("utf-8")),
                  "lines": len(text.splitlines()),
                  "reason": "does not fit even at the minimum stub"}


def render_harness_memory(fold, store):
    """The harness-loaded MEMORY.md: the operator INDEX minus on-demand slugs,
    plus an appendix naming what /recall can reach — sized so the WHOLE file
    clears the loader's ceilings. Returns (text, report)."""
    def lesson_slug(e):
        return (e["subject"].split("/", 1)[1]
                if e["subject"].startswith("lesson/") else None)
    exclude = ondemand_slugs(store)
    ranked = [e for e in ranked_index(fold, "operator")
              if lesson_slug(e) not in exclude]
    # The on-demand appendix advertises slugs as reachable by /recall. A
    # quarantined slug advertised there was the standing index pointing every
    # session at withheld material — and until the same day's recall fix, /recall
    # then served its body. Advertising a withheld fact is serving it in the weak
    # sense, so quarantined slugs come out of the appendix. They do NOT vanish:
    # the count line below and the store's quarantine projection both name them,
    # which is the difference between withheld and invisible.
    quar_slugs = {e["subject"].split("/", 1)[1]
                  for e in fold.get("quarantined", [])
                  if e["subject"].startswith("lesson/")}
    exclude = {s for s in exclude if s not in quar_slugs}
    head = [
        "# MEMORY — GENERATED by memory-mesh fold; never hand-edit "
        "(overwritten within minutes)",
        f"# fold of {fold['total']} events; {len(fold['parked'])} subject(s) "
        "parked (see ~/memory-events/views/operator/CONFLICTS.md)"]
    # A withheld fact must not be a silent one. This line is the always-on trace
    # of the quarantine's existence: without it, "nothing untrusted is pending"
    # and "the quarantine is not wired" render identically, which is the failure
    # this whole build was fixing ([[no-data-must-not-render-as-positive-data]]).
    # Emitted only when the count is nonzero, so steady state costs zero bytes.
    n_quar = sum(1 for e in fold.get("quarantined", [])
                 if e["audience"] in VIEW_INCLUDES["operator"])
    if n_quar:
        head.append(f"# {n_quar} untrusted-lineage fact(s) QUARANTINED and not "
                    "served — views/operator/QUARANTINE.md; promote: "
                    "memory-mesh/sign.py --promote <id>")
    head.append("")
    return fit_harness_memory(head, ranked, sorted(exclude))


def render_store_quarantine(fold):
    """The store's quarantine list, as a FOLD PROJECTION (2026-07-30).

    Craig ruled "if I promote it, that must be fact everywhere", and this file is
    why that was false: `memory_write.py` appended to it, its stated routing owner
    `consolidate.py` no longer exists, so nothing ever removed an entry. A memory
    promoted and served in the morning was still listed as quarantined hours later.

    The objection to generating it at all was one-home-per-fact — a second artifact
    that must agree with the mesh view. Grok 4.5's pass, and then the codebase
    itself, answered that: the fold ALREADY renders the SERVED set twice, as
    `views/<aud>/INDEX.md` and as the harness index, from one generator. With a
    single writer the two cannot disagree, which is the difference between a
    duplicate and a rendering. Refusing for the withheld set what the code already
    does for the served set was an inconsistency, not a principle.

    SLUGS AND ONE-LINE HOOKS ONLY — never bodies. This file sits in the store next
    to the memories themselves and is read by an agent looking for context; the
    quarantine exists precisely to keep untrusted prose out of that context.
    """
    inc = VIEW_INCLUDES["operator"]
    quar = sorted((e for e in fold.get("quarantined", [])
                   if e["audience"] in inc),
                  key=lambda x: x["subject"])
    lines = [
        "# 🚧 Quarantined memories — GENERATED by memory-mesh fold; never hand-edit",
        "#",
        "# Distilled by sessions working on ingested/untrusted content "
        "(lineage: contains-untrusted).",
        "# HELD OUT of the always-on index and of /recall's pack (recall serves a",
        "# tombstone, never the body). NOT standing policy.",
        "#",
        "# PROMOTE (needs Craig's passphrase-gated key — an agent cannot):",
        "#     memory-mesh/sign.py --promote <event-id>",
        "# REJECT: emit a retract superseding the event, then delete the file.",
        "#",
        "# Slugs and one-line hooks only — bodies are deliberately absent.",
        "",
    ]
    if not quar:
        lines.append("_Nothing quarantined._")
    for e in quar:
        slug = (e["subject"].split("/", 1)[1] if "/" in e["subject"]
                else e["subject"])
        lines.append(f"- **{slug}** (event `{e['id']}`) — {e['content'][:200]}")
    return "\n".join(lines) + "\n"


def write_store_quarantine(fold):
    """Atomically publish the store's quarantine projection. Opt-in like the
    index (same `.mesh-generated` marker, same sandbox guard via harness_store).
    Never raises: a broken store must not fail the fold."""
    store = harness_store()
    if store is None:
        return {"status": "skipped", "alarms": []}
    try:
        text = render_store_quarantine(fold)
        tmp = store / f"QUARANTINE.md.tmp.{os.getpid()}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, store / "QUARANTINE.md")
        return {"status": "written", "entries": len(fold.get("quarantined", [])),
                "alarms": []}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "error": str(e),
                "alarms": [f"store quarantine projection write failed: {e}"]}


def write_harness_memory(fold):
    """Atomically regenerate <store>/MEMORY.md if this host has opted in.

    THE WRITE GATE. A derived view whose consumer hard-truncates is not
    "produced" until the composed artifact satisfies the CONSUMER's bound, so
    nothing is published here that `delivery_breach` rejects, and what landed
    is re-read and re-measured afterwards — publishing bytes is not the same as
    the harness being able to load them ([[check-the-delivery-not-just-the-doing]]).

    Returns a report dict describing what happened (never None once a store is
    present); the caller is expected to surface `alarms`. Still never raises: a
    broken store must not fail the fold, whose MESH views are unaffected — but
    it must never look like success either.
    """
    store = harness_store()
    if store is None:
        return {"status": "skipped", "alarms": []}
    try:
        text, report = render_harness_memory(fold, store)
        # What is worth waking someone for. Trimming the NAMED SLUG LIST is the
        # designed, healthy degradation — the appendix is meat, /recall reaches
        # those slugs by name regardless, and it re-trims every time a memory is
        # added. Alarming on it would page on a number that drifts constantly
        # and teach the operator to ignore the channel. Losing an INDEX ROW is
        # different: that row is a memory's only always-on trace.
        alarms = []
        if not report["ok"]:
            alarms.append(
                "harness MEMORY.md cannot fit the loader ceiling even at the "
                f"minimum stub ({report['rows_total']} rows, "
                f"{report['slugs_total']} on-demand) — curate (merge/delete)")
        elif report["rows"] < report["rows_total"]:
            alarms.append(
                f"harness MEMORY.md is dropping always-on index rows: "
                f"{report['rows']}/{report['rows_total']} kept — the index has "
                f"outgrown its ceiling, curate (merge/delete)")
        tmp = store / f"MEMORY.md.tmp.{os.getpid()}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, store / "MEMORY.md")
        # Post-write verification: measure what is actually on disk, not what we
        # think we rendered. An atomic replace publishes a known-bad artifact
        # just as reliably as a good one.
        published = (store / "MEMORY.md").read_text(encoding="utf-8")
        landed = delivery_breach(published)
        if landed:
            alarms.append("harness MEMORY.md PUBLISHED OVER CEILING — "
                          "sessions are being truncated: " + "; ".join(landed))
        report.update(status="written", path=str(store / "MEMORY.md"),
                      alarms=alarms)
        return report
    except Exception as e:  # noqa: BLE001
        # The MESH views are intact; the HARNESS view — the thing that just
        # failed — is not. Say which.
        print(f"fold: harness MEMORY.md write FAILED, sessions keep the stale "
              f"copy (mesh views unaffected): {e}", file=sys.stderr)
        return {"status": "failed", "error": str(e),
                "alarms": [f"harness MEMORY.md write failed: {e}"]}


_FRONT_LINEAGE = re.compile(r"^lineage:\s*(\S+)\s*$", re.M)

# The store's vocabulary for "the operator stands behind this" differs from the
# mesh's (`craig-direct` vs `operator-direct`). Two names for one idea is itself
# a seam, but renaming either side would rewrite 140 events or every memory file,
# so the mapping is stated in one place instead of assumed in several.
TRUSTED_LINEAGES = {"craig-direct", "operator-direct"}


def store_quarantine_drift(fold, store=None):
    """Ways the STORE's quarantine surface disagrees with the mesh's (2026-07-30).

    Craig's ruling: "if I promote it, that must be fact everywhere." Promotion
    now writes both surfaces (sign.py reconcile_store), but a rule that is only
    enforced at the moment of one command drifts the first time anything else
    touches either side — which is precisely how the gate this session repaired
    came to be documented-but-absent. So the fold checks the join every run.

    This is DETECTION, not repair. It deliberately does not rewrite store files:
    the per-file `lineage:` is a fact with an owner (memory_write.py), and a fold
    that silently edited facts to match its own view would be the same class of
    mistake in the other direction. Returns a list of human-readable drifts.
    """
    store = Path(store or store_dir())
    if not store.is_dir():
        return []
    quar_subjects = {e["subject"] for e in fold.get("quarantined", [])}
    live_subjects = {e["subject"] for e in fold["live"]}
    drift = []

    def file_lineage(slug):
        p = store / f"{slug}.md"
        if not p.exists():
            return None
        m = _FRONT_LINEAGE.search(p.read_text(encoding="utf-8", errors="replace"))
        return m.group(1) if m else "absent"

    # 1. Served by the mesh, still marked untrusted in the store.
    for subj in sorted(live_subjects):
        if not subj.startswith("lesson/"):
            continue
        lin = file_lineage(subj.split("/", 1)[1])
        if lin == "contains-untrusted":
            drift.append(f"{subj} is SERVED by the mesh but its store file still "
                         f"says lineage: contains-untrusted — a half-applied "
                         f"promotion; retag it (memory_write.py retag)")
    # 2. Quarantined by the mesh, marked trusted in the store.
    for subj in sorted(quar_subjects):
        if not subj.startswith("lesson/"):
            continue
        lin = file_lineage(subj.split("/", 1)[1])
        if lin in TRUSTED_LINEAGES:
            drift.append(f"{subj} is QUARANTINED by the mesh but its store file "
                         f"says lineage: {lin} — the store would let /recall "
                         f"serve it as trusted")
    # 3. Untrusted in the store but unknown to the mesh entirely. These predate
    #    the mesh cutover, so no event carries their lineage and the mesh cannot
    #    hold them back. Named rather than fixed: backfilling them is an operator
    #    act, and silently dropping them from the quarantine picture is how a
    #    withheld fact becomes an invisible one.
    known = {s.split("/", 1)[1] for s in (live_subjects | quar_subjects)
             if s.startswith("lesson/")}
    for p in sorted(store.glob("*.md")):
        slug = p.stem
        if slug in ("MEMORY", "QUARANTINE") or slug in known:
            continue
        m = _FRONT_LINEAGE.search(p.read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1) == "contains-untrusted":
            drift.append(f"lesson/{slug} is contains-untrusted in the store but "
                         f"has NO mesh event — the mesh cannot quarantine what "
                         f"it cannot see; emit one (emit.py --kind lesson "
                         f"--subject lesson/{slug} --lineage contains-untrusted). "
                         f"NOT backfill.py: that reads the PRE-CUTOVER index "
                         f"format and matches nothing now")

    # A fourth check used to live here: parse the store's quarantine LIST and
    # flag entries no longer quarantined. It existed because that file had no
    # writer (consolidate.py, which memory_write.py's retag docstring still
    # names, does not exist) and kept listing a memory hours after it was
    # promoted and served. As of 2026-07-30 the fold GENERATES that file from
    # this same verdict set, so checking it would be asking the fold whether it
    # agrees with itself. Retired deliberately, not lost: what it protected
    # against is now structurally impossible rather than merely detected.
    return drift


def view_version(fold):
    """sha256 of folded state — identical on every host for identical logs
    (drill 5), and the staleness contract for the future PreToolUse hook."""
    reg_hash = hashlib.sha256((CODE_DIR / "subjects.toml").read_bytes()).hexdigest()[:16]
    basis = json.dumps(
        {"registry": reg_hash,
         "signed": sorted(e["id"] for e in fold["live"] if e.get("_signed")),
         "live": sorted(e["id"] for e in fold["live"]),
         "parked": {k: sorted(e["id"] for e in v) for k, v in fold["parked"].items()},
         # Quarantine is folded state: promoting a held-back fact changes what
         # every host serves, so it must move the staleness contract too.
         "quarantined": sorted(e["id"] for e in fold.get("quarantined", [])),
         "unnormalized": sorted(e["id"] for e in fold["unnormalized"])},
        sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()
