"""Shared stdlib-only helpers for the workspace's projects (enforced by selftest.py).

Import from a sibling project::

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _lib import secrets
"""
from . import (  # noqa: F401
    claude_headless, event_bus, frontmatter, report, secrets,
)

__all__ = ["claude_headless", "event_bus", "frontmatter", "report", "secrets"]
