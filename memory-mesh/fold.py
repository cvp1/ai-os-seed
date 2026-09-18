#!/usr/bin/env python3
"""memory-mesh consumer — fetch peers (FF-guarded), merge, fold, materialize.

Timer-driven on every host + on demand (the emit nudge starts this unit).
Edge-triggered output: prints FINDINGS and pages only when the parked set
CHANGES; a steady-state fold is silent (exit 0, no output).

Exit codes: 0 ok (incl. found-work with FINDINGS line), 1 real breakage.
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

# How long a standing alarm may stay silent before the fold says it again.
# Seven days: long enough that a fault fixed within the week never nags, short
# enough that nothing rots for a month unseen (the 28 half-applied promotions
# of 2026-09-12 had been silent since August).
RESURFACE_DAYS = 7

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
    # ontology: direct-telegram — fold.py SHIPS (cc-seed/dist, ai-os-seed) and
    # deliberately has no default chat id: an operator's chat is theirs to set.
    # _lib/telegram.py falls back to Craig's chat when OWNER_TG_CHAT_ID is unset,
    # so importing it would send another operator's fold notices at Craig's id
    # instead of staying silent. Deviation from HANDOFF-EXTENSIONS WP1b step 2,
    # which listed this file for migration; the seed posture wins.
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


def _peer_unreachable(host, timeout=3.0):
    """Return a short reason if the peer's git transport is not answering, else
    None. Resolves the remote URL (`<sshhost>:<path>`), then the ssh alias via
    `ssh -G` (HostName/Port), then one TCP connect. Any resolution failure is
    reported as unreachable rather than guessed (Principle 4)."""
    import socket
    url = M.git("remote", "get-url", host, check=False).strip()
    if not url:
        return "no remote url"
    # A filesystem remote (the drills' fixture peers, or a same-host clone) has
    # no transport to probe — git reads it directly.
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
    """git fetch each peer with the fast-forward guard (SPEC transport).
    A peer that rewrote history gets REFUSED, loudly — never merged.
    Peers = the events repo's own git remotes (the remote list IS the mesh
    membership on the consumer side; mesh.toml serves the producer's ssh
    nudges). Drills wire local-path remotes — identical git mechanics.

    Found 2026-08-15 ({{REDACTED}} silently stopped merging for ~29h): the ref
    filter used to compare `%(refname:short)` output against the literal
    string "{host}/HEAD" to drop the remote's HEAD symref. git's short-form
    renderer collapses `refs/remotes/<host>/HEAD` to the BARE remote name
    (e.g. "{{REDACTED}}", not "{{REDACTED}}/HEAD") — a real, verified quirk,
    not a guess (confirmed live: `for-each-ref --format=%(refname:short)
    refs/remotes/{{REDACTED}}` on the affected host printed exactly
    ["{{REDACTED}}", "{{REDACTED}}/master"]). Whether that symref exists at
    all depends on the git version/config on each host (newer git can
    auto-create it on fetch; older git doesn't) — {{REDACTED}}'s newer git had it,
    {{REDACTED}}/{{REDACTED}}'s didn't, so only {{REDACTED}} tripped the "expected
    exactly one branch" branch on every single scheduled run and silently
    skipped the merge, forever, with no signal anywhere. Fixed by comparing
    FULL ref paths (unambiguous across git versions) instead of the
    short-form rendering. `git rev-parse`/`git merge` accept a full ref path
    identically to the short form, so nothing downstream changes.

    Second, independent bug this uncovered: this function used to return
    ALL non-happy-path conditions (unreachable, wrong branch count, no ref
    yet) via a `notes` list that main() collected and then never printed or
    otherwise used anywhere — a structurally silent failure channel. That is
    exactly why the {{REDACTED}} stall produced zero signal for 29 hours despite
    the scheduled job exiting 0 every 5 minutes. Every non-"ok" condition now
    goes to `alarms` instead, which main() already surfaces (edge-triggered,
    on change) and folds into the FINDINGS printout.

    3-model panel review (grok-4.6/gpt-5.6-terra/gemini-pro-latest,
    2026-08-15) confirmed this diagnosis and both fixes unanimously. openai
    additionally suggested `--ff-only` on the merge, reasoning the log looked
    linear with zero merge commits in 857+ real events — plausible-sounding,
    WRONG: drill_1 (split-brain replay) and drill_2 (partition: emit
    everywhere, converge after) explicitly drill concurrent divergent writes
    across hosts that require a real 3-way merge to reconcile, which
    --ff-only refuses outright. Tried it, ran the drill suite, watched drills
    1/2/5/7 fail that pass clean without it, reverted. Left here as a record
    of a plausible panel suggestion that live verification (Principle 13)
    caught before it shipped — a lesson in why every suggestion still needs
    its own proof, panel-endorsed or not."""
    notes, alarms = [], []
    state_f = M.MESH_ROOT / "state" / "last-seen.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    remotes = [r for r in M.git("remote", check=False).split() if r]
    for host in remotes:
        # A sleeping peer ({{REDACTED}} naps) used to cost a 60 s TimeoutExpired
        # that killed the whole fold (3 failed runs, week to 2026-09-08). Probe
        # the transport first: no route / refused / no answer in 3 s -> skip
        # this peer, keep folding the others. The fetch timeout stays as the
        # outer bound for a peer that answers TCP but stalls git.
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


def bodyless_promoted_rows(diff_text, store):
    """Slugs the staged residency diff would publish that have NO body file in
    `store` — i.e. index rows whose memory cannot be read on this host.

    Residency and the store projection are two separate holds (this flag vs
    `--project`), and one batch of peer events routinely stages both. Promoting
    residency alone therefore publishes rows whose files were never
    materialised here. Measured 2026-09-17: a 36-row promote went live while
    all 36 bodies were missing, and nothing said so — the always-on hook text
    still renders from the index, so only `/recall` saw the hole. The display
    below is the only place an operator could have caught it (Principle 17: the
    approval must show what is actually being approved).

    Pure on purpose: `confirm_residency_promote` runs BEFORE the fold, so this
    cannot consult the projection and must read the staged diff directly.
    """
    missing = []
    for line in (diff_text or "").splitlines():
        line = line.strip()
        if not line.startswith("+ lesson/"):
            continue
        slug = line[len("+ lesson/"):].strip()
        # Same path-safety stance as project_store: this is a subject string
        # becoming a filesystem path, and the registry is not a trust boundary.
        if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
            continue
        if not (store / f"{slug}.md").exists():
            missing.append(slug)
    return missing


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

    # DECAY: an edge trigger alone cannot report a STANDING fault (2026-09-12).
    #
    # Edge-triggering is right for transitions and wrong for a condition that
    # never self-heals. A drift alarm fires once, the state goes constant, and
    # from the next run on the fault is indistinguishable from health — which is
    # how 28 half-applied promotions sat silent for a month. They surfaced only
    # because an unrelated write perturbed the state and reprinted the block.
    #
    # This is the same failure the freshness acks already forbid ("a park can
    # never become silence" — acks EXPIRE and resurface). So give the alarm the
    # same property: remember when each was first seen and last reported, and
    # force it back into the output every RESURFACE_DAYS while it persists.
    # Steady state stays silent (PRINCIPLES 7); a fault nobody fixed cannot.
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
    # Drop bookkeeping for alarms that are gone, so a repaired fault does not
    # keep a first_seen date that would make a RECURRENCE look weeks old.
    live_alarms = set(alarms)
    first_seen = {k: v for k, v in first_seen.items() if k in live_alarms}
    last_reported = {k: v for k, v in last_reported.items() if k in live_alarms}

    drifted = any(drift.values()) and (
        prev is None or prev.get("drift") != now_state["drift"])
    changed = {k: v for k, v in (prev or {}).items()
               if k not in ("first_seen", "last_reported")} != now_state \
        if prev is not None else True
    if drifted:
        # The 62 legacy stumps make file_richer chronically non-empty, so the
        # summary names the DELTA classes; the full sets live in the edge state.
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
        # Anything shown NOW restarts its decay clock — whether it appeared
        # because the picture changed or because it aged out.
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

    # Written AFTER the report, not before: last_reported is only true once the
    # printing has actually happened, and an early write would record a report
    # that a crash in between meant nobody ever saw.
    edge_f.write_text(json.dumps({**now_state, "first_seen": first_seen,
                                  "last_reported": last_reported}, indent=1))
    # Views/state are NEVER committed: they are derived, per-host, and every
    # host writes the same paths — committing them would make the fold itself
    # violate the single-writer invariant (drill 1 caught exactly this on the
    # first run: three hosts merging each other's INDEX.md = guaranteed
    # conflict). History holds events only; replay regenerates any past view.
    return 0


if __name__ == "__main__":
    sys.exit(main())
