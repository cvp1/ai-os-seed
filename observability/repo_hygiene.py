#!/usr/bin/env python3
"""Repo-hygiene guard (Story 008): keep the "everything committed + pushed" state
that Stories 005–007 established from silently decaying.

Sweeps every top-level git repo under ~/Github/CC and reports, edge-triggered:
  * missing remote            — a durability hole (flagged immediately; rare + bad)
  * ahead of upstream > N days — unpushed work, aged by the OLDEST unpushed commit's
                                 committer date (fresh work-in-flight stays quiet)
  * dirty tracked files > N days — uncommitted edits, aged by the NEWEST dirty file's
                                 mtime (actively-edited trees stay quiet)
Plus the Story-006 class: any cron-wrapper / sasha-config exec target that
`git ls-files` doesn't know (untracked code prod runs).

No network: "ahead" is measured against the local upstream ref (@{u}), no fetch —
so it's bounded to a few seconds over ~40 repos. Prints ONLY problems and exits 1
when any exist (found-work ≠ crash; mirrors freshness.py / 2026-07-06 Story 008).

    repo_hygiene.py            # human report (default N=7 days)
    repo_hygiene.py --days 14
    repo_hygiene.py --json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# CC_HYGIENE_ROOT lets a non-Craig install (cc-seed) point this at its own
# workspace root instead of ~/Github/CC — unset default preserves this
# host's exact behavior. Without the override, a root that doesn't exist
# (any fresh seed install before the env var is set) degrades to "no repos
# found" rather than crashing _repos()'s unconditional iterdir().
CC = Path(os.path.expanduser(os.environ.get("CC_HYGIENE_ROOT", "~/Github/CC")))
DEFAULT_DAYS = 7
SASHA_CONFIG = Path(os.path.expanduser("~/.config/sasha/config.json"))
HERMES_SCRIPTS = Path(os.path.expanduser("~/.hermes/scripts"))


def _git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def _porcelain_paths(repo: Path) -> list:
    """Dirty tracked paths from `git status --porcelain -uno`. Parsed from RAW
    output (never .strip()'d — that would eat the leading status-column space of
    the first line and mangle `line[3:]`). Handles the `R old -> new` rename form."""
    r = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, timeout=15)
    paths = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        p = line[3:]
        if " -> " in p:            # rename/copy: take the destination
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip('"'))  # git quotes paths with odd chars
    return paths


def _repos() -> list:
    repos = []
    if not CC.is_dir():
        return repos
    if (CC / ".git").is_dir():
        repos.append(CC)
    for child in sorted(CC.iterdir()):
        if child.is_dir() and (child / ".git").is_dir():
            repos.append(child)
    return repos


def sweep_repos(days: int, now: float) -> list:
    cutoff = days * 86400
    problems = []
    for repo in _repos():
        name = repo.name if repo != CC else "CC (cc-meta)"

        has_upstream = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "@{u}"],
            capture_output=True, text=True).returncode == 0
        if not has_upstream:
            # No configured upstream at all = durability hole. (A repo with a
            # remote but an unpushed branch still counts as "ahead" below.)
            if not _git(repo, "remote"):
                problems.append({"repo": name, "kind": "no-remote",
                                 "detail": "no git remote configured"})
                continue

        if has_upstream:
            ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD")
            if ahead and ahead != "0":
                # age by the OLDEST unpushed commit's committer timestamp
                cts = _git(repo, "log", "@{u}..HEAD", "--format=%ct")
                oldest = min((int(x) for x in cts.split() if x.isdigit()), default=int(now))
                age_days = (now - oldest) / 86400
                if now - oldest > cutoff:
                    problems.append({"repo": name, "kind": "ahead",
                                     "detail": f"{ahead} commit(s) unpushed, oldest "
                                               f"{age_days:.0f}d old (>{days}d)"})

        # dirty tracked files, aged by the newest such file's mtime
        dirty_paths = _porcelain_paths(repo)
        if dirty_paths:
            newest = 0.0
            for p in dirty_paths:
                fp = repo / p
                try:
                    newest = max(newest, fp.stat().st_mtime)
                except OSError:
                    newest = now  # deleted/renamed → treat as fresh, stay quiet
            age_days = (now - newest) / 86400
            if now - newest > cutoff:
                problems.append({"repo": name, "kind": "dirty",
                                 "detail": f"{len(dirty_paths)} dirty tracked file(s), "
                                           f"untouched {age_days:.0f}d (>{days}d)"})
    return problems


def _exec_targets() -> set:
    """CC .py paths exec'd by a hermes shim or the sasha dashboard config."""
    pat = re.compile(r"(?:/home/{{REDACTED}}|~)/Github/CC/[A-Za-z0-9_./-]+\.py")
    found = set()
    if HERMES_SCRIPTS.is_dir():
        for sh in HERMES_SCRIPTS.glob("*.sh"):
            try:
                found.update(pat.findall(sh.read_text()))
            except OSError:
                pass
    if SASHA_CONFIG.exists():
        try:
            found.update(pat.findall(SASHA_CONFIG.read_text()))
        except OSError:
            pass
    return {p.replace("~", os.path.expanduser("~")) for p in found}


def sweep_exec_targets() -> list:
    problems = []
    for t in sorted(_exec_targets()):
        fp = Path(t)
        if not fp.exists():
            problems.append({"repo": "-", "kind": "exec-missing",
                             "detail": f"exec target does not exist: {t}"})
            continue
        tracked = subprocess.run(
            ["git", "-C", str(fp.parent), "ls-files", "--error-unmatch", fp.name],
            capture_output=True, text=True).returncode == 0
        if not tracked:
            rel = t.replace(os.path.expanduser("~/Github/CC/"), "")
            problems.append({"repo": "-", "kind": "exec-untracked",
                             "detail": f"cron/dashboard execs an untracked file: {rel}"})
    return problems


def problems(days: int = DEFAULT_DAYS, now: float | None = None) -> list:
    now = now if now is not None else time.time()
    return sweep_repos(days, now) + sweep_exec_targets()


def main() -> int:
    ap = argparse.ArgumentParser(description="Repo-hygiene guard for ~/Github/CC.")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"grace period before dirty/ahead pages (default {DEFAULT_DAYS})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    probs = problems(args.days)
    if args.json:
        print(json.dumps({"problems": probs}, indent=2))
        return 1 if probs else 0
    if not probs:
        return 0  # silent success — freshness/hermes send no ping
    for p in probs:
        tag = p["kind"].upper()
        print(f"[{tag:14}] {p['repo']}: {p['detail']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
