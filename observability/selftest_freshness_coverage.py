#!/usr/bin/env python3
"""Self-test for freshness.json coverage and soft-failure detection.

Run: /usr/bin/python3 observability/selftest_freshness_coverage.py  (from repo root)
Checks: target jobs are configured under their real --job names; max_age
fits each job's schedule gap (real runs.db, read-only); every job named in
`_skipped` exists; soft_failure() tracks current state. Exits non-zero on failure.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db as obs_db  # noqa: E402
import freshness  # noqa: E402

CRON_JOBS_FILE = Path("~/.{{REDACTED}}/cron/jobs.json").expanduser()

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


TARGET_JOBS = [
    "ranch_watch", "device_watch", "owner_alerts", "frontier_watch",
    "starlink_watch", "meshtastic_health", "panel_health", "rain_watch",
    "vivint_ha_watchdog", "unifi_links", "fleet_sweep",
    "telegram_bridge_healthcheck", "notes_backup",
    "signal_scan", "signal_scan_eval", "home_digest",
]

cfg_jobs = freshness.json.loads(freshness._CONFIG.read_text())["jobs"]

for job in TARGET_JOBS:
    check("%s: configured in freshness.json" % job, job in cfg_jobs)

# --- every target evaluates against the real runs.db and is never MISSING
conn = obs_db.connect()
now = datetime.now(timezone.utc)
results = {r["job"]: r for r in freshness.evaluate(conn, now)}
for job in TARGET_JOBS:
    r = results.get(job)
    check("%s: evaluates (present in freshness.evaluate() output)" % job,
          r is not None)
    if r is not None:
        check("%s: not MISSING (job-name matches real runs.db rows)" % job,
              r["status"] != "MISSING")


# --- boundary checks: max_age survives the normal gap but catches a missed run
def status_at(job, now):
    """Status of `job` at `now`, or "ABSENT" if freshness doesn't evaluate it."""
    for r in freshness.evaluate(conn, now):
        if r["job"] == job:
            return r["status"]
    return "ABSENT"


def last_run(job):
    row = conn.execute(
        "SELECT started_at FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1",
        (job,)).fetchone()
    return freshness._parse_iso(row["started_at"])

# panel_health: 09:00-15:00 AZ only -> ~18h overnight gap by design.
lr = last_run("panel_health")
check("panel_health: NOT stale just before the next scheduled run (~18h gap)",
      status_at("panel_health", lr + timedelta(hours=17, minutes=59)) != "STALE")
check("panel_health: IS stale if a run is genuinely missed (well past 20h)",
      status_at("panel_health", lr + timedelta(hours=21)) == "STALE")

# {{REDACTED}}_brief is retired; fail if it returns without a freshness entry.
check("{{REDACTED}}_brief: retired — absent from freshness.json, and that is correct",
      "{{REDACTED}}_brief" not in cfg_jobs)


# --- _skipped documentation: every job it names must actually exist
def real_job_scripts():
    """Live job names from cron/schedules.toml."""
    import tomllib
    manifest = Path(__file__).resolve().parent.parent / "cron" / "schedules.toml"
    with open(manifest, "rb") as fh:
        data = tomllib.load(fh)
    return {(j.get("script") or "").replace(".sh", "")
            for j in data.get("job", []) if j.get("script")}


def job_names_in_key(key):
    """A _skipped key may name several jobs: 'a / b (career_* jobs)' -> [a, b]."""
    names = []
    for part in key.split("/"):
        part = re.sub(r"\(.*?\)", "", part).strip()
        if part:
            names.append(part)
    return names


full_cfg = json.loads(freshness._CONFIG.read_text())
skipped = full_cfg.get("_skipped", {})
check("_skipped: block exists as a sibling of 'jobs' (not nested inside it)",
      "_skipped" not in full_cfg.get("jobs", {}) and bool(skipped))

real_scripts = real_job_scripts()
for key in skipped:
    if key.startswith("_"):
        continue  # _comment: metadata about the block, not a job reference
    for name in job_names_in_key(key):
        check("_skipped: '%s' (from key %r) is a real live job" % (name, key),
              name in real_scripts)

# ---------------------------------------------------------------------------
# soft_failure() must track the current state, not a stale window.
# Synthetic latest-first patterns, independent of the real runs.db.
# ---------------------------------------------------------------------------
import sqlite3 as _sq  # noqa: E402


def _window(pattern, all_ok=True):
    """Build an in-memory runs table from a latest-first pattern (N=noisy)."""
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.execute("CREATE TABLE runs (job TEXT, started_at TEXT, ok INT, "
              "stderr_bytes INT, error_tail TEXT)")
    for i, ch in enumerate(pattern):
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                  ("j", "2026-08-%02d" % (30 - i),
                   1 if all_ok else (0 if i == 0 else 1),
                   57 if ch == "N" else 0, "some stderr line"))
    return c


for _pat, _desc, _want_report in [
    ("..NNNNNNNNNN", "repaired (latest 2 clean) goes silent",           False),
    (".NNNNNNNNNNN", "repaired (latest 1 clean) goes silent",           False),
    ("NNNNNNNNNNNN", "chronic (latest noisy, whole window) reports",    True),
    ("N...........", "a single blip does not report",                   False),
    ("NN..........", "below the persistence ratio does not report",     False),
    ("NNN",          "too little history does not report",              False),
]:
    _got = freshness.soft_failure(_window(_pat), "j") is not None
    check("soft_failure: %s [%s]" % (_desc, _pat), _got == _want_report)

check("soft_failure: a real failure in the window defers to FAILING",
      freshness.soft_failure(_window("NNNNNNNNNNNN", all_ok=False), "j") is None)

_detail = freshness.soft_failure(_window("NNNNNNNNNNNN"), "j") or ""
check("soft_failure: detail says the LAST run was noisy, not just 'recent' ones",
      "last run" in _detail)

conn.close()

if FAILS:
    print("\n%d check(s) FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nall checks passed")
