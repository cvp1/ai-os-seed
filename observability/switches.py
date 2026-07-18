#!/usr/bin/env python3
"""Soft on/off switches for cron jobs — toggled by the status-site control panel.

A job whose name is in ``control/switches.json``'s ``disabled`` list is skipped by
``log_run.py`` (the wrapper every cron job routes through) and ignored by
``freshness.py`` (so a deliberately-off job doesn't page as STALE).

This is deliberately decoupled from hermes' own enable/pause state: the
status-site runs in a container and can write this file via a bind mount without
racing the hermes scheduler that owns ``~/.hermes/cron/jobs.json``. The job name
is the log_run ``--job`` key, which equals the shim filename without ``.sh``
(e.g. ``panel_health.sh`` -> ``panel_health``). Stdlib only; never raises on read.
"""
import json
import os
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL_DIR = os.path.join(_HERE, "control")
PATH = os.path.join(CONTROL_DIR, "switches.json")


def _load():
    try:
        with open(PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def disabled_jobs():
    """Set of job names currently switched OFF (empty on any read error)."""
    return set(_load().get("disabled", []) or [])


def is_disabled(job):
    return job in disabled_jobs()


def set_disabled(job, disabled):
    """Switch a job off (disabled=True) or on. Atomic write. Returns the new set."""
    d = _load()
    cur = set(d.get("disabled", []) or [])
    cur.add(job) if disabled else cur.discard(job)
    d["disabled"] = sorted(cur)
    os.makedirs(CONTROL_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONTROL_DIR, prefix=".sw_", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, indent=1)
    # World-readable: the status-site container writes this as root, but the host
    # log_run.py / freshness.py read it as the unprivileged user (mkstemp is 0600,
    # which would lock them out and silently fail the gate open).
    os.chmod(tmp, 0o644)
    os.replace(tmp, PATH)
    return cur
