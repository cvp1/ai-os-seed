#!/usr/bin/env python3
"""memory-mesh consumer — fetch peers (FF-guarded), merge, fold, materialize.

Timer-driven on every host + on demand (the emit nudge starts this unit).
Edge-triggered output: prints FINDINGS and pages only when the parked set
CHANGES; a steady-state fold is silent (exit 0, no output).

Exit codes: 0 ok (incl. found-work with FINDINGS line), 1 real breakage.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M


def fetch_peers():
    """git fetch each peer with the fast-forward guard (SPEC transport).
    A peer that rewrote history gets REFUSED, loudly — never merged.
    Peers = the events repo's own git remotes (the remote list IS the mesh
    membership on the consumer side; mesh.toml serves the producer's ssh
    nudges). Drills wire local-path remotes — identical git mechanics."""
    notes, alarms = [], []
    state_f = M.MESH_ROOT / "state" / "last-seen.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    remotes = [r for r in M.git("remote", check=False).split() if r]
    for host in remotes:
        r = subprocess.run(["git", "-C", str(M.MESH_ROOT), "fetch", "-q", host],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            notes.append(f"{host}: unreachable ({r.stderr.strip()[:80]})")
            continue
        sha = M.git("rev-parse", f"{host}/master", check=False).strip()
        if not sha:
            notes.append(f"{host}: no master ref yet")
            continue
        last = state.get(host)
        if last:
            anc = subprocess.run(
                ["git", "-C", str(M.MESH_ROOT), "merge-base",
                 "--is-ancestor", last, sha], capture_output=True)
            if anc.returncode != 0:
                alarms.append(f"NON-FAST-FORWARD from {host}: {last[:8]} !> "
                              f"{sha[:8]} — history rewritten, REFUSING merge")
                continue
        merge = subprocess.run(
            ["git", "-C", str(M.MESH_ROOT), "merge", "-q", "--no-edit", sha],
            capture_output=True, text=True, timeout=60)
        if merge.returncode != 0:
            M.git("merge", "--abort", check=False)
            alarms.append(f"MERGE CONFLICT with {host} — single-writer "
                          f"invariant broke: {merge.stderr.strip()[:120]}")
            continue
        state[host] = sha
        notes.append(f"{host}: ok @{sha[:8]}")
    state_f.parent.mkdir(parents=True, exist_ok=True)
    state_f.write_text(json.dumps(state, indent=1))
    return notes, alarms


def main():
    if not (M.MESH_ROOT / ".git").is_dir():
        print(f"fold: no events repo at {M.MESH_ROOT}", file=sys.stderr)
        return 1
    notes, alarms = fetch_peers()

    reg = M.load_registry()
    events, problems = M.read_all_events()
    fold = M.fold_events(events, reg)
    alarms.extend(fold["alarms"])

    for aud in sorted(M.VIEW_INCLUDES):
        outdir = M.MESH_ROOT / "views" / aud
        outdir.mkdir(parents=True, exist_ok=True)
        for name, text in M.render_views(fold, aud).items():
            (outdir / name).write_text(text)
    version = M.view_version(fold)
    (M.MESH_ROOT / "view.version").write_text(version + "\n")

    # Cutover phase 7: on hosts that opted in (.mesh-generated marker in the
    # store), the harness-loaded MEMORY.md is regenerated from this fold.
    M.write_harness_memory(fold)

    # Edge trigger: page only when the parked/alarm picture CHANGES.
    edge_f = M.MESH_ROOT / "state" / "alert-edge.json"
    now_state = {"parked": sorted(fold["parked"]), "alarms": sorted(alarms),
                 "problems": sorted(problems),
                 "unnormalized": len(fold["unnormalized"])}
    prev = json.loads(edge_f.read_text()) if edge_f.exists() else None
    edge_f.write_text(json.dumps(now_state, indent=1))

    changed = prev != now_state
    if changed and (now_state["parked"] or alarms or problems):
        print(f"FINDINGS: {len(fold['parked'])} parked subject(s), "
              f"{len(alarms)} alarm(s), {len(problems)} log problem(s)")
        for s in fold["parked"]:
            print(f"[PARKED ] {s} — opposing/inconsistent claims; see CONFLICTS.md")
        for a in alarms:
            print(f"[ALARM  ] {a}")
        for p in problems:
            print(f"[LOG    ] {p}")
    # Views/state are NEVER committed: they are derived, per-host, and every
    # host writes the same paths — committing them would make the fold itself
    # violate the single-writer invariant (drill 1 caught exactly this on the
    # first run: three hosts merging each other's INDEX.md = guaranteed
    # conflict). History holds events only; replay regenerates any past view.
    return 0


if __name__ == "__main__":
    sys.exit(main())
