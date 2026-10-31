#!/usr/bin/env python3
"""fold_watch: check mesh health from outside the fold (run under the scheduler).

Checks: fold liveness (newest state file age), index age vs newest event,
approved hook entries still wired in settings.json, and peer last-seen ages.
Silent with exit 0 when all hold; exit 1 on FAIL or UNKNOWN.

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
    # A residency delta held for the operator freezes the index ON PURPOSE
    # (the fold pages the owner daily about it). That is a decision waiting,
    # not a publish failure — name it, don't cry wolf.
    try:
        with open(index, encoding="utf-8") as fh:
            head = fh.read(600)
    except OSError:
        head = ""
    if "residency delta is HELD" in head:
        return _check_hold(store)
    events = _newest((M.MESH_ROOT / "events").glob("*.ndjson"))
    if events is None:
        return ("index", "OK", "no events yet")
    if events[1] > index.stat().st_mtime + 600:
        return ("index", "FAIL",
                f"the served index is older than the event log "
                f"({int((events[1] - index.stat().st_mtime) // 60)}m behind) — "
                f"the fold ran and could not publish")
    return ("index", "OK", "index is current with the log")


HOLD_MAX_DAYS = 7


def _check_hold(store):
    """A held residency delta is a decision waiting, not a publish failure —
    but only while it is genuinely staged, recent, and the owner was told.
    Each guard closes a false green found by the 2026-10-27 panel."""
    staged = store / "MEMORY.md.staged"
    if not staged.exists():
        return ("index", "FAIL",
                "MEMORY.md says a residency delta is HELD but nothing is staged "
                f"({staged} missing) — the header is stale; the fold is not republishing")
    try:
        st = json.loads((M.MESH_ROOT / "state" / "held-notify.json")
                        .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("index", "FAIL",
                "a residency delta is HELD but no hold-notify state exists — "
                "the owner is not being paged about it")
    days = (time.time() - float(st.get("since") or time.time())) / 86400
    if st.get("delivered") is False:
        return ("index", "FAIL",
                f"a residency delta has been HELD {days:.1f}d and the last page "
                "to the owner was NOT delivered — nobody knows the index is frozen")
    if days > HOLD_MAX_DAYS:
        return ("index", "FAIL",
                f"a residency delta has been HELD {days:.0f}d (> {HOLD_MAX_DAYS}d) — "
                f"review {store / 'MEMORY.md.staged.diff'}, then run fold.py "
                "--promote-residency")
    return ("index", "HELD",
            f"the index is frozen by design for {days:.1f}d: a residency delta "
            "awaits the operator's word (paged). Review "
            f"{store / 'MEMORY.md.staged.diff'}, then run fold.py --promote-residency")


# Approved hooks retired BY DESIGN, keyed (event, script path suffix). A
# retired hook is reported in the check's detail, never silently dropped.
RETIRED_HOOKS = {
    ("PostToolUse", "memory-mesh/retrieve.py"):
        "2026-10-17 (ai-os 0e5b978): plain stdout after a tool never reaches "
        "the model; it cost ~110 ms per tool call",
    ("PreToolUse", "memory-mesh/hooks/memory-fresh.py"):
        "2026-10-17 (ai-os 0e5b978): kicked a launchd label that does not "
        "exist; the fold runs every 5 min",
}
SCRIPT_RX = re.compile(r"(\S+\.py)\b")


def _settings_files():
    """Project settings first, then user-global (where the write guard lives
    since 2026-10-17 so sessions outside the workspace are guarded too)."""
    return [ROOT / ".claude" / "settings.json",
            Path(os.path.expanduser("~")) / ".claude" / "settings.json"]


def _wired_commands(doc, wired):
    for event, entries in (doc.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            for h in (e.get("hooks") or []):
                if isinstance(h, dict) and h.get("command"):
                    wired.setdefault(event, set()).add(h["command"])


LEAD_KEYWORDS = ("then", "else", "do", "exec", "command")
PY_RX = re.compile(r"^(?:python|python[0-9.]*|pypy[0-9]*)$")


def _program_and_args(argv):
    """(program, args) the argv actually runs: skips VAR=val and `env`, and
    looks through a python interpreter to its script. None for -c/-m."""
    k = 0
    while k < len(argv) and (argv[k] == "env" or
                             re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[k])):
        k += 1
    if k >= len(argv):
        return None
    prog = argv[k]
    if PY_RX.match(os.path.basename(prog)):
        k += 1
        while k < len(argv) and argv[k].startswith("-"):
            if argv[k] in ("-c", "-m"):
                return None
            k += 1
        if k >= len(argv):
            return None
        prog = argv[k]
    return prog, argv[k + 1:]


def _runs(wired_cmd, approved_cmd):
    """Does `wired_cmd` RUN the approved script with the approved arguments?

    Verbatim match, or a shell segment of it (split on ; && ||, leading
    then/exec stripped) whose program — through a python interpreter — is the
    approved script and whose arguments are identical. Mentioning the path
    (`echo X`, `[ -f X ]`) does not count."""
    import shlex
    if wired_cmd == approved_cmd:
        return True
    try:
        want = _program_and_args(shlex.split(approved_cmd))
    except ValueError:
        return False
    if not want:
        return False
    for seg in re.split(r";|&&|\|\|", wired_cmd):
        try:
            argv = shlex.split(seg)
        except ValueError:
            continue
        while argv and argv[0] in LEAD_KEYWORDS:
            argv = argv[1:]
        got = _program_and_args(argv) if argv else None
        if not got or got[1] != want[1]:
            continue
        if got[0] == want[0] or os.path.realpath(got[0]) == os.path.realpath(want[0]):
            return True
    return False


def _is_wired(event, cmd, wired):
    """(wired?, problem) for one approved entry under its own event."""
    if not any(_runs(c, cmd) for c in wired.get(event, ())):
        return False, None
    import shlex
    try:
        prog = _program_and_args(shlex.split(cmd))
    except ValueError:
        prog = None
    if prog and prog[0].startswith("/") and not os.path.isfile(prog[0]):
        # A guarded wrapper (`if [ -f X ]`) silently no-ops when X is gone.
        return True, f"{event}: wired but its script is MISSING: {prog[0]}"
    return True, None


def _retired(event, cmd):
    for (ev, suffix), why in RETIRED_HOOKS.items():
        if ev == event and suffix in cmd:
            return why
    return None


def check_hook_wiring():
    """Check that hook entries approved in the install receipt are still wired
    in the project or user-global settings.json."""
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
    wired, read = {}, []
    for f in _settings_files():
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            return ("hooks", "FAIL", f"{f} is not valid JSON: {e}")
        # Parse the hooks structure rather than grepping the file text.
        if doc.get("disableAllHooks") is True:
            return ("hooks", "FAIL",
                    f"{f} sets disableAllHooks: true — every approved hook "
                    f"is present in the file and none of them runs")
        _wired_commands(doc, wired)
        read.append(str(f))
    if not read:
        return ("hooks", "FAIL",
                f"no settings.json at {settings} or user scope, but the receipt "
                f"says memory hooks are approved — nothing is wired")
    # Each command must be wired under its own event, unless retired by design.
    missing, retired, broken = [], [], []
    for event, cmds in approved.get("entries", {}).items():
        for cmd in cmds:
            ok, problem = _is_wired(event, cmd, wired)
            if problem:
                broken.append(problem)
            if ok:
                continue
            why = _retired(event, cmd)
            if why:
                retired.append(f"{event} {SCRIPT_RX.findall(cmd)[-1].rsplit('/', 1)[-1]} ({why})")
            else:
                missing.append(f"{event}: {cmd}")
    note = f"; {len(retired)} retired by design: " + "; ".join(retired) if retired else ""
    if broken:
        return ("hooks", "FAIL", "; ".join(broken) + note)
    if missing:
        return ("hooks", "FAIL",
                f"{len(missing)} approved hook entr(ies) are no longer wired in "
                f"{' or '.join(read)}: {missing[0]}{note}")
    return ("hooks", "OK", "every approved hook entry is still wired" + note)


def check_peers(max_age):
    """Judge each DECLARED peer by its mesh.toml liveness role.

    required: its last direct fetch must be within max_age*8, else FAIL —
      no relay rescue (a broken direct transport is a real fault).
    sleeps:   never fails and never counts as healthy; reported as UNKNOWN
      liveness with last fetch and newest own event, for the operator's eye.
    Roles come from mesh.toml, never from `git remote`, so deleting a remote
    cannot demote a required peer. Hosts in last-seen but not declared are
    named as undeclared and ignored."""
    try:
        roles = M.peer_roles()
    except Exception as e:  # noqa: BLE001 — a bad config must be loud
        return ("peers", "FAIL", f"mesh.toml peer roles unreadable: {e}")
    seen = M.MESH_ROOT / "state" / "last-seen.json"
    data = {}
    if seen.exists():
        try:
            data = json.loads(seen.read_text(encoding="utf-8")) or {}
        except ValueError as e:
            return ("peers", "FAIL", f"peer state is not valid JSON: {e}")
    if not roles:
        return ("peers", "SKIP", "single-host mesh (no peers declared in mesh.toml)")
    if not data:
        # last-seen is never pruned, so an empty one means this node has never
        # fetched any peer (a sandbox or a brand-new host) — not that a remote
        # was removed; a removed remote leaves its stale entry here to judge.
        return ("peers", "SKIP", "this node has never fetched a peer (sandbox or "
                                 "new host); declared: " + ", ".join(sorted(roles)))
    now = time.time()
    ok, stale, unknown, asleep = [], [], [], []
    for host, role in sorted(roles.items()):
        entry = data.get(host)
        ts = entry.get("ts") if isinstance(entry, dict) else None
        try:
            age = now - float(ts) if ts is not None else None
        except (TypeError, ValueError):
            age = None
        if role == "sleeps":
            own = _newest_own_event(host, now)
            asleep.append(
                f"{host} (sleeps; liveness UNKNOWN — last fetch "
                + (f"{int(age // 3600)}h ago" if age is not None else "never")
                + ", newest own event "
                + (f"{int((now - own) // 3600)}h ago)" if own is not None else "none)"))
            continue
        if entry is None:
            stale.append(f"{host} (declared required, never fetched here)")
        elif age is None:
            unknown.append(host)    # legacy bare SHA: no timestamp
        elif age > max_age * 8:     # peers sync less often than the local fold
            stale.append(f"{host} ({int(age // 3600)}h)")
        else:
            ok.append(host)
    extra = sorted(set(data) - set(roles))
    note = ("; " + "; ".join(asleep) if asleep else "") + \
           (f"; undeclared in mesh.toml, ignored: {', '.join(extra)}" if extra else "")
    if stale:
        return ("peers", "FAIL", "required peers not seen recently: "
                + ", ".join(stale) + note)
    if unknown:
        return ("peers", "UNKNOWN",
                "peer liveness is UNKNOWN AGE for " + ", ".join(unknown)
                + " — last-seen.json carries no timestamp for them (the "
                  "pre-2026-09-19 bare-SHA format). The next fold rewrites it; "
                  "until then this is not evidence the peer is alive." + note)
    return ("peers", "OK", f"{len(ok)} required peer(s) seen recently" + note)


FUTURE_SKEW = 600   # seconds; a later timestamp is a bad clock, not liveness


def _newest_own_event(host, now):
    """Epoch of the newest plausible event `host` wrote to its own committed
    log; None if none. Lines are parsed as JSON and must carry host == `host`;
    events dated more than FUTURE_SKEW ahead of now are REJECTED (never
    clamped — a clamped future stamp reads as fresh forever). Informational
    only: no status is ever derived from it."""
    import calendar
    blob = None
    try:
        blob = M.head_blob(f"events/{host}.ndjson")
    except Exception:  # noqa: BLE001
        blob = None
    if blob is None:
        try:
            blob = (M.MESH_ROOT / "events" / f"{host}.ndjson").read_text(encoding="utf-8")
        except OSError:
            return None
    best = None
    for line in blob.splitlines():
        try:
            ev = json.loads(line)
            if ev.get("host") != host:
                continue
            t = calendar.timegm(time.strptime(ev["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:  # noqa: BLE001
            continue
        if t > now + FUTURE_SKEW:
            continue
        if best is None or t > best:
            best = t
    return best


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
