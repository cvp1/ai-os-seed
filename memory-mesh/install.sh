#!/usr/bin/env bash
# memory-mesh install — idempotent, per machine. Solo by default; joining an
# existing mesh needs SEED=<host> (see ENROLL.md). Linux (systemd user
# timers) and macOS (launchd).
set -euo pipefail
CODE="$(cd "$(dirname "$0")" && pwd)"
EVENTS="$HOME/memory-events"
ME="${MESH_HOST:-$( [ -f "$HOME/.config/memory-mesh/host" ] && cat "$HOME/.config/memory-mesh/host" || hostname -s | tr '[:upper:]' '[:lower:]')}"
mkdir -p "$HOME/.config/memory-mesh"
echo "$ME" > "$HOME/.config/memory-mesh/host"

echo "== memory-mesh install on $ME =="

# 1. events repo: init locally (solo / first machine), or clone the shared
#    seed when joining (a common root commit is what makes "unrelated
#    histories" a refusable attack, not a mergeable accident).
if [ ! -d "$EVENTS/.git" ]; then
  if [ -n "${SEED:-}" ] && [ "$SEED" != "$ME" ]; then
    git clone -q "$SEED:memory-events" "$EVENTS"
    git -C "$EVENTS" remote remove origin
  else
    git init -q "$EVENTS"
    git -C "$EVENTS" config user.email >/dev/null 2>&1 || {
      git -C "$EVENTS" config user.name "memory-mesh"
      git -C "$EVENTS" config user.email "mesh@$ME"
    }
    mkdir -p "$EVENTS/events"
    : > "$EVENTS/events/.keep"
    printf 'views/\nstate/\nview.version\n' > "$EVENTS/.gitignore"
    git -C "$EVENTS" add -A
    git -C "$EVENTS" commit -qm "mesh seed"
  fi
fi

# 2. durability + hygiene (SPEC Integrity) + a local commit identity if the
#    machine has none configured (events commits are per-repo bookkeeping):
git -C "$EVENTS" config user.email >/dev/null 2>&1 || {
  git -C "$EVENTS" config user.name "memory-mesh"
  git -C "$EVENTS" config user.email "mesh@$ME"
}
git -C "$EVENTS" config core.fsync committed
git -C "$EVENTS" config gc.pruneExpire never
git -C "$EVENTS" config receive.denyNonFastForwards true
git -C "$EVENTS" config core.autocrlf false

# 3. peer remotes from mesh.toml (skip self; empty [[hosts]] = solo mode)
python3 - "$CODE" "$EVENTS" "$ME" <<'PY'
import subprocess, sys, pathlib
code, events, me = sys.argv[1:4]
sys.path.insert(0, code)
from mesh_lib import _load_toml
cfg = _load_toml(pathlib.Path(code) / "mesh.toml")
existing = subprocess.run(["git", "-C", events, "remote"],
                          capture_output=True, text=True).stdout.split()
for h in (cfg.get("hosts") or []):
    if h["name"] == me or h["name"] in existing:
        continue
    subprocess.run(["git", "-C", events, "remote", "add", h["name"],
                    f"{h['ssh']}:{h['repo']}"], check=True)
    print(f"remote added: {h['name']} -> {h['ssh']}:{h['repo']}")
PY

# 4. schedule the fold (5 min) + home watcher (hourly)
if [ "$(uname)" = "Darwin" ]; then
  for JOB in "memory-fold:fold.py:300" "memory-home-watch:home_watch.py:3600"; do
    NAME="${JOB%%:*}"; REST="${JOB#*:}"; SCRIPT="${REST%%:*}"; EVERY="${REST##*:}"
    P="$HOME/Library/LaunchAgents/local.$NAME.plist"
    cat > "$P" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.$NAME</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$CODE/$SCRIPT</string></array>
  <key>StartInterval</key><integer>$EVERY</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    launchctl unload "$P" 2>/dev/null || true
    launchctl load "$P"
  done
  echo "launchd agents loaded (fold 300s, home-watch hourly)"
else
  U="$HOME/.config/systemd/user"; mkdir -p "$U"
  cat > "$U/memory-fold.service" <<EOF
[Unit]
Description=memory-mesh fold — fetch peers, detect contradictions, materialize views
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $CODE/fold.py
TimeoutStartSec=180
EOF
  cat > "$U/memory-fold.timer" <<EOF
[Unit]
Description=memory-mesh fold every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30
[Install]
WantedBy=timers.target
EOF
  cat > "$U/memory-home-watch.service" <<EOF
[Unit]
Description=memory-mesh home watcher — emit update-pointer on canonical-home change
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $CODE/home_watch.py
TimeoutStartSec=300
EOF
  cat > "$U/memory-home-watch.timer" <<EOF
[Unit]
Description=memory-mesh home watch hourly
[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
[Install]
WantedBy=timers.target
EOF
  if systemctl --user daemon-reload 2>/dev/null && \
     systemctl --user enable --now memory-fold.timer memory-home-watch.timer 2>/dev/null; then
    echo "systemd user timers enabled (fold 5 min, home-watch hourly)"
  else
    echo "WARN: no systemd user manager — schedule fold.py (5 min) and home_watch.py (hourly) with your scheduler"
  fi
fi

# 5. flip the harness index to fold-generated, backfill any existing
#    always-on notes as lesson events, and prove the loop once, live.
STORE="$(python3 -c "import sys; sys.path.insert(0,'$CODE'); import mesh_lib; print(mesh_lib.store_dir())")"
mkdir -p "$STORE"
if [ ! -f "$STORE/.mesh-generated" ]; then
  [ -f "$STORE/MEMORY.md" ] && cp "$STORE/MEMORY.md" "$STORE/MEMORY.md.pre-mesh"
  printf 'MEMORY.md is GENERATED by the memory-mesh fold.\nRevert: delete this marker (pre-flip index, if any, at MEMORY.md.pre-mesh).\n' > "$STORE/.mesh-generated"
fi
[ -f "$STORE/MEMORY.md.pre-mesh" ] && python3 "$CODE/backfill.py" --commit || true
python3 "$CODE/fold.py" || true
echo "== install complete on $ME — view.version: $(cat "$EVENTS/view.version" 2>/dev/null || echo none) =="
