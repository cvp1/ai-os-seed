#!/usr/bin/env python3
"""memory-mesh producer — append one event to THIS host's log, commit, nudge.

    emit.py --kind correct --subject ssh-route/{{REDACTED}} --polarity exists \
            --content "ssh st21 verified live" --home "FLEET.md#reachability" \
            --session $SESH [--sync] [--pin] [--supersedes ID] ...

Local append + local commit: succeeds through any partition (acks=1).
--sync: block until ≥1 peer has fetched this event id — opt-in acks=all for
operator corrections (SPEC Kafka-gap row 1). Nudge is garnish, never
load-bearing.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

FACT_RX = M.re.compile(
    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\blocalhost:\d{2,5}\b")


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
    args = ap.parse_args()

    # Fact-shape gate at the producer: a lesson carrying infrastructure
    # literals without a home is a fact-copy being born (one home per fact).
    if args.kind == "lesson" and FACT_RX.search(args.content) and not args.home:
        sys.exit("emit: lesson carries fact literals but no --home pointer — "
                 "put the fact in its home first, then point at it")

    body = args.body
    if args.body_file:
        if body:
            sys.exit("emit: pass --body or --body-file, not both")
        body = Path(args.body_file).read_text(encoding="utf-8")
    hook = args.hook
    if hook is not None and len(hook) > M.HOOK_MAX_CHARS:
        # Refuse, never truncate (SPEC v4 A4). A machine-shortened hook is a
        # rule with its qualifier cut off, and this one is the line the agent
        # actually reads every session — degrading it silently is how a bounded
        # rule becomes a wrong rule.
        sys.exit(f"emit: --hook is {len(hook)} chars, over the "
                 f"{M.HOOK_MAX_CHARS} limit — rewrite it shorter; it is the "
                 "line every session reads")
    # Audience confidentiality under body-in-event (SPEC v4 A2): bodies ride
    # the fleet git transport, so only fleet-visible audiences may carry one.
    # family/host-private memories emit hook-only and keep the body local —
    # render-time filtering is not confidentiality.
    if body is not None and args.audience not in M.BODY_AUDIENCES:
        sys.exit(f"emit: audience {args.audience!r} may not carry a body — "
                 "bodies replicate to every peer host. Emit hook-only and "
                 "keep the body in the local store.")
    # SPEC v4: close the ghost hole. A lesson event with NO body and NO store
    # file behind it renders an always-on index row that /recall cannot serve —
    # measured 2026-07-31: 11 such rows, 2,825 B, invisible to an exact-title
    # query. The ghosts came from agents invoking this producer directly, so the
    # gate belongs here rather than in any one caller.
    #
    # This cannot cause amnesia: BOTH escapes keep the lesson. Carry --body (the
    # event is then self-sufficient and the fold projects the file), or write
    # through memory_write, which creates the file and emits in one locked step.
    #
    # Resolved through harness_store(), which carries the SANDBOX GUARD: a
    # drill or replay pointed at a throwaway event log gets None and the gate
    # stands down. Hardcoding the operator's store path here would have made
    # every sandbox consult — and be judged against — the real brain, which is
    # the same class of bug that guard was written for.
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
        # Lessons chain EXPLICITLY (invariant 4 — resolution is never
        # temporal): a lesson re-emit on a subject supersedes every live
        # predecessor it can see. Two hosts revising blind to each other
        # therefore leave two live lessons — which the fold PARKS, making the
        # race visible instead of letting a clock decide who wins.
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
        supersedes=supersedes, pin=args.pin, residency=args.residency,
        hook=hook, body=body, expires=args.expires,
        carry_forward=args.carry_forward)

    # Everything above is validation — make_event raises on a refused admission,
    # a bad schema or an oversized event, so reaching here means this event WOULD
    # be accepted. That is the whole answer a diagnosis needs, and it is now
    # available without a write. Placed before repo_lock so a dry run takes no
    # lock and cannot block a concurrent writer.
    if args.dry_run:
        print(json.dumps(ev, ensure_ascii=False, indent=1))
        print(f"dry-run: VALID — {len(line.encode())} B, would append to "
              f"{M.HOST}.ndjson as {ev['id']}. Nothing written.", file=sys.stderr)
        return 0

    # One write() of one line — a torn append is a torn LINE, which the fold
    # holds out as unparseable rather than corrupting neighbors (drill 3).
    # append_event_line also heals a pre-existing torn tail (drill 7).
    # Lock the WHOLE append+add+commit, not each git call: several agents in
    # one shell on one host are several writers to this single-writer log, and
    # a commit that loses index.lock leaves its event uncommitted — invisible
    # to the fold, which reads committed state only. Measured 2/5 failures at
    # 5-way concurrency before this lock (2026-07-28).
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
        # acks=all, opt-in: a peer's fold fetches us; we then see OUR event
        # replicated by fetching THEIR last-seen state marker. Simplest
        # honest check at 3 nodes: ask each peer for the id over ssh.
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
