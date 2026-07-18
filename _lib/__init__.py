"""Shared library — stdlib-only helpers for the workspace's projects.

Projects each live in their own git repo but sit side-by-side on disk, so
consumers reach this package with a 3-line sibling-path bootstrap at the
top of the script::

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _lib import secrets

Everything here is **stdlib-only** so scripts keep running under bare
``/usr/bin/python3`` (no venv) — the invariant is enforced by
``selftest.py`` (imports every module under ``python3 -I -S``). Grow this
package as your system grows; keep the invariant.
"""
from . import (  # noqa: F401
    event_bus, report, secrets,
)

__all__ = ["event_bus", "report", "secrets"]
