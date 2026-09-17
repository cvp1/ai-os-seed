#!/usr/bin/env python3
"""Run a command, record one observability row, and pass its stdout/exit through.

Usage:
    log_run.py --job morning_brief -- /usr/bin/python3 /path/to/morning_brief.py [args...]

Behaviour contract (so it can sit transparently between {{REDACTED}} cron and the real
script):
  * The wrapped command's stdout is streamed to OUR stdout unchanged — {{REDACTED}}
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

# The ledger's `host` column is the FLEET SLUG, never the raw hostname: on
# {{REDACTED}} gethostname() is `iMac`, and until 2026-09-06 that split every
# per-host query (94,271 `iMac` rows vs 44 `{{REDACTED}}` over 30d). Resolved
# through _lib.fleet_host; if _lib is missing or broken we fall back to the raw
# hostname rather than break the estate — this wrapper runs EVERY scheduled job.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from _lib import fleet_host as _fleet_host  # noqa: E402
except Exception:  # noqa: BLE001
    _fleet_host = None


def _host() -> str:
    try:
        if _fleet_host is not None:
            return _fleet_host.slug()
    except Exception:  # noqa: BLE001
        pass
    return socket.gethostname()


SUMMARY_MAX = 500      # chars stored for the first stdout line
ERRTAIL_MAX = 2000     # chars stored for the stderr tail (any run that wrote one)

# A wrapped job can report token usage by appending one JSON object per line to
# the file named in $CC_OBS_TOKENS_FILE, with any of: tokens_in, tokens_out,
# cache_read, cost_usd. log_run sums them into the run's row. Jobs that spend no
# tokens just ignore the env var (the columns stay NULL). See README "Cost tracking".
TOKENS_ENV = "CC_OBS_TOKENS_FILE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


SINK_HEAD_MAX = 1 << 20     # bytes of a stream kept from the start
SINK_TAIL_MAX = 64 << 10    # bytes kept from the end (error_tail lives here)


class _Sink:
    """Bounded accumulator for one stream (Principle 8, bug-bash 2026-09-08 A18).

    Forwarding to the terminal stays live and unbounded; what is KEPT for the
    runs.db row is the first SINK_HEAD_MAX bytes plus the last SINK_TAIL_MAX,
    with the true total in `.total`. Before this every byte of a chatty job
    was held in memory twice (list + join) until the row was written."""

    def __init__(self):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes):
        self.total += len(chunk)
        room = SINK_HEAD_MAX - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > SINK_TAIL_MAX:
                del self.tail[:len(self.tail) - SINK_TAIL_MAX]

    def bytes(self) -> bytes:
        if not self.tail:
            return bytes(self.head)
        elided = self.total - len(self.head) - len(self.tail)
        marker = (b"\n... [%d bytes elided by log_run] ...\n" % elided) if elided > 0 else b""
        return bytes(self.head) + marker + bytes(self.tail)


def _pump(src, dst, sink):
    """Forward bytes from src to dst (live) while accumulating a bounded copy."""
    for chunk in iter(lambda: src.readline(), b""):
        dst.buffer.write(chunk)
        dst.buffer.flush()
        sink.add(chunk)


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
    # on stdout with exit 0 (so {{REDACTED}} sends no ping). freshness.py likewise
    # ignores disabled jobs, so this won't page as STALE.
    if switches.is_disabled(args.job):
        return 0

    started = _utc_now()
    out_chunks = _Sink()
    err_chunks = _Sink()
    exit_code = None

    # Hand the child a fresh file to append token usage to (it may ignore it).
    tokens_fd, tokens_path = tempfile.mkstemp(prefix="cc_obs_tok_")
    os.close(tokens_fd)
    # CC_SCHEDULED_JOB is the positive "a supervised scheduled job" signal
    # that _lib/mail.py and ontology/_sender require alongside systemd's
    # INVOCATION_ID (2026-09-09: INVOCATION_ID alone let Corral panes -- an
    # inherited environment under a systemd unit -- pass as scheduled).
    child_env = {**os.environ, TOKENS_ENV: tokens_path, "CC_SCHEDULED_JOB": args.job}

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
    stdout_b = out_chunks.bytes()
    stderr_b = err_chunks.bytes()
    usage = _read_usage(tokens_path)
    _cleanup(tokens_path)
    _record(args.job, started, finished, exit_code, stdout_b, stderr_b, usage,
            stdout_total=out_chunks.total, stderr_total=err_chunks.total)
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


def _record(job, started, finished, exit_code, stdout_b, stderr_b, usage,
            stdout_total=None, stderr_total=None):
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
        # Keep the stderr tail whenever the child wrote one — NOT only on
        # failure. A job that catches its own exception, logs the reason to
        # stderr and exits 0 is the "soft failure" shape: healthy to every
        # consumer while doing nothing at all. rain_watch sat there for 26 days
        # (7,505 runs, all exit 0, ~260 bytes of stderr each) because this line
        # stored a byte count and threw the words away. freshness.py now reads
        # these tails to catch the shape; see cron/AUDIT-2026-07-26.md.
        error_tail = stderr_s.strip()[-ERRTAIL_MAX:]
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
                (job, _host(), _iso(started), _iso(finished),
                 duration_ms, exit_code, ok,
                 stdout_total if stdout_total is not None else len(stdout_b),
                 stderr_total if stderr_total is not None else len(stderr_b),
                 summary, error_tail, tok_in, tok_out, cost, cache_read,
                 cache_creation, model),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 - observability must never crash the job
        print(f"log_run.py: WARNING failed to record run for {job!r}: {e}",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
