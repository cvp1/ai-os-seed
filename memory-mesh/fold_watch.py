#!/usr/bin/env python3
"""fold_watch — the mesh reporting its own breakage, from outside the fold.

SEED-080 M6. A monitor cannot certify its own liveness (PRINCIPLES 21): the
fold runs on a timer, and if that timer stops, the fold is the last thing that
will say so — it is not running. Everything downstream still looks fine for a
while: MEMORY.md is on disk, the servable manifest is on disk, retrieval keeps
serving. The corpus simply stops being true, silently, and the first symptom
is an agent acting on a rule that was superseded days ago.

So this runs under the SCHEDULER, not under the fold — a different failure
domain — and the scheduler's own run log and freshness backstop are in turn
watched from outside it. Four checks, each a fact with a source:

  1. FOLD LIVENESS  — the newest mesh state file's age against the timer's
                      cadence. The fold rewrites state on every run, so an old
                      state file means the timer stopped, not that nothing
                      happened.
  2. INDEX AGE      — the served index against the newest event. An index
                      older than the log is a fold that ran and could not
                      publish, which looks identical to health from a session.
  3. HOOK WIRING    — the hook entries the install receipt says were approved,
                      still present in settings.json. An agent cannot notice
                      that its own retrieval hook stopped firing.
  4. PEERS          — each configured peer's last-seen age, where a mesh has
                      peers. Single-host meshes skip it by name, never
                      silently.

Edge-triggered (PRINCIPLES 7): silent and exit 0 when everything holds; loud
and exit 1 on the first real problem. A check that cannot run says so and does
NOT count as a pass.

    fold_watch.py                 # under the scheduler
    fold_watch.py --json
    fold_watch.py --max-age 30m   # default: 30m (the fold timer is 5m)

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402

DEFAULT_MAX_AGE = "30m"
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_age(text):
    m = re.fullmatch(r"(\d+)([smhd])", str(text).strip())
    if not m:
        raise ValueError(f"bad duration {text!r} — use 30m, 2h, 1d")
    return int(m.group(1)) * UNITS[m.group(2)]


def _newest(paths):
    best = None
    for p in paths:
        try:
            ts = p.stat().st_mtime
        except OSError:
            continue
        if best is None or ts > best[1]:
            best = (p, ts)
    return best


def check_fold_liveness(max_age):
    state = M.MESH_ROOT / "state"
    if not state.is_dir():
        return ("fold", "UNKNOWN",
                f"no mesh state directory at {state} — the fold has never run here")
    newest = _newest(state.glob("*.json"))
    if newest is None:
        return ("fold", "UNKNOWN", f"no state files under {state}")
    path, ts = newest
    age = time.time() - ts
    if age > max_age:
        return ("fold", "FAIL",
                f"the fold has not run for {int(age // 60)}m (newest state "
                f"{path.name}, cadence is 5m) — the timer is stopped, and "
                f"everything downstream still looks fine")
    return ("fold", "OK", f"last fold {int(age // 60)}m ago")


def check_index_age():
    store = M.harness_store()
    if store is None:
        return ("index", "SKIP",
                "this store is not fold-generated (no .mesh-generated marker), "
                "so no index is published here")
    index = store / "MEMORY.md"
    if not index.exists():
        return ("index", "FAIL", f"{index} is missing but the store is marked "
                                 f"fold-generated — nothing is being served")
    events = _newest((M.MESH_ROOT / "events").glob("*.ndjson"))
    if events is None:
        return ("index", "OK", "no events yet")
    if events[1] > index.stat().st_mtime + 600:
        return ("index", "FAIL",
                f"the served index is older than the event log "
                f"({int((events[1] - index.stat().st_mtime) // 60)}m behind) — "
                f"the fold ran and could not publish")
    return ("index", "OK", "index is current with the log")


def check_hook_wiring():
    """What the receipt says was approved must still be wired. This is the
    check an agent cannot perform on itself: a hook that stopped firing
    produces silence, and silence is what a working hook produces too."""
    receipt = ROOT / ".cc-seed" / "receipt.json"
    if not receipt.exists():
        return ("hooks", "SKIP", f"no install receipt at {receipt} (not a seed "
                                 f"install — the fleet wires hooks by hand)")
    try:
        rec = json.loads(receipt.read_text(encoding="utf-8"))
    except ValueError as e:
        return ("hooks", "FAIL", f"receipt is not valid JSON: {e}")
    approved = ((rec.get("gated_writes") or {}).get("memory-hooks") or {})
    if not approved.get("written"):
        return ("hooks", "SKIP",
                "memory hooks were never approved on this install — M1's "
                "enforcement and M4 do not hold, by the operator's choice")
    settings = ROOT / ".claude" / "settings.json"
    text = settings.read_text(encoding="utf-8") if settings.exists() else ""
    missing = [cmd for cmds in approved.get("entries", {}).values()
               for cmd in cmds if cmd not in text]
    if missing:
        return ("hooks", "FAIL",
                f"{len(missing)} approved hook entr(ies) are no longer in "
                f"{settings}: {missing[0]}")
    return ("hooks", "OK", "every approved hook entry is still wired")


def check_peers(max_age):
    seen = M.MESH_ROOT / "state" / "last-seen.json"
    if not seen.exists():
        return ("peers", "SKIP", "single-host mesh (no peer state recorded)")
    try:
        data = json.loads(seen.read_text(encoding="utf-8"))
    except ValueError as e:
        return ("peers", "FAIL", f"peer state is not valid JSON: {e}")
    if not data:
        return ("peers", "SKIP", "single-host mesh (no peers configured)")
    now = time.time()
    stale = []
    for host, ts in data.items():
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            continue
        if age > max_age * 8:      # peers sync less often than the local fold
            stale.append(f"{host} ({int(age // 3600)}h)")
    if stale:
        return ("peers", "FAIL", "peers not seen recently: " + ", ".join(stale))
    return ("peers", "OK", f"{len(data)} peer(s) seen recently")


def run(max_age):
    checks = [check_fold_liveness(max_age), check_index_age(),
              check_hook_wiring(), check_peers(max_age)]
    bad = [c for c in checks if c[1] in ("FAIL", "UNKNOWN")]
    return checks, bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-age", default=os.environ.get("MESH_FOLD_MAX_AGE", DEFAULT_MAX_AGE))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true",
                    help="print the passing checks too (a scheduled run stays silent)")
    args = ap.parse_args(argv)
    checks, bad = run(parse_age(args.max_age))
    if args.json:
        print(json.dumps({"checks": [{"name": n, "status": s, "detail": d}
                                     for n, s, d in checks],
                          "ok": not bad}, indent=1))
        return 1 if bad else 0
    if bad:
        print("memory mesh: " + f"{len(bad)} check(s) failing")
        for name, status, detail in checks:
            if status in ("FAIL", "UNKNOWN"):
                print(f"  {status}: {name} — {detail}")
        return 1
    if args.verbose:
        for name, status, detail in checks:
            print(f"  {status}: {name} — {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
