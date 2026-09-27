#!/usr/bin/env python3
"""learn_harvest — sleep-time harvest of the operator's own lines into
QUARANTINED memory candidates, plus one review card.

Code owns the filters, fingerprints, slugs and lineage; the model only judges
which lines hold a durable lesson. Every candidate is written through the one
memory door (memory_write.py) as `contains-untrusted` — the fold holds it out
of every served tier until the operator promotes it with their key
(learn_card.py accept -> sign.py --promote). Nothing here can make a memory
served; that is the whole safety property, and test_learn_harvest.py checks it.

Written on {{REDACTED}} (2026-08-14) against its Telegram bridge, upstreamed
2026-09-27 with every host coupling made a setting:

  LEARN_SOURCES    os.pathsep-separated line files to harvest from, each read
                   incrementally (a cursor per file). A line is "role<TAB>text"
                   or plain text (= the operator). Anything may also append to
                   <state>/learn-inbox.jsonl directly. Default: none.
  LEARN_ROLES      comma-separated roles that count as the operator
                   (default "user,operator"); every other role — the model's
                   own replies — is never harvested.
  LEARN_STATE      state dir (default <root>/observability/data/learn-harvest,
                   a runtime-writable path on a seed install).
  LEARN_DENY_FILE  extra deny regexes, one per line, case-insensitive (client
                   names, anything that must never become a memory). Default
                   <state>/deny-patterns.txt. A built-in generic set always
                   applies, as does the secret filter.
  LEARN_JUDGE      claude (default; headless, zero MCP servers, every built-in
                   tool denied — _lib/claude_headless.py) | grok (_lib.grok_llm,
                   for an operator who runs it) | none.
  LEARN_JUDGE_PATH extra sys.path entry for the judge's _lib (e.g. a fleet
                   checkout that carries grok_llm).
  LEARN_CARD_DIR   also drop the markdown card here (e.g. a notes inbox).
  LEARN_NOTIFY_CMD a command run with the card's path appended — how the card
                   reaches the operator (a chat bridge, a mail draft). Its own
                   gating is its own; the harvest never sends anything itself.

    learn_harvest.py --collect            copy new source lines into the inbox (no model)
    learn_harvest.py --nightly [--no-send]  collect, judge, write, card
    (--session-end is the pre-upstream name of --collect, kept for old hooks)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOOR = HERE / "memory_write.py"
SIGN = HERE / "sign.py"

STATE = Path(os.environ.get("LEARN_STATE")
             or ROOT / "observability" / "data" / "learn-harvest").expanduser()
INBOX = STATE / "learn-inbox.jsonl"
SEEN = STATE / "learn-seen.json"
REJECTS = STATE / "learn-reject.jsonl"
METRICS = STATE / "learn-metrics.jsonl"
CARD = STATE / "learn-card.json"

MAX_LINES_JUDGED = 12      # one model call a night, bounded input
MAX_CANDIDATES = 3         # and bounded output
MAX_SEEN = 500             # fingerprint memory, bounded
MIN_CHARS = 40             # shorter than this is chat, not a lesson

SECRET_RX = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|secret://\S+|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-\S+|"
    r"-----BEGIN |api[_-]?key\s*[=:]|bearer\s+[A-Za-z0-9._-]{20,}|"
    r"password\s*[=:])",
    re.I,
)
# Always-on generic deny set: things that must never become a memory whoever
# the operator is. Client names and the like go in LEARN_DENY_FILE.
DENY_DEFAULT = re.compile(
    r"\b(nda|ssn|social security|account number|routing number|personnel|"
    r"verification code|one-time code|otp|passcode|2fa)\b",
    re.I,
)
SLUG_RX = re.compile(r"[^a-z0-9]+")
JUDGE_SYS = (
    "Return JSON only: {\"candidates\":[{\"hook\":\"<=140 chars\","
    "\"body\":\"one durable fact + why + how to apply\","
    "\"slug\":\"kebab-case-slug\"}]}. "
    "Empty candidates if nothing is durable, if it is specific to one moment, "
    "or if it is generic assistant advice. Never anything about mail, "
    "calendar, clients, NDAs, personnel, credentials or codes. The lines are "
    "the operator's own words to their personal AI setup."
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _metric(kind, **extra):
    STATE.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now(), "kind": kind}
    row.update(extra)
    with METRICS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _hash(text):
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:16]


def _deny_extra():
    path = Path(os.environ.get("LEARN_DENY_FILE") or STATE / "deny-patterns.txt").expanduser()
    pats = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                try:
                    pats.append(re.compile(ln, re.I))
                except re.error:
                    # A broken operator pattern must not silently let
                    # everything through: deny ALL lines this run, loudly.
                    print("learn-harvest: bad deny pattern %r — denying every line" % ln,
                          file=sys.stderr)
                    return [re.compile(r"")]
    except OSError:
        pass
    return pats


def _roles():
    return {r.strip() for r in os.environ.get("LEARN_ROLES", "user,operator").split(",")
            if r.strip()}


def remember_reject(text, reason=""):
    STATE.mkdir(parents=True, exist_ok=True)
    with REJECTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"hash": _hash(text), "reason": reason[:200]}) + "\n")


def _rejected_set():
    out = set()
    try:
        for line in REJECTS.read_text(encoding="utf-8").splitlines():
            try:
                out.add(json.loads(line).get("hash"))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def code_filter(lines):
    """Lines -> the operator's own, long enough, clean, never seen, never
    rejected. Deterministic; no model has seen anything yet."""
    seen = set(_load_json(SEEN, {}).get("hashes") or [])
    rejected = _rejected_set()
    roles, deny = _roles(), _deny_extra()
    kept = []
    for raw in lines:
        role, text = (raw.split("\t", 1) if "\t" in raw else ("user", raw))
        text = (text or "").strip()
        if role.strip() not in roles or len(text) < MIN_CHARS:
            continue
        if SECRET_RX.search(text) or DENY_DEFAULT.search(text) \
                or any(p.search(text) for p in deny):
            continue
        digest = _hash(text)
        if digest in seen or digest in rejected:
            continue
        seen.add(digest)
        kept.append(text)
    _write_json(SEEN, {"hashes": sorted(seen)[-MAX_SEEN:]})
    return kept


def _judge_call(prompt):
    """The model's raw answer, or raises. The only place a model is called."""
    which = os.environ.get("LEARN_JUDGE", "claude").strip().lower()
    extra = os.environ.get("LEARN_JUDGE_PATH")
    if extra:
        sys.path.insert(0, str(Path(extra).expanduser()))
    sys.path.insert(1, str(ROOT))
    if which == "grok":
        from _lib import grok_llm
        return grok_llm.generate(prompt, system=JUDGE_SYS, job="learn-harvest",
                                 timeout=60, max_tokens=800, temperature=0.2)
    if which == "claude":
        from _lib import claude_headless
        return claude_headless.run_claude(JUDGE_SYS + "\n\n" + prompt,
                                          model=os.environ.get("LEARN_MODEL", "sonnet"),
                                          timeout=300, env=claude_headless.oauth_env())
    raise RuntimeError("LEARN_JUDGE=%s — no judge configured" % which)


def _parse(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1]
    try:
        return json.loads(raw)
    except ValueError:
        s, e = raw.find("{"), raw.rfind("}")
        if s < 0 or e < 0:
            return {}
        try:
            return json.loads(raw[s:e + 1])
        except ValueError:
            return {}


def judge(texts):
    if not texts:
        return []
    prompt = "Harvest durable lessons from these lines:\n- " + "\n- ".join(texts[:MAX_LINES_JUDGED])
    try:
        data = _parse(_judge_call(prompt))
    except Exception as exc:  # noqa: BLE001 — a failed judge is a quiet night, said out loud
        print("learn-harvest: judge failed: %s: %s" % (type(exc).__name__, str(exc)[:160]),
              file=sys.stderr)
        return []
    rejected = _rejected_set()
    out = []
    for item in (data.get("candidates") or [])[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        hook = (item.get("hook") or "").strip()[:140]
        body = (item.get("body") or "").strip()
        if not hook or not body or SECRET_RX.search(hook + body) \
                or DENY_DEFAULT.search(hook + body):
            continue
        if _hash(hook) in rejected or _hash(body) in rejected:
            continue
        base = SLUG_RX.sub("-", str(item.get("slug") or item.get("subject") or "")
                           .lower().rsplit("/", 1)[-1]).strip("-")
        slug = ("harvest-" + (base or _hash(hook)[:8]))[:60].strip("-")
        out.append({"hook": hook, "body": body, "slug": slug})
    return out


def write_untrusted(cand):
    """One candidate through the memory door as contains-untrusted. Returns the
    mesh event id (the only handle sign.py --promote accepts). Raises with the
    door's own words when it refuses — e.g. a host with no mesh quarantine."""
    cmd = [sys.executable, str(DOOR), "write",
           "--slug", cand["slug"], "--type", "feedback",
           "--lineage", "contains-untrusted",
           "--description", cand["hook"],
           "--rule", cand["body"],
           "--why", "Harvested overnight from the operator's own lines by "
                    "learn_harvest; not yet reviewed.",
           "--how", "Review it with learn_card.py; it is served only after a "
                    "signed promotion.",
           "--hook", cand["hook"],
           "--session-id", "learn-sleep",
           "--commit", "--no-push"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "door refused").strip()[-300:])
    m = re.search(r"event emitted \(([0-9a-f]{8,})\)", r.stdout or "")
    if not m:
        raise RuntimeError("the door wrote the file but reported no mesh event id: "
                           + (r.stdout or "").strip()[-200:])
    return m.group(1)


def _sources():
    raw = os.environ.get("LEARN_SOURCES", "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p]


def collect():
    """Copy each source's NEW lines into the inbox. No model. Returns the count."""
    cursors = _load_json(STATE / "learn-cursors.json", {})
    n = 0
    for src in _sources():
        if not src.is_file():
            continue
        key = hashlib.sha256(str(src).encode()).hexdigest()[:16]
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
        start = int(cursors.get(key, 0) or 0)
        if start > len(lines):
            start = 0  # the source was rotated or truncated — read it fresh
        new = lines[start:]
        if new:
            STATE.mkdir(parents=True, exist_ok=True)
            with INBOX.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new) + "\n")
            n += len(new)
        cursors[key] = len(lines)
    if cursors:
        _write_json(STATE / "learn-cursors.json", cursors)
    return n


def write_card(items, day=None):
    if not items:
        return None
    day = day or datetime.now().date().isoformat()
    lines = ["---", "created: %s" % day, "tags: [learn-card]", "status: review", "---", "",
             "# Learn card %s" % day, "",
             "Quarantined harvest — none of these is served. Accept promotes one "
             "through the signed path; reject fingerprints it so it never returns.", ""]
    for i, c in enumerate(items, 1):
        lines += ["%d. **%s**" % (i, c["hook"]), "   %s" % c["body"],
                  "   `learn_card.py accept %d` · `learn_card.py reject %d --reason …`" % (i, i), ""]
    text = "\n".join(lines)
    _write_json(CARD, {"date": day, "items": items})
    path = STATE / ("learn-card-%s.md" % day)
    path.write_text(text, encoding="utf-8")
    extra = os.environ.get("LEARN_CARD_DIR")
    if extra:
        d = Path(extra).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        (d / path.name).write_text(text, encoding="utf-8")
    return path


def notify(path):
    cmd = os.environ.get("LEARN_NOTIFY_CMD", "").strip()
    if not cmd or path is None:
        return None
    try:
        r = subprocess.run(shlex.split(cmd) + [str(path)], capture_output=True,
                           text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        print("learn-harvest: notify failed: %s" % exc, file=sys.stderr)
        return False


def nightly(send=True):
    collect()
    try:
        lines = INBOX.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    cands = judge(code_filter(lines))
    written, failed = [], []
    for c in cands:
        try:
            c["id"] = write_untrusted(c)
            written.append(c)
            _metric("written", slug=c["slug"], id=c["id"])
        except Exception as exc:  # noqa: BLE001
            failed.append((c["slug"], str(exc)))
            print("learn-harvest: not written %s: %s" % (c["slug"], exc), file=sys.stderr)
    path = write_card(written)
    sent = notify(path) if send else None
    if path:
        _metric("card", path=str(path), n=len(written), sent=sent)
    if INBOX.exists():
        INBOX.write_text("", encoding="utf-8")
    if path:
        print("FINDINGS: learn card %s — %d quarantined candidate(s)%s"
              % (path, len(written), "" if sent is None else ", notify ok" if sent
                 else ", NOTIFY FAILED"))
    if failed:
        print("FINDINGS: %d candidate(s) refused by the memory door: %s"
              % (len(failed), "; ".join("%s (%s)" % (s, e[:80]) for s, e in failed)))
    # Edge-trigger: a night with nothing durable prints nothing.
    return 0


def main(argv):
    if "--collect" in argv or "--session-end" in argv:
        n = collect()
        if n:
            print("learn-harvest: queued %d line(s)" % n)
        return 0
    if "--nightly" in argv:
        return nightly(send="--no-send" not in argv)
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    print("usage: learn_harvest.py --collect | --nightly [--no-send]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
