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
    ap.add_argument("--pin", action="store_true")
    ap.add_argument("--sync", action="store_true",
                    help="block until one peer confirms replication (bounded)")
    ap.add_argument("--no-nudge", action="store_true")
    args = ap.parse_args()

    # Fact-shape gate at the producer: a lesson carrying infrastructure
    # literals without a home is a fact-copy being born (one home per fact).
    if args.kind == "lesson" and FACT_RX.search(args.content) and not args.home:
        sys.exit("emit: lesson carries fact literals but no --home pointer — "
                 "put the fact in its home first, then point at it")

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
        supersedes=supersedes, pin=args.pin)

    # One write() of one line — a torn append is a torn LINE, which the fold
    # holds out as unparseable rather than corrupting neighbors (drill 3).
    # append_event_line also heals a pre-existing torn tail (drill 7).
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
