#!/usr/bin/env python3
"""Reconcile the real OS scheduler (crontab on Linux, launchd on macOS) to
match scheduler/manifest.yml — the portable re-target of CC's own
cron/sync.sh (manifest-as-source-of-truth + drift-check) at plain
crontab/launchd instead of the {{REDACTED}} gateway (SEED-014).

    scheduler/sync.py            install/reconcile every manifest.yml job (idempotent)
    scheduler/sync.py --check    report drift only, change nothing; exit 1 on drift

Exit 0 = in sync (or successfully reconciled); exit 1 = drift found in
--check mode; exit 2 = usage/manifest error. Never touches a job it doesn't
own — everything this script writes lives inside a clearly marked managed
block (crontab) or a `dev.cc-seed.*` plist (launchd), so a recipient's own
existing crontab entries or launchd agents are left alone.

Two kinds of finding, not one (SEED-075). DRIFT is "the installed scheduler
no longer matches the manifest." RISK/WARN are supervision findings about the
manifest's own commands — a scratch path that can vanish, a target that isn't
on disk, an unfilled placeholder, or a job that doesn't route through
log_run.py and so would fail invisibly. `--check` reports both and exits 1;
install refuses on RISK and proceeds with a warning on WARN.

Stdlib + PyYAML (matches build_seed.py's own build-time-only dependency;
the manifest format, not a shipped runtime dependency of the jobs it runs).
"""
import argparse
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.yml"
# The installed --target root: the scheduler ships to <target>/scheduler/,
# so the root is this file's grandparent. Used by the supervision checks to
# decide whether a job's command points inside the install or out of it.
SEED_ROOT = HERE.parent

CRON_BEGIN = "# BEGIN cc-seed managed jobs (scheduler/sync.py — do not hand-edit this block)"
CRON_END = "# END cc-seed managed jobs"
CRON_LINE_TAG = re.compile(r"# cc-seed:(\S+)$")

LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCHD_PREFIX = "dev.cc-seed."

CRON_FIELD_RE = re.compile(r"^\*/(\d+) \* \* \* \*$")
HOURLY_FIELD_RE = re.compile(r"^(\d{1,2}) \* \* \* \*$")
DAILY_FIELD_RE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* \*$")
WEEKLY_FIELD_RE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* (\d)$")


class ScheduleError(Exception):
    """A job's cron expression doesn't translate to this platform's
    scheduler — refused rather than silently mis-scheduled."""


# --- supervision checks (SEED-075) ------------------------------------------
#
# The drift checks below answer "does the installed scheduler match the
# manifest?" They already catch a THIRD party rewriting an installed entry —
# the rewritten line stops matching and reports as content drift.
#
# What nothing caught was a poisoned MANIFEST. On 2026-08-09 an agent working
# in a /tmp sandbox rewrote two of the source fleet's production jobs to point
# into that sandbox; the sandbox was then deleted and both jobs died for ~27
# hours. Reconciling a manifest whose commands name a scratch directory
# installs that fault rather than reporting it, so the manifest is the seam
# these checks sit at — they fire at install time, while the sandbox still
# exists, which is *before* the job dies.
#
# The worse half of that incident was not the dead job. The rewritten entries
# also lost their log_run.py wrapper, so failures stopped being RECORDED: the
# freshness monitor could only ever render STALE, never FAILING, with no exit
# code and no cause. A supervised job that can be edited to become
# unsupervised was never supervised — hence UNSUPERVISED is its own finding
# and not a footnote on the path check.

BLOCK = "RISK"          # refuse to install; fails --check
WARN = "WARN"           # report and proceed; fails --check

LOG_RUN_MARKER = "observability/log_run.py"
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")
# Scratch roots a live job must never depend on. Deliberately a small,
# explicit list: this is the incident's exact shape and it has effectively no
# false positives, which is what lets it BLOCK rather than merely warn.
EPHEMERAL_ROOTS = ("/tmp/", "/var/tmp/", "/dev/shm/",
                   "/private/tmp/", "/private/var/folders/")
# Interpreters and system tools live outside the target root by design, so
# they are exempt from the outside-the-root check — but NOT from the
# does-it-exist check, which still catches a typo'd interpreter.
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/",
                   "/opt/", "/etc/")


def _command_paths(command):
    """Absolute filesystem paths named anywhere in a command line.

    shlex so a quoted path with spaces survives; a fallback split so a command
    with unbalanced quotes is still inspected rather than skipped silently —
    refusing to look is how a malformed entry would slip past every check.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    paths = []
    for tok in tokens:
        if tok.startswith("~"):
            tok = str(Path(tok).expanduser())
        if tok.startswith("/"):
            paths.append(tok)
    return paths


def _inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def command_problems(job, root=None):
    """Supervision findings for one manifest entry, as (severity, message)."""
    root = Path(root or SEED_ROOT)
    name = job.get("name", "<unnamed>")
    command = (job.get("command") or "").strip()
    out = []

    if not command:
        return [(BLOCK, f"{name}: manifest entry has no command")]

    m = PLACEHOLDER_RE.search(command)
    if m:
        out.append((BLOCK, f"{name}: command still contains the unfilled "
                           f"template placeholder {m.group(0)} — it would be "
                           f"scheduled verbatim and fail every run"))

    if LOG_RUN_MARKER not in command:
        out.append((WARN, f"{name}: command does not route through "
                          f"{LOG_RUN_MARKER} — the job would run UNSUPERVISED: "
                          f"no runs.db row, so a failure can only ever surface "
                          f"as STALE, never as FAILING, with no exit code and "
                          f"no cause"))

    for p in _command_paths(command):
        # Ephemeral AND outside the install. An operator who installs the seed
        # under /tmp has made that choice for the whole tree, and flagging the
        # install's own files would make the check unusable there; the fault
        # this catches is a job reaching OUT of the install into a scratch dir
        # — which is exactly the shape of the 2026-08-09 incident, where units
        # under ~/.config pointed into a /tmp sandbox.
        if p.startswith(EPHEMERAL_ROOTS) and not _inside(p, root):
            out.append((BLOCK, f"{name}: command targets an ephemeral path "
                               f"{p} — a scratch/sandbox directory outside "
                               f"the install that can vanish under a live job"))
            continue
        if not Path(p).exists():
            out.append((BLOCK, f"{name}: command targets {p}, which does not "
                               f"exist on disk"))
        if not p.startswith(SYSTEM_PREFIXES) and not _inside(p, root):
            out.append((WARN, f"{name}: command targets {p}, outside the "
                              f"installed target root {root}"))
    return out


def manifest_problems(jobs, root=None):
    out = []
    for job in jobs:
        out.extend(command_problems(job, root))
    return out


def load_jobs():
    if not MANIFEST.exists():
        sys.exit(f"scheduler/sync.py: manifest not found: {MANIFEST}")
    with open(MANIFEST) as f:
        data = yaml.safe_load(f) or {}
    jobs = data.get("jobs") or []
    names = [j["name"] for j in jobs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit(f"scheduler/sync.py: duplicate job name(s) in manifest: {sorted(dupes)}")
    return jobs


# --- Linux: crontab -----------------------------------------------------

def _cron_line(job):
    return f"{job['schedule']} {job['command']} # cc-seed:{job['name']}"


def _read_crontab():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        return []  # no crontab yet — not an error
    return r.stdout.splitlines()


def _write_crontab(lines):
    r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
    if r.returncode != 0:
        sys.exit("scheduler/sync.py: `crontab -` failed to install the new table")


def _managed_block(existing_lines):
    """Return (before, managed, after) — the lines outside vs inside our
    marked block. managed is [] if the block isn't present yet."""
    try:
        start = existing_lines.index(CRON_BEGIN)
        end = existing_lines.index(CRON_END)
    except ValueError:
        return existing_lines, [], []
    return existing_lines[:start], existing_lines[start + 1:end], existing_lines[end + 1:]


def cron_desired_lines(jobs):
    return [_cron_line(j) for j in jobs]


def cron_check(jobs):
    existing = _read_crontab()
    before, managed, after = _managed_block(existing)
    desired = cron_desired_lines(jobs)
    if managed == desired:
        return [], 0
    drift = []
    desired_names = {j["name"] for j in jobs}
    managed_names = {m.group(1) for line in managed if (m := CRON_LINE_TAG.search(line))}
    for name in sorted(desired_names - managed_names):
        drift.append(f"DRIFT: job not installed: {name}")
    for name in sorted(managed_names - desired_names):
        drift.append(f"DRIFT: installed job has no manifest entry: {name}")
    for line in managed:
        m = CRON_LINE_TAG.search(line)
        if m and m.group(1) in desired_names and line not in desired:
            drift.append(f"DRIFT: content differs for job: {m.group(1)}")
    return drift, (1 if drift else 0)


def cron_install(jobs):
    existing = _read_crontab()
    before, _, after = _managed_block(existing)
    new_lines = before + [CRON_BEGIN] + cron_desired_lines(jobs) + [CRON_END] + after
    _write_crontab(new_lines)


# --- macOS: launchd -------------------------------------------------------

def _plist_path(name):
    return LAUNCHD_DIR / f"{LAUNCHD_PREFIX}{name}.plist"


def _schedule_to_launchd(schedule, name):
    m = CRON_FIELD_RE.match(schedule)
    if m:
        every_min = int(m.group(1))
        if 60 % every_min != 0:
            raise ScheduleError(
                f"job {name!r}: '*/{every_min} * * * *' doesn't evenly divide 60; "
                f"launchd needs an exact-minute translation — pick a divisor of 60")
        return "interval", every_min * 60

    m = WEEKLY_FIELD_RE.match(schedule)
    if m:
        minute, hour, weekday = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return "calendar", {"Minute": minute, "Hour": hour, "Weekday": weekday}

    m = DAILY_FIELD_RE.match(schedule)
    if m:
        minute, hour = int(m.group(1)), int(m.group(2))
        return "calendar", {"Minute": minute, "Hour": hour}

    # 'M * * * *' — hourly at a fixed minute. launchd runs a
    # StartCalendarInterval dict with every field omitted as a wildcard, so a
    # Minute-only dict fires once an hour at M, exactly like cron.
    m = HOURLY_FIELD_RE.match(schedule)
    if m:
        return "calendar", {"Minute": int(m.group(1))}

    raise ScheduleError(
        f"job {name!r}: schedule {schedule!r} doesn't translate to launchd — "
        f"only '*/N * * * *' (N divides 60), 'M * * * *', and a fixed "
        f"'M H * * *'/'M H * * D' are supported today. "
        f"Refusing rather than guessing.")


def _render_plist(job):
    kind, val = _schedule_to_launchd(job["schedule"], job["name"])
    label = f"{LAUNCHD_PREFIX}{job['name']}"
    # /bin/sh -c wraps the command so the manifest's plain shell command line
    # (pipes, --job flags, etc.) doesn't need ProgramArguments array-splitting.
    body = (
        f'  <key>Label</key>\n  <string>{label}</string>\n'
        f'  <key>ProgramArguments</key>\n'
        f'  <array>\n    <string>/bin/sh</string>\n    <string>-c</string>\n'
        f'    <string>{job["command"]}</string>\n  </array>\n'
    )
    if kind == "interval":
        body += f'  <key>StartInterval</key>\n  <integer>{val}</integer>\n'
    else:
        body += '  <key>StartCalendarInterval</key>\n  <dict>\n'
        for k, v in val.items():
            body += f'    <key>{k}</key>\n    <integer>{v}</integer>\n'
        body += '  </dict>\n'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n' + body + '</dict>\n</plist>\n'
    )


def _launchd_installed_names():
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    return {line.split()[-1][len(LAUNCHD_PREFIX):]
            for line in r.stdout.splitlines() if LAUNCHD_PREFIX in line}


def launchd_check(jobs):
    drift = []
    desired_names = {j["name"] for j in jobs}
    for job in jobs:
        path = _plist_path(job["name"])
        try:
            desired_text = _render_plist(job)
        except ScheduleError as e:
            drift.append(f"DRIFT: {e}")
            continue
        if not path.exists():
            drift.append(f"DRIFT: job not installed: {job['name']}")
        elif path.read_text() != desired_text:
            drift.append(f"DRIFT: content differs for job: {job['name']}")
    if LAUNCHD_DIR.exists():
        for path in LAUNCHD_DIR.glob(f"{LAUNCHD_PREFIX}*.plist"):
            name = path.stem[len(LAUNCHD_PREFIX):]
            if name not in desired_names:
                drift.append(f"DRIFT: installed job has no manifest entry: {name}")
    return drift, (1 if drift else 0)


def launchd_install(jobs):
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    installed = _launchd_installed_names()
    desired_names = {j["name"] for j in jobs}
    for name in installed - desired_names:
        path = _plist_path(name)
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    for job in jobs:
        path = _plist_path(job["name"])
        text = _render_plist(job)  # raises ScheduleError -> caller reports + exits
        if path.exists() and path.read_text() == text and job["name"] in installed:
            continue  # already correct and loaded — idempotent, don't reload
        if path.exists():
            subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.write_text(text)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=False)


# --- entry point ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report drift only, change nothing")
    args = ap.parse_args()

    jobs = load_jobs()
    problems = manifest_problems(jobs)
    blocking = [msg for sev, msg in problems if sev == BLOCK]
    soft = [msg for sev, msg in problems if sev == WARN]
    system = platform.system()

    if system == "Linux":
        check_fn, install_fn = cron_check, cron_install
    elif system == "Darwin":
        check_fn, install_fn = launchd_check, launchd_install
    else:
        sys.exit(f"scheduler/sync.py: unsupported platform {system!r} (Linux/macOS only)")

    if args.check:
        drift, _ = check_fn(jobs)
        for line in drift:
            print(line)
        for msg in blocking:
            print(f"{BLOCK}: {msg}")
        for msg in soft:
            print(f"{WARN}: {msg}")
        if not drift and not problems:
            print("scheduler/sync.py --check: in sync.")
        return 1 if (drift or problems) else 0

    # Refuse to INSTALL a manifest that names a scratch path, an unfilled
    # placeholder or a missing target: reconciling it would schedule the fault
    # instead of reporting it. A missing log_run.py wrapper only warns — the
    # manifest documents unwrapped jobs as legal ("still runs; just never
    # appears in runs.db"), so blocking on it would refuse something the
    # product tells the operator they may do.
    if blocking:
        for msg in blocking:
            print(f"scheduler/sync.py: {BLOCK}: {msg}", file=sys.stderr)
        print("scheduler/sync.py: refusing to install — fix the manifest, or "
              "run --check to see every finding.", file=sys.stderr)
        return 2
    for msg in soft:
        print(f"scheduler/sync.py: {WARN}: {msg}", file=sys.stderr)

    try:
        install_fn(jobs)
    except ScheduleError as e:
        print(f"scheduler/sync.py: {e}", file=sys.stderr)
        return 2
    print(f"scheduler/sync.py: reconciled {len(jobs)} job(s) on {system}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
