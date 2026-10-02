#!/usr/bin/env python3
"""Repo-hygiene guard: report git repos under the workspace root that are not
committed and pushed.

Reports: missing remote; unpushed commits older than N days; dirty tracked files
untouched for N days; files gutted vs HEAD; and untracked scheduled exec targets.
No network ("ahead" uses the local @{u}). Prints only problems; exits 1 if any.

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

# Workspace root; override with CC_HYGIENE_ROOT or --root. A missing root
# yields no repos rather than an error.
CC = Path(os.path.expanduser(os.environ.get("CC_HYGIENE_ROOT", "~/{{REDACTED}}")))
DEFAULT_DAYS = 7

# --- Content loss: emptied/gutted tracked files page immediately ------------
GUTTED_MIN_BYTES = 400   # under this, "90% smaller" is noise, not destruction
GUTTED_KEEP_FRAC = 0.10  # keeping <=10% of the committed bytes = gutted
GUTTED_MAX_FILES = 300   # bound the per-repo work
SASHA_CONFIG = Path(os.path.expanduser("~/.config/sasha/config.json"))
CRON_SHIM_SCRIPTS = Path(os.path.expanduser("~/.{{REDACTED}}/scripts"))


def _git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def _porcelain_paths(repo: Path) -> list:
    """Dirty tracked paths from `git status --porcelain -uno`. Output is parsed
    unstripped so `line[3:]` stays aligned; renames yield the destination."""
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
    """{path: byte size} for every regular-file blob at HEAD (symlinks and
    submodules skipped); empty on an unborn HEAD."""
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
    """Tracked files that lost nearly all committed content, compared HEAD vs
    disk (not via `git status`, which can miss it).

      * emptied  — 0 bytes where HEAD had content.
      * gutted   — kept <=10% of a >=400-byte file.

    Missing files are out of scope; the `dirty` check covers deletes.
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
        # A symlinked dir is a shim onto a real repo, not a second repo.
        if child.is_symlink():
            continue
        if child.is_dir() and (child / ".git").is_dir():
            repos.append(child)
    return repos


def sweep_repos(days: int, now: float) -> list:
    cutoff = days * 86400
    problems = []
    for repo in _repos():
        name = repo.name if repo != CC else "CC (cc-meta)"

        dirty_paths = _porcelain_paths(repo)

        # Content loss: no grace period, checked before the no-remote exit.
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
    """CC .py paths exec'd by a cron shim or the sasha dashboard config."""
    pat = re.compile(r"(?:/home/{{REDACTED}}|~)/{{REDACTED}}/[A-Za-z0-9_./-]+\.py")
    found = set()
    if CRON_SHIM_SCRIPTS.is_dir():
        for sh in CRON_SHIM_SCRIPTS.glob("*.sh"):
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
            rel = t.replace(os.path.expanduser("~/{{REDACTED}}/"), "")
            problems.append({"repo": "-", "kind": "exec-untracked",
                             "detail": f"cron/dashboard execs an untracked file: {rel}"})
    return problems


def problems(days: int = DEFAULT_DAYS, now: float | None = None) -> list:
    now = now if now is not None else time.time()
    return sweep_repos(days, now) + sweep_exec_targets()


def main() -> int:
    global CC
    ap = argparse.ArgumentParser(description="Repo-hygiene guard for ~/{{REDACTED}}.")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"grace period before dirty/ahead pages (default {DEFAULT_DAYS})")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="behaviour checks on a scratch tree; no live repos touched")
    ap.add_argument("--root", metavar="PATH",
                    help="sweep root for this invocation, overriding CC_HYGIENE_ROOT "
                         "and the ~/{{REDACTED}} default — a CLI arg (not just the env "
                         "var) so a scheduler that execs argv directly, no shell, "
                         "still works (cc-seed's SEED-070 starter job: launchd's "
                         "ProgramArguments doesn't expand VAR=val prefixes the way "
                         "a crontab line, run through sh -c, would)")
    ap.add_argument("--findings-exit0", action="store_true",
                    help="cc-seed scheduler convention (SEED-070, scheduler/"
                         "CONVENTIONS.md rule 1): found-work is success, not "
                         "breakage — print a leading FINDINGS: line and exit 0 "
                         "when problems exist, instead of this script's own "
                         "default of exit 1. Off by default: unchanged for the "
                         "existing freshness.py dependency-check usage on this host, "
                         "which reads problems() as data and never looks at this "
                         "exit code.")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if args.root:
        CC = Path(os.path.expanduser(args.root))

    probs = problems(args.days)
    if args.json:
        print(json.dumps({"problems": probs}, indent=2))
        if args.findings_exit0:
            return 0
        return 1 if probs else 0
    if not probs:
        return 0  # silent success
    if args.findings_exit0:
        print(f"FINDINGS: {len(probs)} repo(s)/target(s) need attention")
    for p in probs:
        tag = p["kind"].upper()
        print(f"[{tag:14}] {p['repo']}: {p['detail']}")
    return 0 if args.findings_exit0 else 1


def _selftest() -> int:
    """Check on a scratch tree that _repos() skips a symlinked shim dir."""
    global CC
    import subprocess
    import tempfile
    fails = []
    saved = CC
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "real").mkdir()
            subprocess.run(["git", "init", "-q", str(root / "real")], check=True)
            (root / "plain").mkdir()                       # dir, not a repo
            (root / "shim").symlink_to("real", target_is_directory=True)
            CC = root
            names = sorted(p.name for p in _repos())
            if names != ["real"]:
                fails.append(f"enumeration: {names} != ['real']")
            print(f"  {'FAIL' if fails else 'ok  '} symlinked shim is not a second repo ({names})")
    finally:
        CC = saved
    print("repo_hygiene selftest: %s" % ("PASS" if not fails else "FAIL " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
