#!/usr/bin/env python3
"""Reconcile the real OS scheduler (crontab on Linux, launchd on macOS) to
match scheduler/manifest.yml — the portable re-target of CC's own
cron/sync.sh (manifest-as-source-of-truth + drift-check) at plain
crontab/launchd instead of the hermes gateway (SEED-014).

    scheduler/sync.py            install/reconcile every manifest.yml job (idempotent)
    scheduler/sync.py --check    report drift only, change nothing; exit 1 on drift

Exit 0 = in sync (or successfully reconciled); exit 1 = drift found in
--check mode; exit 2 = usage/manifest error. Never touches a job it doesn't
own — everything this script writes lives inside a clearly marked managed
block (crontab) or a `dev.cc-seed.*` plist (launchd), so a recipient's own
existing crontab entries or launchd agents are left alone.

Stdlib + PyYAML (matches build_seed.py's own build-time-only dependency;
the manifest format, not a shipped runtime dependency of the jobs it runs).
"""
import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.yml"

CRON_BEGIN = "# BEGIN cc-seed managed jobs (scheduler/sync.py — do not hand-edit this block)"
CRON_END = "# END cc-seed managed jobs"
CRON_LINE_TAG = re.compile(r"# cc-seed:(\S+)$")

LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCHD_PREFIX = "dev.cc-seed."

CRON_FIELD_RE = re.compile(r"^\*/(\d+) \* \* \* \*$")
DAILY_FIELD_RE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* \*$")
WEEKLY_FIELD_RE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* (\d)$")


class ScheduleError(Exception):
    """A job's cron expression doesn't translate to this platform's
    scheduler — refused rather than silently mis-scheduled."""


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

    raise ScheduleError(
        f"job {name!r}: schedule {schedule!r} doesn't translate to launchd — "
        f"only '*/N * * * *' (N divides 60) and a fixed 'M H * * *'/'M H * * D' "
        f"are supported today. Refusing rather than guessing.")


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
    system = platform.system()

    if system == "Linux":
        check_fn, install_fn = cron_check, cron_install
    elif system == "Darwin":
        check_fn, install_fn = launchd_check, launchd_install
    else:
        sys.exit(f"scheduler/sync.py: unsupported platform {system!r} (Linux/macOS only)")

    if args.check:
        drift, code = check_fn(jobs)
        if drift:
            for line in drift:
                print(line)
        else:
            print("scheduler/sync.py --check: in sync.")
        return code

    try:
        install_fn(jobs)
    except ScheduleError as e:
        print(f"scheduler/sync.py: {e}", file=sys.stderr)
        return 2
    print(f"scheduler/sync.py: reconciled {len(jobs)} job(s) on {system}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
