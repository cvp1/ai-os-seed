#!/usr/bin/env python3
"""hello-fleet — the seed's one working demo job (SEED-017).

Proves the whole substrate end-to-end in a single scheduled run:
    scheduler -> this script -> observability/log_run.py -> runs.db -> freshness.py

Reads local host stats (uptime, disk free, load average) and prints ONE
summary line — nothing to configure, nothing that assumes a domain (no
solar, no cameras, no fleet roster). Delete this once you have a real first
job; it exists to be the thing you point at and say "that's alive."

Always exits 0 — a heartbeat has no failure mode of its own to report; if a
stat is unreadable on this platform it degrades to "unknown" rather than
breaking the run (see PRINCIPLES.md: degrade toward safety, loudly).

Stdlib only; targets /usr/bin/python3 on Linux or macOS.
"""
import os
import platform
import shutil
import socket
import sys
from datetime import timedelta


def _uptime_str():
    """Linux: /proc/uptime. macOS: no stdlib equivalent (no /proc) — degrade
    to 'unknown' rather than shelling out to `sysctl` (keep this stdlib-only,
    per the _lib invariant this demo is supposed to model)."""
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        return str(timedelta(seconds=int(seconds)))
    except OSError:
        return "unknown (no /proc/uptime on this platform)"


def _load_str():
    try:
        one, five, fifteen = os.getloadavg()
        return f"{one:.2f} {five:.2f} {fifteen:.2f}"
    except (OSError, AttributeError):
        return "unknown"


def _disk_str(path="/"):
    try:
        total, used, free = shutil.disk_usage(path)
        pct_free = 100 * free / total
        return f"{pct_free:.0f}% free ({free // (1024**3)}GB)"
    except OSError:
        return "unknown"


def main():
    host = socket.gethostname()
    report = (
        f"hello-fleet: {host} ({platform.system()}) alive — "
        f"up {_uptime_str()}, load {_load_str()}, disk {_disk_str()}"
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
