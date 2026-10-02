#!/usr/bin/env python3
"""Soft on/off switches for scheduled jobs, stored in ``control/switches.json``.

A job in the ``disabled`` list is skipped by ``log_run.py`` and not paged by
``freshness.py``. Each pause carries a ``reasons`` entry with an expiry::

    {"disabled": ["warm_llm"],
     "reasons": {"warm_llm": {"why": "...", "owner": "agent",
                              "since": "YYYY-MM-DD", "until": "YYYY-MM-DD",
                              "resume_when": "..."}}}

``disabled_jobs()`` is permissive (never raises; a corrupt file never stops
jobs). ``load_strict()``/``active()``/``expired()`` report an unreadable file
as an error. An expired switch resumes watching only; jobs stay skipped until
switched back on.
"""
import datetime
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


DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 90
OWNERS = ("craig", "agent")


def _today():
    return datetime.date.today()


def _parse_date(v):
    """An ISO date, or None. Never raises."""
    try:
        return datetime.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def load_strict(path=None):
    """Return ``(dict, None)`` or ``(None, reason)``; a missing file means nothing
    is paused, anything malformed is a reason. `path` overrides the default file."""
    try:
        with open(path or PATH) as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return {}, None
    except (ValueError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(d, dict):
        return None, f"top level is {type(d).__name__}, expected an object"
    dis = d.get("disabled", [])
    if not isinstance(dis, list) or not all(isinstance(x, str) for x in dis):
        return None, "'disabled' is not a list of job-name strings"
    reasons = d.get("reasons", {})
    if not isinstance(reasons, dict):
        return None, "'reasons' is not an object keyed by job name"
    for job, r in reasons.items():
        if not isinstance(r, dict):
            return None, f"reasons[{job!r}] is {type(r).__name__}, expected an object"
        for field in ("since", "until"):
            if r.get(field) is not None and _parse_date(r.get(field)) is None:
                return None, f"reasons[{job!r}].{field} is not an ISO date: {r.get(field)!r}"
    return d, None


def _switch_map(now=None, path=None):
    """Return ``(unexpired, expired, err)``; both maps are empty on a read error."""
    d, err = load_strict(path)
    if err:
        return {}, {}, err
    now = now or _today()
    reasons = d.get("reasons") or {}
    live, dead = {}, {}
    for job in d.get("disabled", []) or []:
        r = reasons.get(job)
        until = _parse_date((r or {}).get("until"))
        # A switch with no reason entry is treated as unexpired.
        if until is not None and until < now:
            dead[job] = r
        else:
            live[job] = r
    return live, dead, None


def active(now=None, path=None):
    """``{job: reason_or_None}`` for unexpired switches; empty on a read error."""
    return _switch_map(now, path)[0]


def expired(now=None, path=None):
    """``{job: reason_or_None}`` for switches whose ``until`` is in the past."""
    return _switch_map(now, path)[1]


def set_disabled(job, disabled, *, why=None, owner=None, until=None,
                 resume_when=None, since=None):
    """Switch a job off or on (atomic write); returns the new disabled set.

    Switching off records why/owner/since/until (default +30 days) in
    ``reasons``; a missing ``why`` is stored as null. Switching on removes it."""
    d = _load()
    cur = set(d.get("disabled", []) or [])
    cur.add(job) if disabled else cur.discard(job)
    d["disabled"] = sorted(cur)
    reasons = d.get("reasons")
    reasons = dict(reasons) if isinstance(reasons, dict) else {}
    if disabled:
        today = _today()
        prev = reasons.get(job) if isinstance(reasons.get(job), dict) else {}
        reasons[job] = {
            "why": why if why is not None else prev.get("why"),
            "owner": owner or prev.get("owner") or "craig",
            # `since` is when the pause started; re-writing an existing switch
            # keeps it. An explicit `since` only records an earlier pause.
            "since": prev.get("since") or since or today.isoformat(),
            "until": until or prev.get("until")
                     or (today + datetime.timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat(),
            "resume_when": resume_when if resume_when is not None else prev.get("resume_when"),
        }
    else:
        reasons.pop(job, None)
    d["reasons"] = reasons
    os.makedirs(CONTROL_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONTROL_DIR, prefix=".sw_", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, indent=1)
    # World-readable: mkstemp's 0600 would hide it from readers running as
    # another user.
    os.chmod(tmp, 0o644)
    os.replace(tmp, PATH)
    return cur
