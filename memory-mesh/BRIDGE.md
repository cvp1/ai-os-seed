# bridge — one memory, every model

The mesh stores memory as plain files and folds it with stdlib Python, so no
model owns it. `bridge.py` is the single door every harness uses to reach it,
whoever orchestrates the session. Config: `bridge.json` (repo defaults),
deep-merged with a host-local `~/.config/memory-mesh/bridge.json`.

## Three channels per harness

| Channel | What it gives the model | How |
|---|---|---|
| Static context | the always-on rules at session start | a managed block in the harness's own instruction file, rewritten every fold |
| Recall | anything else, on demand | the `memory` MCP server (`mcp_server.py`), or `bridge.py recall` from a shell |
| Write door | proposing a new lesson | `memory_propose` / `bridge.py propose`, written as `contains-untrusted`, served only after the operator promotes it |

A guard (`bridge.py guard`) normalizes any harness's tool event (any path key,
nested edit lists, patch `Move to:` lines, `command`/`cmd` shell shapes) and
enforces two things: the memory store is written only through
`memory_write.py`, and the **policy files** (`~/.config/memory-mesh/`, the repo
`bridge.json`, and every harness wiring file the bridge installs) are never
written by a model. It fails closed when a call names a protected path and it
cannot decide (unknown tool shape, timeout, crash, missing guard); calls that
touch nothing protected always pass.

## Reference wiring (one fully wired host)

| Harness | Static context | Recall | Guard |
|---|---|---|---|
| Claude Code | fold-generated MEMORY.md in ai-os/CC; elsewhere the SessionStart hook | per-turn hook, user MCP, Corral MCP | user-scope store guard; policy guard* |
| Grok CLI | `~/.grok/memory/MEMORY.md` | `~/.grok/config.toml` MCP | `~/.grok/hooks/memory-mesh.json` |
| Codex (ChatGPT app, CLI) | `~/.codex/AGENTS.md` | `~/.codex/config.toml` MCP | sandbox only |
| Codex in Corral | `codex-home/AGENTS.md` | Corral MCP registry | sandbox only |
| Antigravity / Gemini | `~/.gemini/GEMINI.md` | Corral MCP registry | Corral posture |
| opencode | `context/local.md` for a local provider, `context/default.md` for any other | plugin per turn + MCP | plugin `tool.execute.before` |

\* needs the operator's approval to install (self-modification gate):
`bridge.py install --apply --only hooks-claude`.

## Lanes and data classes

Each memory is classed `personal`, `employer-client` or `unknown`, over
everything emitted together (id, index line, description and body; the worst
piece wins). A match against `~/.config/memory-mesh/client-terms.txt`
(host-local, never committed; regex per line) or the learn harvester's deny
list makes it `employer-client`. An explicit `data_class:` line, a source
default, or a command source's own label can only tighten, never loosen. A
missing or empty terms file means `unknown` (the deny list cannot stand in for
it), and the fold raises an alarm. Both files reload on change, so a running
MCP server never serves a stale policy. `memory_get` answers a withheld id
exactly like a missing one.

A lane's `allow` list decides what it receives. Defaults: `claude` and `local`
get everything (status quo, and local never leaves the machine). `codex`,
`grok`, `gemini`, `corral` and any unknown lane get `personal` only. Filtering
happens before ranking is cut, so a withheld memory never displaces a served one.

## Static budget

Each lane's always-on block is capped by `lanes.<lane>.max_chars` (default
3,500 chars, about 900 tokens; `local` 2,500; `claude` 9,500, its status quo).
Rows past the cap are reachable by recall. A failed render serves a stub ("use
recall") instead of leaving the previous rows in place; damaged markers make the
file a fresh block, with its old text kept in the backups dir.

## Add a source

Declare it in `bridge.json` under `sources` with `"enabled": true`:

- `markdown_dir`: `path`, `glob`, optional `data_class` default. Scored with
  the mesh's own TF-IDF.
- `command`: `argv`, `timeout`. The program reads one JSON object on stdin and
  prints one on stdout:
  - `{"op":"search","query":Q,"k":K}` gives `{"hits":[{"id","desc","score","text"?,"data_class"?}]}`
  - `{"op":"get","id":ID}` gives `{"text":T,"data_class"?}`
  - `{"op":"always_on"}` gives `{"rows":[{"id","line"}]}`, used only when the
    source sets `"always_on": true`.

  A failing source returns nothing; it never breaks a turn.
- A new kind: subclass `Source` in `bridge.py` and add it to `SOURCE_TYPES`.

`weight` scales a source's normalized scores when results merge. A lane can
restrict itself to named sources with `"sources": [...]`.

## Add a harness

Add a `targets` entry. Types: `static_file` (managed block in a file),
`render_only` (just `context/<lane>.md`), `mcp_toml` (Codex/Grok style),
`mcp_opencode`, `mcp_cli` (probe/add/remove argv), `claude_settings`,
`json_file` / `text_file` (templates in `bridge.py`). Then:

    bridge.py install            # dry run with diffs
    bridge.py install --apply    # backups land in ~/.config/memory-mesh/backups
    bridge.py uninstall --apply  # removes only what the bridge added
    bridge.py status

## Tests

    /usr/bin/python3 test_bridge.py               # 43 offline cases, temp dirs only
    node selftest_opencode_memory_plugin.mjs      # 15 checks: lane by provider, recall, guard

Live falsifier: open each harness in an unrelated folder and ask for Craig's
amateur license class and grid square. Every lane must answer "General, N0CVP"
and "DM41vv", and a recall for client terms must return no client memory on
the non-Claude lanes. Passed on Grok, Codex and Gemini on 2026-10-27, and again
on 2026-10-28 after the panel fixes (3.2k-char static block).
