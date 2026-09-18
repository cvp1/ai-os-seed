# session-brief — the fifth transferable asset

Four things already move between sessions, harnesses and providers in this
system: tools (repo CLIs), memory (`memory/` + `memory-mesh/`), connectors,
and doctrine (`PRINCIPLES.md`, your `CLAUDE.md`). A fifth never had a home:
**work in progress**. A live session's goal, the decisions taken and *why*,
what was ruled out, and what to do next lived only in one harness's
transcript. Close the window and the expensive part is gone; open another
harness and you re-explain from scratch — which is the definition of work
that did not move.

A brief is that asset as a file: markdown with YAML frontmatter, stdlib-only,
no harness API anywhere in it. Any agent that can read a file can resume.

## The test it has to pass

> Freeze a thread in one harness and continue in another — or on a small
> local model — without re-explaining. If you cannot, portability is fiction.

## Use

```bash
# freeze — a JSON payload in, a brief out
printf '%s' "$payload" | python3 <ROOT>/session-brief/session_brief.py write

# thaw — harness-neutral opening context, paste it into any chat or pipe it at a CLI
python3 <ROOT>/session-brief/session_brief.py resume --id latest

python3 <ROOT>/session-brief/session_brief.py list
python3 <ROOT>/session-brief/session_brief.py show --id latest
python3 <ROOT>/session-brief/session_brief.py selftest
```

`briefs/` (next to the script) is the store; `CC_BRIEFS_DIR` overrides it.
The installer's `--audit` knows `session-brief/briefs/` is a runtime path,
so your briefs are never flagged as unexpected content.

Two skills drive it: **`/freeze`** writes a brief for the current session
(the fast path), and **`/capture`** does the same as the last of its three
routing destinations (memory note, knowledge note, brief). `/recall` reads
the store back as its third source.

## What a brief holds, and what it refuses to

Decisions and their reasons — not narration. A transcript is a record of what
was *said*; a brief is a record of what was *settled* and what it cost to
settle. Two fields are mandatory and the writer fails without them:

- **`goal`** — a brief without one cannot be resumed.
- **`next_action`** — a brief without one is a summary, not a handoff.

Three more carry most of the value in practice:

- **`constraints`** — restated in full, every time. Doctrine does not travel
  with capability; a brief handed to a harness that never loaded your
  `CLAUDE.md` has to carry its own boundaries or the receiving agent will
  not know them. Phrase each so that *degrading* it fails safe — a smaller
  model compresses, and compression turns a trailing qualifier into
  permission. "X is enforced, but Y defeats it" survives losing its second
  clause and stays safe; "you may do Z when W" does not.
- **`provenance`** — what was actually verified, and what was not. The
  resume header instructs the receiving agent to treat anything absent from
  that list as unconfirmed. Handing over unmarked assumptions is how a fresh
  agent inherits your errors with more confidence than you had.
- **`failed_paths`** — what was tried and dead-ended, and why. This is the
  field briefs are weakest on when it's skipped, and the missing failed path
  is exactly what the next agent re-tries.

Then `decisions` (the *why*, not the what), `state`, `files`, `open_threads`.

The frontmatter carries **`session`** — the harness session id of the
transcript the brief summarizes. Claude Code exports `CLAUDE_CODE_SESSION_ID`
into every tool shell, so it is picked up automatically; other harnesses
pass `session` in the payload. It is declared, not mined: re-linking briefs
to transcripts by timestamp and content is guesswork, the id is exact.
`harness`, `model` and `host` should be set explicitly too — they default to
`unknown` / `unknown` / the machine's nodename, and the resume preamble
prints all three.

## Bounds

Lists are capped (`decisions` 12, `failed_paths` 10, `open_threads` 10,
`files` 25, `constraints` 10, `state` 15) and every field clips at 600
characters. **Every truncation is reported** — on stderr at write time and
in a "Truncation notes" section inside the brief — never silent. A brief
that does not fit a small local model's context cannot do the one job it
exists for, so the bound is a feature; the report is what keeps it honest.

A colliding id (same minute, same goal) gets a `-2`/`-3` suffix and a note,
never an overwrite.
