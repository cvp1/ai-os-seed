---
name: status
description: Give an honest, one-screen answer to "how is my system doing"
  over everything this seed schedules and watches. Use when the operator
  says "/status", "how's everything looking", "is anything broken", "check
  my jobs", or asks about a specific job's health.
---

# /status — the operational record reaches the operator

The one-verb front door. The run log and freshness backstop are only
useful if answering "how is my system doing" doesn't require knowing which
script to run with which flags. This skill is read-only: it reports, it
never restarts, edits, or heals anything — propose fixes, don't apply them,
the operator's own automation posture is theirs to set.

## Procedure

1. Run `observability/freshness.py --all --json` and
   `observability/report.py --stats --json`.
2. Also check scheduler drift: `scheduler/sync.sh --check` (the manifest
   and what's actually installed can diverge silently otherwise).
3. Render **one compact view**:
   - Per job: status (OK / STALE / FAILING / MISSING), age of last run.
   - Recent failures, each with its error tail (the last line of stderr,
     not a truncated summary that hides what actually broke).
   - Scheduler drift, if `sync.sh --check` reports any.
4. **Check memory notes.** If any note in `memory/` references a job that
   is currently STALE or FAILING (grep note bodies for the job name),
   surface it alongside that job's line — a past incident note next to a
   live failure is often the fastest path to the fix, and it's the first
   place this system's memory and its operational floor visibly meet.

## Doctrine (bake this into every answer, not just this skill's prose)

- **Distrust green.** If the operator says something is broken and this
  view says OK, believe the operator and dig further — check the actual
  job output, not just the freshness classification. A passing check is a
  claim, not proof.
- **Never summarize a FAILING away.** Show the real error tail. "hello_fleet
  is having some issues" is not an answer; the actual exception text is.
- **A healthy system produces a short answer.** If everything is OK, say
  so in one or two lines and stop — don't pad a clean report to look more
  thorough than it needs to be.

## Example (all healthy)

> All 2 scheduled jobs OK. `hello_fleet` last ran 4m ago (ok). No
> scheduler drift. No memory notes reference any currently-failing job
> (there are none).

## Example (one problem)

> `hello_fleet`: **STALE** — last ran 3h ago, expected every 20m.
> Error tail from its last run: none (it just stopped running — check
> `scheduler/sync.sh --check` and your crontab/launchd for the entry).
> Memory note `memory/hello-fleet-flaky-network.md` mentions this job
> going quiet before under a network drop — may be the same cause.
