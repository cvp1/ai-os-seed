#!/usr/bin/env python3
"""Bound the observability store (principle 8): delete run/egress/fleet rows
older than a retention cutoff, then VACUUM to reclaim the space.

Safe to run repeatedly. The daily/weekly rollups already project history into
InfluxDB (cost_rollup 35d window, egress_rollup 14d, fleet_rollup 14d,
interactive_rollup 90d) and every eval/freshness reader looks only at recent
rows, so raw rows past the cutoff are dead weight — nothing reads them.

Default is --dry-run (report only). Pass --apply to actually delete + VACUUM.
Prints DB size before/after. Stdlib only; targets /usr/bin/python3.

    prune.py                 # dry-run: how many rows WOULD be pruned
    prune.py --apply         # delete >RETENTION_DAYS + VACUUM
    prune.py --days 365 --apply
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import db

RETENTION_DAYS = 180
# Every append-only table keyed by an ISO-8601 timestamp column.
TABLES = {"runs": "started_at", "egress": "ts", "fleet": "ts"}


def _size_bytes(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _fmt_mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune the observability store to a retention window.")
    ap.add_argument("--days", type=int, default=RETENTION_DAYS,
                    help=f"retention window in days (default {RETENTION_DAYS})")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete + VACUUM (default is a dry-run report)")
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat(timespec="seconds")
    path = db.db_path()
    before = _size_bytes(path)

    with db.connect() as conn:
        counts = {}
        for table, col in TABLES.items():
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} < ?", (cutoff,)).fetchone()[0]
            except Exception as e:  # table may not exist on a fresh DB
                n = 0
                print(f"  ({table}: {e})", file=sys.stderr)
            counts[table] = n
        total = sum(counts.values())

        summary = ", ".join(f"{t}={counts[t]}" for t in TABLES)
        if not args.apply:
            print(f"[dry-run] would prune rows older than {args.days}d (< {cutoff}): {summary} "
                  f"({total} total). DB {_fmt_mb(before)}. Pass --apply to execute.")
            return 0

        if total == 0:
            print(f"prune: nothing older than {args.days}d (< {cutoff}); DB {_fmt_mb(before)} unchanged.")
            return 0

        for table, col in TABLES.items():
            conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
        conn.commit()

    # VACUUM must run outside a transaction and on its own connection.
    with db.connect() as conn:
        conn.isolation_level = None
        conn.execute("VACUUM")

    after = _size_bytes(path)
    print(f"prune: deleted {total} rows older than {args.days}d ({summary}); "
          f"VACUUM {_fmt_mb(before)} -> {_fmt_mb(after)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
