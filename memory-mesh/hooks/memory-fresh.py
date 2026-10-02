#!/usr/bin/env python3
"""PreToolUse hook: kick a detached fold when the mesh views are stale.

Installed by the operator via finish-mesh.sh. Always exits 0, never prints,
and kicks at most once per KICK_EVERY_S.
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
