#!/usr/bin/env python3
"""weekly — regenerate NOW.md, a deterministic weekly projection over this
install's own facts.

The last arrow of the loop: jobs write facts (runs.db, via log_run.py);
this reads them back and derives a view, no LLM involved (see
memory/THE-LOOP.md for how this fits with /status, /improve, /recall).
Modeled on CC's own weekly fleet-state projection, authored fresh here —
same shape, only the sources an installed system actually has:

  observability/report.py's store (runs.db)     per-job week-over-week
  observability/freshness.py                     current exceptions
  git log, this workspace                        recent activity

Every source degrades to a marked "unavailable" line rather than crashing
the whole page — one broken source shouldn't hide the rest. Silent on
success (edge-trigger); exit 1 only if writing NOW.md itself fails.

Stdlib only; targets /usr/bin/python3.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "observability"))
import db  # noqa: E402

NOW_MD = ROOT / "NOW.md"
WINDOW_DAYS = 7


def section(title, lines):
    return [f"## {title}", ""] + (lines or ["- _nothing to report_"]) + [""]


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="milliseconds")


def job_lines():
    """Per-job runs/failures this week vs the prior week, new-vs-resolved FAILING."""
    since, prev_since = _iso(WINDOW_DAYS), _iso(2 * WINDOW_DAYS)
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT job, ok, started_at FROM runs WHERE started_at >= ? "
                "ORDER BY started_at", (prev_since,)).fetchall()
    except Exception as e:  # noqa: BLE001 — a broken store degrades, doesn't crash the page
        return [f"- _runs.db unavailable ({e})_"]
    if not rows:
        return ["- _no runs recorded in the last 14 days_"]
    cur, prev = {}, {}
    for r in rows:
        bucket = cur if r["started_at"] >= since else prev
        d = bucket.setdefault(r["job"], {"runs": 0, "fails": 0})
        d["runs"] += 1
        if not r["ok"]:
            d["fails"] += 1
    lines = []
    for job in sorted(set(cur) | set(prev)):
        c = cur.get(job, {"runs": 0, "fails": 0})
        p = prev.get(job, {"runs": 0, "fails": 0})
        flag = ""
        if c["fails"] and not p["fails"]:
            flag = " — NEW FAILING this week"
        elif p["fails"] and not c["fails"]:
            flag = " — RESOLVED since last week"
        lines.append(f"- **{job}** — {c['runs']} run(s), {c['fails']} failure(s) this week "
                    f"(prior week: {p['runs']}/{p['fails']}){flag}")
    return lines


def _age_seconds(age):
    """Parse freshness.py's formatted age string (e.g. '45s', '20m', '3h',
    '2d') back to seconds, purely to rank STALE jobs oldest-first — the
    JSON contract doesn't carry raw seconds, only the formatted label."""
    if not age:
        return -1
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(age[-1])
    try:
        return int(age[:-1]) * unit if unit else -1
    except ValueError:
        return -1


def freshness_lines():
    """Current exceptions (STALE/FAILING/MISSING) via the same JSON contract
    the /status skill reads — one source of truth, two consumers."""
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "observability" / "freshness.py"), "--all", "--json"],
            capture_output=True, text=True, timeout=30)
        payload = json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        return [f"- _freshness check unavailable ({e})_"], None
    problems = [x for x in payload["results"] if x["status"] != "OK"]
    if not problems:
        return ["- _all scheduled jobs OK_"], None
    lines = [f"- **{p['status']}** {p['label']}: {p['detail']}" for p in problems]
    stale = [p for p in problems if p["status"] == "STALE"]
    oldest = max(stale, key=lambda p: _age_seconds(p.get("age"))) if stale else None
    return lines, (f"{oldest['label']} — {oldest['detail']}" if oldest else None)


def git_lines():
    """Recent activity in this workspace. A single install is usually one repo
    (ROOT itself); the growth path — several repos under one workspace root,
    same convention this seed's own source workspace uses — is handled too:
    if ROOT isn't a repo, scan its immediate subdirectories for ones that are."""
    candidates = [ROOT] if (ROOT / ".git").is_dir() else [
        p for p in sorted(ROOT.iterdir()) if (p / ".git").is_dir()
    ]
    if not candidates:
        return ["- _not a git repo, and no repos found under this workspace root_"]
    lines = []
    for repo in candidates:
        try:
            r = subprocess.run(
                ["git", "log", "--since", f"{WINDOW_DAYS} days ago", "--format=%s"],
                cwd=repo, capture_output=True, text=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            lines.append(f"- **{repo.name}** — git log unavailable ({e})")
            continue
        subjects = [s for s in r.stdout.splitlines() if s]
        if not subjects:
            continue
        lines.append(f"- **{repo.name}** — {len(subjects)} commit(s) this week; last: {subjects[0][:90]}")
    return lines or ["- _no commits in the last 7 days_"]


def build():
    now = datetime.now(timezone.utc)
    fresh_lines, oldest_stale = freshness_lines()
    if oldest_stale:
        fresh_lines = [f"- ⚠️ oldest STALE: {oldest_stale}"] + fresh_lines
    out = [f"# NOW — {now.strftime('%Y-%m-%d %H:%M')} UTC", "",
          "> Regenerated weekly by `views/weekly.py`. Deterministic projection "
          "over this install's own runs.db + git history — no LLM. See "
          "`memory/THE-LOOP.md` for how this fits the rest of the spine.", ""]
    out += section("Jobs — this week vs last", job_lines())
    out += section("Freshness exceptions", fresh_lines)
    out += section("Workspace activity", git_lines())
    return "\n".join(out).rstrip() + "\n"


def main():
    try:
        NOW_MD.write_text(build())
    except OSError as e:
        print(f"weekly.py: failed to write {NOW_MD}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
