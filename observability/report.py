#!/usr/bin/env python3
"""Query the CC observability store.

Examples:
    report.py                      # last 20 runs across all jobs
    report.py --job morning_brief  # last 20 runs of one job
    report.py --failures           # only failed runs (last 20)
    report.py --since 7d           # runs in the last 7 days
    report.py --stats              # per-job summary (count / fail / p50 / last)
    report.py --json               # machine-readable (for status-site)

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import db

_DUR = re.compile(r"^\s*(\d+)\s*([dhm])\s*$")


def _since_to_iso(s: str) -> str:
    m = _DUR.match(s)
    if not m:
        raise SystemExit(f"--since: expected e.g. 7d / 24h / 90m, got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds")


def _fmt_dur(ms):
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def cmd_list(conn, args):
    where, params = [], []
    if args.job:
        where.append("job = ?"); params.append(args.job)
    if args.failures:
        where.append("ok = 0")
    if args.since:
        where.append("started_at >= ?"); params.append(_since_to_iso(args.since))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM runs {clause} ORDER BY started_at DESC LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        print("(no matching runs)")
        return
    print(f"{'WHEN (UTC)':20} {'JOB':22} {'OK':3} {'DUR':8} SUMMARY")
    print("-" * 90)
    for r in rows:
        when = r["started_at"][:19].replace("T", " ")
        ok = "ok" if r["ok"] else "FAIL"
        note = r["summary"] or ("" if r["ok"] else (r["error_tail"] or "").splitlines()[-1:] and
                                (r["error_tail"] or "").splitlines()[-1] or "")
        print(f"{when:20} {r['job'][:22]:22} {ok:3} {_fmt_dur(r['duration_ms']):8} {note[:60]}")


def cmd_stats(conn, args):
    since = _since_to_iso(args.since) if args.since else None
    clause, params = ("WHERE started_at >= ?", [since]) if since else ("", [])
    rows = conn.execute(
        f"""SELECT job,
                   COUNT(*)                          AS runs,
                   SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS fails,
                   SUM(COALESCE(cost_usd, 0))        AS cost_usd,
                   SUM(COALESCE(tokens_out, 0))      AS tokens_out,
                   MAX(started_at)                   AS last_run
            FROM runs {clause}
            GROUP BY job ORDER BY job""",
        params,
    ).fetchall()
    # p50 duration per job (median via ordered fetch — datasets are small).
    out = []
    for r in rows:
        durs = [x[0] for x in conn.execute(
            "SELECT duration_ms FROM runs WHERE job=? AND duration_ms IS NOT NULL "
            "ORDER BY duration_ms", (r["job"],)).fetchall()]
        p50 = durs[len(durs) // 2] if durs else None
        out.append({"job": r["job"], "runs": r["runs"], "fails": r["fails"],
                    "p50_ms": p50, "cost_usd": round(r["cost_usd"], 4),
                    "tokens_out": r["tokens_out"], "last_run": r["last_run"]})
    if args.json:
        print(json.dumps(out, indent=2))
        return
    if not out:
        print("(no runs recorded yet)")
        return
    total_cost = sum(o["cost_usd"] for o in out)
    print(f"{'JOB':22} {'RUNS':>5} {'FAIL':>5} {'p50':>8} {'COST$':>8}  LAST RUN (UTC)")
    print("-" * 80)
    for o in out:
        last = (o["last_run"] or "")[:19].replace("T", " ")
        cost = f"{o['cost_usd']:.4f}" if o["cost_usd"] else "-"
        print(f"{o['job'][:22]:22} {o['runs']:>5} {o['fails']:>5} "
              f"{_fmt_dur(o['p50_ms']):>8} {cost:>8}  {last}")
    if total_cost:
        print("-" * 80)
        print(f"{'TOTAL':22} {'':>5} {'':>5} {'':>8} {total_cost:>8.4f}")


def main():
    ap = argparse.ArgumentParser(description="Query the CC observability store.")
    ap.add_argument("--job", help="filter to one job")
    ap.add_argument("--failures", action="store_true", help="only failed runs")
    ap.add_argument("--since", help="window, e.g. 7d / 24h / 90m")
    ap.add_argument("--limit", type=int, default=20, help="max rows (list mode)")
    ap.add_argument("--stats", action="store_true", help="per-job summary instead of a list")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    with db.connect() as conn:
        if args.stats:
            cmd_stats(conn, args)
        else:
            cmd_list(conn, args)


if __name__ == "__main__":
    sys.exit(main())
