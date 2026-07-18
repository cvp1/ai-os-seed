-- Observability: one row per scheduled/cron run across the CC monorepo.
-- Written by log_run.py (which wraps each cron job), queried by report.py,
-- freshness.py, and the status-site dashboard.
--
-- Rollback-journal (DELETE) mode, NOT WAL: the status-site container mounts this
-- DB read-only, and a read-only consumer of a WAL database needs to write the
-- -wal/-shm side-files (impossible on a :ro mount). DELETE keeps the main file
-- always-current and readable stand-alone. Writes here are tiny and infrequent
-- (push_solar every 5 min, rest daily/weekly), so losing WAL concurrency costs
-- nothing; the 30s busy_timeout in db.py covers the rare overlap.
PRAGMA journal_mode = DELETE;
PRAGMA synchronous  = FULL;

CREATE TABLE IF NOT EXISTS runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job          TEXT    NOT NULL,        -- logical job name, e.g. "morning_brief"
  host         TEXT,                     -- hostname the job ran on
  started_at   TEXT    NOT NULL,         -- ISO-8601 UTC
  finished_at  TEXT,                     -- ISO-8601 UTC
  duration_ms  INTEGER,                  -- wall-clock milliseconds
  exit_code    INTEGER,                  -- child process exit status
  ok           INTEGER,                  -- 1 if exit_code == 0 else 0
  stdout_bytes INTEGER,                  -- size of child stdout
  stderr_bytes INTEGER,                  -- size of child stderr
  summary      TEXT,                     -- first non-empty stdout line (truncated)
  error_tail   TEXT,                     -- tail of stderr when the run failed (truncated)
  -- Cost tracking. Populated when a job emits usage to $CC_OBS_TOKENS_FILE
  -- (see log_run.py / README "Cost tracking"). NULL for jobs that spend no tokens.
  tokens_in    INTEGER,                  -- summed input tokens across the run's LLM calls (incl. cache)
  tokens_out   INTEGER,                  -- summed output tokens
  cost_usd     REAL,                     -- summed USD cost
  -- Of tokens_in, how many were cache READS (already-cached prompt prefix billed
  -- at ~1/10 the input rate). cache_read/tokens_in is the cache-hit ratio — a
  -- falling ratio flags a prompt-caching regression. NULL for pre-column rows.
  cache_read_tokens INTEGER,
  -- Of tokens_in, how many were cache WRITES (creation, billed ~1.25x input).
  -- With cache_read + cache_creation, tokens_in fully decomposes into
  -- input/cache-write/cache-read — enough to RE-PRICE a run under any price table.
  cache_creation_tokens INTEGER,
  -- Model id (e.g. claude-sonnet-4-6) the run's LLM calls used, or "mixed".
  -- Lets pricing.py re-derive cost per model when rates change. NULL = unknown.
  model TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job_started ON runs(job, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_started     ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_ok          ON runs(ok);

-- Per-call audit for the credential-shielding egress proxy (egress-proxy/). One
-- row per proxied request so egress is observable alongside job runs. Metadata
-- only — never the request path/query, never an injected credential. Written
-- best-effort by egress-proxy/proxy.py (a locked/busy DB logs nothing).
CREATE TABLE IF NOT EXISTS egress (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL,   -- ISO-8601 UTC
  route         TEXT,               -- route key (or the unknown key on a deny)
  method        TEXT,
  status        INTEGER,            -- HTTP status returned to the client
  upstream_host TEXT,               -- host only; NEVER path/query, NEVER a secret
  duration_ms   INTEGER,
  decision      TEXT                -- forward | deny_route | deny_method | error
);
CREATE INDEX IF NOT EXISTS idx_egress_ts    ON egress(ts);
CREATE INDEX IF NOT EXISTS idx_egress_route ON egress(route);

-- Per-worker outcome for a parallel-omnigent fleet sweep (egress-proxy/fleet.py).
-- One row per worker per sweep: the coordinator fans N least-privilege jails out
-- concurrently and records each one's result here. Metadata only — never the
-- worker's output, never a secret. Written best-effort (a locked/busy DB logs
-- nothing). Workers of one sweep share a run_id.
CREATE TABLE IF NOT EXISTS fleet (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,   -- ISO-8601 UTC, sweep start
  run_id      TEXT    NOT NULL,   -- one id per sweep, shared by its workers
  worker      TEXT,               -- worker name from the manifest
  scope       TEXT,               -- least-privilege scope it ran under
  rc          INTEGER,            -- worker exit code
  duration_ms INTEGER,
  status      TEXT                -- ok | fail | timeout
);
CREATE INDEX IF NOT EXISTS idx_fleet_run ON fleet(run_id);
CREATE INDEX IF NOT EXISTS idx_fleet_ts  ON fleet(ts);
