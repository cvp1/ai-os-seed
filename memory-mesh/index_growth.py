#!/usr/bin/env python3
"""index_growth: record how full the always-on MEMORY.md index is, over time.

Reports bytes/lines/rows against the loader ceiling and the remaining slack:
bytes of on-demand appendix that can be shed before index rows start dropping.

    python3 memory-mesh/index_growth.py --dry-run

Stdlib; _lib.influx optional (the write is skipped with a stderr note if absent).
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import mesh_lib as M     # noqa: E402

MEASUREMENT = "cc_doctrine_index"
ROW_RE = re.compile(r"^- \[")


def snapshot():
    """Return the on-disk index composition, or None when this host has not opted in."""
    store = M.harness_store()
    if store is None:
        return None
    path = os.path.join(str(store), "MEMORY.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None

    lines = text.splitlines()
    nbytes = len(text.encode("utf-8"))
    rows = [l for l in lines if ROW_RE.match(l)]

    # Slack = size of the on-demand appendix (heading to EOF).
    slack = 0
    named = total_ondemand = 0
    for i, l in enumerate(lines):
        if l.startswith(M.ONDEMAND_HEADING.split("—")[0].strip()):
            slack = len(("\n".join(lines[i:]) + "\n").encode("utf-8"))
            tail = "\n".join(lines[i:])
            named = tail.count(" · ") + max(0, tail.count("\n") - 1)
            # Match both summary-line shapes: "(N on-demand total)" and
            # "N on-demand memories".
            m = (re.search(r"\((\d+) on-demand total\)", tail)
                 or re.search(r"(\d+) on-demand memories", tail))
            total_ondemand = int(m.group(1)) if m else 0
            break

    return {
        "bytes": nbytes,
        "lines": len(lines),
        "rows": len(rows),
        "ceiling_bytes": M.LOADER_BYTE_CEILING,
        "ceiling_lines": M.LOADER_LINE_CEILING,
        "pct_full": round(100.0 * nbytes / M.LOADER_BYTE_CEILING, 2),
        # Slack in bytes and as an approximate row count.
        "slack_bytes": slack,
        "slack_rows": (slack // max(1, nbytes // max(1, len(rows)))) if rows else 0,
        "ondemand_named": named,
        "ondemand_total": total_ondemand,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    snap = snapshot()
    if snap is None:
        print("index_growth: host has not opted in to a fold-generated index")
        return 0

    if args.dry_run or True:  # always print — this is a metric a human reads too
        print(f"index_growth: {snap['bytes']:,} B / {snap['ceiling_bytes']:,} B "
              f"({snap['pct_full']}% full), {snap['rows']} rows, "
              f"{snap['lines']}/{snap['ceiling_lines']} lines")
        print(f"  slack before doctrine rows drop: {snap['slack_bytes']:,} B "
              f"(~{snap['slack_rows']} rows); on-demand named "
              f"{snap['ondemand_named']}/{snap['ondemand_total']}")
    if args.dry_run:
        print("-- dry run: NOT written")
        return 0

    ts = int(datetime.now(timezone.utc).timestamp() * 1e9)
    try:
        from _lib import influx  # lazy: _lib.influx may be absent
    except ImportError as e:
        print(f"index_growth: skipped — _lib.influx unavailable ({e})", file=sys.stderr)
        return 0
    try:
        influx.write_points([(MEASUREMENT, {"host": os.uname().nodename}, snap, ts)])
    except influx.InfluxError as e:
        print(f"index_growth: skipped — {e}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
