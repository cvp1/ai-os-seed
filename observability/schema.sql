-- Observability store: one row per scheduled run, written by log_run.py and
-- read by report.py and freshness.py.
--
-- DELETE journal mode, not WAL: read-only consumers of a WAL DB must write
-- side-files, which a read-only mount forbids.
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
  -- Token/cost usage, populated from $CC_OBS_TOKENS_FILE; NULL if unused.
  tokens_in    INTEGER,                  -- summed input tokens across the run's LLM calls (incl. cache)
  tokens_out   INTEGER,                  -- summed output tokens
  cost_usd     REAL,                     -- summed USD cost
  -- Of tokens_in, how many were cache reads (cache_read/tokens_in = hit ratio).
  cache_read_tokens INTEGER,
  -- Of tokens_in, how many were cache writes; lets a run be re-priced later.
  cache_creation_tokens INTEGER,
  -- Model id the run's LLM calls used, "mixed", or NULL if unknown.
  model TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job_started ON runs(job, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_started     ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_ok          ON runs(ok);

-- One row per request through the egress proxy. Metadata only: never the
-- path/query or a credential. Written best-effort.
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

-- One row per worker per parallel sweep; workers of one sweep share a run_id.
-- Metadata only: never worker output or a secret. Written best-effort.
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
