#!/usr/bin/env python3
"""Time-travel replay — the exact view any agent had at any past moment.

    replay.py --at 2026-07-27T22:15:00Z [--subject ssh-route/{{REDACTED}}] [--audience operator]

Because logs are append-only and the fold is deterministic, folding only the
events with ts <= T reproduces the T-time view byte-for-byte. Mutable-DB
memory systems cannot do this; it is this design's signature capability.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", required=True, help="UTC instant, e.g. 2026-07-27T22:15:00Z")
    ap.add_argument("--subject")
    ap.add_argument("--audience", default="operator", choices=sorted(M.VIEW_INCLUDES))
    args = ap.parse_args()

    events, problems = M.read_all_events()
    then = [e for e in events if e["ts"] <= args.at]
    fold = M.fold_events(then, M.load_registry())
    print(f"# REPLAY @ {args.at} — {len(then)} of {len(events)} events existed")
    if args.subject:
        hits = [e for e in fold["live"] if e["subject"] == args.subject]
        parked = fold["parked"].get(args.subject, [])
        for e in hits:
            print(f"LIVE   {e['ts']} {e['host']} [{e['kind']}/{e['polarity']}] {e['content']}")
        for e in parked:
            print(f"PARKED {e['ts']} {e['host']} [{e['kind']}/{e['polarity']}] {e['content']}")
        if not hits and not parked:
            print(f"(no live or parked events on {args.subject} at that instant)")
    else:
        views = M.render_views(fold, args.audience)
        print(views["INDEX.md"])
        print(views["CONFLICTS.md"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
