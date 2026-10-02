#!/usr/bin/env python3
"""hello-fleet — demo job exercising scheduler -> log_run.py -> runs.db -> freshness.py.

Prints one line of local host stats (uptime, load, disk free). Always exits 0;
unreadable stats show as "unknown". Replace it once you have a real job.
Stdlib only; Linux or macOS.
"""
import os
import platform
import shutil
import socket
import sys
from datetime import timedelta


def _uptime_str():
    """Uptime from /proc/uptime; 'unknown' where /proc is absent (macOS)."""
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
