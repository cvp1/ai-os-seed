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

# --- Catastrophic content loss (added 2026-07-27) ----------------------------
# Written after PRINCIPLES.md — all 16 first principles — sat at 0 bytes for
# roughly five hours and NOTHING noticed. The `dirty` check below could not have
# caught it: it waits 7 days and then reports a COUNT ("3 dirty tracked files"),
# so a doctrine file emptied by a stray `> $UNSET_VAR` reads exactly like a
# work-in-progress edit. Content LOSS is a different class from content CHANGE
# and pages immediately, with the file named.
GUTTED_MIN_BYTES = 400   # under this, "90% smaller" is noise, not destruction
GUTTED_KEEP_FRAC = 0.10  # keeping <=10% of the committed bytes = gutted
GUTTED_MAX_FILES = 300   # bound the per-repo work (Principle 8)
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


def _head_blob_sizes(repo: Path) -> dict:
    """{path: byte size} for every regular-file blob at HEAD, from ONE
    `git ls-tree -r -l -z HEAD` call. Symlinks (mode 120000) and submodules
    (type commit) are skipped. Empty on unborn HEAD — no comparison, no finding.

    v2 (2026-07-27, same day as v1): the guard originally walked `git status`'s
    dirty list. Grok's eval pass called the hole and a live control CONFIRMED it:
    the incident's own destroying command printed a CLEAN status, and a file
    truncated under `update-index --assume-unchanged` was invisible to v1. A
    guard against silent destruction cannot take git's word for which files
    changed — HEAD is the ground truth, so enumerate HEAD and stat the tree."""
    r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-l", "-z", "HEAD"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {}
    sizes = {}
    for rec in r.stdout.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        parts = meta.split()               # mode type hash size
        if len(parts) != 4 or parts[1] != "blob" or parts[0] == "120000":
            continue
        if parts[3].isdigit():
            sizes[path] = int(parts[3])
    return sizes


def _gutted(repo: Path) -> list:
    """Tracked files whose working copy has lost nearly all of its committed
    content, measured HEAD-vs-disk for EVERY tracked file — deliberately NOT via
    `git status`, which the incident proved can report clean over destruction.
    Two shapes, both flagged with NO grace period:

      * emptied  — 0 bytes where HEAD had content. This is never a deliberate
                   edit; it is a failed write or a redirect onto the wrong path.
      * gutted   — kept <=10% of a >=400-byte file. Rewrites shrink; they don't
                   evaporate.

    SCOPE BOUNDARY — a tracked file *missing* from the working tree is out of
    scope: usually a deliberate delete, and the `dirty` check ages it out. NOTE
    the known residual: a delete that ALSO fools `git status` evades both checks.
    Accepted for now — a missing file is at least loud the moment anything
    imports it, where a 0-byte file imports cleanly and lies.
    """
    out = []
    heads = _head_blob_sizes(repo)
    for p, head in heads.items():
        if head == 0:
            continue                       # nothing committed to lose
        if len(out) >= GUTTED_MAX_FILES:
            out.append(("…", 0, 0, f"more than {GUTTED_MAX_FILES} hits; truncated"))
            break
        try:
            live = (repo / p).stat().st_size
        except OSError:
            continue                       # missing/unreadable → out of scope
        if live == 0:
            out.append((p, head, live, "emptied to 0 bytes"))
        elif head >= GUTTED_MIN_BYTES and live <= head * GUTTED_KEEP_FRAC:
            pct = 100.0 * (head - live) / head
            out.append((p, head, live, f"lost {pct:.0f}% of its content"))
    return out


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

        dirty_paths = _porcelain_paths(repo)

        # Content loss pages IMMEDIATELY — no `days` grace — and is checked
        # FIRST, before the no-remote early-exit below: a repo with no remote is
        # the LAST place you want content destruction to go unreported. NOT fed
        # from dirty_paths: the incident's status output was clean (v2).
        for path, head, live, why in _gutted(repo):
            problems.append({"repo": name, "kind": "gutted",
                             "detail": f"{path}: {why} "
                                       f"({head} bytes at HEAD, {live} now) — "
                                       f"restore with: git -C {repo} restore {path}"})

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
