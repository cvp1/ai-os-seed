#!/usr/bin/env python3
"""memory-mesh producer: append one event to this host's log, commit, nudge peers.

    emit.py --kind correct --subject ssh-route/HOST --polarity exists \
            --content "..." --home "FLEET.md#reachability" \
            --session $SESH [--sync] [--pin] [--supersedes ID] ...

Local append + commit succeeds through any partition; --sync blocks until a peer has the event.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=sorted(M.KINDS))
    ap.add_argument("--subject", required=True)
    ap.add_argument("--content", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--polarity", default="n/a", choices=sorted(M.POLARITIES))
    ap.add_argument("--home")
    ap.add_argument("--lineage", default="operator-direct", choices=sorted(M.LINEAGES))
    ap.add_argument("--audience", default="operator", choices=sorted(M.AUDIENCES))
    ap.add_argument("--confidence", default="inferred", choices=sorted(M.CONFIDENCES))
    ap.add_argument("--supersedes")
    ap.add_argument("--supersedes-live-on", metavar="SUBJECT",
                    help="resolve to the ids of every LIVE event on SUBJECT "
                         "(a local fold pass) and supersede them all — the "
                         "retract/replace path for lessons, where the caller "
                         "knows the subject but not the event ids")
    ap.add_argument("--residency", choices=sorted(M.RESIDENCIES),
                    help="SPEC v4 tier. Omit while a memory's residency is "
                         "undeclared — the renderer then treats it exactly as "
                         "v3 did. doctrine/pinned require an operator "
                         "signature to hold across the mesh; unsigned events "
                         "are READ as state (mesh_lib.effective_residency).")
    ap.add_argument("--hook", help="SPEC v4: the served index line, <=140 chars")
    ap.add_argument("--body", help="SPEC v4: the full memory body (the fact)")
    ap.add_argument("--body-file", help="read --body from a file")
    ap.add_argument("--expires", metavar="YYYY-MM-DD",
                    help="state rows only: render-hide after this date")
    ap.add_argument("--pin", action="store_true")
    ap.add_argument("--tier", choices=sorted(M.TIERS),
                    help="with --kind tier: the index tier this subject holds")
    ap.add_argument("--sync", action="store_true",
                    help="block until one peer confirms replication (bounded)")
    ap.add_argument("--no-nudge", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="VALIDATE and print the event; write nothing. Until "
                         "this existed the only way to find out whether an "
                         "event would be accepted was to create it, so "
                         "diagnosing a refusal meant polluting the log — which "
                         "is exactly how a junk 'probe' event reached the live "
                         "log on 2026-07-31 and had to be retracted. A log "
                         "whose only test is a real write has no test.")
    ap.add_argument("--carry-forward", action="store_true",
                    help="re-emit an EXISTING event's content verbatim "
                         "(lineage/retag work), bypassing the admission gate "
                         "that refuses stumped or over-length lesson content. "
                         "Perpetuating a legacy stump adds no new loss; "
                         "minting one does — never use this for new content.")
    ap.add_argument("--pointer", action="store_true",
                    help="this lesson is a REFERENCE memory whose purpose is "
                         "to point at a fact's home, so fact-shaped literals "
                         "(an IP, a host:port) are admitted in its text. "
                         "memory_write passes this for --type reference; no "
                         "other caller should need it — a lesson that wants "
                         "to state a fact points at the fact's home instead "
                         "(one home per fact).")
    args = ap.parse_args()

    body = args.body
    if args.body_file:
        if body:
            sys.exit("emit: pass --body or --body-file, not both")
        body = Path(args.body_file).read_text(encoding="utf-8")
    hook = args.hook
    if hook is not None and len(hook) > M.HOOK_MAX_CHARS:
        # Refuse rather than truncate: a cut hook can lose its qualifier.
        sys.exit(f"emit: --hook is {len(hook)} chars, over the "
                 f"{M.HOOK_MAX_CHARS} limit — rewrite it shorter; it is the "
                 "line every session reads")
    # Bodies replicate to every peer, so only fleet-visible audiences may carry one.
    if body is not None and args.audience not in M.BODY_AUDIENCES:
        sys.exit(f"emit: audience {args.audience!r} may not carry a body — "
                 "bodies replicate to every peer host. Emit hook-only and "
                 "keep the body in the local store.")
    # Refuse a lesson with no body and no store file (an index row recall cannot
    # serve). harness_store() returns None in a sandbox, which disables the gate.
    ghost = M.ghost_refusal_reason(args.kind, args.subject, body,
                                   M.harness_store())
    if ghost:
        sys.exit("emit: " + ghost)
    if args.expires and args.residency == "pinned":
        sys.exit("emit: a pinned memory may not carry --expires — expiry is "
                 "for state rows; pinned is the tier that never lapses")

    supersedes = [s for s in (args.supersedes or "").split(",") if s] or None
    if args.supersedes_live_on:
        ids = M.unsuperseded_ids(args.supersedes_live_on)
        if not ids:
            print(f"emit: nothing live on {args.supersedes_live_on!r} — "
                  "no event emitted")
            return 0
        supersedes = sorted(set(ids) | set(supersedes or []))
    elif args.kind == "lesson" and not supersedes:
        # A lesson re-emit supersedes every live predecessor it can see;
        # concurrent revisions on two hosts stay live and the fold parks them.
        supersedes = M.unsuperseded_ids(args.subject) or None

    reg = M.load_registry()
    warn = M.subject_problem(args.subject, reg)
    if warn:
        print(f"emit: WARNING {warn} — event will be PARKED as UNNORMALIZED "
              f"until subjects.toml knows this class", file=sys.stderr)

    ev, line = M.make_event(
        args.kind, args.subject, args.content, session=args.session,
        polarity=args.polarity, home=args.home, lineage=args.lineage,
        audience=args.audience, confidence=args.confidence,
        supersedes=supersedes, pin=args.pin, tier=args.tier, residency=args.residency,
        hook=hook, body=body, expires=args.expires,
        carry_forward=args.carry_forward, pointer=args.pointer)

    # make_event has validated; a dry run stops here, before taking the lock.
    if args.dry_run:
        print(json.dumps(ev, ensure_ascii=False, indent=1))
        print(f"dry-run: VALID — {len(line.encode())} B, would append to "
              f"{M.HOST}.ndjson as {ev['id']}. Nothing written.", file=sys.stderr)
        return 0

    # Lock append+add+commit together: concurrent local writers would otherwise
    # race on index.lock and leave events uncommitted (the fold reads commits only).
    with M.repo_lock():
        log = M.append_event_line(line)
        M.git("add", str(log.relative_to(M.MESH_ROOT)))
        M.git("commit", "-q", "-m", f"emit {ev['kind']} {ev['subject']} {ev['id']}")
    print(f"emitted {ev['id']} ({ev['kind']} {ev['subject']}) → {log.name}")

    if not args.no_nudge:
        for host, ssh in M.peers():
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes", ssh,
                 "systemctl --user start memory-fold.service"],
                capture_output=True, timeout=10)

    if args.sync:
        # Poll each peer over ssh until one has the event id.
        deadline = time.time() + 60
        confirmed = None
        while time.time() < deadline and not confirmed:
            for host, ssh in M.peers():
                r = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes", ssh,
                     f"grep -l {ev['id']} ~/memory-events/events/*.ndjson "
                     f"2>/dev/null | head -1"],
                    capture_output=True, text=True, timeout=15)
                if r.stdout.strip():
                    confirmed = host
                    break
            if not confirmed:
                time.sleep(3)
        if confirmed:
            print(f"sync: replicated on {confirmed}")
        else:
            sys.exit("sync: NO peer confirmed within 60s — event is committed "
                     "LOCALLY ONLY (acks=1). Retry --sync when a peer is up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
