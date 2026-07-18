#!/usr/bin/env python3
"""Stdlib-only self-test for Story 007's freshness.json coverage additions.

Run: /usr/bin/python3 observability/selftest_freshness_coverage.py
     (from repo root)
Exits 0 on success, non-zero with the failing checks listed.

Covers the two things a coverage-only PR can silently get wrong:
1. Every target job is actually configured, under the EXACT --job name its
   own wrapper passes to log_run.py (a name mismatch makes the entry
   permanently MISSING — never wrong-but-visible, just silently useless).
2. The max_age chosen for each job's real schedule is neither too tight
   (false STALE during a normal scheduled gap) nor so loose it can't catch
   a genuinely missed run. Two jobs have non-trivial gaps by design —
   panel_health (only runs 09:00-15:00 AZ, an ~18h overnight gap) and
   cognizant_brief (weekdays only, a ~72h Fri-Mon gap) — and are checked at
   both boundaries using the REAL runs.db (read-only; never writes).
3. Every job NAMED in the `_skipped` documentation block actually exists —
   `_skipped` is pure documentation (evaluate() never reads it), so nothing
   else would ever catch a wrong or invented job name in there; a
   /review-story pass caught "career_check / career_pipeline" referencing
   two job names that don't exist (the real jobs are career_jobscan /
   career_content / career_audit) — this makes that class of mistake a red
   test instead of a silent doc rot.
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

HERMES_JOBS = Path("~/.hermes/cron/jobs.json").expanduser()

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


TARGET_JOBS = [
    "ranch_watch", "device_watch", "owner_alerts", "frontier_watch",
    "starlink_watch", "meshtastic_health", "panel_health", "rain_watch",
    "vivint_ha_watchdog", "unifi_links", "fleet_sweep",
    "telegram_bridge_healthcheck", "notes_backup", "cognizant_brief",
    "energy_advisor", "signal_scan", "signal_scan_eval", "home_digest",
]

cfg_jobs = freshness.json.loads(freshness._CONFIG.read_text())["jobs"]

for job in TARGET_JOBS:
    check("%s: configured in freshness.json" % job, job in cfg_jobs)

# --- every target evaluates against the REAL runs.db as a real status,
# never MISSING (all 18 have live run history — a MISSING result here means
# the freshness.json job name doesn't match the wrapper's real --job string)
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


# --- boundary checks: max_age must survive the job's OWN normal gap, but
# still catch a genuinely missed run. Uses real runs.db (read-only) + a
# synthetic `now` to probe both edges without waiting for the real clock.
def status_at(job, now):
    return next(r for r in freshness.evaluate(conn, now) if r["job"] == job)["status"]


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

# cognizant_brief: weekdays only -> ~72h Fri->Mon gap by design.
lr = last_run("cognizant_brief")
check("cognizant_brief: NOT stale across a normal weekend gap (~72h)",
      status_at("cognizant_brief", lr + timedelta(hours=71, minutes=59)) != "STALE")
check("cognizant_brief: IS stale if a run is genuinely missed (well past 4d)",
      status_at("cognizant_brief", lr + timedelta(days=5)) == "STALE")


# --- _skipped documentation: every job it names must actually exist. Pure
# documentation is invisible to evaluate(), so this is the ONLY thing that
# would ever catch an invented or stale job name in there.
def real_job_scripts():
    jobs = json.loads(HERMES_JOBS.read_text())
    jobs = jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs
    return {(j.get("script") or "").replace(".sh", "")
            for j in jobs if j.get("script")}


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

conn.close()

if FAILS:
    print("\n%d check(s) FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nall checks passed")
