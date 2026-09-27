#!/usr/bin/env python3
"""friction-miner — the generator that proposes generators (SPEC.md).

Weekly deterministic pass over corpora that already exist and nobody reads
— session transcripts (Craig's own typed `!` commands ONLY), the proposal
audit trail, and git hand-commit history across CC — surfacing at most ONE
automation candidate per run as an initiative opener (source
`initiative.friction`, wired via initiative/engine.py cond_friction). The
miner never builds anything: its entire output is one candidates.jsonl row;
the opener machinery owns caps, expiry, and the trust-ledger learning loop.

Hard boundaries (SPEC.md): on-host only, zero LLM, evidence is command
SHAPES and counts, never content. A command carrying an inline secret-like
assignment is dropped entirely (when in doubt, drop the candidate). The
miner reads only Craig's own actions — agent-run commands and ingested
text are never mined.

Usage:  miner.py run          weekly cron entry (edge-trigger: a no-find
                              run prints nothing)
        miner.py run --dry-run  mine + report, write nothing
        miner.py show <fp>    evidence for one candidate (the opener's
                              suggested command)
        miner.py status       candidates + dispositions, read-only

On a cc-seed install (a .cc-seed/receipt.json at the root) there is no
initiative engine: the candidate lands in
observability/data/friction-miner/candidates.jsonl (a runtime-writable path
the install audit expects), the run's FINDINGS line reaches runs.db, and
`miner.py status` is the review surface. Shell history is read from
~/.bash_history and ~/.zsh_history (zsh's extended ": <epoch>:<dur>;cmd"
lines are dated), or from FRICTION_HISTORY (os.pathsep-separated paths).

Deterministic, stdlib, /usr/bin/python3-safe.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
# A seed install keeps runtime state out of its shipped component dirs (the
# install audit hashes those); observability/data/ is where it expects writes.
SEED_INSTALL = os.path.isfile(os.path.join(CC, ".cc-seed", "receipt.json"))
STATE_PATH = os.environ.get("FRICTION_MINER_STATE") or (
    os.path.join(CC, "observability", "data", "friction-miner", "candidates.jsonl")
    if SEED_INSTALL else os.path.join(HERE, "state", "candidates.jsonl"))
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def history_paths():
    """Every shell history this operator has: FRICTION_HISTORY if set, else
    whichever of bash's and zsh's default files exist (macOS defaults to zsh —
    a bash-only reader mined nothing there; {{REDACTED}}'s fork, 2026-09-27)."""
    raw = os.environ.get("FRICTION_HISTORY")
    if raw:
        return [os.path.expanduser(p) for p in raw.split(os.pathsep) if p]
    return [p for p in (os.path.expanduser("~/.bash_history"),
                        os.path.expanduser("~/.zsh_history"))
            if os.path.isfile(p)]


HISTORY_PATH = history_paths()
AUDIT_PATH = os.path.join(CC, "proposal-feed", "state", "audit.jsonl")
CONDITIONS_PATH = os.path.join(CC, "initiative", "state", "conditions.json")

WINDOW_DAYS = 28
MIN_SESSIONS = 3        # D1/D2: distinct sessions (claude session or a dated
                        # terminal day) before a shape is friction
MIN_SHELL_HITS = 4      # D1: occurrence floor for UNDATED shell history —
                        # Craig's correction 2026-07-21: most hand work
                        # happens in a plain terminal, not via `!`; history
                        # without HISTTIMEFORMAT can't window or count days,
                        # and ignoredups collapses repeats, so raw counts
                        # already undercount
MIN_EDITS = 3           # D3: same-way edits before a generator is suspect
MIN_COMMITS = 3         # D4: hand commits touching the same file
MIN_COMMIT_DAYS = 3     # D4: across >= this many distinct days — a cadence,
                        # not one day's dev burst (live-tuned 2026-07-21)
SEQ_GAP_MINUTES = 15    # D2: max gap for two commands to count as a sequence
MAX_NEW_PER_RUN = 1     # the hard cap — one candidate a week, the best one
MAX_DATES_KEPT = 14     # bound the evidence payload

# Commands that are browsing/diagnosis, not toil — never candidates alone.
STOP_FIRST_TOKENS = {"ls", "ll", "cd", "pwd", "clear", "cat", "less", "more",
                     "tail", "head", "echo", "history", "exit", "htop", "top",
                     "vim", "nano", "man", "which", "date", "df", "du", "wc",
                     "ps", "free", "uptime"}

# An inline secret-looking assignment means the whole command is untouchable —
# dropped before any shape is derived, never redacted-and-kept.
_SECRET_ASSIGN = re.compile(
    r"(?i)[A-Za-z_]*(key|token|secret|passw|pwd|auth|cred)[A-Za-z_]*\s*[=:]\s*\S")
# Redactions applied to the surviving shape, in order. Paths keep their
# basename (that IS the signal: …/unlock.sh) — everything else templates out.
_PATH = re.compile(r"(?:~|/home/[\w.-]+|/(?:usr|etc|var|tmp|opt|mnt|srv|root|"
                   r"proc|dev|boot))[\w./@+~-]*")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
# secret-shaped runs: long, no separators a flag would have, letters AND digits
_TOKEN = re.compile(r"\b(?=[A-Za-z0-9+/=]*\d)(?=[A-Za-z0-9+/=]*[A-Za-z])"
                    r"[A-Za-z0-9+/=]{20,}\b")
_NUM = re.compile(r"\b\d{2,}\b")


def _path_shape(m):
    tail = m.group(0).rstrip("/").rsplit("/", 1)[-1]
    return ("…/" + tail) if tail and tail not in ("~",) else "{path}"


def normalize(cmd):
    """Command -> shape, or None when the command must not be mined at all.
    Shapes template out the varying parts (path prefixes, IPs, hashes,
    token-looking runs, numbers) so the same toil lands on one fingerprint."""
    cmd = " ".join((cmd or "").split())
    if not cmd or len(cmd) < 3:
        return None
    if not re.match(r"[A-Za-z0-9_./~]", cmd):
        return None  # pasted non-command noise (box-drawing lines etc.)
    if _SECRET_ASSIGN.search(cmd):
        return None  # when in doubt, drop — never redact-and-keep
    tokens = cmd.split()
    first = tokens[0].rsplit("/", 1)[-1]
    if first in STOP_FIRST_TOKENS:
        return None
    # A bare program launch (`claude`, `{{REDACTED}}`) is entering a workspace,
    # not automatable toil — but a bare SCRIPT invocation (…/unlock.sh) is
    # exactly the canonical candidate. Live-tuned 2026-07-21.
    if len(tokens) == 1 and "/" not in tokens[0] \
            and not tokens[0].endswith((".sh", ".py")):
        return None
    shape = _PATH.sub(_path_shape, cmd)
    shape = _IP.sub("{ip}", shape)
    shape = _TOKEN.sub("{token}", shape)
    shape = _HEX.sub("{hex}", shape)
    shape = _NUM.sub("{n}", shape)
    return shape


def _fp(detector, key):
    return hashlib.sha256(("%s:%s" % (detector, key)).encode()).hexdigest()[:12]


# ----------------------------------------------------------------- corpora ---

def iter_bash_inputs(projects_dir, since):
    """Yield (session_id, ts_iso, raw_command) for every command CRAIG TYPED
    (the `!` prefix -> <bash-input> rows). Agent-run commands never appear
    here — that's the learn-from-Craig's-actions boundary, structurally."""
    since_ts = since.timestamp()
    if not os.path.isdir(projects_dir):
        return
    for proj in sorted(os.listdir(projects_dir)):
        pdir = os.path.join(projects_dir, proj)
        if not os.path.isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            try:
                if os.path.getmtime(path) < since_ts:
                    continue  # every event inside predates the window
                with open(path, errors="replace") as f:
                    for line in f:
                        if "<bash-input>" not in line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if row.get("type") != "user":
                            continue
                        content = (row.get("message") or {}).get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict))
                        m = re.search(r"<bash-input>(.*?)</bash-input>",
                                      content, re.S)
                        ts = row.get("timestamp", "")
                        if not m or ts < since.strftime("%Y-%m-%dT%H:%M:%S"):
                            continue
                        yield (row.get("sessionId", name[:-6]), ts,
                               m.group(1).strip())
            except OSError:
                continue


_ZSH_EXT = re.compile(r"^: (\d{9,12}):\d+;(.*)$")


def iter_shell_history(history_path, since):
    """Yield (pseudo_session_or_None, ts_iso_or_None, raw_command) from bash
    history — the corpus where MOST of Craig's hand work actually lives (his
    correction, 2026-07-21: the majority of manual commands run in a plain
    terminal, never through `!`). Lines under a `#<epoch>` timestamp marker
    (HISTTIMEFORMAT) get a real date and a per-day pseudo-session
    (`shell-YYYY-MM-DD`), and the 28d window applies. Undated lines — all of
    them until timestamps were enabled 2026-07-21 — yield (None, None, cmd):
    countable occurrences, honestly unwindowed and undated. zsh's extended
    history (`: <epoch>:<duration>;command`) is dated per line the same way."""
    since_iso = since.strftime("%Y-%m-%d")
    pending_ts = None
    try:
        with open(history_path, errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                z = _ZSH_EXT.match(line)
                if z:
                    try:
                        ts = datetime.fromtimestamp(
                            int(z.group(1)), timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%S")
                    except (ValueError, OSError, OverflowError):
                        continue
                    pending_ts = None
                    if ts[:10] >= since_iso and z.group(2).strip():
                        yield ("shell-" + ts[:10], ts, z.group(2))
                    continue
                if re.fullmatch(r"#\d{9,12}", line):
                    try:
                        pending_ts = datetime.fromtimestamp(
                            int(line[1:]), timezone.utc)
                    except (ValueError, OSError, OverflowError):
                        pending_ts = None
                    continue
                if not line.strip():
                    continue
                if pending_ts is not None:
                    ts = pending_ts.strftime("%Y-%m-%dT%H:%M:%S")
                    pending_ts = None
                    if ts[:10] < since_iso:
                        continue
                    yield ("shell-" + ts[:10], ts, line)
                else:
                    yield (None, None, line)
    except OSError:
        return


# --------------------------------------------------------------- detectors ---

def d1_repeated_command(events):
    """D1 — the same shape run by hand again and again. Two floors, one per
    evidence quality: >= MIN_SESSIONS distinct sessions (a claude session or
    a dated terminal day both count), OR >= MIN_SHELL_HITS occurrences in
    undated shell history (counts are all that corpus can honestly offer)."""
    hits = defaultdict(lambda: {"sessions": set(), "dates": set(),
                                "undated": 0})
    for sid, ts, raw in events:
        shape = normalize(raw)
        if shape is None:
            continue
        h = hits[shape]
        if sid:
            h["sessions"].add(sid)
        if ts:
            h["dates"].add(ts[:10])
        if sid is None:
            h["undated"] += 1
    out = []
    for shape, h in hits.items():
        n_sess, n_und = len(h["sessions"]), h["undated"]
        if n_sess < MIN_SESSIONS and n_und < MIN_SHELL_HITS:
            continue
        evidence = {"sessions": n_sess,
                    "days": sorted(h["dates"])[-MAX_DATES_KEPT:]}
        if n_und:
            evidence["undated_shell_hits"] = n_und
        if n_sess >= MIN_SESSIONS:
            claim = ("in %d sessions across %d days"
                     % (n_sess, max(len(h["dates"]), 1)))
        else:
            claim = "%d+ times in recent shell history" % n_und
        out.append({
            "detector": "D1", "fp": _fp("D1", shape), "shape": shape,
            "score": n_sess + n_und,
            "evidence": evidence,
            "carrier": "cron job or skill",
            "text": ("friction miner: you've run `%s` by hand %s — a cron "
                     "job or skill could carry it" % (shape, claim))})
    return out


def d2_sequence(events):
    """D2 — a fixed two-step sequence (A then B, close together, same
    session) recurring in >= MIN_SESSIONS sessions. SPEC delta: v1 keys the
    sequence on its own leading command rather than an external event feed
    (reboot/drill markers aren't in any corpus the miner reads yet)."""
    by_session = defaultdict(list)
    for sid, ts, raw in events:
        if sid is None or ts is None:
            continue  # undated shell lines can't prove a timed sequence
        shape = normalize(raw)
        if shape is not None:
            by_session[sid].append((ts, shape))
    pairs = defaultdict(lambda: {"sessions": set(), "dates": set()})
    for sid, rows in by_session.items():
        rows.sort()
        for (ts_a, a), (ts_b, b) in zip(rows, rows[1:]):
            if a == b:
                continue
            try:
                gap = (datetime.fromisoformat(ts_b.replace("Z", "+00:00"))
                       - datetime.fromisoformat(ts_a.replace("Z", "+00:00")))
            except ValueError:
                continue
            if gap > timedelta(minutes=SEQ_GAP_MINUTES):
                continue
            pairs[(a, b)]["sessions"].add(sid)
            pairs[(a, b)]["dates"].add(ts_a[:10])
    out = []
    for (a, b), h in pairs.items():
        if len(h["sessions"]) < MIN_SESSIONS:
            continue
        out.append({
            "detector": "D2", "fp": _fp("D2", a + " && " + b),
            "shape": "%s && %s" % (a, b), "score": len(h["sessions"]),
            "evidence": {"sessions": len(h["sessions"]),
                         "days": sorted(h["dates"])[-MAX_DATES_KEPT:]},
            "carrier": "single script",
            "text": ("friction miner: the sequence `%s` then `%s` recurs in "
                     "%d sessions — one script could carry both steps"
                     % (a, b, len(h["sessions"])))})
    return out


def d3_approve_with_edit(audit_path, since):
    """D3 — a card type repeatedly approved WITH edits: a mis-calibrated
    generator's confession. Latent until the audit trail records an edit
    marker (`edited: true` or decision `approved_with_edit`) — today it
    records neither, so D3 correctly finds nothing (stated, not silent)."""
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    counts = defaultdict(lambda: {"n": 0, "dates": set()})
    try:
        with open(audit_path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", "") < since_iso:
                    continue
                if row.get("edited") or row.get("decision") == "approved_with_edit":
                    ct = row.get("card_type", "?")
                    counts[ct]["n"] += 1
                    counts[ct]["dates"].add(row.get("ts", "")[:10])
    except OSError:
        return []
    out = []
    for ct, h in counts.items():
        if h["n"] < MIN_EDITS:
            continue
        out.append({
            "detector": "D3", "fp": _fp("D3", ct), "shape": ct,
            "score": h["n"],
            "evidence": {"edited_approvals": h["n"],
                         "days": sorted(h["dates"])[-MAX_DATES_KEPT:]},
            "carrier": "generator calibration (proposal-feed)",
            "text": ("friction miner: %s cards keep being approved WITH "
                     "edits (%d in %dd) — the generator is mis-calibrated"
                     % (ct, h["n"], WINDOW_DAYS))})
    return out


# Commit-subject prefixes of VERIFIED scheduled committers — the fleet bus
# (cc-handoff fleetd/worker/post_task traffic) and the typed publish verb.
# Author identity CANNOT discriminate here: Craig's own terminal commits and
# hand-run scripts (e.g. the seed pipeline) also land as {{REDACTED}} — his
# correction, 2026-07-21, replacing the author-based filter that threw his
# real hand toil away. Believe the operator.
AUTOMATED_SUBJECT_PREFIXES = ("post:", "claim:", "worker:", "sign:",
                              "done:", "reply:", "race-measure:")


def d4_recurring_hand_edit(cc_root, since):
    """D4 — the same file hand-committed in >= MIN_COMMITS commits across
    >= MIN_COMMIT_DAYS distinct days, per repo under CC. "Hand" = no Claude
    co-author trailer AND the subject isn't a verified scheduled committer's
    (AUTOMATED_SUBJECT_PREFIXES). Hand-RUN scripts that commit (the seed
    pipeline) count deliberately: running them by hand is exactly the toil
    a cron could carry."""
    since_arg = "--since=" + since.strftime("%Y-%m-%d")
    hits = defaultdict(lambda: {"commits": 0, "dates": set()})
    repos = [d for d in sorted(os.listdir(cc_root))
             if os.path.isdir(os.path.join(cc_root, d, ".git"))]
    # A seed install is usually ONE repo at its root, with no per-project
    # repos under it — scanning only children would find nothing there.
    if SEED_INSTALL and os.path.isdir(os.path.join(cc_root, ".git")):
        repos.insert(0, ".")
    for repo in repos:
        try:
            out = subprocess.run(
                ["git", "-C", os.path.join(cc_root, repo), "log", since_arg,
                 "--no-merges", "--invert-grep", "--grep=Co-Authored-By",
                 "--name-only", "--format=%x01%cI|%s"],
                capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        date = None
        seen_this_commit = set()
        for line in out.splitlines():
            if line.startswith("\x01"):
                date_part, _, subject = line[1:].partition("|")
                automated = subject.strip().lower().startswith(
                    AUTOMATED_SUBJECT_PREFIXES)
                date = None if automated else date_part[:10]
                seen_this_commit = set()
            elif line.strip() and date:
                key = (line.strip() if repo == "." else
                       "%s/%s" % (repo, line.strip()))
                if key in seen_this_commit:
                    continue
                seen_this_commit.add(key)
                hits[key]["commits"] += 1
                hits[key]["dates"].add(date)
    out_c = []
    for path, h in hits.items():
        if h["commits"] < MIN_COMMITS or len(h["dates"]) < MIN_COMMIT_DAYS:
            continue
        out_c.append({
            "detector": "D4", "fp": _fp("D4", path), "shape": path,
            "score": h["commits"],
            "evidence": {"hand_commits": h["commits"],
                         "days": sorted(h["dates"])[-MAX_DATES_KEPT:]},
            "carrier": "cron job",
            "text": ("friction miner: `%s` was hand-committed %d times in "
                     "%dd — a cron could carry that edit"
                     % (path, h["commits"], WINDOW_DAYS))})
    return out_c


# -------------------------------------------------------------------- state ---

def load_state(state_path):
    """-> (candidates_by_fp, disposition_by_fp). Every fingerprint ever
    raised is permanent — raise-once is the whole anti-noise contract."""
    cands, disps = {}, {}
    try:
        with open(state_path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") == "candidate":
                    cands[row["fp"]] = row
                elif row.get("kind") == "disposition":
                    disps[row["fp"]] = row
    except OSError:
        pass
    return cands, disps


def _append_state(state_path, row):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "a") as f:
        f.write(json.dumps(row) + "\n")


def sync_dispositions(state_path, conditions_path, now):
    """Pull opener verdicts back from initiative/state/conditions.json —
    done -> accepted, dismissed -> rejected, expired -> expired. Once a
    candidate has a disposition, cond_friction stops reporting it and the
    engine's condition entry re-arms (but the fingerprint stays burned)."""
    outcome_map = {"done": "accepted", "dismissed": "rejected",
                   "expired": "expired"}
    try:
        with open(conditions_path) as f:
            conds = json.load(f)
    except (OSError, ValueError):
        return 0
    cands, disps = load_state(state_path)
    synced = 0
    for key, v in conds.items():
        if not key.startswith("friction."):
            continue
        fp = key.split(".", 1)[1]
        outcome = outcome_map.get(v.get("status"))
        if outcome and fp in cands and fp not in disps:
            _append_state(state_path, {
                "kind": "disposition", "fp": fp, "outcome": outcome,
                "ts": now.strftime("%Y-%m-%dT%H:%M:%S")})
            synced += 1
    return synced


# ---------------------------------------------------------------------- run ---

def mine(projects_dir, audit_path, cc_root, since, history_path=None):
    events = list(iter_bash_inputs(projects_dir, since))
    paths = [history_path] if isinstance(history_path, str) else (history_path or [])
    for p in paths:
        events += list(iter_shell_history(p, since))
    return (d1_repeated_command(events) + d2_sequence(events)
            + d3_approve_with_edit(audit_path, since)
            + d4_recurring_hand_edit(cc_root, since))


def run(dry_run=False, projects_dir=PROJECTS_DIR, audit_path=AUDIT_PATH,
        cc_root=CC, state_path=STATE_PATH, conditions_path=CONDITIONS_PATH,
        history_path=HISTORY_PATH, now=None):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    if not dry_run:
        synced = sync_dispositions(state_path, conditions_path, now)
    else:
        synced = 0
    cands, _ = load_state(state_path)
    found = mine(projects_dir, audit_path, cc_root, since,
                 history_path=history_path)
    fresh = [c for c in found if c["fp"] not in cands]
    fresh.sort(key=lambda c: (-c["score"], c["fp"]))
    picked = fresh[:MAX_NEW_PER_RUN]

    if dry_run:
        print("friction-miner (dry-run): %d shape(s) over the floor, "
              "%d fresh, would raise %d" % (len(found), len(fresh),
                                            len(picked)))
        for c in fresh:
            mark = "-> " if c in picked else "   "
            print("  %s[%s %s] score=%d %s"
                  % (mark, c["detector"], c["fp"], c["score"], c["shape"]))
        return picked

    for c in picked:
        row = dict(c, kind="candidate",
                   ts=now.strftime("%Y-%m-%dT%H:%M:%S"))
        row.pop("score", None)
        _append_state(state_path, row)
    if picked or synced:
        parts = []
        if picked:
            parts.append("1 candidate raised: [%s] %s"
                         % (picked[0]["detector"], picked[0]["shape"]))
        if synced:
            parts.append("%d disposition(s) synced" % synced)
        print("FINDINGS: " + "; ".join(parts))
    # a run with nothing to say prints nothing — edge-trigger
    return picked


def show(fp_sub, state_path=STATE_PATH):
    cands, disps = load_state(state_path)
    hits = [c for fp, c in cands.items() if fp_sub in fp]
    if len(hits) != 1:
        sys.exit("'%s' matches %d candidates — be more specific"
                 % (fp_sub, len(hits)))
    c = hits[0]
    print("%s  [%s]  raised %s" % (c["fp"], c["detector"], c.get("ts", "?")))
    print("  %s" % c["text"])
    print("  shape:    %s" % c["shape"])
    print("  carrier:  %s" % c.get("carrier", "?"))
    print("  evidence: %s" % json.dumps(c.get("evidence", {})))
    d = disps.get(c["fp"])
    print("  disposition: %s" % (d["outcome"] if d else
                                 "none — resolve via initiative/openers.py"))


def status(state_path=STATE_PATH):
    cands, disps = load_state(state_path)
    if not cands:
        print("no candidates raised yet")
        return
    for fp, c in sorted(cands.items(), key=lambda kv: kv[1].get("ts", "")):
        d = disps.get(fp)
        print("%s  %s  [%s]  %s  %s"
              % (c.get("ts", "?")[:10], fp, c["detector"],
                 (d["outcome"] if d else "OPEN"), c["shape"][:70]))
    n = len(disps)
    rej = sum(1 for d in disps.values() if d["outcome"] == "rejected")
    print("%d raised, %d resolved (%d rejected) — falsifier trips at "
          "mostly-dismissed n>=6" % (len(cands), n, rej))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run",
                    choices=["run", "show", "status"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "run":
        run(dry_run=args.dry_run)
    elif args.cmd == "show":
        if not args.arg:
            sys.exit("usage: miner.py show <fp>")
        show(args.arg)
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
