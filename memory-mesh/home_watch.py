#!/usr/bin/env python3
"""memory-mesh home watcher — SPEC differentiator 3 (build-order phase 6).

One-home-per-fact's weak edge is a home that CHANGES or moves while memories
point at it. This timer hashes the registered canonical homes (mesh.toml
`[[homes]]`) and emits an `update-pointer` event when one changes or goes
missing — mechanical enforcement of pointer freshness instead of "sessions
should notice". Each new event supersedes the previous notice on that home,
so the generated index carries at most one change banner per home.

EVERY host watches (Grok review 4, priority 2 — a single watcher was a
coordinator in disguise): the first host to notice a change emits; the
others see a live event already carrying the new digest and adopt it
silently. A same-cycle race leaves two notices until the next change
supersedes both — visible residue, never lost signal. A home absent on a
host (partial checkouts) just seeds MISSING and stays silent.

Edge-triggered (Principle: silent in steady state): first sight of a home
seeds its hash with no event; an unchanged home emits nothing. State only
advances on a CONFIRMED emit — a failed emit leaves the old hash in place so
the change is retried next run, never latched away unreported.
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
    """First-noticer-wins: True if a live event on the subject already
    carries the new digest (any host's notice, seen via the last fetch)."""
    token = digest[:12] if digest != "MISSING" else "is MISSING"
    events, _ = M.read_all_events()
    live = set(M.unsuperseded_ids(subject, events))
    return any(e["id"] in live and token in e["content"]
               for e in events if e["subject"] == subject)


def main():
    cfg = M._load_toml(M.CODE_DIR / "mesh.toml")
    homes = cfg.get("homes") or []
    if not homes:
        # Nothing registered = nothing to watch — a valid solo/starter state
        # (seed installs begin with an empty registry), not a failure.
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
