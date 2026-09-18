#!/usr/bin/env python3
"""session_brief — freeze work-in-progress so it can continue in another session.

WHY THIS EXISTS
    Four things already move between sessions, harnesses and providers in
    this system: tools (repo CLIs), memory (memory/ + memory-mesh), connectors
    (MCP servers + CLIs) and doctrine (PRINCIPLES.md, CLAUDE.md). A fifth does
    not: a live session's goal, the decisions taken and WHY, what was ruled
    out, and what to do next live only in a transcript owned by one harness.
    Close the window and the expensive part is gone; open another harness and
    you re-explain from scratch.

    A brief is that missing asset as a file. Plain markdown + YAML frontmatter,
    stdlib-only, no harness API anywhere in it. Any agent that can read a file
    can resume from one.

THE TEST IT HAS TO PASS
    Freeze a thread in one harness; hand `resume` output to a different one —
    or to a small local model — and continue without re-explaining. If that
    fails, portability is a claim rather than a property.

WHAT IT DELIBERATELY IS NOT
    Not a transcript. Not a summary of what was said. A brief holds decisions
    and their reasons, not narration — the things that are expensive to
    rediscover and cheap to state. Bounded on purpose (see CAPS): an artifact
    that grows without limit stops being loadable by the small local models
    this exists to hand work to.

VERBS
    write    read a JSON payload on stdin, write briefs/<id>.md
    show     render a brief (default: latest)
    resume   emit a harness-neutral opening context block — the load-bearing verb
    list     one line per brief
    selftest offline round-trip checks

    printf '%s' "$json" | ./session_brief.py write
    ./session_brief.py resume --id latest      # paste into any chat, or pipe at a CLI

STORE
    briefs/ next to this file; CC_BRIEFS_DIR overrides it. The installer's
    --audit treats session-brief/briefs/ as a declared runtime path, so your
    briefs are never flagged as unexpected content.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEFS = os.environ.get("CC_BRIEFS_DIR", os.path.join(HERE, "briefs"))

# Bounds. A brief that does not fit a small local model's context cannot do the
# one job it exists for, so truncation is a feature and is reported, never silent.
CAPS = {"decisions": 12, "failed_paths": 10, "open_threads": 10, "files": 25,
        "constraints": 10, "state": 15}
FIELD_CHARS = 600

SECTIONS = [
    ("goal", "Goal", str),
    ("constraints", "Constraints", list),
    ("decisions", "Decisions and why", list),
    # Paths tried that failed, and why. Briefs measure weakest on "what
    # failed" questions — and a missing failed path is exactly what the next
    # agent re-tries. Recording it is cheaper than a search engine over
    # transcripts.
    ("failed_paths", "Paths tried that failed, and why", list),
    ("state", "State", list),
    ("files", "Files touched", list),
    ("open_threads", "Open threads", list),
    ("next_action", "Next action", str),
    ("provenance", "Provenance and what is unverified", list),
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text, maxlen=48):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "brief").lower()).strip("-")
    return (s[:maxlen].rstrip("-") or "brief")


def _clip(value, notes, where):
    """Truncate a single field, and RECORD it. Silent truncation of a handoff
    artifact loses exactly the decision the next agent needed."""
    text = str(value).replace("\r", "").strip()
    if len(text) > FIELD_CHARS:
        notes.append(f"{where}: truncated {len(text)} -> {FIELD_CHARS} chars")
        text = text[:FIELD_CHARS].rstrip() + " …[truncated]"
    return text


def normalize(payload):
    """Coerce a payload into the brief shape. Returns (brief, notes)."""
    notes = []
    brief = {}
    for key, label, kind in SECTIONS:
        raw = payload.get(key)
        if kind is str:
            brief[key] = _clip(raw or "", notes, key) if raw else ""
        else:
            items = raw or []
            if isinstance(items, str):
                items = [items]
            cap = CAPS.get(key, 20)
            if len(items) > cap:
                notes.append(f"{key}: kept {cap} of {len(items)} items")
                items = items[:cap]
            brief[key] = [_clip(i, notes, key) for i in items if str(i).strip()]
    if not brief["goal"]:
        raise ValueError("a brief without a goal cannot be resumed — set 'goal'")
    if not brief["next_action"]:
        raise ValueError("a brief without a next_action is a summary, not a handoff")
    return brief, notes


def _reserve_brief_path(bid, notes):
    """Claim briefs/<bid>.md, retrying with a -2/-3/... suffix on collision
    instead of silently overwriting (same minute + same goal produce the same
    id). O_CREAT|O_EXCL makes the claim atomic; bounded at 20 tries so a stuck
    loop fails loud rather than spinning. A suffix is reported, exactly like
    every other bound this tool enforces."""
    os.makedirs(BRIEFS, exist_ok=True)
    for i in range(20):
        candidate = bid if i == 0 else f"{bid}-{i + 1}"
        path = os.path.join(BRIEFS, candidate + ".md")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        if candidate != bid:
            notes.append(f"id: {bid} already existed — wrote {candidate} instead")
        return fd, path, candidate
    raise SystemExit(f"write: {bid} and 19 suffixed retries all exist in {BRIEFS} — giving up")


def write_brief(payload):
    brief, notes = normalize(payload)
    created = now_iso()
    bid = payload.get("id") or (
        f"{created[:4]}{created[5:7]}{created[8:10]}T"
        f"{created[11:13]}{created[14:16]}{created[17:19]}Z_{slugify(brief['goal'])}"
    )
    fd, path, bid = _reserve_brief_path(bid, notes)
    meta = {
        "id": bid,
        "created": created,
        "harness": payload.get("harness") or os.environ.get("CC_HARNESS") or "unknown",
        "model": payload.get("model") or "unknown",
        # Explicit payload wins, then CC_HOST_SLUG, then the nodename as an
        # honest last resort — a machine's nodename is not always the name
        # you'd use for it anywhere else.
        "host": payload.get("host") or os.environ.get("CC_HOST_SLUG") or os.uname().nodename,
        "status": "open",
    }
    # The transcript this brief summarizes. Declared, not mined: re-linking a
    # brief to its transcript by timestamp and content is guesswork, while the
    # harness's own session id is exact. Claude Code exports
    # CLAUDE_CODE_SESSION_ID into every tool shell; other harnesses pass
    # `session` in the payload. Verbatim recall is then one command
    # (`claude --resume <session>` in Claude Code) — no index needed.
    session = payload.get("session") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        meta["session"] = session
    # A brief authored somewhere else and delivered here. Record that on the
    # artifact: the resume header tells the reader to treat constraints as
    # binding, so where those constraints came from is not a detail.
    if payload.get("origin"):
        meta["origin"] = payload["origin"]
        meta["received"] = created
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {brief['goal']}")
    for key, label, kind in SECTIONS:
        if key == "goal":
            continue
        val = brief[key]
        if not val:
            continue
        lines.append("")
        lines.append(f"## {label}")
        if kind is str:
            lines.append(val)
        else:
            for item in val:
                lines.append(f"- {item}")
    if notes:
        lines += ["", "## Truncation notes", *[f"- {n}" for n in notes]]
    lines.append("")

    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))
    return path, notes


def parse_brief(path):
    """Minimal frontmatter split. Deliberately self-contained — this file is
    handed verbatim to whatever harness or small local model is resuming a
    brief, with zero repo dependencies."""
    text = open(path).read()
    meta, body = {}, text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        for ln in fm.strip().splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta, body.strip()


def latest_path():
    if not os.path.isdir(BRIEFS):
        return None
    files = [os.path.join(BRIEFS, f) for f in os.listdir(BRIEFS) if f.endswith(".md")]
    return max(files, key=os.path.getmtime) if files else None


def resolve(idarg):
    if not idarg or idarg == "latest":
        p = latest_path()
        if not p:
            raise SystemExit("no briefs found in %s" % BRIEFS)
        return p
    p = os.path.join(BRIEFS, idarg if idarg.endswith(".md") else idarg + ".md")
    if not os.path.exists(p):
        raise SystemExit("no such brief: %s" % idarg)
    return p


RESUME_HEADER = """You are resuming work that another agent started. This is the
complete handoff; there is no transcript behind it. Everything the previous agent
thought worth carrying is below.

Read the constraints as binding. Treat 'Provenance' as the limit of what was
actually verified — anything not listed there is unconfirmed, so check it rather
than assuming it holds. Begin with the stated next action; if you judge it wrong,
say so and why before doing something else.
"""


def cmd_resume(args):
    path = resolve(args.id)
    meta, body = parse_brief(path)
    out = [RESUME_HEADER,
           f"Handoff origin: {meta.get('harness', '?')} / {meta.get('model', '?')} "
           f"on {meta.get('host', '?')}, frozen {meta.get('created', '?')}.",
           f"Brief id: {meta.get('id', os.path.basename(path))}"]
    if meta.get("session"):
        out.append(f"Source transcript: session {meta['session']} — in Claude Code, "
                   f"verbatim recall on the origin machine is `claude --resume {meta['session']}`.")
    if meta.get("origin"):
        out.append(f"Delivered from {meta['origin']} — this is someone else's work, "
                   f"not this machine's own.")
    out += ["", "---", "", body]
    print("\n".join(out))
    return 0


def cmd_write(args):
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("write: expected a JSON payload on stdin")
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"write: payload is not valid JSON ({e})")
    path, notes = write_brief(payload)
    print(path)
    for n in notes:
        print("note: " + n, file=sys.stderr)
    return 0


def cmd_show(args):
    print(open(resolve(args.id)).read())
    return 0


def cmd_list(args):
    if not os.path.isdir(BRIEFS):
        print("no briefs yet")
        return 0
    rows = []
    for f in sorted(os.listdir(BRIEFS)):
        if not f.endswith(".md"):
            continue
        meta, _ = parse_brief(os.path.join(BRIEFS, f))
        rows.append((meta.get("created", "?"), meta.get("status", "?"),
                     meta.get("harness", "?"), meta.get("id", f[:-3])))
    if not rows:
        print("no briefs yet")
        return 0
    for created, status, harness, bid in sorted(rows):
        print(f"{created}  {status:<8} {harness:<12} {bid}")
    return 0


def cmd_selftest(args):
    import shutil
    import tempfile
    global BRIEFS
    tmp = tempfile.mkdtemp(prefix="brief-selftest-")
    saved, BRIEFS = BRIEFS, tmp
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    try:
        # a brief with no next action is a summary, not a handoff
        try:
            write_brief({"goal": "g"})
            check("missing next_action rejected", False)
        except ValueError:
            check("missing next_action rejected", True)
        try:
            write_brief({"next_action": "do"})
            check("missing goal rejected", False)
        except ValueError:
            check("missing goal rejected", True)

        path, notes = write_brief({
            "goal": "Test the round trip",
            "constraints": ["never send mail"],
            "decisions": ["chose X over Y because Z"],
            "state": ["baseline captured"],
            "files": ["a.py — the thing"],
            "open_threads": ["thread one"],
            "next_action": "run the gate",
            "provenance": ["verified live"],
            "failed_paths": ["tried W first; it dead-ended on Q"],
            "harness": "selftest", "model": "none", "session": "sess-abc-123",
        })
        check("brief written", os.path.exists(path))
        meta, body = parse_brief(path)
        check("frontmatter parses", meta.get("harness") == "selftest")
        check("session id in frontmatter", meta.get("session") == "sess-abc-123")
        check("failed path survives", "dead-ended on Q" in body)
        # env fallback: Claude Code exports CLAUDE_CODE_SESSION_ID
        saved_env = os.environ.get("CLAUDE_CODE_SESSION_ID")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "env-sess-9"
        try:
            p3, _ = write_brief({"goal": "env session", "next_action": "go"})
            check("session id from env", parse_brief(p3)[0].get("session") == "env-sess-9")
        finally:
            if saved_env is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
        check("goal survives", "Test the round trip" in body)
        check("decision survives", "chose X over Y because Z" in body)
        check("constraint survives", "never send mail" in body)

        # truncation must be reported, never silent
        _, n2 = write_brief({"goal": "cap test", "next_action": "go",
                             "decisions": [f"d{i}" for i in range(40)]})
        check("over-cap list truncated", any("kept 12 of 40" in x for x in n2))

        _, n3 = write_brief({"goal": "clip test", "next_action": "go",
                             "decisions": ["x" * 900]})
        check("long field clipped and reported", any("truncated" in x for x in n3))

        # same explicit id twice must not silently clobber the first write
        p4a, _ = write_brief({"id": "collide-test", "goal": "first", "next_action": "go"})
        p4b, n4 = write_brief({"id": "collide-test", "goal": "second", "next_action": "go"})
        check("colliding id does not overwrite", p4a != p4b)
        check("first write survives a colliding second write",
              "first" in open(p4a).read())
        check("collision is reported, not silent", any("already existed" in x for x in n4))

        class A:
            id = "latest"
        import io
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        cmd_resume(A())
        sys.stdout = real
        text = buf.getvalue()
        check("resume emits binding-constraints framing", "binding" in text)
        check("resume carries origin provenance", "Handoff origin:" in text)
    finally:
        BRIEFS = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write a brief from a JSON payload on stdin")
    w.set_defaults(func=cmd_write)
    for name, fn, helptext in (("show", cmd_show, "print a brief"),
                               ("resume", cmd_resume, "emit harness-neutral opening context")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--id", default="latest")
        p.set_defaults(func=fn)
    sub.add_parser("list", help="list briefs").set_defaults(func=cmd_list)
    sub.add_parser("selftest", help="offline round-trip checks").set_defaults(func=cmd_selftest)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
