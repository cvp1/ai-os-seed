#!/usr/bin/env python3
"""memory-mesh consumer — fetch peers (FF-guarded), merge, fold, materialize.

Timer-driven on every host + on demand (the emit nudge starts this unit).
Edge-triggered output: prints FINDINGS and pages only when the parked set
CHANGES; a steady-state fold is silent (exit 0, no output).

Exit codes: 0 ok (incl. found-work with FINDINGS line), 1 real breakage.
"""
import argparse
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

TG_ENV = os.path.expanduser(
    os.environ.get("TELEGRAM_ENV_PATH", "~/.claude/channels/telegram/.env"))
# No hardcoded default: an operator's chat ID is theirs to set, not ours to ship.
# Set OWNER_TG_CHAT_ID in the unit/environment that runs this job.
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
    """Send `text` to Craig's Telegram; True on confirmed delivery, False
    otherwise (never raises — a notify failure must not fail the fold).
    Fleet convention: each self-notifying job owns its own small copy of this
    helper (per connector-drift/notify.py, which checked for a shared _lib
    module before writing its copy). NO parse_mode: memory slugs carry
    underscores/hyphens, and Telegram's legacy Markdown parser 400s on odd
    underscore counts (live-tested by connector-drift 2026-08-06)."""
    tok = _tg_token()
    if not tok:
        print("fold: no telegram token — held-residency notice stays in the "
              "journal:\n" + text, file=sys.stderr)
        return False
    if not TG_CHAT_ID:
        print("fold: no OWNER_TG_CHAT_ID set — held-residency notice stays "
              "in the journal:\n" + text, file=sys.stderr)
        return False
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
    """Edge-triggered operator notification for a HELD residency delta.

    Origin (R1, audits/2026-08-05-continuous-verification): the residency gate
    held a +2-row delta on every 6-minute fold from 2026-08-02 to 2026-08-05
    and the only trace was the systemd journal — three days of every session
    loading a stale index. The gate is correct
    (decisions/residency-autonomy-2026-07-31.md); the silence was the defect.
    One Telegram when a delta BECOMES held (keyed by its row content, so a
    changed delta re-pages), one reminder per 24h while it stays held, state
    cleared the moment the hold clears.
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
    """While a delta is held, the live MEMORY.md keeps serving pre-hold rows.
    Put that fact in the file's own header so a session reading it knows it is
    stale, instead of trusting a fresh-looking index (R1 part b —
    [[no-data-must-not-render-as-positive-data]] applied to the index itself).
    Idempotent; the marker vanishes on the next full write because that
    regenerates the whole file. Skips rather than breaching the loader
    ceiling: a truncated index is worse than an unmarked one.
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
        refs = [ln for ln in M.git(
            "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{host}",
            check=False).splitlines() if ln.strip() and ln != f"{host}/HEAD"]
        if len(refs) != 1:
            notes.append(f"{host}: expected exactly one branch (single-writer "
                         f"invariant), found {refs or 'none'}")
            continue
        sha = M.git("rev-parse", refs[0], check=False).strip()
        if not sha:
            notes.append(f"{host}: no ref yet")
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


def confirm_residency_promote(assume_yes):
    """Show the staged residency change and get a real yes. Returns True to go.

    Origin, 2026-08-01: `--promote-residency` published a held residency delta
    from a MISTYPED flag. `--promote-residenc` is an unambiguous argparse
    prefix, so it did not fail — it ran. Nothing in the flow required that a
    human had ever opened MEMORY.md.staged.diff, so the gate collected
    PRESENCE, not consent (Principle 17): the staging machinery held the delta,
    wrote the diff, refused to publish on its own — and then handed all of that
    authority to one unconfirmed flag.

    Fails CLOSED with no tty. A promote is the operator's act by definition, so
    "nobody is here to read it" resolves to NO — never to a silent yes. That is
    what keeps a timer, a hook or a headless agent from promoting residency.
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
    # allow_abbrev=False so a TRUNCATED flag fails instead of silently
    # resolving. `--promote-residenc` is an unambiguous prefix of
    # `--promote-residency`, so argparse accepted it and published a held
    # residency delta on 2026-08-01. A mangled paste should be
    # distinguishable from a deliberate command; the confirmation gate is the
    # real control, this is the cheap second layer.
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

    # SPEC v4 projection (A1/A3). DETECTION by default: writing the store is a
    # mutation of Craig's memory, so the timer-driven fold reports and the
    # operator flips it on deliberately with --project. Creation of a missing
    # file is safe (nothing to lose); overwrite of a divergent one is not, and
    # both travel behind the same flag rather than splitting the risk into a
    # flag nobody remembers is half-on.
    proj = M.project_store(fold, M.harness_store(), apply=args.project)
    if proj["created"] or proj["repaired"]:
        verb = "projected" if args.project else "WOULD project (run with --project)"
        print(f"projection: {verb} — {len(proj['created'])} created, "
              f"{len(proj['repaired'])} repaired")
    alarms.extend(proj["alarms"])

    # SPEC v4 SHADOW render: what MEMORY.md becomes under declared residency,
    # written beside the live one and serving nobody. The migration plan gates
    # the flip on reviewing this diff for >=3 days (Grok round 1 named the
    # retag as the step most likely to corrupt silently), and a shadow that
    # nobody can diff is not a gate. While everything is undeclared this is
    # near-identical by construction — which is the point: the divergence
    # appears only as Craig declares.
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

    # Cutover phase 7: on hosts that opted in (.mesh-generated marker in the
    # store), the harness-loaded MEMORY.md is regenerated from this fold.
    # Its write gate reports compaction, an unfittable index, or a write
    # failure; those join the edge-triggered alarm picture below rather than
    # being printed into the victim session's own context.
    harness = M.write_harness_memory(
        fold, allow_residency_delta=args.promote_residency)
    alarms.extend(harness.get("alarms", []))

    # R1: a held promotion is a decision waiting on the operator — page him
    # and stamp the live index as stale, instead of leaving both facts in the
    # journal. Neither may ever fail the fold.
    try:
        notify_held_residency(harness)
        mark_live_index_held(harness)
    except Exception as e:  # noqa: BLE001
        alarms.append(f"held-residency notify/mark failed: {e}")

    # One fact, one answer, on both surfaces (Craig's ruling 2026-07-30: "if I
    # promote it, that must be fact everywhere"). The store's quarantine list is
    # a projection of this fold, published beside the index by the same writer on
    # the same timer — so a promotion clears it and an untrusted write adds to it
    # without anyone maintaining a second list.
    alarms.extend(M.write_store_quarantine(fold).get("alarms", []))

    # The RETRIEVAL tier's copy of the same verdict. Published by the same
    # writer on the same timer as the index and the quarantine list, so all
    # three delivery surfaces agree by construction rather than by discipline.
    alarms.extend(M.write_servable_manifest(fold).get("alarms", []))

    # The frontmatter join still needs checking, and this stays DETECTION only:
    # per-file `lineage:` is a fact with an owner (memory_write.py), and a fold
    # that silently edited facts to match its own view would be the same mistake
    # pointed the other way.
    if M.harness_store():
        alarms.extend("quarantine drift: " + d
                      for d in M.store_quarantine_drift(fold))

    # Projection drift: events and store files each carry a one-line essence,
    # and the 2026-07-31 stumps proved nothing compared them. DETECTION only,
    # by subject so the edge trigger fires on the SET changing, not the count —
    # a repair and a fresh drift can cancel out numerically.
    drift = M.projection_drift(fold, M.harness_store())

    # Edge trigger: page only when the parked/alarm picture CHANGES.
    edge_f = M.MESH_ROOT / "state" / "alert-edge.json"
    # Quarantined ids join the edge state by ID, not by count: a promotion and a
    # fresh untrusted write can cancel out numerically, and "the held-back set
    # changed" is the thing worth telling the operator about.
    now_state = {"parked": sorted(fold["parked"]), "alarms": sorted(alarms),
                 "problems": sorted(problems),
                 "quarantined": sorted(e["id"] for e in fold["quarantined"]),
                 "unnormalized": len(fold["unnormalized"]),
                 "drift": {k: sorted(v) for k, v in drift.items()}}
    prev = json.loads(edge_f.read_text()) if edge_f.exists() else None
    edge_f.write_text(json.dumps(now_state, indent=1))

    drifted = any(drift.values()) and (
        prev is None or prev.get("drift") != now_state["drift"])
    changed = prev != now_state
    if drifted:
        # The 62 legacy stumps make file_richer chronically non-empty, so the
        # summary names the DELTA classes; the full sets live in the edge state.
        print(f"[DRIFT  ] event/file essence drift changed: "
              f"{len(drift['file_richer'])} file-richer, "
              f"{len(drift['event_richer'])} event-richer, "
              f"{len(drift['disjoint'])} disjoint "
              f"(sets in state/alert-edge.json; file-richer = a producer "
              f"read a derivative, or a legacy stump awaiting repair)")
    if changed and (now_state["parked"] or now_state["quarantined"]
                    or alarms or problems):
        print(f"FINDINGS: {len(fold['parked'])} parked subject(s), "
              f"{len(fold['quarantined'])} quarantined, "
              f"{len(alarms)} alarm(s), {len(problems)} log problem(s)")
        for s in fold["parked"]:
            print(f"[PARKED ] {s} — opposing/inconsistent claims; see CONFLICTS.md")
        for e in fold["quarantined"]:
            print(f"[QUARANT] {e['subject']} (id {e['id']}) — untrusted lineage, "
                  f"NOT served; promote with sign.py --promote {e['id']}")
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
