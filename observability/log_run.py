#!/usr/bin/env python3
"""Run a command, record one observability row, and pass its stdout/exit through.

Usage:
    log_run.py --job morning_brief -- /usr/bin/python3 /path/to/morning_brief.py [args...]

Behaviour contract (so it can sit transparently between hermes cron and the real
script):
  * The wrapped command's stdout is streamed to OUR stdout unchanged — hermes
    decides delivery from stdout, so this preserves the "ping only on non-empty
    stdout" semantics of the failure-only jobs.
  * The wrapped command's stderr is streamed to OUR stderr unchanged.
  * We exit with the wrapped command's exit code.
  * Exactly one row is written to the runs table, even if the child crashes or
    is killed by a signal. Observability never changes the job's outcome: if
    logging itself fails, we warn on stderr and still return the child's code.

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

import db
import switches

SUMMARY_MAX = 500      # chars stored for the first stdout line
ERRTAIL_MAX = 2000     # chars stored for the stderr tail on failure

# A wrapped job can report token usage by appending one JSON object per line to
# the file named in $CC_OBS_TOKENS_FILE, with any of: tokens_in, tokens_out,
# cache_read, cost_usd. log_run sums them into the run's row. Jobs that spend no
# tokens just ignore the env var (the columns stay NULL). See README "Cost tracking".
TOKENS_ENV = "CC_OBS_TOKENS_FILE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _pump(src, dst, sink: list):
    """Forward bytes from src to dst (live) while accumulating them for the row."""
    for chunk in iter(lambda: src.readline(), b""):
        dst.buffer.write(chunk)
        dst.buffer.flush()
        sink.append(chunk)


def main() -> int:
    ap = argparse.ArgumentParser(description="Wrap a command with observability logging.")
    ap.add_argument("--job", required=True, help="logical job name, e.g. morning_brief")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- followed by the command to run")
    args = ap.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("log_run.py: no command given after --", file=sys.stderr)
        return 2

    # Soft-disabled via the status-site control panel: skip instantly. We don't
    # run the child, don't write a run row (keeps the log clean), and emit nothing
    # on stdout with exit 0 (so hermes sends no ping). freshness.py likewise
    # ignores disabled jobs, so this won't page as STALE.
    if switches.is_disabled(args.job):
        return 0

    started = _utc_now()
    out_chunks: list = []
    err_chunks: list = []
    exit_code = None

    # Hand the child a fresh file to append token usage to (it may ignore it).
    tokens_fd, tokens_path = tempfile.mkstemp(prefix="cc_obs_tok_")
    os.close(tokens_fd)
    child_env = {**os.environ, TOKENS_ENV: tokens_path}

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=child_env)
    except OSError as e:
        # Could not even launch the child — record a synthetic failed run.
        finished = _utc_now()
        _record(args.job, started, finished, 127, b"", f"exec failed: {e}".encode(), {})
        _cleanup(tokens_path)
        print(f"log_run.py: failed to exec {cmd!r}: {e}", file=sys.stderr)
        return 127

    t_out = threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, out_chunks))
    t_err = threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, err_chunks))
    t_out.start(); t_err.start()
    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        exit_code = proc.wait()
    t_out.join(); t_err.join()

    finished = _utc_now()
    stdout_b = b"".join(out_chunks)
    stderr_b = b"".join(err_chunks)
    usage = _read_usage(tokens_path)
    _cleanup(tokens_path)
    _record(args.job, started, finished, exit_code, stdout_b, stderr_b, usage)
    return exit_code if exit_code is not None else 1


def _read_usage(path):
    """Sum the JSONL usage records the child may have appended. Never raises."""
    totals = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0,
              "cache_creation": 0, "cost_usd": 0.0}
    models = set()
    seen = False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                seen = True
                totals["tokens_in"] += int(rec.get("tokens_in", 0) or 0)
                totals["tokens_out"] += int(rec.get("tokens_out", 0) or 0)
                totals["cache_read"] += int(rec.get("cache_read", 0) or 0)
                totals["cache_creation"] += int(rec.get("cache_creation", 0) or 0)
                totals["cost_usd"] += float(rec.get("cost_usd", 0) or 0)
                if rec.get("model"):
                    models.add(rec["model"])
    except OSError:
        return {}
    if not seen:
        return {}
    # One model → store it; several in one run → "mixed"; none reported → NULL.
    totals["model"] = models.pop() if len(models) == 1 else ("mixed" if models else None)
    return totals


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _record(job, started, finished, exit_code, stdout_b, stderr_b, usage):
    """Insert one row; never raise into the caller — logging must not break jobs."""
    try:
        stdout_s = stdout_b.decode("utf-8", "replace")
        stderr_s = stderr_b.decode("utf-8", "replace")
        summary = ""
        for line in stdout_s.splitlines():
            if line.strip():
                summary = line.strip()[:SUMMARY_MAX]
                break
        ok = 1 if exit_code == 0 else 0
        error_tail = "" if ok else stderr_s.strip()[-ERRTAIL_MAX:]
        duration_ms = int((finished - started).total_seconds() * 1000)
        tok_in = usage.get("tokens_in") if usage else None
        tok_out = usage.get("tokens_out") if usage else None
        cache_read = usage.get("cache_read") if usage else None
        cache_creation = usage.get("cache_creation") if usage else None
        model = usage.get("model") if usage else None
        cost = round(usage["cost_usd"], 6) if usage else None
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO runs
                   (job, host, started_at, finished_at, duration_ms, exit_code,
                    ok, stdout_bytes, stderr_bytes, summary, error_tail,
                    tokens_in, tokens_out, cost_usd, cache_read_tokens,
                    cache_creation_tokens, model)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job, socket.gethostname(), _iso(started), _iso(finished),
                 duration_ms, exit_code, ok, len(stdout_b), len(stderr_b),
                 summary, error_tail, tok_in, tok_out, cost, cache_read,
                 cache_creation, model),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 - observability must never crash the job
        print(f"log_run.py: WARNING failed to record run for {job!r}: {e}",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
