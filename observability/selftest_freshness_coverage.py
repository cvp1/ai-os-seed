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
   a genuinely missed run. panel_health has a non-trivial gap by design (only
   runs 09:00-15:00 AZ, an ~18h overnight gap) and is checked at both
   boundaries using the REAL runs.db (read-only; never writes). {{REDACTED}}_brief
   was a second such case until it was retired 2026-08-07.
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

{{REDACTED}}_JOBS = Path("~/.{{REDACTED}}/cron/jobs.json").expanduser()

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
# energy_advisor removed 2026-08-04 — the 06:30 daily push was retired, so a
# staleness monitor on it would alarm on the retirement, not on a fault.

cfg_jobs = freshness.json.loads(freshness._CONFIG.read_text())["jobs"]

for job in TARGET_JOBS:
    check("%s: configured in freshness.json" % job, job in cfg_jobs)

# --- every target evaluates against the REAL runs.db as a real status,
# never MISSING (all 17 have live run history — a MISSING result here means
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
    """Status of `job` at `now`, or "ABSENT" if freshness doesn't evaluate it.

    Returns a sentinel rather than raising: a bare next() here made ONE
    unconfigured job abort the entire selftest with StopIteration, so every
    check after it never ran and the suite reported nothing at all rather than
    one red line. Found 2026-08-11 — {{REDACTED}}_brief is in TARGET_JOBS but not
    in freshness.json, and it was hiding the rest of the file.
    """
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

# {{REDACTED}}_brief was RETIRED 2026-08-07 (cron commit 983b68d, "retire the
# weekday work brief"): manifest entry enabled=false, unit no longer rendered,
# last real run 2026-08-07. Its coverage entry and its two weekend-gap edge
# checks stayed behind and failed forever after — the retirement removed the
# job but not the things watching it. Asserting the retirement instead, so this
# turns red if the job comes back without its freshness entry.
check("{{REDACTED}}_brief: retired — absent from freshness.json, and that is correct",
      "{{REDACTED}}_brief" not in cfg_jobs)


# --- _skipped documentation: every job it names must actually exist. Pure
# documentation is invisible to evaluate(), so this is the ONLY thing that
# would ever catch an invented or stale job name in there.
def real_job_scripts():
    """Live job names, from cron/schedules.toml — the current source of truth.

    Was reading ~/.{{REDACTED}}/cron/jobs.json. {{REDACTED}} cron has been PAUSED since
    2026-07-26 (the estate runs on systemd user timers), so that file froze on
    the migration date: every job added afterwards read as "not a real live
    job". Measured 2026-08-11 — grok_tier_check and soak_check both failed this
    check while being scheduled and running normally, which is the checker
    lying about the estate rather than the estate being wrong.
    """
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
# soft_failure() must track the CURRENT state, not a stale window.
#
# Until 2026-08-11 it judged purely on "what share of the last SOFT_WINDOW runs
# wrote stderr", with no recency gate — so a repaired job kept reporting for a
# further 12 runs. On a nightly job that is twelve nights of telling the
# operator something is broken after it was fixed, and the note was quoted from
# the most recent NOISY run, which could be days old, printed beside the word
# "recent". Measured live: system_status was fixed on 08-09 and still read
# SOFTFAIL on 08-12.
#
# Synthetic patterns, latest-first, so this holds regardless of what the real
# runs.db happens to contain today.
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
