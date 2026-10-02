#!/usr/bin/env python3
"""memory-mesh home watcher: hash registered canonical homes (mesh.toml `[[homes]]`).

Emits an `update-pointer` event when a home changes or goes missing, superseding
the previous notice. Every host watches; a host that sees a peer's notice with
the new digest adopts it silently. First sight seeds the hash; state advances
only after a confirmed emit, so a failed emit retries next run.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

STATE = M.MESH_ROOT / "state" / "home-hashes.json"


def digest_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"


def emit_change(name, shown_path, prev, digest):
    subject = f"home/{name}"
    content = (f"canonical home {shown_path} is MISSING (was {prev[:12]})"
               if digest == "MISSING" else
               f"canonical home {shown_path} changed ({prev[:12]} → {digest[:12]}) "
               f"— memories/pointers referencing it may describe the old text")
    cmd = [sys.executable, str(M.CODE_DIR / "emit.py"),
           "--kind", "update-pointer", "--subject", subject,
           "--content", content, "--session", "home-watch",
           "--confidence", "verified-live"]
    ids = M.unsuperseded_ids(subject)
    if ids:
        cmd += ["--supersedes", ",".join(ids)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"home-watch: EMIT FAILED for {name} — change NOT recorded, "
              f"will retry next run: {(r.stderr or r.stdout).strip()[:200]}",
              file=sys.stderr)
    return r.returncode == 0


def peer_already_noticed(subject, digest):
    """True if a live event on `subject` already carries the new digest."""
    token = digest[:12] if digest != "MISSING" else "is MISSING"
    events, _ = M.read_all_events()
    live = set(M.unsuperseded_ids(subject, events))
    return any(e["id"] in live and token in e["content"]
               for e in events if e["subject"] == subject)


def main():
    cfg = M._load_toml(M.CODE_DIR / "mesh.toml")
    homes = cfg.get("homes") or []
    if not homes:
        # An empty registry is a valid state, not a failure.
        return 0
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    failed = 0
    for h in homes:
        name = h["name"]
        digest = digest_of(Path(os.path.expanduser(h["path"])))
        prev = state.get(name)
        if prev is None:
            print(f"home-watch: seeded {name} ({digest[:12]})")
        elif digest == prev:
            continue                   # steady state: silence
        elif peer_already_noticed(f"home/{name}", digest):
            print(f"home-watch: {name} changed — peer already noticed, adopting")
        elif not emit_change(name, h["path"], prev, digest):
            failed += 1
            continue                   # keep old hash — retry next run
        else:
            print(f"home-watch: {name} changed — update-pointer emitted")
        state[name] = digest
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
