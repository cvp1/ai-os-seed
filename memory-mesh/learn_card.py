#!/usr/bin/env python3
"""learn_card — review the night's quarantined harvest (learn_harvest.py).

    learn_card.py show                  the pending items and their event ids
    learn_card.py accept N              promote item N: runs `sign.py --promote <id>`,
                                        which needs the operator's signing key
    learn_card.py reject N [--reason]   fingerprint item N so it never comes back

Accept only hands the event id to sign.py; this tool never writes trusted lineage.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import learn_harvest  # noqa: E402


def _items():
    try:
        return json.loads(learn_harvest.CARD.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError):
        return []


def _pick(n):
    items = _items()
    if not 1 <= n <= len(items):
        print("learn-card: no item %d (%d pending)" % (n, len(items)), file=sys.stderr)
        return None
    return items[n - 1]


def cmd_show(_args):
    items = _items()
    if not items:
        print("learn-card: none pending")
        return 0
    for i, item in enumerate(items, 1):
        print("%d. %s  [%s]" % (i, item.get("hook"), item.get("id") or "no event id"))
        print("   %s" % item.get("body"))
    return 0


def cmd_accept(args):
    item = _pick(args.n)
    if item is None:
        return 2
    eid = item.get("id")
    if not eid:
        print("learn-card: item %d has no mesh event id (written before the "
              "upstream, or the door never emitted) — nothing to promote" % args.n,
              file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(learn_harvest.SIGN),
                           "--promote", eid]).returncode


def cmd_reject(args):
    item = _pick(args.n)
    if item is None:
        return 2
    learn_harvest.remember_reject(item.get("hook") or "", args.reason)
    learn_harvest.remember_reject(item.get("body") or "", args.reason)
    learn_harvest._metric("rejected", slug=item.get("slug"), reason=args.reason)
    print("learn-card: rejected %s — it stays quarantined and will not be "
          "harvested again" % (item.get("slug") or args.n))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Review the quarantined learn harvest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    a = sub.add_parser("accept")
    a.add_argument("n", type=int)
    r = sub.add_parser("reject")
    r.add_argument("n", type=int)
    r.add_argument("--reason", default="")
    args = ap.parse_args(argv)
    return {"show": cmd_show, "accept": cmd_accept, "reject": cmd_reject}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
