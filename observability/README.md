# observability — run log for scheduled jobs

One SQLite row per scheduled run, so a cron/launchd job stops being a black
box: when did it last run, did it succeed, how long did it take, what did
it say. Ships with one worked example — `demo/hello_fleet.py` — so you can
see the whole spine before you write your first real job.

## How it works

Every job in `scheduler/manifest.yml` is wrapped through `log_run.py`:

```
/usr/bin/python3 .../observability/log_run.py \
  --job hello_fleet -- \
  /usr/bin/python3 .../demo/hello_fleet.py
```

`log_run.py` is transparent on purpose:

- the child's **stdout** is streamed through unchanged;
- the child's **stderr** is streamed through unchanged;
- it exits with the **child's exit code**;
- exactly one row is written to `runs`, even on crash. If logging itself
  fails it warns on stderr and still returns the child's code —
  observability never changes a job's outcome.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | the `runs` table (rollback-journal `DELETE` mode, not WAL — safe to read with a plain read-only file open; created on first use) |
| `db.py` | connection helper; DB path = `$CC_OBS_DB` (must be absolute) or the packaged `data/runs.db` |
| `prune.py` | retention: delete run rows older than 180d + VACUUM (`--apply`; dry-run by default) |
| `log_run.py` | the wrapper that runs a command and records the row |
| `report.py` | query/inspect the store |
| `freshness.py` | liveness check — flags jobs that went silent or last crashed; also runs the scheduler-drift (`scheduler/sync.sh --check`) + `repo_hygiene` guards |
| `repo_hygiene.py` | repo-hygiene guard: dirty/ahead-of-remote > 7d, missing remote, untracked scheduler exec targets — no fetch, exit 1 on drift |
| `freshness.json` | expected cadence (`max_age`) per scheduled job — ships pre-seeded with just `hello_fleet` |

The DB lives at `data/runs.db` (gitignored — it's data, not source).

## Querying

```sh
python3 report.py                  # last 20 runs, all jobs
python3 report.py --stats          # per-job: runs / fails / p50 / last run
python3 report.py --failures       # only failed runs
python3 report.py --job hello_fleet
python3 report.py --since 7d       # 7d / 24h / 90m windows
python3 report.py --json           # machine-readable
```

## Freshness / liveness check

`freshness.py` reads `freshness.json` and classifies each scheduled job:

- **OK** — newest run is recent and succeeded
- **STALE** — newest run is older than `max_age` (the job stopped running)
- **FAILING** — newest run is recent but exited non-zero
- **MISSING** — configured but never logged a run

It prints **only problems** and exits non-zero on trouble, so it works as
its own scheduled job — add it to `scheduler/manifest.yml` too, so a
scheduler that stops running entirely still gets caught (the one failure
mode per-job pings can't cover).

STALE and FAILING page by default. MISSING is informational (a
newly-instrumented job that hasn't hit its next schedule yet) — promote it
with `--strict`.

```sh
python3 freshness.py            # problems only (silent if all healthy)
python3 freshness.py --all      # show OK + MISSING too
python3 freshness.py --strict   # page on MISSING as well
python3 freshness.py --json
```

To watch a new job, add it to `freshness.json` with a `max_age` = its
interval plus slack. Only list jobs that are actually in
`scheduler/manifest.yml`.

## Cost tracking (optional, off by default)

If a job calls an LLM, `log_run.py` hands it a fresh file path in
**`$CC_OBS_TOKENS_FILE`**; the job can append **one JSON object per line**
with any of: `tokens_in`, `tokens_out`, `cache_read`, `cache_creation`,
`cost_usd`, `model`. `log_run.py` sums every line into the run's columns.
Jobs that spend nothing (like `hello_fleet`) just ignore the env var —
those columns stay NULL. This is the raw capture only; rollups, re-pricing,
and dashboards are not part of the seed — build your own over `report.py
--json` if you want them.

## Adding a new job

Wrap its command through `log_run.py` in a `scheduler/manifest.yml` entry
and pass a unique `--job` name. Nothing else to register — the row appears
on first run. If it's scheduled and you want liveness alerts, also add it
to `freshness.json`.

## Schema fields

`job, host, started_at, finished_at, duration_ms, exit_code, ok,
stdout_bytes, stderr_bytes, summary` (first stdout line), `error_tail`
(stderr tail on failure), `tokens_in, tokens_out, cache_read_tokens,
cache_creation_tokens, model, cost_usd` (NULL unless the job emits usage —
see Cost tracking). Times are ISO-8601 UTC. New nullable columns are added
via the `_ADDED_COLUMNS` migration in `db.py` (`CREATE TABLE IF NOT
EXISTS` never alters an existing table).
