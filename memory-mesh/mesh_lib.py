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

# --- residency (SPEC v4) -----------------------------------------------------
# WHICH TIER a memory occupies, DECLARED at write time by the human at the
# /improve gate — never derived from a score. This replaces the v3 ranking as
# the residency authority because the ranking could not carry the load:
# measured 2026-07-31, `score_for_index`'s correction term fired on 2 of 142
# rows and its breadth term on ZERO, so 122 of 142 rows shared one sort key and
# ordering collapsed to `ts`. Stable doctrine was evicted by whatever was
# written most recently, which no amount of curation could fix.
#
#   pinned   — Craig-signed. Hard floor, existing PIN_DELIVERY_SHARE hard cap.
#   doctrine — behavioural rules that must be resident to fire. Leaves the
#              always-on tier ONLY by a human event (supersede / demote),
#              never by ranking.
#   state    — project/reference/pointer facts and notices. NEVER always-on;
#              /recall reaches them. A notice is state + `expires`, not a
#              fourth class (every class is a migration, a conflict rule, a
#              drill case and a wrong-tag target — Grok round 1).
RESIDENCIES = {"pinned", "doctrine", "state"}
# Unset means "not yet declared" — the migration retag has not reached this
# memory. The renderer treats undeclared rows exactly as v3 did, so the field
# is inert until Craig declares it (see fit_harness_memory).
RESIDENCY_UNSET = None
# Only an operator-SIGNED event may carry doctrine/pinned across the mesh; an
# unsigned event from any host caps here. This is what makes cross-host
# residency conflict impossible by construction rather than by a merge rule:
# a peer that re-derives a fact and calls it doctrine cannot outrank the
# signed tip, and two hosts can never hold two residencies for one slug.
MAX_UNSIGNED_RESIDENCY = "state"
# The served index line. Craig approves this string at the /improve gate, and
# it is what every session reads — so it is bounded by REWRITE at the door, not
# by truncation at render. The old path collected an approved `--hook`, threw
# it away, and rendered `content[:200]` instead: a machine-cut rule whose
# qualifier ("...only when X") could land past the cut.
HOOK_MAX_CHARS = 140
# Which audiences may carry a body IN THE EVENT. Bodies replicate to every peer
# through the shared git transport, so this is a confidentiality boundary, not
# a preference (SPEC v4 A2). family/host-private memories emit hook-only.
BODY_AUDIENCES = {"operator", "shared"}

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
# The on-demand slug appendix's OWN budget, inside the ceiling above. Declared
# 2026-08-01: MEMORY.md's objective is a SMALLER file, not a fixed-size one
# allocated differently, so bytes freed by re-homing a fact must leave the file
# instead of being respent on slug names. See
# `decisions/index-byte-objective-2026-08-01.md`. Raising this is a real
# decision — it spends always-on context on advertisement, and the measurement
# in fit_harness_memory says advertisement is not what drives retrieval.
APPENDIX_BYTES = 2_000
LOADER_LINE_CEILING = 200
# Share of the DELIVERED file (not the loader ceiling — the delivered file is
# what a session actually gets) that the pinned tier may hold. Unsigned pins
# past this are REFUSED by fold_events, oldest-admitted-first.
#
# This is a POLICY limit, not a measured one, and saying otherwise was the
# thing Grok caught: the first version called 0.5 "a regime boundary rather
# than a threshold anyone had to measure or invent", which is a tuned constant
# wearing a costume. There is no measured cliff in loader behaviour at half the
# file. What IS measured (2026-07-31, 16 pins live): the pinned tier renders
# 3,047 B, or 12.6% of DELIVERY_BYTES — so the cap sits at ~4x today's usage
# and refusing at it costs nothing now. The number to revisit is this headroom,
# and the alarm names the byte figure so drift is visible rather than inferred.
PIN_DELIVERY_SHARE = 0.5
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


# B3 (continuous-verification audit, 2026-08-06, Grok-reviewed —
# memory-mesh/reviews/2026-08-06-grok-b3-plan-review.md): a signed promotion
# used to bind a short --content description string, not the store file's
# actual bytes -- so has_signed_promotion() (cc-skills/improve/
# memory_write.py) could not tell a promoted memory's real content from a
# later overwrite. This binds the signature to the file.
_FM_LINEAGE_STRIP = re.compile(r"^lineage:[ \t]*.*$\n?", re.M)


def content_fingerprint(text):
    """sha256 of a memory file's text with its `lineage:` frontmatter line
    stripped — invariant under retag's ONE sanctioned mutation
    (memory_write.py's set_lineage(), which byte-preserves everything else),
    sensitive to any other change (body, description, any other frontmatter
    field). Mirrors memory_write.py's own `_FM_LINEAGE` regex deliberately —
    same duplicate-with-an-equality-comment posture as HOOK_MAX_CHARS below,
    because this file predates the mesh being mandatory and callers must
    keep working with mesh_lib absent."""
    return hashlib.sha256(_FM_LINEAGE_STRIP.sub("", text).encode()).hexdigest()


def make_event(kind, subject, content, *, session, polarity="n/a", home=None,
               lineage="operator-direct", audience="operator",
               confidence="inferred", supersedes=None, pin=False, ts=None,
               residency=RESIDENCY_UNSET, hook=None, body=None, expires=None,
               carry_forward=False, body_sha256=None):
    # THE PRODUCER GATE (2026-07-31). Every event path funnels through here, so
    # this is the one place a stump can be refused before it becomes doctrine —
    # backfill.py was gated first and the same week five more stumps arrived
    # through emit (the retag verb re-emitting legacy content), proving that
    # gating one producer is gating none of them
    # ([[trust-gates-cover-all-read-channels]], pointed at writes).
    #
    # `carry_forward=True` is the retag/supersede carve-out: lineage work must
    # re-emit an OLD event's content verbatim, and refusing that would make the
    # 62 legacy stumps un-retaggable — perpetuating an existing stump adds no
    # new loss, only MINTING one does. A per-call argument on purpose, the
    # _lib/mail.py authorized=True pattern: no env var a caller can flip once
    # and forget.
    if kind == "lesson" and not carry_forward:
        why = admission_reject(content)
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
    # SPEC v4 fields are OMITTED when unset rather than written as null: every
    # byte here is replicated forever, and an absent key reads the same as a
    # null to `.get()` while costing nothing. Grandfathered events simply lack
    # them, which is exactly how `event_carries_body` tells old from new.
    if residency is not None:
        ev["residency"] = residency
    if hook is not None:
        ev["hook"] = hook
    if body is not None:
        ev["body"] = body
    # B3 (2026-08-06): the fingerprint of the store file this signature
    # actually vouches for. A plain dict key like every other optional field
    # here — covered automatically by canonical_bytes()'s signature scope
    # (excludes only sig/signer/underscore-prefixed keys), so the hash is
    # part of what Craig's signature attests to, not a side-channel a
    # forger could swap after the fact. Grandfathered events (signed before
    # this existed) simply lack it — has_signed_promotion() treats absence
    # as "no content binding on record", not as a failure.
    if body_sha256 is not None:
        ev["body_sha256"] = body_sha256
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
    if ev.get("residency") is not None and ev.get("residency") not in RESIDENCIES:
        p.append(f"bad residency {ev.get('residency')!r}")
    # A2 at the schema level: an event carrying a body for a non-fleet audience
    # is INVALID, not merely refused by one producer. This makes the boundary a
    # property of the event rather than of the door it came through.
    if ev.get("body") and ev.get("audience") not in BODY_AUDIENCES:
        p.append(f"audience {ev.get('audience')!r} may not carry a body")
    if ev.get("kind") == "assert" and not ev.get("home"):
        # One home per fact: an assertion without a home is a fact-copy trying
        # to be born. The home pointer is what keeps memory out of the truth
        # business (FLEET.md seam rule 1).
        p.append("assert requires home (one home per fact)")
    return p


# ── SPEC v4: residency, bodies, projection ───────────────────────────────────
def effective_residency(ev):
    """The residency this event may actually claim (SPEC v4 A1).

    The cap is applied HERE, at read time, and never at write time alone. A
    write-side check only constrains events this host produced through the
    sanctioned door; the fold also consumes events fetched from peers, replayed
    from history, and hand-written by anything with a shell. Enforcing at
    selection means an unsigned event CANNOT be read as doctrine no matter how
    it entered the log — the same reasoning that put the data-class gate in
    `route.py` rather than in each caller.
    """
    r = ev.get("residency")
    if r is None:
        return RESIDENCY_UNSET
    if r == "pinned" and not ev.get("_signed"):
        # `pinned` is the tier that outranks everything and is capped as a
        # share of the delivered file. It is Craig's signature or nothing,
        # local or not.
        return MAX_UNSIGNED_RESIDENCY
    if r == "doctrine" and not ev.get("_signed") and ev.get("host") != HOST:
        # REMOTE unsigned doctrine caps at state — that is what makes
        # cross-host residency conflict impossible (a peer that re-derives a
        # fact cannot outrank the local declaration, so two hosts can never
        # hold two residencies for one slug).
        #
        # LOCAL unsigned doctrine is allowed, and the distinction is the whole
        # trust model rather than a convenience: `host` is bound to the log
        # filename and single-writer-enforced (read_all_events holds out any
        # mismatch), so host == HOST means the event came through this host's
        # own sanctioned door under Craig's /improve approval — exactly the
        # authority that writes the always-on index today. Requiring a
        # signature here instead would have demanded ~113 passphrase-gated
        # signings to declare the existing corpus, which is not a security
        # control anyone completes; it is a control everyone routes around.
        # Signing remains what makes a doctrine row TRAVEL to peers.
        return MAX_UNSIGNED_RESIDENCY
    return r


def residency_capped(ev):
    """True when this event asked for a tier it may not hold (and was demoted)."""
    return (ev.get("residency") in ("doctrine", "pinned")
            and effective_residency(ev) != ev.get("residency"))


RECONSTRUCTED_MARK = "reconstructed_from_event: true"


def ghost_refusal_reason(kind, subject, body, store_root):
    """Why this emit would create a ghost, or None if it is safe.

    A pure function ON PURPOSE. The live gate resolves `store_root` through
    harness_store(), whose sandbox guard returns None inside a drill — so a
    drill that exercised the gate through emit.py would exercise a gate that
    is switched off, and pass while proving nothing. Splitting the DECISION
    from the LOOKUP lets the drill test the decision with a real temp store,
    and the sandbox guard keep doing its job.
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


def project_store(fold, store, apply=False):
    """Materialise/repair store files from the event log (SPEC v4 A1/A3).

    The tip of a subject's supersede lineage is the fact's one home; store files
    are disposable projections of it. `fold['live']` already IS the tip set —
    superseded events are resolved out by fold_events — so tip selection here is
    a filter, not a second resolution pass that could disagree with the fold.

    Three cases, and the asymmetry between them is the whole safety argument:

    * tip carries a `body`  -> CREATE or OVERWRITE. The event is authoritative,
      so a divergent file is a hand-edit or tamper: replace it and alarm. The
      content is not lost — it is in git, and the event says what it should be.
    * tip is GRANDFATHERED (pre-v4, no body) and NO file exists -> CREATE a
      stub from the event's `content`, marked reconstructed. This is the ghost
      repair: measured 2026-07-31, 11 index rows had no file and /recall
      returned nothing for an exact-title query, while their events carried
      298-915 chars of real content. Recovering that beats serving a row whose
      body cannot be reached.
    * tip is grandfathered and a file EXISTS -> LEAVE IT ALONE. Overwriting a
      full body with a 200-char event content would destroy the very content
      the projection exists to protect (Grok round 3, A3).

    The fold NEVER deletes a store file. A file with no event at all is an
    inverse ghost: reported for `memory_write adopt`, never removed — deletion
    on a detection heuristic is how a bug becomes data loss.
    """
    if store is None:
        return {"created": [], "repaired": [], "inverse_ghosts": [], "alarms": []}
    out = {"created": [], "repaired": [], "inverse_ghosts": [], "alarms": []}
    seen = set()
    for e in fold["live"]:
        if e["kind"] != "lesson" or not e["subject"].startswith("lesson/"):
            continue
        # A body may only be projected for audiences allowed to carry one; a
        # family-audience event has no body by construction (A2), so this loop
        # cannot write private content into the operator store.
        slug = e["subject"].split("/", 1)[1]
        # Path safety: a subject is a controlled-vocabulary slug, but this is
        # the one place a subject string becomes a FILESYSTEM PATH, and the
        # registry is not a security boundary. `lesson/../../x` must never
        # escape the store.
        if "/" in slug or "\\" in slug or slug in ("", ".", ".."):
            out["alarms"].append(f"refusing to project unsafe slug {slug!r}")
            continue
        seen.add(slug)
        f = store / f"{slug}.md"
        if event_carries_body(e):
            # A2 enforced AT THE PROJECTOR, not only at the producer. emit.py
            # refuses a family-audience body, but the projector consumes events
            # from peers, replay and anything with a shell — so a hand-crafted
            # family event carrying a body would otherwise land in the
            # OPERATOR's store. A confidentiality boundary checked on one side
            # of the wire is not a boundary.
            if e.get("audience") not in BODY_AUDIENCES:
                out["alarms"].append(
                    f"refusing to project a body from audience "
                    f"{e.get('audience')!r}: {slug} (bodies are operator/shared "
                    f"only — this event should not exist)")
                continue
            want = e["body"]
            if not f.exists():
                if apply:
                    f.write_text(want, encoding="utf-8")
                out["created"].append(slug)
            elif f.read_text(encoding="utf-8") != want:
                # OVERWRITE IS THE DESTRUCTIVE BRANCH, so it is the one that
                # needs authority. An unsigned event can be appended by any
                # peer, any replay, anything with a shell; letting it silently
                # replace a memory's body would make "the event is canonical"
                # into a forge primitive — write a lesson event on a victim
                # slug, supersede the prior ids, and the next --project
                # rewrites the store. So: repair only from a SIGNED tip, or
                # when the file on disk is a fold-written reconstruction stub
                # (upgrading a stub to a real body loses nothing).
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
    # Inverse ghosts are NOT alarmed while the v4 backfill is still running:
    # 239 of them is the expected pre-migration state (the whole store predates
    # the mesh), and an alarm that fires on every fold for a known condition
    # trains the operator to ignore the channel — which is the same failure as
    # silently shedding, aimed at attention instead of data. After the backfill
    # marker exists, a file with no event is a real defect and alarms.
    if out["inverse_ghosts"] and (MESH_ROOT / "state" / "v4-backfill-complete").exists():
        out["alarms"].append(
            f"{len(out['inverse_ghosts'])} store file(s) have no event — the "
            f"mesh cannot replicate them and peers will never see them. "
            f"Repair: memory_write.py adopt <slug> --commit "
            f"(e.g. {' '.join(out['inverse_ghosts'][:3])})")
    return out


def event_carries_body(ev):
    """True for a SPEC-v4 lesson event that can drive projection.

    Grandfathered pre-v4 events carry no `body`, and projecting from one would
    materialise an EMPTY store file over a good one — destroying the very
    content the projection exists to protect (Grok round 3, A3). Absence of the
    key is the whole test: v4 events always set it, old ones never can.
    """
    return bool(ev.get("body"))


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
    #
    # The pinned tier is a HARD CAP, not an alarm (Grok review, 2026-07-31 — the
    # first version only appended to `alarms`). Pinned rows are UNCONTESTED
    # residency, an agent can emit a `pin` event, and `pin` outranks `_signed`
    # in score_for_index — so alarm-only left an unmetered write path into the
    # one file every session loads, which is the memory-poisoning amplifier
    # shape ([[improve-loop-poisoning-surface]]) and the same "absence is
    # silent" failure this overlay was written to kill, moved from eviction to
    # capture. An alarm nobody reads is not a bound.
    #
    # Admission is OLDEST-FIRST, and that ordering is the security property: a
    # flood of new pins is refused at the door, rather than displacing the
    # boundaries already resident. Operator-SIGNED pins are admitted before any
    # cap applies — an agent cannot sign by construction, so Craig can always
    # pin past the cap and nothing an agent emits can crowd him out.
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
    # Assigned for EVERY servable event, not just the pinned ones: `_pin` is an
    # overlay on a dict the caller may hold across folds, so a retract has to
    # clear it rather than leave a stale True behind.
    for e in servable:
        e["_pin"] = e["subject"] in pinned_subjects
    if refused:
        alarms.append(
            f"{len(refused)} PIN(s) REFUSED — the pinned tier is at {spent} B of "
            f"its {int(budget)} B cap ({PIN_DELIVERY_SHARE:.0%} of the "
            f"{DELIVERY_BYTES} B delivered file). Not applied: "
            + ", ".join(f"{p['id']}/{p['subject']}" for p in refused[:5])
            + ". Retract a pin to make room, or sign these to admit them.")
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
            "proposals": proposals, "alarms": alarms, "total": len(events),
            # Pin events never render as index rows, so without this projection
            # the only record of WHY a memory is resident — and the only place
            # to find the id needed to retract it — is raw log spelunking.
            "pins": pins, "pins_active": sorted(pinned_subjects),
            "pins_refused": refused, "pinned_bytes": spent}


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
    return (f"- [{e['subject']}] {e['content'][:INDEX_CONTENT_CHARS]}"
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

    # PINS.md — the residency audit trail. A pin event never renders as an index
    # row, so without this the answer to "why is this memory always-on, who
    # decided, and what id do I retract to undo it?" lives only in the raw
    # ndjson. That makes the documented unpin path unreachable in practice a few
    # months out, which is the same as not having one.
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


def index_excluded(ev, exclude):
    """Is this event held OUT of the always-on index by the exclude manifest?

    THE ONE HOME for that question. It was previously implemented twice — once
    inside `residency_partition` and again inside `render_harness_memory` — as
    a private `lesson_slug(e) not in exclude`. Two writers of one rule, and on
    2026-08-01 fixing only the first left `home/*` rows still published: the
    partition agreed they were demoted while the renderer, which actually
    decides what lands in MEMORY.md, never asked. One home per fact applies to
    the code that implements a rule, not just to the facts the rule is about.

    Matches EITHER the bare lesson slug (`one-home-per-fact`) or the full
    subject (`home/cc-claude-md`). Non-lesson subjects have no bare slug, so
    without the second form nothing in `_index-exclude.txt` could ever demote
    them — an always-on row that structurally could not leave the tier.
    """
    subject = ev["subject"]
    slug = subject.split("/", 1)[1] if subject.startswith("lesson/") else None
    return slug in exclude or subject in exclude


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
    # THE APPENDIX GETS ITS OWN CAP, so freed rule bytes are RECLAIMED rather
    # than silently respent on slug names (Craig's declaration, 2026-08-01,
    # `decisions/index-byte-objective-2026-08-01.md`). Without this the loop
    # below only ever shrinks the appendix under breach, so it grows to fill
    # whatever the rules give back: the 2026-08-01 re-homing pass dropped 13
    # rows (-2,329 B of rules) and the file shrank by 315 B, because the
    # appendix took 2,014 B of it. A ceiling the fitter treats as a target is
    # not a ceiling.
    #
    # Capping is safe because the appendix does NOT gate access. retrieve.py is
    # a UserPromptSubmit hook that scores the WHOLE corpus and auto-injects;
    # being named here is advertisement, not reachability. Measured over 1,847
    # logged turns: 373 distinct slugs served, and 241 of them (65%) are never
    # named in the appendix — including the most-served slug in the estate
    # (`soft-failure-exit-zero-with-stderr`, 847 hits). 17 slugs the appendix
    # does name have never been served at all.
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
    # Even the minimum does not fit: publish the safe minimum and say so LOUDLY
    # rather than a plausible-looking file the loader will amputate.
    text = _assemble_harness_memory(head, ranked, 0, slugs, 0)
    return text, {"ok": False, "rows": 0, "rows_total": len(ranked),
                  "slugs": 0, "slugs_total": len(slugs),
                  "bytes": len(text.encode("utf-8")),
                  "lines": len(text.splitlines()),
                  "reason": "does not fit even at the minimum stub"}


def residency_partition(fold, store):
    """Split the operator's live rows by DECLARED residency (SPEC v4).

    Returns (always_on, on_demand, undeclared, report). During migration most
    rows are undeclared and keep exactly their v3 treatment — this is what lets
    the law ship before the retag, and what makes the shadow render a true
    no-op diff until Craig starts declaring.
    """
    def lesson_slug(e):
        return (e["subject"].split("/", 1)[1]
                if e["subject"].startswith("lesson/") else None)

    exclude = ondemand_slugs(store) if store else set()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    always, demand, undeclared, expired = [], [], [], []
    for e in ranked_index(fold, "operator"):
        if index_excluded(e, exclude):
            continue
        r = effective_residency(e)
        # Expiry is a RENDER concern only — no fold-generated events, so replay
        # stays a pure function of human-origin input (Grok round 1, F3).
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


def render_harness_memory_v4(fold, store):
    """The SPEC-v4 index: declared residency decides, ranking only orders.

    Doctrine and pinned rows are rendered in STABLE SLUG ORDER, not by recency.
    That is the whole point: measured 2026-07-31, `score_for_index` collapsed to
    `ts` for 122 of 142 rows, so a memory's survival depended on when it was
    written rather than on what it was for. Stable order is also non-gameable —
    nothing an agent controls moves a row up.
    """
    always, demand, undeclared, rep = residency_partition(fold, store)
    always_sorted = sorted(always, key=lambda e: (
        0 if (e.get("pin") or e.get("_pin")) else 1,
        0 if e.get("_signed") else 1,
        e["subject"]))
    # Undeclared rows keep v3 ranking and sit AFTER declared doctrine: during
    # migration they are the ones that should shed first, because an undeclared
    # row is one nobody has yet said must be resident.
    ranked = always_sorted + undeclared
    slugs = sorted({e["subject"].split("/", 1)[1] for e in demand
                    if e["subject"].startswith("lesson/")})
    head = [
        "# MEMORY — GENERATED by memory-mesh fold (SPEC v4 SHADOW); never hand-edit",
        f"# residency: {rep['always_on']} always-on, {rep['on_demand']} on-demand, "
        f"{rep['undeclared']} undeclared, {rep['expired_hidden']} expiry-hidden",
        ""]
    text, report = fit_harness_memory(head, ranked, slugs)
    report.update(rep)
    return text, report


def render_harness_memory(fold, store):
    """The harness-loaded MEMORY.md: the operator INDEX minus on-demand slugs,
    plus an appendix naming what /recall can reach — sized so the WHOLE file
    clears the loader's ceilings. Returns (text, report)."""
    exclude = ondemand_slugs(store)
    ranked = [e for e in ranked_index(fold, "operator")
              if not index_excluded(e, exclude)]
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


def servable_slugs(fold):
    """The slugs a delivery channel may serve: live, non-superseded,
    non-quarantined, non-parked.

    `fold["live"]` is already the tip set — supersedes resolved out, quarantine
    held out, denial/propose-correct/retract excluded — so this is that set
    minus parked subjects, expressed in the store's filename vocabulary.
    """
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
    """Where the delivery manifest lives — mesh state, not the operator's store."""
    return MESH_ROOT / "state" / "servable.json"


def write_servable_manifest(fold):
    """Publish the delivery manifest the RETRIEVAL tier filters against.

    Why this exists (2026-07-31, found by an outside review and then verified
    live): `retrieve.py` globbed the store directly and consulted no lifecycle
    state whatsoever, so it served QUARANTINED untrusted-lineage facts and
    SUPERSEDED doctrine as "STANDING RULES" — a memory and its own correction
    could ride into the same turn as co-equal rules. The always-on tier honored
    the fold's verdict; the retrieval tier never saw it. Story 029's gate was
    shipped write-side and index-side and simply had no third half.

    The manifest is PRECOMPUTED here rather than derived at retrieval time on
    purpose: retrieval runs on every turn, and a fold is git reads plus an
    ssh-keygen subprocess per signed event — nothing that belongs on the hot
    path.

    It lives in the mesh's own `state/` (already gitignored, already where
    retrieve.py writes its injection log), NOT in the operator's memory store.
    The store is human-owned and write-guarded; a fold that drops derived files
    into it creates untracked noise the guard then refuses to let anyone clean
    up. Derived state belongs with the deriver.

    Never raises: a broken state dir must not fail the fold.
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


# The renderer's own cut, named so the ADMISSION GATE and the RENDERER can never
# disagree about what fits. A content string longer than this does not become a
# longer row; it becomes a row that stops mid-clause.
INDEX_CONTENT_CHARS = 200

# A row that trails off is a FAILED ADMISSION, not a compact one. Measured
# 2026-07-31: 87 events carry content ending in an ellipsis, 62 of them resident
# in the always-on index — inherited from a pre-mesh authoring loop that wrote a
# teaser, truncated it to fit, and appended "…". backfill.py then read that
# derivative as if it were the source. Both halves are now refused at admission:
# a rule the operator cannot read to the end cannot bind, and re-emitting the
# stumps is not a repair anyone can make cheaply once the tail is gone
# ([[bound-the-composed-artifact-not-a-section]]).
_TRAILS_OFF = re.compile(r"(…|\.\.\.)\s*$")


def admission_reject(content):
    """Why this content may not enter the always-on index, or None if it may.

    A REFUSAL, not an alarm. An earlier bound on this channel only appended to a
    warning list while the write proceeded, which is how 62 stumps became
    resident doctrine without anyone deciding they should be
    ([[alarm-only-bound-is-not-a-bound]]).
    """
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
    """Where an event's `content` and its store file's `description:` disagree.

    Every memory has two renderings of its one-line essence, and until this
    check existed nothing compared them — the 2026-07-31 stumps (a producer
    composing content from an already-truncated derivative while the lossless
    text sat in the same file) were invisible until someone counted ellipses.

    Classification, not repair — the fold detects, the operator decides:
    * FILE-RICHER: the file's description extends the event's content. The
      smoking gun that a producer read a derivative again (or a legacy stump
      whose repair is still pending).
    * EVENT-RICHER: the event extends the file. Projection lag or a hand-edit.
    * DISJOINT: neither extends the other — two writers told two stories.

    v4 tips carrying a `body` are exempt: for those the event is the declared
    home and `project_store` already repairs the file from it. Grandfathered
    tips are exactly the era where the FILE was the source (backfill read it),
    which is why the comparison is worth a timer slot at all.
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
            continue                      # ghost repair's problem, not drift
        m = _STORE_DESC.search(f.read_text(encoding="utf-8"))
        if not m:
            continue
        desc, cont = norm(m.group(1).strip().strip('"')), norm(e["content"])
        if desc == cont:
            continue
        # Strip a "Title: " head before comparing: the legacy composer prepended
        # one, and flagging every stump as DISJOINT because of its own prefix
        # would bury the real disjoints in 62 rows of known history.
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
    """What this render would ADD to / DROP from always-on, or None if neither.

    Membership only. A row whose prose changed is not a residency change: the
    memory is still resident and the operator still lives with it. Conflating
    the two would stage on every content refresh, the gate would be routine, and
    a routine gate is one the operator clicks through — which is how a control
    becomes a rubber stamp instead of a decision.
    """
    if not live_path.exists():
        return None                      # first write on a fresh host
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
        # THE RESIDENCY GATE. Residency is the operator's data; a scheduler may
        # not change it on his behalf. On 2026-07-31 a renderer edit freed bytes,
        # the 5-minute timer folded, and 15 rows were promoted into always-on
        # four minutes before the author could show the operator the diff he had
        # promised. The review step was on the CONSUMER path and the machine path
        # is faster than a human, every time — so the fix is here, at the
        # producer, not in a resolution to be careful
        # ([[fix-human-loop-races-at-the-producer]]).
        #
        # Asymmetric, not blanket: a fold that keeps the SAME row set is a
        # refresh of rows the operator already lives with, and blocking it would
        # freeze the index and call that safety. A fold that ADDS or DROPS a row
        # is a residency change, and that one stages and waits.
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
