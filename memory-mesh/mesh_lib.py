#!/usr/bin/env python3
"""memory-mesh shared core — config, event IO, identity, registry, fold logic.

Deterministic only (no model calls): same logs in, same views out on every host.

Layout (events repo, default ~/memory-events — separate from this code repo):
    events/<host>.ndjson      single-writer append-only log
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
import tempfile
import sys
import time
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
MESH_ROOT = Path(os.environ.get("MESH_ROOT", os.path.expanduser("~/memory-events")))


def _host():
    """Fleet slug, not hostname: env → persisted host file → hostname."""
    if os.environ.get("MESH_HOST"):
        return os.environ["MESH_HOST"]
    f = Path(os.path.expanduser("~/.config/memory-mesh/host"))
    if f.exists():
        return f.read_text().strip()
    return socket.gethostname().split(".")[0].lower()


HOST = _host()

KINDS = {"assert", "correct", "lesson", "denial", "retract",
         "propose-correct", "update-pointer", "pin", "tier"}
# Index tier of a subject, replicated as an event. A `tier` event is an overlay
# like `pin`: it names a subject, never renders; the newest one wins.
TIERS = {"ondemand", "always"}
POLARITIES = {"exists", "absent", "n/a"}
LINEAGES = {"operator-direct", "contains-untrusted"}
# Promotion classes for an untrusted-lineage fact. Not lineages: lineage is the
# source and never changes; promotion is whether the owner vouched for it.
PROMOTION_KEY = "key-signed"        # cryptographic, agent-impossible by construction
PROMOTION_VERBAL = "verbally-signed"  # owner approved it in session
MIN_APPROVAL_WORDS = 8              # an attestation shorter than this quotes nothing
AUDIENCES = {"operator", "family", "shared"}
CONFIDENCES = {"operator-stated", "verified-live", "inferred"}

# --- residency ---------------------------------------------------------------
# Which tier a memory occupies, declared at write time — never derived from a score.
#   pinned   — owner-signed; capped by PIN_DELIVERY_SHARE.
#   doctrine — behavioural rules that must be resident; leaves always-on only
#              by a human event (supersede / demote).
#   state    — project/reference/pointer facts and notices; never always-on.
#              A notice is state + `expires`.
RESIDENCIES = {"pinned", "doctrine", "state"}
# Unset = not yet declared; the renderer treats it with legacy ranking.
RESIDENCY_UNSET = None
# Unsigned events cap at this residency; only a signed event may carry
# doctrine/pinned, so peers cannot outrank the signed tip.
MAX_UNSIGNED_RESIDENCY = "state"
# Max length of the served index line; enforced by rewrite at the door, never
# by truncation at render.
HOOK_MAX_CHARS = 140
# Audiences whose events may carry a body. Bodies replicate to every peer, so
# this is a confidentiality boundary; other audiences emit hook-only.
BODY_AUDIENCES = {"operator", "shared"}
# --- fact-shape gate -----------------------------------------------------------
# Infrastructure facts (hosts, endpoints, reachability) have one home elsewhere;
# memory points at it. make_event applies this to every text field of a lesson;
# memory_write.py keeps a mirror. Widen only on an observed miss. `0.0.0.0` is
# excluded: it is the "all sources" idiom, not a host.
FACT_SHAPES = [
    (re.compile(r"\b(?!0\.0\.0\.0\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     "an IPv4 address"),
    (re.compile(r"\blocalhost:\d{2,5}\b"), "a host:port endpoint"),
    (re.compile(r"\bno ssh\b|\bssh (?:works|fails|key)\b|permission denied \(publickey\)",
                re.I),
     "an SSH reachability claim"),
]
FACT_HOMES = ("vault 00 Meta/Fleet/FLEET.md (hosts/reachability) · the owning "
              "repo's CLAUDE.md/OPS.md/README (project facts) · the code itself "
              "(policy/config)")


def fact_shape(*texts):
    """First (match, label) found in any text, else None."""
    for t in texts:
        for rx, label in FACT_SHAPES:
            m = rx.search(t or "")
            if m:
                return m.group(0), label
    return None


def fact_refusal(content, hook=None, body=None):
    """Why this lesson text may not enter the log, or None if it may.

    make_event raises on it; batch producers report it instead.
    """
    hit = fact_shape(content, hook, body)
    if not hit:
        return None
    return (f"fact-shaped content ({hit[1]}: {hit[0]!r}) — memory stores "
            f"behavior and pointers, never a second copy of an infrastructure "
            f"fact (one home per fact). Put the fact in its home "
            f"({FACT_HOMES}), then point at it; a reference memory passes "
            f"pointer=True (emit --pointer).")

# Audience visibility: which event audiences each view folds in.
VIEW_INCLUDES = {"operator": {"operator", "shared"}, "family": {"family", "shared"}}

INDEX_BUDGET = 20_480          # bytes; the mesh's own views only, not MEMORY.md

# --- delivery ceilings ----------------------------------------------------------
# The harness loads MEMORY.md and silently truncates past either ceiling.
# Measured on the fully assembled file, never on a section.
LOADER_BYTE_CEILING = 24_986
# Byte budget for the on-demand slug appendix. 0 = only the existence stub
# renders, no slug names; /recall and retrieve.py are unaffected.
APPENDIX_BYTES = 0
LOADER_LINE_CEILING = 200
# Policy cap: share of DELIVERY_BYTES the pinned tier may hold. Unsigned pins
# past it are refused by fold_events.
PIN_DELIVERY_SHARE = 0.5
# Publish target, with headroom below the loader ceilings.
DELIVERY_BYTES = 24_200
DELIVERY_LINES = 190

MAX_EVENT_BYTES = 8_192
MAX_LINE_SUSPECT_SKEW = 300    # seconds into the future before ts is SUSPECT


# ── config ────────────────────────────────────────────────────────────────────
def _load_toml(path):
    """Parse TOML: tomllib on 3.11+, else a minimal fallback that handles only
    [[array-of-tables]] and flat string keys and fails loudly on anything else."""
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
    """[(host, ssh_alias)] for every other mesh host in mesh.toml; empty = solo mode."""
    cfg = _load_toml(CODE_DIR / "mesh.toml")
    return [(h["name"], h["ssh"]) for h in (cfg.get("hosts") or [])
            if h["name"] != HOST]


# ── event identity & IO ───────────────────────────────────────────────────────
def event_id(host, session, ts, content, kind="", subject=""):
    """Content-derived id, so a retried append yields the same id and the fold dedups.

    Ids are stored in events, never recomputed at fold."""
    raw = "|".join((host, session, ts, kind, subject, content))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Binds a signed promotion to the store file's bytes (minus its lineage line).
_FM_LINEAGE_STRIP = re.compile(r"^lineage:[ \t]*.*$\n?", re.M)


def content_fingerprint(text):
    """sha256 of a memory file's text with its `lineage:` frontmatter line stripped.

    Mirrors memory_write.py's `_FM_LINEAGE` regex; keep them in sync."""
    return hashlib.sha256(_FM_LINEAGE_STRIP.sub("", text).encode()).hexdigest()


def make_event(kind, subject, content, *, session, polarity="n/a", home=None,
               lineage="operator-direct", audience="operator",
               confidence="inferred", supersedes=None, pin=False, ts=None,
               residency=RESIDENCY_UNSET, hook=None, body=None, expires=None,
               carry_forward=False, body_sha256=None, verbal_approval=None,
               pointer=False, tier=None):
    # Producer gate: every event path funnels through here.
    # carry_forward=True lets retag/supersede re-emit old content verbatim.
    if kind == "lesson" and not carry_forward:
        why = admission_reject(content)
        if why:
            raise ValueError(f"make_event refused {subject}: {why}")
        # Fact-shape gate over every text field; a `home` grants no exemption.
        # pointer=True exempts reference memories.
        if not pointer:
            why = fact_refusal(content, hook, body)
            if why:
                raise ValueError(f"make_event refused {subject}: {why}")
    ts = ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = {"id": event_id(HOST, session, ts, content, kind, subject), "ts": ts, "host": HOST,
          "session": session, "kind": kind, "subject": subject,
          "polarity": polarity, "content": content, "home": home,
          "lineage": lineage, "audience": audience, "confidence": confidence,
          "supersedes": supersedes, "sig": None}
    if pin:
        ev["pin"] = True
    if tier is not None:
        ev["tier"] = tier
    # Optional fields are omitted when unset, not written as null.
    if residency is not None:
        ev["residency"] = residency
    if hook is not None:
        ev["hook"] = hook
    if body is not None:
        ev["body"] = body
    # Fingerprint of the store file the signature vouches for; covered by
    # canonical_bytes(). Absent on older events.
    if body_sha256 is not None:
        ev["body_sha256"] = body_sha256
    # Verbal approval: an auditable record of the owner's words, not a
    # security control (any caller can pass a string). Lineage is unchanged.
    if verbal_approval is not None:
        ev["verbal_approval"] = verbal_approval
    if expires is not None:
        ev["expires"] = expires
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
    # A verbal approval must carry quotable words and a timestamp.
    va = ev.get("verbal_approval")
    if va is not None:
        if not isinstance(va, dict):
            p.append("verbal_approval must be an object")
        else:
            words = (va.get("words") or "").strip()
            if len(words) < MIN_APPROVAL_WORDS:
                p.append(f"verbal_approval.words is {len(words)}B — under "
                         f"{MIN_APPROVAL_WORDS}B is not a quotable approval")
            if not va.get("ts"):
                p.append("verbal_approval.ts missing — an approval with no "
                         "date cannot be placed in a session")
    # Body and body_sha256 must agree; a mismatch is never served.
    if (ev.get("body") and ev.get("body_sha256")
            and content_fingerprint(ev["body"]) != ev["body_sha256"]):
        p.append("body does not match body_sha256")
    if ev.get("kind") == "tier":
        if ev.get("tier") not in TIERS:
            p.append(f"tier event needs tier in {sorted(TIERS)}, got {ev.get('tier')!r}")
        if ev.get("body"):
            p.append("a tier event carries no body")
    if ev.get("residency") is not None and ev.get("residency") not in RESIDENCIES:
        p.append(f"bad residency {ev.get('residency')!r}")
    # A body for a non-body audience makes the event invalid.
    if ev.get("body") and ev.get("audience") not in BODY_AUDIENCES:
        p.append(f"audience {ev.get('audience')!r} may not carry a body")
    if ev.get("kind") == "assert" and not ev.get("home"):
        # One home per fact: an assertion must point at its home.
        p.append("assert requires home (one home per fact)")
    return p


# ── residency, bodies, projection ───────────────────────────────────────────
def effective_residency(ev):
    """The residency this event may actually claim.

    Capped at read time so an unsigned event cannot be read as doctrine
    however it entered the log.
    """
    r = ev.get("residency")
    if r is None:
        return RESIDENCY_UNSET
    if r == "pinned" and not ev.get("_signed"):
        # pinned requires a signature, local or not.
        return MAX_UNSIGNED_RESIDENCY
    if r == "doctrine" and not ev.get("_signed") and ev.get("host") != HOST:
        # Remote unsigned doctrine caps at state. Local unsigned doctrine is
        # allowed: `host` is bound to the single-writer log, so it came through
        # this host's own door. Signing is what lets doctrine travel to peers.
        return MAX_UNSIGNED_RESIDENCY
    return r


def residency_capped(ev):
    """True when this event asked for a tier it may not hold (and was demoted)."""
    return (ev.get("residency") in ("doctrine", "pinned")
            and effective_residency(ev) != ev.get("residency"))


RECONSTRUCTED_MARK = "reconstructed_from_event: true"


def ghost_refusal_reason(kind, subject, body, store_root):
    """Why this emit would create a ghost (index row with no reachable body), or None.

    Pure (store_root passed in) so drills can test it against a temp store.
    """
    if kind != "lesson" or body or store_root is None:
        return None
    slug = subject.split("/", 1)[1] if "/" in subject else subject
    f = store_root / f"{slug}.md"
    if f.exists():
        return None
    return (f"lesson {subject!r} has no --body and no store file at {f}.\n"
            f"  That combination is a GHOST: an always-on index row whose body "
            f"/recall can never reach.\n"
            f"  Either pass --body/--body-file, or write it through the one "
            f"door:\n"
            f"    memory_write.py write --slug {slug} ... --commit")


CHAIN_WALK_MAX_HOPS = 64   # bounds the walk so a cycle cannot hang the fold


def _stamp(body, lineage, klass=None, words=None):
    """Rewrite a body's `lineage:`/`promotion:`/`approved:` stamps from the verdict.

    Matches memory_write.set_lineage + set_promotion; the body's own stamps are
    replaced, never trusted."""
    if not _FM_LINEAGE_STRIP.search(body):
        return body
    stamp = f"lineage: {lineage}\n"
    if klass:
        stamp += f"promotion: {klass}\n"
        if words:
            stamp += f"approved: {json.dumps(words, ensure_ascii=False)}\n"
    body = re.sub(r"(?m)^(promotion|approved):[ \t]*.*$\n?", "", body)
    return _FM_LINEAGE_STRIP.sub(stamp, body, count=1)


def _chain(tip, by_id):
    """The tip's supersede ancestry, nearest first (BFS, bounded, cycle-safe)."""
    out, seen, frontier, hops = [], {tip["id"]}, [tip], 0
    while frontier and hops < CHAIN_WALK_MAX_HOPS:
        hops += 1
        nxt = []
        for e in frontier:
            raw = e.get("supersedes")
            for sid in ([raw] if isinstance(raw, str) else (raw or [])):
                a = by_id.get(sid)
                if a is not None and sid not in seen:
                    seen.add(sid)
                    out.append(a)
                    nxt.append(a)
        frontier = nxt
    return out


def chain_body(tip, events):
    """The stamped body a bodyless lesson tip stands for, or None.

    Walks the supersede chain:
      1. A vouching event (verified signature or verbal approval) with
         `body_sha256` is the authority; bytes come from any event on the
         subject whose body matches that hash. No match: None.
      2. Otherwise the nearest ancestor carrying a body, stamped with the
         least trusted lineage of tip and carrier.
    Only BODY_AUDIENCES may supply a body.
    """
    by_id = {e["id"]: e for e in (events or ())}
    chain = [tip] + _chain(tip, by_id)
    subject = tip["subject"]

    def ok_carrier(e):
        return (e.get("subject") == subject and event_carries_body(e)
                and e.get("audience") in BODY_AUDIENCES)

    # Nearest vouching event: verified signature or verbal approval.
    authority = next((e for e in chain if e.get("body_sha256")
                      and (e.get("_signed") or e.get("verbal_approval"))), None)
    if authority is not None:
        want = authority["body_sha256"]
        carrier = next((e for e in chain if ok_carrier(e)
                        and content_fingerprint(e["body"]) == want), None)
        if carrier is None:
            carrier = next((e for e in (events or ()) if ok_carrier(e)
                            and content_fingerprint(e["body"]) == want), None)
        if carrier is None:
            return None
        if authority.get("_signed"):
            return _stamp(carrier["body"], "craig-direct", PROMOTION_KEY)
        return _stamp(carrier["body"], "craig-direct", PROMOTION_VERBAL,
                      (authority.get("verbal_approval") or {}).get("words"))
    carrier = next((e for e in chain[1:] if ok_carrier(e)), None)
    if carrier is None:
        return None
    trusted = (tip.get("lineage") == "operator-direct"
               and carrier.get("lineage") == "operator-direct")
    return _stamp(carrier["body"], "craig-direct" if trusted else "contains-untrusted")


# Alias kept for callers and tests.
signed_promotion_body = chain_body


def project_store(fold, store, apply=False, events=None):
    """Materialise/repair store files from the event log's live tips.

    * tip carries a body -> create, or overwrite (only from a signed tip or
      over a reconstruction stub; otherwise alarm and leave it).
    * tip has no body and no file exists -> create a stub from `content`.
    * tip has no body and a file exists -> leave it alone.
    Never deletes a store file; files with no event are reported as inverse ghosts.
    """
    if store is None:
        return {"created": [], "repaired": [], "inverse_ghosts": [], "alarms": []}
    out = {"created": [], "repaired": [], "inverse_ghosts": [], "alarms": []}
    seen = set()
    for e in fold["live"]:
        if not e["subject"].startswith("lesson/"):
            continue
        promoted = None
        if e["kind"] == "correct" or (e["kind"] == "lesson" and not event_carries_body(e)):
            promoted = chain_body(e, events)
            if promoted is None and e["kind"] == "correct":
                continue
        elif e["kind"] != "lesson":
            continue
        slug = e["subject"].split("/", 1)[1]
        # Path safety: the slug becomes a filename and must not escape the store.
        if "/" in slug or "\\" in slug or slug in ("", ".", ".."):
            out["alarms"].append(f"refusing to project unsafe slug {slug!r}")
            continue
        seen.add(slug)
        f = store / f"{slug}.md"
        if promoted is not None and f.exists():
            # Chain-walked bodies are create-only: a repair would strip the
            # signing host's post-promotion frontmatter.
            continue
        if promoted is not None or event_carries_body(e):
            # Audience boundary re-checked here: events may arrive from peers or replay.
            if e.get("audience") not in BODY_AUDIENCES:
                out["alarms"].append(
                    f"refusing to project a body from audience "
                    f"{e.get('audience')!r}: {slug} (bodies are operator/shared "
                    f"only — this event should not exist)")
                continue
            want = promoted if promoted is not None else e["body"]
            if not f.exists():
                if apply:
                    f.write_text(want, encoding="utf-8")
                out["created"].append(slug)
            elif f.read_text(encoding="utf-8") != want:
                # Overwrite only from a signed tip or over a reconstruction
                # stub; an unsigned event must not be able to rewrite a body.
                stub = RECONSTRUCTED_MARK in f.read_text(encoding="utf-8")
                if e.get("_signed") or stub:
                    if apply:
                        f.write_text(want, encoding="utf-8")
                    out["repaired"].append(slug)
                    out["alarms"].append(
                        f"store file diverged from its event and was rewritten: "
                        f"{slug} ({'stub upgraded' if stub else 'signed tip'})")
                else:
                    out["alarms"].append(
                        f"store file diverges from its UNSIGNED event and was "
                        f"LEFT ALONE: {slug} — hand-edit, or an event claiming "
                        f"a body it should not. Resolve deliberately: "
                        f"memory_write.py adopt {slug} --reconcile --commit "
                        f"(file wins) or sign the event (event wins). "
                        f"--reconcile is REQUIRED here: plain adopt refuses a "
                        f"slug whose event already carries a body, which is "
                        f"every divergence, so the advice without it was a "
                        f"dead end (2026-08-01 audit).")
        elif not f.exists():
            content = (e.get("content") or "").strip()
            if not content:
                continue
            stub = (f"---\nname: {slug}\n"
                    f"description: {content.splitlines()[0][:200]}\n"
                    f"lineage: {'craig-direct' if e.get('lineage') == 'operator-direct' else 'contains-untrusted'}\n"
                    f"{RECONSTRUCTED_MARK}\n"
                    f"metadata:\n  node_type: memory\n  type: feedback\n---\n\n"
                    f"{content}\n\n"
                    f"> Reconstructed by the fold from event {e['id']} "
                    f"({e['ts']}). The original write never created a store "
                    f"file, so this is the full surviving text — there is no "
                    f"richer body behind it.\n")
            if apply:
                f.write_text(stub, encoding="utf-8")
            out["created"].append(slug)
    for f in sorted(store.glob("*.md")):
        slug = f.stem
        if slug in seen or slug.startswith("_") or f.name in ("MEMORY.md", "QUARANTINE.md"):
            continue
        out["inverse_ghosts"].append(slug)
    # Inverse ghosts alarm only once the backfill-complete marker exists.
    if out["inverse_ghosts"] and (MESH_ROOT / "state" / "v4-backfill-complete").exists():
        out["alarms"].append(
            f"{len(out['inverse_ghosts'])} store file(s) have no event — the "
            f"mesh cannot replicate them and peers will never see them. "
            f"Repair: memory_write.py adopt <slug> --commit "
            f"(e.g. {' '.join(out['inverse_ghosts'][:3])})")
    return out


def event_carries_body(ev):
    """True when the event carries a body and so can drive projection."""
    return bool(ev.get("body"))


# ── signatures ───────────────────────────────────────────────────────────────
# ssh-keygen -Y over Ed25519 keys, verified against cc-handoff/allowed_signers.
# A distinct namespace prevents cross-protocol replay of task signatures.
SIG_NAMESPACE = "memory-mesh"
# Env overrides below exist for drills only (throwaway keypairs).
def _signers_file():
    """Locate allowed_signers: env override, then root-relative, then legacy paths.

    If not found, every signature reads as unverified."""
    if os.environ.get("MESH_ALLOWED_SIGNERS"):
        return Path(os.environ["MESH_ALLOWED_SIGNERS"])
    root = Path(__file__).resolve().parent.parent
    cands = [root / "cc-handoff" / "allowed_signers",
             root / "fleet" / "cc-handoff" / "allowed_signers"] + [
        Path(os.path.expanduser(p)) for p in
        ("~/{{REDACTED}}/cc-handoff/allowed_signers",
         "~/cc-handoff/allowed_signers")]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


ALLOWED_SIGNERS = _signers_file()
# Signer identity + key are per-operator; the id must match allowed_signers.
SIGNER = os.environ.get("MESH_SIGNER", "craig@fleet")


def _signing_key():
    """Signing key path: MESH_SIGNING_KEY override, else the hardware (sk) key,
    else the file key. Affects only which key signs, not which keys verify."""
    override = os.environ.get("MESH_SIGNING_KEY")
    if override:
        return Path(override)
    for cand in ("~/.key/signing/craig_sk_ed25519", "~/.key/signing/craig2_ed25519"):
        p = Path(os.path.expanduser(cand))
        if p.exists():
            return p
    return Path(os.path.expanduser("~/.key/signing/craig2_ed25519"))


SIGNING_KEYS = {SIGNER: _signing_key()}
# Prefer Homebrew's ssh-keygen: Apple's build has no FIDO provider for sk- keys.
SSH_KEYGEN = next((p for p in ("/opt/homebrew/bin/ssh-keygen",)
                   if Path(p).exists()), "ssh-keygen")


def canonical_bytes(ev):
    """Bytes a signature covers: the event minus sig/signer and underscore fields, keys sorted."""
    payload = {k: v for k, v in ev.items()
               if k not in ("sig", "signer") and not k.startswith("_")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def verify_sig(ev, allowed=None):
    """True iff ev's signature verifies for its signer; any failure yields False."""
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

    Interactive by design: signs from the key file only, never via ssh-agent,
    so it fails without a human at the terminal (passphrase or PIN+touch).
    """
    key = SIGNING_KEYS.get(signer)
    if key is None:
        raise ValueError(f"unknown signer {signer!r} (known: {sorted(SIGNING_KEYS)})")
    if not key.exists():
        raise RuntimeError(f"no signing material for {signer}: {key} absent — "
                           f"is ~/.key unlocked? (keyvault/unlock.sh)")
    # Strip SSH_AUTH_SOCK so ssh-keygen cannot fall back to an agent identity.
    env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
    # Sign a temp file, not stdin: from stdin ssh-keygen uses SSH_ASKPASS
    # instead of the terminal for the sk- key's touch prompt.
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = Path(tmp.name)
    try:
        tmp.write(canonical_bytes(ev))
        tmp.close()
        sig_path = tmp_path.with_name(tmp_path.name + ".sig")
        r = subprocess.run([SSH_KEYGEN, "-Y", "sign", "-f", str(key),
                            "-n", SIG_NAMESPACE, str(tmp_path)],
                           capture_output=True, timeout=300, env=env)
        if r.returncode != 0:
            raise RuntimeError(
                "signing failed: " + r.stderr.decode().strip()[:200] +
                "\nSigning is deliberately interactive — it needs the owner at a "
                "terminal to enter the key's passphrase or PIN+touch. Do not "
                "load this key into ssh-agent to work around this: that "
                "hands every local process the owner's authority (see this "
                "function's docstring).")
        if not sig_path.exists():
            raise RuntimeError(
                "signing reported success but no .sig file was written — "
                f"expected {sig_path}")
        ev["signer"] = signer
        ev["sig"] = sig_path.read_text()
        ev["_signed_via"] = "key file"
        return ev
    finally:
        tmp_path.unlink(missing_ok=True)
        tmp_path.with_name(tmp_path.name + ".sig").unlink(missing_ok=True)


# ── subject registry ─────────────────────────────────────────────────────────
_SUBJECT_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9._:-]+$")


def load_registry():
    return _load_toml(CODE_DIR / "subjects.toml")


def subject_problem(subject, registry):
    """None if the subject parses against the registry, else a reason.

    The producer warns and the fold parks; emit does not reject."""
    if not _SUBJECT_RE.match(subject):
        return f"subject {subject!r} not class/entity shaped"
    cls = subject.split("/", 1)[0]
    if cls not in registry.get("classes", {}):
        return f"unregistered subject class {cls!r}"
    return None


# ── concurrency ──────────────────────────────────────────────────────────────
# One writer per host log, but several local processes may emit concurrently.
# The lock serializes append+commit; an uncommitted event never folds.
LOCK_PATH = MESH_ROOT / ".mesh.lock"
LOCK_WAIT = 30          # seconds to wait for the lock before failing loudly


@contextlib.contextmanager
def repo_lock(timeout=LOCK_WAIT):
    """Serialize the whole append+add+commit sequence across processes (flock).

    Raises on timeout rather than proceeding unserialized.
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
    """Run git, retrying only lock-contention failures with bounded backoff."""
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
    """Read a file as committed at HEAD (not the working tree), or None."""
    r = subprocess.run(["git", "-C", str(cwd or MESH_ROOT), "show", f"HEAD:{path}"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def committed_log_paths(cwd=None):
    out = git("ls-tree", "-r", "--name-only", "HEAD", cwd=cwd, check=False)
    return [p for p in out.splitlines()
            if (p.startswith("events/") or p.startswith("_archive/"))
            and p.endswith(".ndjson")]


def append_event_line(line, log=None):
    """Append one event line to this host's log, first newline-terminating any torn tail."""
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
    """Ids of every committed, unsuperseded, non-denial/retract event on `subject`.

    Wider than fold['live'] so a supersede also covers older revisions."""
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
    """All committed events from all logs, deduped by id. Returns (events, problems)."""
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
            ev["_line"] = n                   # per-writer offset
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


def _contradiction_alarm(subj, evs, suffix=""):
    """Alarm text for a parked contradiction, naming what actually disagrees:
    unsigned vs signed, signed vs signed, or both."""
    signed_contents = {e["content"] for e in evs if e.get("_signed")}
    unsigned_contents = {e["content"] for e in evs if not e.get("_signed")}
    parts = []
    if unsigned_contents - signed_contents:
        parts.append("unsigned event contradicts SIGNED truth")
    if len(signed_contents) > 1:
        parts.append(f"{len(signed_contents)} SIGNED events disagree "
                     f"(operator signed contradictory claims — often a promote "
                     f"naming an already-superseded event id; supersede the "
                     f"stale one to clear)")
    if not parts:
        parts.append("contradictory claims")
    return f"{' AND '.join(parts)} on {subj}{suffix}"


def fold_events(events, registry):
    """Deterministic rule pass; resolution is by explicit supersedes, never by time."""
    by_id = {e["id"]: e for e in events}
    superseded, dangling = set(), []
    for e in events:
        # supersedes may be one id or a list.
        raw = e.get("supersedes")
        for s in ([raw] if isinstance(raw, str) else (raw or [])):
            if s in by_id:
                superseded.add(s)
            else:
                dangling.append((e["id"], s))   # compaction-bug tripwire
    # Every host re-verifies signatures at fold time.
    for e in events:
        e["_signed"] = verify_sig(e) if e.get("sig") else False
        if e.get("sig") and not e["_signed"]:
            e["_badsig"] = True
    # retract only carries a supersedes edge; it never renders.
    live = [e for e in events if e["id"] not in superseded
            and e["kind"] not in ("denial", "propose-correct", "retract")]

    # Lineage quarantine: untrusted-lineage events are held out before the
    # contradiction pass, so they can never park (silence) trusted doctrine.
    # Promotion routes, strongest first:
    #   1. sign.py --promote <id> (signed `correct`), or a signature on the
    #      event itself — PROMOTION_KEY.
    #   2. sign.py --promote-verbal <id> --approved "<words>" — PROMOTION_VERBAL,
    #      an audit record, not a cryptographic gate.
    # Unknown/absent lineage quarantines and alarms.
    alarms = []
    quarantined, keep = [], []
    for e in live:
        lin = e.get("lineage")
        if lin == "operator-direct":
            keep.append(e)
        elif lin == "contains-untrusted":
            # `_promotion` is fold-local, derived every fold, never read from the log.
            if e.get("_signed"):
                e["_promotion"] = PROMOTION_KEY
                keep.append(e)
            elif e.get("verbal_approval"):
                e["_promotion"] = PROMOTION_VERBAL
                keep.append(e)
            else:
                quarantined.append(e)
        else:
            quarantined.append(e)
            alarms.append(
                f"event {e['id']} carries lineage {lin!r}, which is not one of "
                f"{sorted(LINEAGES)} — quarantined, not served")
    live = keep

    # ── pin overlay ──────────────────────────────────────────────────────────
    # A `pin` event names a subject and never renders; the pinned lesson keeps
    # its own id, content and ts. Unpin = retract the pin event.
    pins = [e for e in live if e["kind"] == "pin"]
    live = [e for e in live if e["kind"] != "pin"]
    # Tier overlays (see TIERS). Disagreeing live tier events on one subject
    # alarm and neither wins; each host keeps its current file.
    tier_evs = [e for e in live if e["kind"] == "tier"]
    live = [e for e in live if e["kind"] != "tier"]
    tiers, _tier_seen = {}, {}
    for e in tier_evs:
        _tier_seen.setdefault(e["subject"], set()).add(e.get("tier"))
    for subj, ts_ in sorted(_tier_seen.items()):
        if len(ts_) == 1:
            tiers[subj] = next(iter(ts_))
        else:
            alarms.append(f"tier conflict on {subj}: live tier events say "
                          f"{sorted(t or '?' for t in ts_)} — left to each host's "
                          f"file; resolve with tier.py")

    unnormalized = [e for e in live if subject_problem(e["subject"], registry)]
    normalized = [e for e in live if not subject_problem(e["subject"], registry)]

    # Contradiction rules — deterministic, no model.
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
        # A signed claim is truth; anything disagreeing with it parks and alarms.
        signed = [e for e in evs if e.get("_signed")]
        if signed and len({e["content"] for e in evs}) > 1:
            parked[subj] = evs
            alarms.append(_contradiction_alarm(subj, evs))
    for e in events:
        if e.get("_badsig"):
            alarms.append(f"event {e['id']} carries a signature that DOES NOT "
                          f"verify (claimed signer {e.get('signer')!r}) — forged "
                          f"or key rotated; treated as unsigned")
    for eid, missing in dangling:
        alarms.append(f"event {eid} supersedes missing {missing} — compaction bug?")

    # >1 live lesson with differing content on a subject is a race: park it.
    # Identical restatements collapse to the earliest.
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

    # Signed-truth tripwire across kinds: anything live disagreeing with a
    # signed event on the same subject parks and alarms.
    for subj in sorted({e["subject"] for e in normalized}):
        evs = [e for e in normalized if e["subject"] == subj]
        if not any(e.get("_signed") for e in evs):
            continue
        if len({e["content"] for e in evs}) > 1 and subj not in parked:
            parked[subj] = evs
            alarms.append(_contradiction_alarm(
                subj, evs,
                suffix=f" (across kinds: "
                       f"{', '.join(sorted({e['kind'] for e in evs}))})"))

    parked_ids = {e["id"] for evs in parked.values() for e in evs}
    servable = [e for e in normalized
                if e["id"] not in parked_ids and e["id"] not in dup_lessons]

    # Apply pins against the final served set; a pin on an unserved subject
    # alarms as dangling. The pinned tier is a hard cap: signed pins are
    # admitted first and uncapped, then unsigned pins oldest-first until full.
    by_subject_servable = {}
    for e in servable:
        by_subject_servable.setdefault(e["subject"], []).append(e)
    budget = DELIVERY_BYTES * PIN_DELIVERY_SHARE
    spent, pinned_subjects, refused = 0, set(), []
    for p in sorted(pins, key=lambda e: (0 if e.get("_signed") else 1,
                                         e["ts"], e["id"])):
        targets = by_subject_servable.get(p["subject"])
        if not targets:
            alarms.append(
                f"PIN {p['id']} on {p['subject']} protects nothing — no live "
                f"event has that subject (typo, or the target was "
                f"retracted/parked); retract the pin or fix the subject")
            continue
        if p["subject"] in pinned_subjects:
            continue                      # duplicate pin, already paid for
        cost = sum(line_bytes(index_row(t)) for t in targets
                   if t["audience"] in VIEW_INCLUDES["operator"])
        if not p.get("_signed") and spent + cost > budget:
            refused.append(p)
            continue
        spent += cost
        pinned_subjects.add(p["subject"])
    # Set on every servable event so a stale True is cleared across folds.
    for e in servable:
        e["_pin"] = e["subject"] in pinned_subjects
    if refused:
        alarms.append(
            f"{len(refused)} PIN(s) REFUSED — the pinned tier is at {spent} B of "
            f"its {int(budget)} B cap ({PIN_DELIVERY_SHARE:.0%} of the "
            f"{DELIVERY_BYTES} B delivered file). Not applied: "
            + ", ".join(f"{p['id']}/{p['subject']}" for p in refused[:5])
            + ". Retract a pin to make room, or sign these to admit them.")
    # Alarm when a quarantined claim disagrees with served content.
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
            "proposals": proposals, "alarms": alarms, "total": len(events),
            # Pin events never render as rows; exposed here for PINS.md.
            "pins": pins, "pins_active": sorted(pinned_subjects),
            "tiers": tiers,
            "pins_refused": refused, "pinned_bytes": spent}


def score_for_index(e, correction_counts, session_breadth):
    """Eviction priority, higher first: pinned, signed, correction count,
    session breadth, recency."""
    # `pin` (emit-time flag) and `_pin` (overlay) rank the same.
    return (1 if (e.get("pin") or e.get("_pin")) else 0,
            1 if e.get("_signed") else 0,
            correction_counts.get(e["subject"], 0),
            session_breadth.get(e["subject"], 0),
            e["ts"])


def ranked_index(fold, audience):
    """One audience's live events in score_for_index order (INDEX and MEMORY.md share it)."""
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
    """One index line: subject, content, optional home pointer, and an MM-DD date."""
    return (f"- [{e['subject']}] {e['content'][:INDEX_CONTENT_CHARS]}"
            f"{' → ' + e['home'] if e.get('home') else ''}"
            # .get: `_suspect` is absent on events not from read_all_events.
            f" ({e['ts'][5:10]}{', SUSPECT-ts' if e.get('_suspect') else ''})")


def line_bytes(s):
    """UTF-8 bytes this line costs in a newline join, newline included."""
    return len(s.encode("utf-8")) + 1


def budgeted_rows(ranked, head_lines, cap=INDEX_BUDGET, line_cap=None):
    """head_lines + one row per event, capped at `cap` bytes and `line_cap` lines.

    The overflow note's width is reserved up front so it stays within the cap.
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
            "# contradict a served fact. Two promotion routes (2026-08-12):",
            "#",
            "#   key-signed (strongest — an agent cannot produce this):",
            "#     python3 ~/{{REDACTED}}/memory-mesh/sign.py --promote <id>",
            "#   verbally-signed (the owner reasoned it through and said yes;",
            "#   an AUDIT record, not a cryptographic gate — buys `served`,",
            "#   never `pinned`/`doctrine`):",
            "#     python3 ~/{{REDACTED}}/memory-mesh/sign.py --promote-verbal <id> \\",
            "#            --approved \"<his verbatim words>\"",
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

    # PINS.md: which pins are active, refused or dangling, with ids to retract.
    cap = int(DELIVERY_BYTES * PIN_DELIVERY_SHARE)
    active = set(fold.get("pins_active", []))
    refused_ids = {p["id"] for p in fold.get("pins_refused", [])}
    pinsmd = [f"# PINS ({audience}) — subjects held in the always-on tier "
              f"regardless of merit rank",
              "#",
              f"# Tier usage: {fold.get('pinned_bytes', 0)} B of the {cap} B cap "
              f"({PIN_DELIVERY_SHARE:.0%} of the {DELIVERY_BYTES} B delivered file).",
              "# Unsigned pins past the cap are REFUSED, oldest admitted first;",
              "# an operator-signed pin is admitted before the cap applies.",
              "#",
              "# Unpin needs no new verb — retract the PIN EVENT by its id:",
              "#   emit.py --kind retract --subject <subject> --supersedes <pin id> \\",
              "#           --content 'unpin: <why>' --session <sesh>",
              ""]
    for p in sorted(fold.get("pins", []), key=lambda x: (x["ts"], x["id"])):
        if p["audience"] not in inc:
            continue
        state = ("ACTIVE" if p["subject"] in active else
                 "REFUSED (over cap)" if p["id"] in refused_ids else
                 "DANGLING (protects nothing)")
        pinsmd.append(f"## {p['subject']}  [{state}]")
        pinsmd.append(f"- pin id {p['id']} · {p['ts']} · {p['host']}"
                      f"{' · SIGNED' if p.get('_signed') else ''}")
        pinsmd.append(f"- {p['content'][:300]}")
        pinsmd.append("")
    if not active and not fold.get("pins"):
        pinsmd.append("_no pins — every memory competes on merit rank._")

    return {"INDEX.md": "\n".join(lines) + "\n",
            "CONFLICTS.md": "\n".join(conflicts) + "\n",
            "DENIALS.md": "\n".join(denials) + "\n",
            "QUARANTINE.md": "\n".join(quar) + "\n",
            "PINS.md": "\n".join(pinsmd) + "\n"}


# ── harness MEMORY.md ────────────────────────────────────────────────────────
# Generated from the folded corpus; per-host opt-in via a `.mesh-generated`
# marker in the store.

def store_dir():
    """This workspace's auto-memory store: ~/.claude/projects/<root with / → ->/memory,
    where root is CODE_DIR's parent."""
    override = os.environ.get("MESH_STORE_DIR")
    if override and os.environ.get("MESH_DRILL_LOCAL"):
        return Path(override)
    return (Path.home() / ".claude" / "projects"
            / str(CODE_DIR.parent).replace("/", "-") / "memory")


DEFAULT_MESH_ROOT = Path(os.path.expanduser("~/memory-events"))


def harness_store():
    """The store, or None if this host hasn't opted in (.mesh-generated marker).

    Also None when MESH_ROOT is not the default log, so sandboxes and drills
    never write the real store.
    """
    if MESH_ROOT.resolve() != DEFAULT_MESH_ROOT.resolve():
        return None
    store = store_dir()
    return store if (store / ".mesh-generated").exists() else None


def _tier_key(subject):
    """The key `_index-exclude.txt` uses: bare slug for lessons, else subject."""
    return subject.split("/", 1)[1] if subject.startswith("lesson/") else subject


def file_ondemand_slugs(store):
    """The entries of store/_index-exclude.txt, as written on this host."""
    f = store / "_index-exclude.txt"
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.add(s[:-3] if s.endswith(".md") else s)
    return out


def ondemand_slugs(store, fold=None):
    """Slugs held out of the always-on index.

    The host file, overlaid by the fold's `tier` events when given:
    `ondemand` adds, `always` removes."""
    out = file_ondemand_slugs(store)
    for subj, t in ((fold or {}).get("tiers") or {}).items():
        k = _tier_key(subj)
        if t == "ondemand":
            out.add(k)
        elif t == "always":
            out.discard(k)
            out.discard(subj)
    return out


EXCLUDE_HEADER = ("# On-demand tier — held out of the always-on MEMORY.md index.\n"
                  "# PROJECTED by the fold from `tier` events + this host's own "
                  "entries (2026-09-27);\n# change a tier with memory_write.py "
                  "demote [--undo] or memory-mesh/tier.py, not by hand.\n")


def project_index_exclude(fold, store, apply=False):
    """Rewrite store/_index-exclude.txt as ondemand_slugs(store, fold).

    Returns (added, removed). Creation-safe and loss-free: an entry with no
    tier event stays; only an `always` event removes one."""
    if store is None:
        return [], []
    before = file_ondemand_slugs(store)
    after = ondemand_slugs(store, fold)
    added, removed = sorted(after - before), sorted(before - after)
    if apply and (added or removed):
        (store / "_index-exclude.txt").write_text(
            EXCLUDE_HEADER + "".join(f"{s}\n" for s in sorted(after)), encoding="utf-8")
    return added, removed


ONDEMAND_HEADING = "## On-demand memories — not always-loaded; /recall reaches them"


def index_excluded(ev, exclude):
    """Is this event held out of the always-on index by the exclude manifest?

    Matches either the bare lesson slug or the full subject. The single
    implementation of this rule; do not duplicate it.
    """
    subject = ev["subject"]
    slug = subject.split("/", 1)[1] if subject.startswith("lesson/") else None
    return slug in exclude or subject in exclude


def delivery_breach(text, byte_cap=LOADER_BYTE_CEILING,
                    line_cap=LOADER_LINE_CEILING):
    """Reasons the assembled `text` exceeds the loader's byte/line limits; [] if it fits."""
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

    The on-demand existence stub is always kept.
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
    """Compose the harness index so the fully assembled file fits the caps.

    Sheds named slugs first, then index rows; header and on-demand stub are
    never dropped. Returns (text, report). Loops are bounded.
    """
    n_rows, n_slugs = len(ranked), len(slugs)
    # The appendix has its own cap (APPENDIX_BYTES) so freed bytes leave the
    # file instead of being respent on slug names.
    if n_slugs:
        base = len(_assemble_harness_memory(
            head, ranked, n_rows, slugs, 0).encode("utf-8"))
        for _ in range(len(slugs) + 1):
            if not n_slugs:
                break
            grown = len(_assemble_harness_memory(
                head, ranked, n_rows, slugs, n_slugs).encode("utf-8")) - base
            if grown <= APPENDIX_BYTES:
                break
            n_slugs = max(0, n_slugs - max(1, n_slugs // 16))
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
    # Even the minimum does not fit: publish it and report failure.
    text = _assemble_harness_memory(head, ranked, 0, slugs, 0)
    return text, {"ok": False, "rows": 0, "rows_total": len(ranked),
                  "slugs": 0, "slugs_total": len(slugs),
                  "bytes": len(text.encode("utf-8")),
                  "lines": len(text.splitlines()),
                  "reason": "does not fit even at the minimum stub"}


def residency_partition(fold, store):
    """Split the operator's live rows by declared residency.

    Returns (always_on, on_demand, undeclared, report).
    """
    def lesson_slug(e):
        return (e["subject"].split("/", 1)[1]
                if e["subject"].startswith("lesson/") else None)

    exclude = ondemand_slugs(store, fold) if store else set()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    always, demand, undeclared, expired = [], [], [], []
    for e in ranked_index(fold, "operator"):
        if index_excluded(e, exclude):
            continue
        r = effective_residency(e)
        # Expiry is applied at render only; the fold emits no events.
        if e.get("expires") and e["expires"] < today:
            expired.append(e)
            continue
        if r in ("pinned", "doctrine"):
            always.append(e)
        elif r == "state":
            demand.append(e)
        else:
            undeclared.append(e)
    return always, demand, undeclared, {
        "always_on": len(always), "on_demand": len(demand),
        "undeclared": len(undeclared), "expired_hidden": len(expired)}


def render_harness_memory_v4(fold, store, live=False):
    """Residency-based index: declared residency decides, ranking only orders.

    Doctrine and pinned rows render in stable slug order, not by recency.
    """
    always, demand, undeclared, rep = residency_partition(fold, store)
    always_sorted = sorted(always, key=lambda e: (
        0 if (e.get("pin") or e.get("_pin")) else 1,
        0 if e.get("_signed") else 1,
        e["subject"]))
    # Undeclared rows keep legacy ranking and sit after doctrine so they shed first.
    ranked = always_sorted + undeclared
    slugs = sorted({e["subject"].split("/", 1)[1] for e in demand
                    if e["subject"].startswith("lesson/")})
    head = [
        ("# MEMORY — GENERATED by memory-mesh fold; never hand-edit (overwritten within minutes)"
         if live else
         "# MEMORY — GENERATED by memory-mesh fold (SPEC v4 SHADOW); never hand-edit"),
        f"# residency: {rep['always_on']} always-on, {rep['on_demand']} on-demand, "
        f"{rep['undeclared']} undeclared, {rep['expired_hidden']} expiry-hidden",
        ""]
    text, report = fit_harness_memory(head, ranked, slugs)
    report.update(rep)
    return text, report


def render_harness_memory(fold, store):
    """The harness MEMORY.md: operator index minus on-demand slugs, plus the
    on-demand appendix, sized to fit the loader ceilings. Returns (text, report)."""
    exclude = ondemand_slugs(store, fold)
    ranked = [e for e in ranked_index(fold, "operator")
              if not index_excluded(e, exclude)]
    # Quarantined slugs are not advertised in the appendix; the count line
    # below still records them.
    quar_slugs = {e["subject"].split("/", 1)[1]
                  for e in fold.get("quarantined", [])
                  if e["subject"].startswith("lesson/")}
    exclude = {s for s in exclude if s not in quar_slugs}
    head = [
        "# MEMORY — GENERATED by memory-mesh fold; never hand-edit "
        "(overwritten within minutes)",
        f"# fold of {fold['total']} events; {len(fold['parked'])} subject(s) "
        "parked (see ~/memory-events/views/operator/CONFLICTS.md)"]
    # Quarantine count line, emitted only when nonzero.
    n_quar = sum(1 for e in fold.get("quarantined", [])
                 if e["audience"] in VIEW_INCLUDES["operator"])
    if n_quar:
        head.append(f"# {n_quar} untrusted-lineage fact(s) QUARANTINED and not "
                    "served — views/operator/QUARANTINE.md; promote: "
                    "sign.py --promote <id> (key) or --promote-verbal <id> "
                    "--approved \"<owner's words>\" (verbal, weaker)")
    head.append("")
    return fit_harness_memory(head, ranked, sorted(exclude))


def render_store_quarantine(fold):
    """The store's QUARANTINE.md, projected from the fold.

    Slugs and one-line hooks only, never bodies: untrusted prose stays out of
    agent context.
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
        "# PROMOTE, key-signed (needs the owner's passphrase-gated key — an agent cannot):",
        "#     python3 ~/{{REDACTED}}/memory-mesh/sign.py --promote <event-id>",
        "# PROMOTE, verbally-signed (the owner's spoken approval, recorded verbatim.",
        "# An agent CAN write this — it is an audit record, not a gate. Serves,",
        "# but never reaches pinned/doctrine):",
        "#     python3 ~/{{REDACTED}}/memory-mesh/sign.py --promote-verbal <event-id> \\",
        "#            --approved \"<his verbatim words>\"",
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


def servable_slugs(fold):
    """Lesson slugs a delivery channel may serve: fold['live'] minus parked subjects."""
    parked = set(fold.get("parked") or {})
    out = set()
    for e in fold["live"]:
        subj = e["subject"]
        if subj in parked or not subj.startswith("lesson/"):
            continue
        slug = subj.split("/", 1)[1]
        if "/" in slug or "\\" in slug or slug in ("", ".", ".."):
            continue
        out.add(slug)
    return sorted(out)


def servable_manifest_path():
    """Path of the delivery manifest (in mesh state, not the memory store)."""
    return MESH_ROOT / "state" / "servable.json"


def write_servable_manifest(fold):
    """Publish the servable-slug manifest that retrieve.py filters against.

    Precomputed so retrieval stays off the fold's hot path. Never raises.
    """
    try:
        doc = {"version": 1, "view_version": view_version(fold),
               "generated": datetime.datetime.now(
                   datetime.timezone.utc).isoformat(timespec="seconds"),
               "slugs": servable_slugs(fold)}
        path = servable_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return {"status": "written", "entries": len(doc["slugs"]), "alarms": []}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "error": str(e),
                "alarms": [f"servable manifest write failed: {e}"]}


def write_store_quarantine(fold):
    """Atomically publish the store's QUARANTINE.md (opt-in via harness_store). Never raises."""
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


# Renderer's content cut; shared with the admission gate so they agree.
INDEX_CONTENT_CHARS = 200

# Content ending in an ellipsis is refused at admission (a truncated rule).
_TRAILS_OFF = re.compile(r"(…|\.\.\.)\s*$")


def admission_reject(content):
    """Why this content may not enter the always-on index, or None if it may."""
    text = (content or "").strip()
    if not text:
        return "empty content"
    if _TRAILS_OFF.search(text):
        return ("content trails off mid-sentence — write a rule that fits, "
                f"do not truncate one that does not (ceiling "
                f"{INDEX_CONTENT_CHARS} chars)")
    if len(text) > INDEX_CONTENT_CHARS:
        return (f"content is {len(text)} chars; the renderer cuts at "
                f"{INDEX_CONTENT_CHARS}, so {len(text) - INDEX_CONTENT_CHARS} "
                f"chars would be silently lost — shorten it at the source")
    return None


_STORE_DESC = re.compile(r"^description:\s*(.*)$", re.M)


def projection_drift(fold, store):
    """Classify where a bodyless lesson's `content` and its store file's
    `description:` disagree: file_richer, event_richer, or disjoint.

    Detection only. Events carrying a body are skipped (project_store owns them).
    """
    out = {"file_richer": [], "event_richer": [], "disjoint": []}
    if store is None:
        return out
    norm = lambda s: " ".join((s or "").split()).rstrip("….").rstrip()
    for e in fold["live"]:
        if e.get("body") or e["kind"] != "lesson":
            continue
        f = store / (e["subject"].split("/")[-1] + ".md")
        if not f.exists():
            continue
        m = _STORE_DESC.search(f.read_text(encoding="utf-8"))
        if not m:
            continue
        desc, cont = norm(m.group(1).strip().strip('"')), norm(e["content"])
        if desc == cont:
            continue
        # Ignore a legacy "Title: " prefix when comparing.
        bare = cont.split(": ", 1)[-1] if ": " in cont[:70] else cont
        if desc.startswith(bare) or desc.startswith(cont):
            out["file_richer"].append(e["subject"])
        elif cont.startswith(desc):
            out["event_richer"].append(e["subject"])
        else:
            out["disjoint"].append(e["subject"])
    return out


_ROW_SUBJECT = re.compile(r"^- \[([^\]]+)\]")


def index_subjects(text):
    """The ordered subject slugs of an index render — its residency set."""
    return [m.group(1) for m in
            (_ROW_SUBJECT.match(l) for l in text.splitlines()) if m]


def residency_delta(live_path, new_text):
    """Subjects this render would add to / drop from always-on, or None.

    Membership only; changed prose on a resident row is not a delta.
    """
    if not live_path.exists():
        return None                      # first write
    old = set(index_subjects(live_path.read_text(encoding="utf-8")))
    new = set(index_subjects(new_text))
    added, dropped = sorted(new - old), sorted(old - new)
    if not added and not dropped:
        return None
    return {"added": added, "dropped": dropped}


def render_residency_diff(delta):
    out = ["# STAGED always-on residency change — needs the operator's word.",
           "# Nothing below is live. Promote with: fold.py --promote-residency",
           ""]
    out += [f"  + {s}" for s in delta["added"]]
    out += [f"  - {s}" for s in delta["dropped"]]
    return "\n".join(out) + "\n"


def write_harness_memory(fold, allow_residency_delta=False):
    """Atomically regenerate <store>/MEMORY.md if this host has opted in.

    A residency change is staged instead of written unless allowed; the
    published file is re-measured against the loader ceilings. Returns a report
    dict whose `alarms` the caller should surface. Never raises.
    """
    store = harness_store()
    if store is None:
        return {"status": "skipped", "alarms": []}
    try:
        # Residency-based render is a per-host opt-in via state/render-v4.
        if (MESH_ROOT / "state" / "render-v4").exists():
            text, report = render_harness_memory_v4(fold, store, live=True)
        else:
            text, report = render_harness_memory(fold, store)
        # Alarm on dropped index rows, not on trimmed slug names.
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
        # Residency gate: same row set writes through; an added or dropped
        # row is staged for the owner to promote.
        live = store / "MEMORY.md"
        delta = residency_delta(live, text)
        if delta and not allow_residency_delta:
            staged = store / "MEMORY.md.staged"
            staged.write_text(text, encoding="utf-8")
            (store / "MEMORY.md.staged.diff").write_text(
                render_residency_diff(delta), encoding="utf-8")
            alarms.append(
                f"harness MEMORY.md HELD: residency delta needs the operator's "
                f"word (+{len(delta['added'])} / -{len(delta['dropped'])} rows). "
                f"Staged at {staged}; review the .diff, then promote.")
            report.update(status="staged", path=str(staged), alarms=alarms,
                          residency_delta=delta)
            return report
        tmp = store / f"MEMORY.md.tmp.{os.getpid()}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, store / "MEMORY.md")
        for leftover in ("MEMORY.md.staged", "MEMORY.md.staged.diff"):
            (store / leftover).unlink(missing_ok=True)
        # Re-measure what actually landed on disk.
        published = (store / "MEMORY.md").read_text(encoding="utf-8")
        landed = delivery_breach(published)
        if landed:
            alarms.append("harness MEMORY.md PUBLISHED OVER CEILING — "
                          "sessions are being truncated: " + "; ".join(landed))
        report.update(status="written", path=str(store / "MEMORY.md"),
                      alarms=alarms)
        return report
    except Exception as e:  # noqa: BLE001
        print(f"fold: harness MEMORY.md write FAILED, sessions keep the stale "
              f"copy (mesh views unaffected): {e}", file=sys.stderr)
        return {"status": "failed", "error": str(e),
                "alarms": [f"harness MEMORY.md write failed: {e}"]}


_FRONT_LINEAGE = re.compile(r"^lineage:\s*(\S+)\s*$", re.M)


def store_file_lineage(slug, store=None):
    """The `lineage:` a store file carries; "absent" if none, None if no file.

    Shared by store_quarantine_drift and sign.py's store reconciliation.
    """
    store = Path(store or store_dir())
    p = store / f"{slug}.md"
    if not p.exists():
        return None
    m = _FRONT_LINEAGE.search(p.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else "absent"

# Trusted lineage in store vocabulary and mesh vocabulary.
TRUSTED_LINEAGES = {"craig-direct", "operator-direct"}


def store_quarantine_drift(fold, store=None):
    """Human-readable ways the store's lineage tags disagree with the mesh's quarantine.

    Detection only; store files are owned by memory_write.py.
    """
    store = Path(store or store_dir())
    if not store.is_dir():
        return []
    quar_subjects = {e["subject"] for e in fold.get("quarantined", [])}
    live_subjects = {e["subject"] for e in fold["live"]}
    drift = []

    def file_lineage(slug):
        return store_file_lineage(slug, store)

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
    # 3. Untrusted in the store but with no mesh event at all.
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
    return drift


def view_version(fold):
    """sha256 of folded state; identical on every host for identical logs."""
    reg_hash = hashlib.sha256((CODE_DIR / "subjects.toml").read_bytes()).hexdigest()[:16]
    basis = json.dumps(
        {"registry": reg_hash,
         "signed": sorted(e["id"] for e in fold["live"] if e.get("_signed")),
         "live": sorted(e["id"] for e in fold["live"]),
         "parked": {k: sorted(e["id"] for e in v) for k, v in fold["parked"].items()},
         "quarantined": sorted(e["id"] for e in fold.get("quarantined", [])),
         "unnormalized": sorted(e["id"] for e in fold["unnormalized"])},
        sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()
