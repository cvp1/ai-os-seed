#!/usr/bin/env python3
"""index_growth — record how full the always-on doctrine index is, over time.

The harness injects the whole of `MEMORY.md` into every session and silently
truncates past 200 lines or ~25 KB, so `mesh_lib` publishes under a hard ceiling
and sheds content to fit. The shed order matters: named on-demand slugs go first
(reachable by `/recall` anyway), and only when those run out does it start
dropping INDEX ROWS — a row being the sole always-on trace of a memory.

So the number that matters is not raw file size, it is **remaining slack** — the
bytes of appendix left to shed before doctrine rows start disappearing. Measured
2026-07-29: 23,929 B of a 24,986 B ceiling with 2,552 B of appendix left, i.e.
~14 rows of runway.

This exists because the same measurement raised a question it could not answer:
the event store was two days old (118 backfill events, then 19), so the organic
growth rate was unknowable and both "we have a month" and "we are fine" were
unfalsifiable. One point a day makes it answerable.

    python3 memory-mesh/index_growth.py --dry-run

Stdlib + _lib (influx); targets /usr/bin/python3.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from _lib import influx  # noqa: E402
import mesh_lib as M     # noqa: E402

MEASUREMENT = "cc_doctrine_index"
ROW_RE = re.compile(r"^- \[")


def snapshot():
    """Current index composition, or None when this host has not opted in.

    Reads what is ON DISK rather than re-rendering: the published artifact is what
    sessions actually load, and a re-render could disagree with it (that gap is
    precisely the failure the write gate exists to catch).
    """
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

    # The appendix is the shock absorber: everything from the on-demand heading to
    # EOF is sheddable before any row is at risk. Its size IS the slack.
    slack = 0
    named = total_ondemand = 0
    for i, l in enumerate(lines):
        if l.startswith(M.ONDEMAND_HEADING.split("—")[0].strip()):
            slack = len(("\n".join(lines[i:]) + "\n").encode("utf-8"))
            tail = "\n".join(lines[i:])
            named = tail.count(" · ") + max(0, tail.count("\n") - 1)
            # Two summary-line shapes leave mesh_lib._assemble_harness_memory:
            # "... (N on-demand total)" when some slugs are still named, or
            # "N on-demand memories - not listed here" when none are (named=0,
            # the all-demoted case) - match both or this silently reads 0.
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
        # Bytes of appendix left to shed before doctrine rows start dropping, and
        # that slack expressed in rows — the operator-legible form of the same fact.
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
        influx.write_points([(MEASUREMENT, {"host": os.uname().nodename}, snap, ts)])
    except influx.InfluxError as e:
        print(f"index_growth: skipped — {e}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
