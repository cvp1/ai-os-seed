#!/usr/bin/env python3
"""PreToolUse staleness guard for memory-mesh views.

INSTALLED ONLY BY THE OPERATOR (finish-mesh.sh) — hook installation is gated
self-modification by standing doctrine; this file living in the repo is the
proposal, Craig running the installer is the signature.

Closes the mid-session window: a running session acts on views loaded at
start; a correction can land elsewhere mid-session. This keeps the local
materialized views fresh so read-at-point-of-use reads fresh state.

HARD RULES for living on the every-tool-call path:
  · fail OPEN, always exit 0 — memory freshness must never block work
  · never print — stdout would inject noise into every tool call
  · microseconds on the happy path (two stats); the kick is detached
  · throttled — at most one fold kick per 120s no matter how stale
"""
import os
import subprocess
import sys
import time

VERSION = os.path.expanduser("~/memory-events/view.version")
THROTTLE = os.path.expanduser("~/.config/memory-mesh/.fold-kick")
STALE_S = 600
KICK_EVERY_S = 120

try:
    st = os.stat(VERSION)          # mesh not installed → FileNotFoundError → open
    if time.time() - st.st_mtime > STALE_S:
        try:
            last = os.stat(THROTTLE).st_mtime
        except FileNotFoundError:
            last = 0
        if time.time() - last > KICK_EVERY_S:
            os.makedirs(os.path.dirname(THROTTLE), exist_ok=True)
            with open(THROTTLE, "w") as f:
                f.write(str(time.time()))
            if sys.platform == "darwin":
                cmd = ["launchctl", "kickstart", f"gui/{os.getuid()}/com.cvp.memory-fold"]
            else:
                cmd = ["systemctl", "--user", "start", "memory-fold.service"]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
except Exception:
    pass                            # fail open, silently, by design
sys.exit(0)
