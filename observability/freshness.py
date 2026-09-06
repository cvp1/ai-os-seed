#!/usr/bin/env python3
"""Freshness / liveness check over the observability store.

Reads expected cadence from freshness.json and, for each scheduled job, reports:
  OK       newest run is recent and succeeded
  STALE    newest run is older than max_age (job has gone silent)
  MISSING  job is configured but has never logged a run
  FAILING  newest run is recent but exited non-zero
  SOFTFAIL every recent run "succeeded" but most wrote to stderr — the job is
           swallowing its own errors and exiting 0 (see soft_failure below)

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
# cron/ is CC's own {{REDACTED}}-shim sync script; scheduler/ is the plain
# crontab/launchd equivalent shipped by cc-seed (SEED-014) — checked in that
# order so this file's behavior here is byte-for-byte unchanged (cron/
# exists on this host and matches first), while the same file, unmodified,
# also works when genericized into the seed.
_SYNC_CANDIDATES = [_HERE.parent / "cron" / "sync.sh", _HERE.parent / "scheduler" / "sync.sh"]
_DUR = re.compile(r"^\s*(\d+)\s*([dhm])\s*$")

# --write-findings (SEED-074) — off by default, so this host's own scheduled
# invocation and the /status import path are byte-for-byte unchanged.
# data/ already holds runs.db and is already a declared runtime-writable path
# in the seed's install audit, so writing here adds no new write-path class
# for that checker to learn.
_FINDINGS = _HERE / "data" / "FINDINGS.md"
# Bound the COMPOSED file, not a section of it: a host with 80 failing jobs
# must not produce an unbounded artifact for an agent to read at session start.
_FINDINGS_MAX_LINES = 60
_FINDINGS_MAX_BYTES = 16000


def write_findings(lines, now):
    """SEED-074: leave the finding somewhere a human's agent will see it.

    A scheduled checker that only prints to stdout reaches a mail spool
    nobody reads. This writes one derived artifact the agent is told (in
    CLAUDE.md) to surface at session start — and DELETES it when everything
    is clean, so a repaired problem cannot linger as a false alarm. Both
    directions matter: write-on-problem alone would be half the mechanism.

    Returns the path if a file was written, None if it was removed/absent.
    Never raises: a findings-file failure must not fail the freshness run
    itself, which has already done its real work by this point.
    """
    try:
        if not lines:
            _FINDINGS.unlink(missing_ok=True)
            return None
        _FINDINGS.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        # generated-at goes FIRST and machine-readably: a reader that finds
        # this file stale learns the checker itself stopped, which a
        # cron-scheduled monitor can never report about its own death.
        head = [f"generated-at: {stamp}", "",
                f"# Findings — {len(lines)} item(s)", ""]
        body, dropped = list(lines), 0
        if len(body) > _FINDINGS_MAX_LINES:
            dropped = len(body) - _FINDINGS_MAX_LINES
            body = body[:_FINDINGS_MAX_LINES]
        out = "\n".join(head + [f"- {ln}" for ln in body])
        if dropped:
            out += f"\n- …and {dropped} more finding(s) truncated"
        out += "\n"
        # Measure the fully composed artifact in the consumer's units, not a
        # per-section estimate, and trim again if the byte cap still bites.
        if len(out.encode("utf-8")) > _FINDINGS_MAX_BYTES:
            keep, acc = [], len("\n".join(head).encode("utf-8"))
            for ln in body:
                enc = len(f"- {ln}\n".encode("utf-8"))
                if acc + enc > _FINDINGS_MAX_BYTES - 200:
                    break
                keep.append(ln)
                acc += enc
            dropped = len(lines) - len(keep)
            out = "\n".join(head + [f"- {ln}" for ln in keep])
            out += f"\n- …and {dropped} more finding(s) truncated\n"
        tmp = _FINDINGS.with_suffix(".tmp")
        tmp.write_text(out, encoding="utf-8")
        tmp.replace(_FINDINGS)
        return _FINDINGS
    except Exception as e:  # noqa: BLE001 — never fail the run over the sidecar
        print(f"[WARN   ] could not write findings file: {e}", file=sys.stderr)
        return None


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


def model_drift_problems():
    """Craig's call 2026-08-13: check the frontier DAILY, and deliver it here.

    Why inside this job rather than a timer of its own: no scheduler in this
    fleet notifies anyone, so a standalone daily `frontier_drift.py` would print
    a perfect report to a journal nobody reads — the same last-hop gap that
    FINDINGS.md was created to close. This job already writes FINDINGS.md, and
    CLAUDE.md already tells every agent to read it at session start. That is a
    delivery path with a proven consumer; a new timer would not be.

    Never crashes the run (the one-bad-entry rule: a malformed key once took the
    staleness monitor out for all 82 jobs). A provider being unreachable is a
    FINDING, not an exception — silence about an unchecked model is the failure
    this exists to prevent.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _lib import model_catalog
        # Standing structural gaps (claude/local have no catalog API here) are
        # excluded: they are true every single day, and a daily FINDINGS.md
        # entry that never changes is how the whole file gets skimmed past.
        # They live in the always-printed `python3 -m _lib.model_catalog` view.
        return ["%s: %s" % (cls, text.strip())
                for cls, text in model_catalog.findings()
                if cls != "NO-INSTRUMENT"]
    except Exception as e:  # noqa: BLE001
        return ["frontier drift check failed to run: %s" % e]


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


def prices_projection_drift():
    """Run `gen_prices.py --check` so a hand-edit to prices.json — or a PRICING
    change that was never projected — is edge-triggered here rather than found
    the next time someone happens to read the file.

    WHY: until 2026-08-12 prices.json and _lib/model_router.py PRICING were two
    hand-maintained Claude rate tables. Both carried an effective-2026-09-01 row
    raising claude-sonnet-5 to $3/$15 that Anthropic had cancelled; the fix
    removed it from _lib only and the copy here survived, found by accident. The
    projection made prices.json derived — this makes the derivation checked.
    Never crashes the job."""
    gen = _HERE / "gen_prices.py"
    if not gen.exists():
        return [f"gen_prices.py missing at {gen}"]
    try:
        r = subprocess.run([sys.executable, str(gen), "--check"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — never let the backstop crash the job
        return [f"gen_prices.py --check failed to run: {e}"]
    if r.returncode == 0:
        return []
    lines = [ln.strip() for ln in r.stderr.splitlines() if ln.startswith("DRIFT:")]
    return lines or [f"gen_prices.py --check exit {r.returncode}: "
                     f"{(r.stderr or r.stdout).strip()[:120]}"]


def key_registry_problems():
    """Run `keyvault/keys.py --check` so vault<->ROTATION.md coverage drift, a
    broken sidecar, or a passed rotate_by reaches FINDINGS.md.

    WHY: the weekly rotation_coverage job found 11 -> 27 -> 34 -> 40 -> 47
    uncovered vault files across five Mondays (runs.db, 2026-08..09), exit 0
    and ok=1 every time, and nobody saw it -- a checker that only prints to a
    journal is the last-hop gap this file exists to close (same reasoning as
    model_drift_problems). Coverage gaps are folded into ONE line with the
    count, because a per-file line that repeats daily is how the whole file
    gets skimmed past; the other finding classes are rare and stay itemised.
    Never crashes the run; a locked vault is a skip, not a finding."""
    keys = _HERE.parent / "keyvault" / "keys.py"
    if not keys.exists():
        return [f"keys.py missing at {keys}"]
    try:
        r = subprocess.run([sys.executable, str(keys), "--check"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 -- never let the backstop crash the job
        return [f"keys.py --check failed to run: {e}"]
    if r.returncode != 0:
        if "locked" in (r.stderr + r.stdout):
            return []
        return [f"keys.py --check exit {r.returncode}: {(r.stderr or r.stdout).strip()[:120]}"]
    items = [ln.strip()[2:] for ln in r.stdout.splitlines() if ln.strip().startswith("- ")]
    uncovered = [i for i in items if i.startswith("no ROTATION.md row: ")]
    other = [i for i in items if not i.startswith("no ROTATION.md row: ")]
    out = []
    if uncovered:
        out.append(f"{len(uncovered)} vault file(s) without a ROTATION.md row "
                   f"(keyvault/keys.py --check lists them; add a row per keyvault/ROTATION.md new-key checklist)")
    return out + other


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


# --- soft-failure detection (cron/AUDIT-2026-07-26.md) ------------------------
# A job can fail without ever failing: catch its own exception, write the reason
# to stderr, exit 0. Every signal this file watches stays green — it ran, it
# ran recently, it exited 0 — while the job does nothing. rain_watch did that
# for 26 days through monsoon season.
#
# The discriminator is PERSISTENCE, not presence: plenty of healthy jobs emit an
# occasional traceback or warning on stderr and recover. A job writing to stderr
# on most of its recent runs, every one of them "successful", is the soft-
# failure shape. Jobs that legitimately narrate to stderr on every good run
# declare "stderr_ok": true in freshness.json.
SOFT_WINDOW = 12       # most recent runs considered
SOFT_MIN_RUNS = 4      # don't judge a job with less history than this
SOFT_RATIO = 0.75      # this share of them writing stderr = persistent


def soft_failure(conn, job):
    """Detail string if `job` is soft-failing RIGHT NOW, else None. Never raises.

    Two conditions, and both are required:

      1. **The most recent run wrote stderr.** This is the state gate. Without
         it the check reported a repaired job as broken for a further
         SOFT_WINDOW runs — a nightly job stayed red for twelve nights after
         the fix landed. That is a false reading, not a slow one: the operator
         is told a thing is wrong when it is already right, and the only way to
         find out is to go read the code. Principle 7 — the alert follows the
         current state, and goes quiet the moment the condition clears.
      2. **It is persistent, not a blip** — SOFT_RATIO of the window is noisy.
         The window survives as CONTEXT for how chronic this is, which was the
         original point; it just no longer decides on its own.

    So a repaired job clears on its very next clean run, and a genuinely
    chronic one still reports every run. The note is taken from `rows[0]` —
    the latest run — never from an older noisy one, because quoting a
    days-old stderr line beside the word "recent" is the same false reading in
    miniature.
    """
    try:
        rows = conn.execute(
            "SELECT ok, stderr_bytes, error_tail FROM runs WHERE job=? "
            "ORDER BY started_at DESC LIMIT ?", (job, SOFT_WINDOW)).fetchall()
    except Exception:  # noqa: BLE001 — a backstop must not crash the job
        return None
    if len(rows) < SOFT_MIN_RUNS:
        return None
    if any(not r["ok"] for r in rows):
        return None        # a real failure in the window — FAILING already covers it
    if (rows[0]["stderr_bytes"] or 0) <= 0:
        return None        # condition 1: latest run is clean -> not failing now
    noisy = [r for r in rows if (r["stderr_bytes"] or 0) > 0]
    if len(noisy) / len(rows) < SOFT_RATIO:
        return None
    tail = (rows[0]["error_tail"] or "").strip().splitlines()
    note = tail[-1][:120] if tail else f"{rows[0]['stderr_bytes']} bytes, text not captured"
    return (f"exit 0 but wrote stderr on its last run and {len(noisy)}/{len(rows)} "
            f"recent ones: {note}")


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
            # Age belongs IN the detail here, not just the dict: FINDINGS.md
            # renders detail only, and a snapshot row outlives the run it
            # describes. An undated "last run failed" read 11h later cannot be
            # told apart from one read 5 days later — measured 2026-08-26, when
            # this row reported Monday's failure and the job had since gone
            # green 35 min after the file was written.
            results.append({"job": job, "label": label, "status": "FAILING",
                            "detail": f"last run failed {_fmt_age(age)} ago: {note[:120]}",
                            "age": _fmt_age(age)})
        else:
            soft = None if spec.get("stderr_ok") else soft_failure(conn, job)
            results.append({"job": job, "label": label,
                            "status": "SOFTFAIL" if soft else "OK",
                            "detail": soft or f"last run {_fmt_age(age)} ago",
                            "age": _fmt_age(age)})
    return results


def main():
    ap = argparse.ArgumentParser(description="Freshness/liveness check over the run log.")
    ap.add_argument("--all", action="store_true", help="also print healthy/never-run jobs")
    ap.add_argument("--strict", action="store_true",
                    help="treat MISSING (never run) as a paging problem too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--write-findings", action="store_true",
                    help="also write/remove data/FINDINGS.md so an agent finds "
                         "it at session start (SEED-074; off by default)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        results = evaluate(conn, now)
    drift = shim_drift()  # Story 025: shim set/content drift is a paging problem too
    repo = repo_hygiene_problems()  # Story 008: dirty/unpushed/untracked-exec drift
    prices = prices_projection_drift()  # prices.json must stay a projection of PRICING
    models = model_drift_problems()  # frontier pins vs what the providers actually serve
    keys = key_registry_problems()  # vault<->ROTATION.md coverage, sidecars, rotate_by

    # STALE (went silent) and FAILING (crashed) are high-confidence — they page.
    # MISSING (never run) is weaker: usually a newly-instrumented job that hasn't
    # hit its next schedule yet, so it's informational unless --strict is given.
    paging = ({"STALE", "FAILING", "SOFTFAIL", "MISSING"} if args.strict
              else {"STALE", "FAILING", "SOFTFAIL"})
    problems = [r for r in results if r["status"] in paging]

    # Compose the findings BEFORE any early return, so --write-findings is
    # honoured on the clean path (where its job is to DELETE a stale file)
    # and under --json, not only on the text-with-problems path.
    if args.write_findings:
        findings = [f"[{r['status']}] {r['label']}: {r['detail']}" for r in
                    sorted(problems, key=lambda x: x["job"])]
        findings += [f"[DRIFT] cron shim reconcile: {d}" for d in drift]
        findings += [f"[REPO] git hygiene: {rp}" for rp in repo]
        findings += [f"[DRIFT] price table projection: {p}" for p in prices]
        findings += [f"[MODEL] frontier drift: {m}" for m in models]
        findings += [f"[KEYS] key registry: {k}" for k in keys]
        write_findings(findings, now)

    if args.json:
        print(json.dumps({"checked_at": now.isoformat(timespec="seconds"),
                          "problems": len(problems), "results": results,
                          "shim_drift": drift, "repo_hygiene": repo,
                          "prices_drift": prices, "model_drift": models,
                          "key_registry": keys},
                         indent=2))
        return 0

    shown = results if args.all else problems
    if not shown and not drift and not repo and not prices and not models and not keys:
        # Silent success: nothing printed, nothing found.
        return 0
    if problems or drift or repo or prices or models or keys:
        # Found work is success (Story 008): report with a FINDINGS: first line
        # (log_run stores it as the run's summary) and exit 0 below.
        print(f"FINDINGS: {len(problems)} job problem(s), "
              f"{len(drift)} shim drift, {len(repo)} repo hygiene, "
              f"{len(prices)} price drift, {len(models)} model drift, {len(keys)} key registry")
    for r in sorted(shown, key=lambda x: (x["status"] == "OK", x["job"])):
        print(f"[{r['status']:7}] {r['label']}: {r['detail']}")
    for d in drift:
        print(f"[{'DRIFT':7}] cron shim reconcile: {d}")
    for rp in repo:
        print(f"[{'REPO':7}] git hygiene: {rp}")
    for p in prices:
        print(f"[{'DRIFT':7}] price table projection: {p}")
    for m in models:
        print(f"[{'MODEL':7}] frontier drift: {m}")
    for k in keys:
        print(f"[{'KEYS':7}] key registry: {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
