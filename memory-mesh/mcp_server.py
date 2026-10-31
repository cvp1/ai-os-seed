#!/usr/bin/env python3
"""mcp_server: the memory mesh as an MCP server (stdio, newline-delimited JSON-RPC).

Any MCP-capable harness (Claude Code, Codex, Grok, opencode, Antigravity, every
Corral lane through Corral's proxy) gets the same four tools:

    memory_recall   top memories for a topic, across every enabled source
    memory_get      one memory's full text by id
    memory_index    the always-on rules for this lane
    memory_propose  a candidate memory, quarantined until the operator promotes it

`--lane` picks the data-class policy (bridge.json `lanes`). An unknown lane
gets the `default` policy, the strictest one. Stdlib only, Python 3.9+.
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as B  # noqa: E402

PROTOCOL = "2025-06-18"
MAX_LINE = 1 << 20

TOOLS = [
    {"name": "memory_recall",
     "description": ("Search Craig's shared memory (standing rules, preferences, project "
                     "facts, past lessons) for a topic. Use it before acting on anything "
                     "about Craig, his fleet, his projects or how he wants work done."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what you need to know"},
         "k": {"type": "integer", "minimum": 1, "maximum": B.MAX_RECALL_K, "default": 5},
         "with_text": {"type": "boolean", "default": False,
                       "description": "include each memory's full body"}},
         "required": ["query"]}},
    {"name": "memory_get",
     "description": "Read one memory in full by the id memory_recall or memory_index gave you.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                     "required": ["id"]}},
    {"name": "memory_index",
     "description": "The always-on rules for this lane: what every session should know.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_propose",
     "description": ("Propose a durable lesson about Craig or how to work with him. It is "
                     "quarantined and served to no one until Craig promotes it. Never "
                     "include secrets or client-identifying detail."),
     "inputSchema": {"type": "object", "properties": {
         "hook": {"type": "string", "description": "one-line summary, <=140 chars"},
         "rule": {"type": "string", "description": "the lesson itself, <=2000 chars"},
         "why": {"type": "string"}, "how": {"type": "string"}},
         "required": ["hook", "rule"]}},
]


def _text(obj):
    return {"content": [{"type": "text",
                         "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1)}]}


def call(name, a, lane):
    if name == "memory_recall":
        hits = B.recall(str(a.get("query", "")), lane, int(a.get("k", 5) or 5),
                        with_text=bool(a.get("with_text")))
        return _text({"lane": lane, "hits": hits} if hits else
                     "No memories matched. Try different words, or memory_index.")
    if name == "memory_get":
        d = B.get(str(a.get("id", "")), lane)
        return _text(d.get("text") if d and d.get("text") else (d or "No servable memory with that id."))
    if name == "memory_index":
        return _text(B.render_lane(lane))
    if name == "memory_propose":
        return _text(B.propose(lane, a.get("hook"), a.get("rule"), a.get("why"), a.get("how")))
    raise KeyError(name)


def handle(msg, lane):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None                         # notification
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "memory-mesh", "version": "1.0"},
            "instructions": ("Craig's shared memory. Call memory_recall before acting on "
                             "anything about Craig, his projects or his preferences.")}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            res = call(p.get("name"), p.get("arguments") or {}, lane)
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {p.get('name')!r}"}}
        except Exception as e:  # noqa: BLE001 — a tool error is a result, not a crash
            traceback.print_exc(file=sys.stderr)
            res = dict(_text(f"memory tool failed: {e.__class__.__name__}: {e}"), isError=True)
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", default="default")
    args = ap.parse_args(argv)
    for line in sys.stdin:
        if len(line) > MAX_LINE or not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg, args.lane) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
