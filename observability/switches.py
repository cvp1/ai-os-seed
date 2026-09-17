#!/usr/bin/env python3
"""Soft on/off switches for cron jobs — toggled by the status-site control panel.

A job whose name is in ``control/switches.json``'s ``disabled`` list is skipped by
``log_run.py`` (the wrapper every cron job routes through) and ignored by
``freshness.py`` (so a deliberately-off job doesn't page as STALE).

This is deliberately decoupled from {{REDACTED}}' own enable/pause state: the
status-site runs in a container and can write this file via a bind mount without
racing the {{REDACTED}} scheduler that owns ``~/.{{REDACTED}}/cron/jobs.json``. The job name
is the log_run ``--job`` key, which equals the shim filename without ``.sh``
(e.g. ``panel_health.sh`` -> ``panel_health``). Stdlib only; never raises on read.

**A pause carries a reason and an expiry** (2026-09-07,
``ontology/HANDOFF-EXTENSIONS-2026-09-07.md`` WP2). A switch is an open-ended
proposal to stay silent, and PRINCIPLES 4 says a proposal decays. Beside
``disabled`` the file now carries a sibling ``reasons`` map::

    {"disabled": ["warm_llm"],
     "reasons": {"warm_llm": {"why": "...", "owner": "craig",
                              "since": "2026-09-06", "until": "2026-10-06",
                              "resume_when": "..."}}}

Two readers, deliberately different:

* ``disabled_jobs()`` stays PERMISSIVE — a corrupt control file must never stop
  jobs from running (degrade toward safety = keep working AND keep watching).
  It is what ``log_run.py`` calls, and it never raises.
* ``load_strict()`` / ``active()`` / ``expired()`` are the strict reader added
  for ``freshness.py`` and the ontology: an unreadable file is reported as
  unreadable rather than read as "nothing is paused".

Expiry means freshness RESUMES WATCHING (the job reads STALE again); nothing
here ever flips, resumes or deletes a switch — resume-watching and
resume-running are different verbs, and only the first is automatic.
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
    """``(dict, None)`` or ``(None, reason)`` — the reader that refuses to
    guess. A missing file is honestly "nothing paused" (the panel creates it on
    first use); anything malformed is a REASON, never an empty set.

    `path` lets a caller read ANOTHER estate's control file with this module's
    rules — the ontology binding reads its fixture tree's copy that way, so the
    format has one home even when the file does not."""
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
    """``({job: reason_or_None}, {job: reason}, err)`` — unexpired, expired, and
    the strict-read failure (in which case both maps are empty)."""
    d, err = load_strict(path)
    if err:
        return {}, {}, err
    now = now or _today()
    reasons = d.get("reasons") or {}
    live, dead = {}, {}
    for job in d.get("disabled", []) or []:
        r = reasons.get(job)
        until = _parse_date((r or {}).get("until"))
        # No reason entry at all (a file written before this shape existed) is
        # NOT expired — it is unexplained, which is `switch-reasoned`'s finding,
        # not a silent resumption of watching.
        if until is not None and until < now:
            dead[job] = r
        else:
            live[job] = r
    return live, dead, None


def active(now=None, path=None):
    """``{job: reason_or_None}`` for switches that have not expired. Empty when
    the file cannot be read strictly — see ``load_strict``."""
    return _switch_map(now, path)[0]


def expired(now=None, path=None):
    """``{job: reason_or_None}`` for switches whose ``until`` is in the past."""
    return _switch_map(now, path)[1]


def set_disabled(job, disabled, *, why=None, owner=None, until=None,
                 resume_when=None, since=None):
    """Switch a job off (disabled=True) or on. Atomic write. Returns the new set.

    Switching OFF records why, who, since and until (default +30 days) in the
    sibling ``reasons`` map. A ``why`` of None is NOT invented into text — it is
    written as null, and the ontology's ``switch-reasoned`` constraint reports it
    the next morning, which is the visible nag. Switching ON evicts the entry
    (PRINCIPLES 23 — eviction is accretion's other half)."""
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
            "owner": owner or prev.get("owner") or "craig",   # a panel click is Craig's hand
            # `since` is when the pause STARTED, so re-writing an already-off
            # switch (a panel re-click, an extended window) must not reset it —
            # switch-reasoned measures the window from here.
            # `since` is normally today; it is settable only to TRANSCRIBE a
            # pause that already happened (the 2026-09-07 migration of six
            # switches Craig turned off on 2026-09-06), never to backdate a new
            # one — the window `switch-reasoned` measures starts here.
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
    # World-readable: the status-site container writes this as root, but the host
    # log_run.py / freshness.py read it as the unprivileged user (mkstemp is 0600,
    # which would lock them out and silently fail the gate open).
    os.chmod(tmp, 0o644)
    os.replace(tmp, PATH)
    return cur
