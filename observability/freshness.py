#!/usr/bin/env python3
"""Freshness / liveness check over the observability store.

Reads expected cadence from freshness.json and, for each scheduled job, reports:
  OK      newest run is recent and succeeded
  STALE   newest run is older than max_age (job has gone silent)
  MISSING job is configured but has never logged a run
  FAILING newest run is recent but exited non-zero

Designed to be run as its own cron job: it prints ONLY problems to stdout,
led by a `FINDINGS:` summary line, and exits 0 — finding stale/failing jobs is
this job WORKING, not it breaking (the Story 008 found-work convention in
cron/MANIFEST.md; non-zero is reserved for freshness itself crashing).
Use --all to also print healthy jobs, --json for machine-readable output
(pure JSON, no FINDINGS prefix; the problem count is in the payload).

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import repo_hygiene
import switches

_HERE = Path(__file__).resolve().parent
_CONFIG = _HERE / "freshness.json"
# cron/ is CC's own hermes-shim sync script; scheduler/ is the plain
# crontab/launchd equivalent shipped by cc-seed (SEED-014) — checked in that
# order so this file's behavior here is byte-for-byte unchanged (cron/
# exists on this host and matches first), while the same file, unmodified,
# also works when genericized into the seed.
_SYNC_CANDIDATES = [_HERE.parent / "cron" / "sync.sh", _HERE.parent / "scheduler" / "sync.sh"]
_DUR = re.compile(r"^\s*(\d+)\s*([dhm])\s*$")


def repo_hygiene_problems():
    """Story 008: sweep every CC git repo for dirty/ahead-of-remote (aged past a
    7-day grace so work-in-flight stays quiet) + untracked cron-exec targets, so
    the "committed + pushed" invariant can't silently decay. Never crashes the job."""
    try:
        return [f"{p['kind']}: {p['repo']}: {p['detail']}" if p['repo'] != '-'
                else f"{p['kind']}: {p['detail']}"
                for p in repo_hygiene.problems(days=7)]
    except Exception as e:  # noqa: BLE001
        return [f"repo_hygiene failed to run: {e}"]


def shim_drift():
    """Run `cron/sync.sh --check` (or the seed's `scheduler/sync.sh` when
    that's what's installed) so set/content drift between the manifest and
    the installed jobs is edge-triggered by the daily freshness job, not
    only found at audit time (Story 025). Returns a list of DRIFT lines
    (empty = in sync); a missing/erroring checker is reported rather than
    silently swallowed."""
    sync = next((p for p in _SYNC_CANDIDATES if p.exists()), None)
    if sync is None:
        candidates = " or ".join(str(p) for p in _SYNC_CANDIDATES)
        return [f"sync.sh missing at {candidates}"]
    try:
        r = subprocess.run(["bash", str(sync), "--check"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — never let the backstop crash the job
        return [f"sync.sh --check failed to run: {e}"]
    if r.returncode == 0:
        return []
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("DRIFT:")]
    return lines or [f"sync.sh --check exit {r.returncode}: {(r.stderr or r.stdout).strip()[:120]}"]


def parse_age(s: str) -> timedelta:
    m = _DUR.match(s)
    if not m:
        raise SystemExit(f"freshness.json: bad duration {s!r} (expected e.g. 26h, 20m, 8d)")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{secs}s"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h"
    return f"{hrs // 24}d"


def evaluate(conn, now):
    cfg = json.loads(_CONFIG.read_text())
    results = []
    disabled = switches.disabled_jobs()
    for job, spec in cfg["jobs"].items():
        if job in disabled:
            continue  # switched off via the control panel — not a staleness fault
        max_age = parse_age(spec["max_age"])
        label = spec.get("label", job)
        row = conn.execute(
            "SELECT started_at, ok, exit_code, summary, error_tail "
            "FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1", (job,)
        ).fetchone()
        if row is None:
            results.append({"job": job, "label": label, "status": "MISSING",
                            "detail": "never run", "age": None})
            continue
        age = now - _parse_iso(row["started_at"])
        if age > max_age:
            results.append({"job": job, "label": label, "status": "STALE",
                            "detail": f"last run {_fmt_age(age)} ago "
                                      f"(max {spec['max_age']})", "age": _fmt_age(age)})
        elif not row["ok"]:
            tail = (row["error_tail"] or "").splitlines()
            note = tail[-1] if tail else f"exit {row['exit_code']}"
            results.append({"job": job, "label": label, "status": "FAILING",
                            "detail": f"last run failed: {note[:120]}",
                            "age": _fmt_age(age)})
        else:
            results.append({"job": job, "label": label, "status": "OK",
                            "detail": f"last run {_fmt_age(age)} ago", "age": _fmt_age(age)})
    return results


def main():
    ap = argparse.ArgumentParser(description="Freshness/liveness check over the run log.")
    ap.add_argument("--all", action="store_true", help="also print healthy/never-run jobs")
    ap.add_argument("--strict", action="store_true",
                    help="treat MISSING (never run) as a paging problem too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        results = evaluate(conn, now)
    drift = shim_drift()  # Story 025: shim set/content drift is a paging problem too
    repo = repo_hygiene_problems()  # Story 008: dirty/unpushed/untracked-exec drift

    # STALE (went silent) and FAILING (crashed) are high-confidence — they page.
    # MISSING (never run) is weaker: usually a newly-instrumented job that hasn't
    # hit its next schedule yet, so it's informational unless --strict is given.
    paging = {"STALE", "FAILING", "MISSING"} if args.strict else {"STALE", "FAILING"}
    problems = [r for r in results if r["status"] in paging]

    if args.json:
        print(json.dumps({"checked_at": now.isoformat(timespec="seconds"),
                          "problems": len(problems), "results": results,
                          "shim_drift": drift, "repo_hygiene": repo}, indent=2))
        return 0

    shown = results if args.all else problems
    if not shown and not drift and not repo:
        # Silent success: nothing printed, nothing found.
        return 0
    if problems or drift or repo:
        # Found work is success (Story 008): report with a FINDINGS: first line
        # (log_run stores it as the run's summary) and exit 0 below.
        print(f"FINDINGS: {len(problems)} job problem(s), "
              f"{len(drift)} shim drift, {len(repo)} repo hygiene")
    for r in sorted(shown, key=lambda x: (x["status"] == "OK", x["job"])):
        print(f"[{r['status']:7}] {r['label']}: {r['detail']}")
    for d in drift:
        print(f"[{'DRIFT':7}] cron shim reconcile: {d}")
    for rp in repo:
        print(f"[{'REPO':7}] git hygiene: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
