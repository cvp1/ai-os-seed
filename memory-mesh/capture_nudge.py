#!/usr/bin/env python3
"""Stop hook: nudge /capture at the end of a SUBSTANTIVE, un-captured session.

Phase 2 of the write-back loop (see auto-memory `capture-skill`). The behavioral
norm (offer /capture) is the primary trigger; this hook is the backstop for the
sessions where real work happened and capture would otherwise be forgotten.

Contract (Claude Code Stop hook): reads a JSON event on stdin
(session_id, transcript_path, stop_hook_active). To make the model act before
stopping, print {"decision":"block","reason":...} and exit 0. To allow the stop,
exit 0 with no output.

Design rules:
  - FAIL-OPEN: any error → silent exit 0. Never break or nag-on-error.
  - NEVER LOOP: if stop_hook_active is already set, stay silent.
  - CONSERVATIVE GATE: only fire when >=3 file mutations happened AND no capture
    signal is present. Pure-discussion decisions are left to the norm, not this.
  - OPT-OUT: a marker at ~/.claude/.capture-skip/<session_id> silences it (touch
    it when Craig says "skip capture this session").
"""
import json
import os
import sys

MUTATION_TOOLS = {"Edit", "Write", "NotebookEdit"}
SUBSTANTIVE_MUTATIONS = 3
# Path fragments whose mutation means "capture already happened this session".
CAPTURE_PATH_HINTS = ("/memory/", "06 logs/decisions", "skills/capture")
# Capture also happens in two shapes that carry no file_path at all:
#   - `/capture` typed as a slash command -> a <command-name> USER entry
#   - memory writes routed through memory_write.py -> a Bash command
# Missing these is why the nudge fired five times in an already-captured
# session on 2026-07-26.
CAPTURE_TEXT_HINTS = ("<command-name>/capture</command-name>",
                      "<command-name>capture</command-name>",
                      "memory_write.py")


def walk_tool_uses(obj):
    """Yield every tool_use block anywhere in a parsed transcript line."""
    if isinstance(obj, dict):
        if obj.get("type") == "tool_use":
            yield obj
        for v in obj.values():
            yield from walk_tool_uses(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_tool_uses(v)


def main():
    raw = sys.stdin.read()
    event = json.loads(raw) if raw.strip() else {}

    # Never loop: if we already blocked once this stop, let it end.
    if event.get("stop_hook_active"):
        return

    session_id = event.get("session_id", "")
    skip = os.path.expanduser(f"~/.claude/.capture-skip/{session_id}")
    if session_id and os.path.exists(skip):
        return

    tpath = event.get("transcript_path", "")
    if not tpath or not os.path.exists(tpath):
        return

    mutations = 0
    captured = False
    with open(tpath, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if any(h in line for h in CAPTURE_TEXT_HINTS):
                captured = True
            for tu in walk_tool_uses(rec):
                name = tu.get("name", "")
                inp = tu.get("input", {}) or {}
                if name == "Skill" and str(inp.get("skill", "")).lower() == "capture":
                    captured = True
                if name == "Bash" and "memory_write.py" in str(inp.get("command", "")):
                    captured = True
                if name in MUTATION_TOOLS:
                    mutations += 1
                    fp = str(inp.get("file_path", "")).lower()
                    if any(h in fp for h in CAPTURE_PATH_HINTS):
                        captured = True

    if mutations >= SUBSTANTIVE_MUTATIONS and not captured:
        reason = (
            "This session looks substantive ({} file changes) and /capture hasn't "
            "run. Before ending, review the session for 1-3 DURABLE items worth "
            "keeping and run the /capture flow: decisions+why -> vault "
            "`06 Logs/Decisions/`, agent rules/preferences -> auto-memory. "
            "If nothing is genuinely durable, say so in one line and stop "
            "(this won't fire again this session)."
        ).format(mutations)
        # Make "won't fire again this session" true: stop_hook_active only
        # covers one stop cycle, so latch on disk before blocking.
        if session_id:
            try:
                os.makedirs(os.path.dirname(skip), exist_ok=True)
                open(skip, "a").close()
            except OSError:
                pass  # fail-open: a latch we can't write must not block firing
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail-open: a broken nudge must never break or spam the session.
        pass
    sys.exit(0)
