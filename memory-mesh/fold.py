#!/usr/bin/env python3
"""memory-mesh consumer: fetch peers (fast-forward guarded), merge, fold, materialize views.

Runs on a timer and on demand. Prints FINDINGS only when the alarm picture
changes (or a standing alarm resurfaces); steady state is silent.
Exit codes: 0 ok (including FINDINGS), 1 real breakage.
"""
import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

# Days a standing alarm may stay silent before it is reported again.
RESURFACE_DAYS = 7

TG_ENV = os.path.expanduser(
    os.environ.get("TELEGRAM_ENV_PATH", "~/.claude/channels/telegram/.env"))
# No default chat ID; set OWNER_TG_CHAT_ID in the environment that runs this job.
TG_CHAT_ID = os.environ.get("OWNER_TG_CHAT_ID")
HELD_MARK = ("# STALE-INDEX: a residency delta is HELD awaiting "
             "`fold.py --promote-residency`")


def _tg_token():
    try:
        with open(TG_ENV) as fh:
            for line in fh:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


def _tg_push(text):
    """Send `text` to the owner's Telegram; True on confirmed delivery, never raises.

    No parse_mode: slugs contain underscores that break Telegram's Markdown parser.
    """
    tok = _tg_token()
    if not tok:
        print("fold: no telegram token — held-residency notice stays in the "
              "journal:\n" + text, file=sys.stderr)
        return False
    if not TG_CHAT_ID:
        print("fold: no OWNER_TG_CHAT_ID set — held-residency notice stays "
              "in the journal:\n" + text, file=sys.stderr)
        return False
    # ontology: direct-telegram — this file ships without a default chat id;
    # the shared helper falls back to a fixed chat when none is set.
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID, "text": text,
        "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("ok", False)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print("fold: telegram push failed: %s" % e, file=sys.stderr)
        return False


def notify_held_residency(harness):
    """Notify the owner when a residency delta becomes held.

    Pages once per distinct delta, then at most once per 24h while it stays
    held; state is cleared when the hold clears.
    """
    state_f = M.MESH_ROOT / "state" / "held-notify.json"
    if harness.get("status") != "staged":
        state_f.unlink(missing_ok=True)
        return
    delta = harness.get("residency_delta") or {}
    key = {"added": sorted(delta.get("added", [])),
           "dropped": sorted(delta.get("dropped", []))}
    now = int(time.time())
    try:
        prev = json.loads(state_f.read_text())
    except (OSError, ValueError):
        prev = {}
    fresh = prev.get("key") != key
    if not fresh and now - prev.get("notified_at", 0) < 24 * 3600:
        return
    since = now if fresh else prev.get("since", now)
    head = ("memory-fold: residency delta HELD — your word needed" if fresh
            else "memory-fold: residency delta STILL held "
                 f"({(now - since) // 86400}d) — your word needed")
    lines = [head,
             f"+{len(key['added'])} / -{len(key['dropped'])} always-on rows:"]
    lines += ["  + " + s for s in key["added"]]
    lines += ["  - " + s for s in key["dropped"]]
    lines += ["Every session loads the pre-hold index until you decide.",
              "Run: python3 ~/{{REDACTED}}/memory-mesh/fold.py "
              "--promote-residency"]
    _tg_push("\n".join(lines)[:3500])
    state_f.parent.mkdir(parents=True, exist_ok=True)
    state_f.write_text(json.dumps(
        {"key": key, "since": since, "notified_at": now}, indent=1))


def mark_live_index_held(harness):
    """Stamp the live MEMORY.md header as stale while a residency delta is held.

    Idempotent; the next full write removes the marker. Skipped if adding it
    would exceed the loader ceiling.
    """
    if harness.get("status") != "staged":
        return
    store = M.harness_store()
    live = (store / "MEMORY.md") if store else None
    if live is None or not live.exists():
        return
    text = live.read_text(encoding="utf-8")
    if HELD_MARK in text:
        return
    d = harness.get("residency_delta") or {}
    mark = (f"{HELD_MARK} (+{len(d.get('added', []))} / "
            f"-{len(d.get('dropped', []))} rows staged)\n")
    lines = text.splitlines(keepends=True)
    out = "".join([lines[0], mark] + lines[1:]) if lines else mark
    if (len(out.encode("utf-8")) >= M.LOADER_BYTE_CEILING
            or out.count("\n") > M.LOADER_LINE_CEILING):
        return
    tmp = live.parent / f"MEMORY.md.tmp.{os.getpid()}"
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, live)


def _peer_unreachable(host, timeout=3.0):
    """Return a short reason if the peer's git transport is not answering, else None.

    Resolves the remote URL, the ssh alias via `ssh -G`, then tries one TCP
    connect. Any resolution failure counts as unreachable.
    """
    import socket
    url = M.git("remote", "get-url", host, check=False).strip()
    if not url:
        return "no remote url"
    # A filesystem remote has no transport to probe.
    if url.startswith(("/", ".", "file://")) or ":" not in url.split("/", 1)[0]:
        return None
    sshhost = url.split(":", 1)[0] if "://" not in url else url.split("://", 1)[1].split("/", 1)[0]
    if "@" in sshhost:
        sshhost = sshhost.split("@", 1)[1]
    hostname, port = sshhost, 22
    try:
        cfg = subprocess.run(["ssh", "-G", sshhost], capture_output=True, text=True, timeout=5).stdout
        for ln in cfg.splitlines():
            k, _, v = ln.partition(" ")
            if k == "hostname" and v:
                hostname = v
            elif k == "port" and v.isdigit():
                port = int(v)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ssh -G failed: {e}"
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return None
    except OSError as e:
        return f"{hostname}:{port} {e.strerror or e}"


def fetch_peers():
    """Fetch and merge each peer (the events repo's git remotes) with a fast-forward guard.

    A peer that rewrote history is refused, never merged. Refs are compared by
    full path because `%(refname:short)` renders the HEAD symref as the bare
    remote name. Every non-ok condition goes to `alarms`. The merge must not be
    `--ff-only`: concurrent divergent writes across hosts need a real merge.

    Returns (notes, alarms).
    """
    notes, alarms = [], []
    state_f = M.MESH_ROOT / "state" / "last-seen.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}

    def _last_sha(entry):
        """Return the SHA from a last-seen entry (legacy bare string or {"sha", "ts"})."""
        if isinstance(entry, dict):
            return entry.get("sha")
        return entry
    remotes = [r for r in M.git("remote", check=False).split() if r]
    for host in remotes:
        # Probe first so a sleeping peer is skipped quickly instead of
        # timing out the whole fold.
        why = _peer_unreachable(host)
        if why:
            alarms.append(f"{host}: unreachable ({why}) — skipped")
            continue
        try:
            r = subprocess.run(["git", "-C", str(M.MESH_ROOT), "fetch", "-q", host],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            alarms.append(f"{host}: fetch timed out (60s) — skipped")
            continue
        if r.returncode != 0:
            alarms.append(f"{host}: unreachable ({r.stderr.strip()[:80]})")
            continue
        head_ref = f"refs/remotes/{host}/HEAD"
        refs = [ln for ln in M.git(
            "for-each-ref", "--format=%(refname)", f"refs/remotes/{host}",
            check=False).splitlines() if ln.strip() and ln != head_ref]
        if len(refs) != 1:
            alarms.append(f"{host}: expected exactly one branch (single-writer "
                          f"invariant), found {refs or 'none'}")
            continue
        sha = M.git("rev-parse", refs[0], check=False).strip()
        if not sha:
            alarms.append(f"{host}: no ref yet")
            continue
        last = _last_sha(state.get(host))
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
        state[host] = {"sha": sha, "ts": int(time.time())}
        notes.append(f"{host}: ok @{sha[:8]}")
    state_f.parent.mkdir(parents=True, exist_ok=True)
    state_f.write_text(json.dumps(state, indent=1))
    return notes, alarms


def bodyless_promoted_rows(diff_text, store):
    """Return slugs added by the staged residency diff that have no body file in `store`.

    Reads the diff directly because it runs before the fold.
    """
    missing = []
    for line in (diff_text or "").splitlines():
        line = line.strip()
        if not line.startswith("+ lesson/"):
            continue
        slug = line[len("+ lesson/"):].strip()
        # Subject becomes a path: reject separators and dot segments.
        if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
            continue
        if not (store / f"{slug}.md").exists():
            missing.append(slug)
    return missing


def confirm_residency_promote(assume_yes):
    """Show the staged residency change and ask for confirmation; True to proceed.

    Fails closed with no tty unless `assume_yes`.
    """
    store = M.harness_store()
    diff = store / "MEMORY.md.staged.diff" if store else None
    if diff is None or not diff.exists():
        print("fold: nothing staged — no residency change to promote.",
              file=sys.stderr)
        return False
    print(diff.read_text(encoding="utf-8").rstrip())
    live = store / "MEMORY.md"
    staged = store / "MEMORY.md.staged"
    if live.exists() and staged.exists():
        a, b = len(live.read_bytes()), len(staged.read_bytes())
        print(f"\n  file: {a:,} B -> {b:,} B ({b - a:+,})")
    bodyless = bodyless_promoted_rows(diff.read_text(encoding="utf-8"), store)
    if bodyless:
        print(f"\n  WARNING: {len(bodyless)} of these rows have NO body file on "
              f"this host. They will publish as index rows whose memory cannot "
              f"be read (/recall returns nothing). Materialise them with: "
              f"fold.py --project")
        for s in bodyless[:10]:
            print(f"    - {s}")
        if len(bodyless) > 10:
            print(f"    … and {len(bodyless) - 10} more")
    if assume_yes:
        print("\n--yes: promoting without confirmation.")
        return True
    if not sys.stdin.isatty():
        print("\nfold: REFUSING — residency is the operator's data and there "
              "is no tty to confirm on. Re-run interactively, or pass --yes "
              "if this is a reviewed scripted promote.", file=sys.stderr)
        return False
    try:
        ans = input("\npromote this residency change? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans != "y":
        print("fold: not promoted. The staged change is still on disk.")
        return False
    return True


def main():
    # allow_abbrev=False: a truncated flag must fail, not resolve to --promote-residency.
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--project", action="store_true",
                    help="APPLY the SPEC-v4 store projection (create missing "
                         "files, rewrite files that diverge from their event). "
                         "Default is detect-and-report.")
    ap.add_argument("--promote-residency", action="store_true",
                    help="ACCEPT the staged always-on residency change (rows "
                         "added/dropped) and publish it live. The timer can "
                         "never do this: residency is the operator's data, so "
                         "the promote is a separate human act. Prints the "
                         "staged diff and asks for confirmation.")
    ap.add_argument("--yes", action="store_true",
                    help="skip the promote confirmation. For a reviewed, "
                         "scripted promote only — it forfeits the display "
                         "that makes the approval meaningful.")
    args = ap.parse_args()
    if args.promote_residency and not confirm_residency_promote(args.yes):
        return 1
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

    # Store projection: report only, unless --project applies it.
    proj = M.project_store(fold, M.harness_store(), apply=args.project,
                            events=events)
    if proj["created"] or proj["repaired"]:
        verb = "projected" if args.project else "WOULD project (run with --project)"
        print(f"projection: {verb} — {len(proj['created'])} created, "
              f"{len(proj['repaired'])} repaired")
    alarms.extend(proj["alarms"])

    # Shadow render: MEMORY.md under declared residency, written beside the
    # live index for diffing; serves nothing.
    store = M.harness_store()
    if store:
        try:
            shadow, srep = M.render_harness_memory_v4(fold, store)
            (store / "MEMORY.md.shadow").write_text(shadow, encoding="utf-8")
            if srep["always_on"] or srep["on_demand"]:
                print(f"shadow: {srep['always_on']} always-on, "
                      f"{srep['on_demand']} on-demand, {srep['undeclared']} "
                      f"undeclared, {srep['expired_hidden']} expiry-hidden "
                      f"({srep['rows']}/{srep['rows_total']} rows fit)")
        except Exception as e:                       # never let the shadow
            alarms.append(f"shadow render failed: {e}")   # break the live fold

    # Project tier events into `_index-exclude.txt`, then regenerate the
    # harness MEMORY.md (opted-in hosts only). Always-on changes still pass
    # the residency gate; write problems become alarms.
    if store:
        try:
            added, removed = M.project_index_exclude(fold, store, apply=True)
            if added or removed:
                print(f"tiers: _index-exclude.txt +{len(added)} -{len(removed)} "
                      f"from tier events")
        except Exception as e:  # noqa: BLE001 — never break the live fold
            alarms.append(f"tier projection failed: {e}")
    harness = M.write_harness_memory(
        fold, allow_residency_delta=args.promote_residency)
    alarms.extend(harness.get("alarms", []))

    # Notify and stamp the live index when a residency delta is held; never fail the fold.
    try:
        notify_held_residency(harness)
        mark_live_index_held(harness)
    except Exception as e:  # noqa: BLE001
        alarms.append(f"held-residency notify/mark failed: {e}")

    # Publish the store's quarantine list as a projection of this fold.
    alarms.extend(M.write_store_quarantine(fold).get("alarms", []))

    # Publish the retrieval tier's servable manifest from the same fold.
    alarms.extend(M.write_servable_manifest(fold).get("alarms", []))

    # Detect (never fix) drift between per-file `lineage:` and the fold's quarantine view.
    if M.harness_store():
        alarms.extend("quarantine drift: " + d
                      for d in M.store_quarantine_drift(fold))

    # Detect event/file essence drift, keyed by subject so the edge trigger
    # fires on set changes rather than counts.
    drift = M.projection_drift(fold, M.harness_store())

    # Edge trigger: page only when the parked/alarm picture CHANGES.
    edge_f = M.MESH_ROOT / "state" / "alert-edge.json"
    # Quarantined entries are tracked by id so offsetting changes still register.
    now_state = {"parked": sorted(fold["parked"]), "alarms": sorted(alarms),
                 "problems": sorted(problems),
                 "quarantined": sorted(e["id"] for e in fold["quarantined"]),
                 "unnormalized": len(fold["unnormalized"]),
                 "drift": {k: sorted(v) for k, v in drift.items()}}
    prev = json.loads(edge_f.read_text()) if edge_f.exists() else None

    # Decay: a standing alarm is re-reported every RESURFACE_DAYS while it persists.
    today = _dt.date.today()
    first_seen = dict((prev or {}).get("first_seen", {}))
    last_reported = dict((prev or {}).get("last_reported", {}))
    for a in alarms:
        first_seen.setdefault(a, today.isoformat())

    def _aged(a):
        ref = last_reported.get(a) or first_seen.get(a)
        try:
            return (today - _dt.date.fromisoformat(ref)).days
        except (TypeError, ValueError):
            return 0

    due = [a for a in alarms if _aged(a) >= RESURFACE_DAYS]
    # Forget cleared alarms so a recurrence starts a fresh clock.
    live_alarms = set(alarms)
    first_seen = {k: v for k, v in first_seen.items() if k in live_alarms}
    last_reported = {k: v for k, v in last_reported.items() if k in live_alarms}

    drifted = any(drift.values()) and (
        prev is None or prev.get("drift") != now_state["drift"])
    changed = {k: v for k, v in (prev or {}).items()
               if k not in ("first_seen", "last_reported")} != now_state \
        if prev is not None else True
    if drifted:
        # Summary counts only; full sets live in the edge state file.
        print(f"[DRIFT  ] event/file essence drift changed: "
              f"{len(drift['file_richer'])} file-richer, "
              f"{len(drift['event_richer'])} event-richer, "
              f"{len(drift['disjoint'])} disjoint "
              f"(sets in state/alert-edge.json; file-richer = a producer "
              f"read a derivative, or a legacy stump awaiting repair)")
    if (changed or due) and (now_state["parked"] or now_state["quarantined"]
                             or alarms or problems):
        if due and not changed:
            print(f"[STANDING] {len(due)} alarm(s) unfixed for "
                  f"{RESURFACE_DAYS}+ days — resurfaced on decay, nothing "
                  f"changed since the last report. Fix or park them "
                  f"deliberately; they will return again in {RESURFACE_DAYS} "
                  f"days for as long as they are true.")
        # Anything reported now restarts its decay clock.
        for a in alarms:
            last_reported[a] = today.isoformat()
        print(f"FINDINGS: {len(fold['parked'])} parked subject(s), "
              f"{len(fold['quarantined'])} quarantined, "
              f"{len(alarms)} alarm(s), {len(problems)} log problem(s)")
        for s in fold["parked"]:
            print(f"[PARKED ] {s} — opposing/inconsistent claims; see CONFLICTS.md")
        for e in fold["quarantined"]:
            print(f"[QUARANT] {e['subject']} (id {e['id']}) — untrusted lineage, "
                  f"NOT served; promote with python3 ~/{{REDACTED}}/memory-mesh/sign.py --promote {e['id']}")
        for a in alarms:
            print(f"[ALARM  ] {a}")
        for p in problems:
            print(f"[LOG    ] {p}")

    # Written after the report so last_reported reflects output actually printed.
    edge_f.write_text(json.dumps({**now_state, "first_seen": first_seen,
                                  "last_reported": last_reported}, indent=1))
    # Views and state are never committed: they are per-host derived files and
    # would conflict on merge. Replay regenerates any past view.
    return 0


if __name__ == "__main__":
    sys.exit(main())
