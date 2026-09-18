---
name: freeze
description: Write a session brief capturing THIS session's real goal,
  decisions, state, and open work, so a future session (any harness, any
  model) can resume cold without re-explaining. Use when the operator says
  "/freeze", "write a handoff brief", "freeze this session", "wrap this up",
  "let's pick this up later", or wants to end a session with real work still
  open. For the fuller ritual that ALSO routes durable lessons into memory
  notes, use /capture instead — /freeze is the brief-only fast path, no
  memory judgment involved.
---

# /freeze — write this session's brief, right now

`session-brief/` is the fifth transferable asset (`session-brief/README.md`):
tools, memory, connectors, and doctrine already move between sessions and
harnesses; work-in-progress didn't, until a brief made it a file. `/freeze`
is the pull: the operator judges a session is done — short session, real
decisions, about to clear the context anyway — and the brief gets written
on the spot.

## What it is NOT

Not a memory writer. If the session also produced a durable behavioral
rule or a decision worth keeping, run `/capture` instead — its last step
does exactly this same brief write, after routing the lessons. Reach for
`/freeze` when the brief is the whole ask: nothing to teach the agent,
just real work that isn't finished yet.

## Procedure

### 1. Build the payload from THIS session's actual content — not a template

Re-read the session. Every field is a judgment call about what really
happened, not a form to fill in on autopilot. Required:

- **`goal`** — a brief without one cannot be resumed; the writer refuses.
- **`next_action`** — a brief without one is a summary, not a handoff; the
  writer refuses without this too.

Carry the most value, get these right:

- **`constraints`** — restate them in full, every time, even though they
  live in `CLAUDE.md`. Doctrine does not travel with capability — a brief
  may be resumed by a harness that never loaded any of it. Phrase each so
  that *degrading* it fails safe: a smaller resuming model compresses, and
  compression turns a trailing qualifier into permission. "X is enforced,
  but Y defeats it" survives losing its second clause and stays safe; "you
  may do Z when W" does not survive losing its qualifier — write the first
  kind.
- **`decisions`** — the *why*, not the what: "chose X over Y because Z," so
  the resuming agent doesn't re-litigate a settled call or silently reverse
  it.
- **`failed_paths`** — every approach tried this session that dead-ended,
  with the reason. A missing failed path is what the next agent re-tries.
  One line each: "tried X; failed because Y."
- **`provenance`** — what was actually verified live, not what you believe.
  The resume header instructs the reader to treat everything absent from
  this field as unconfirmed. Omitting a claim here hands the next agent your
  assumption with more confidence than you had for it.
- **`state`**, **`files`**, **`open_threads`** — current, concrete, and
  specific enough that someone who wasn't in the room can act on them.

`session` is captured automatically from `CLAUDE_CODE_SESSION_ID` in Claude
Code; on any other harness pass `session` in the payload. It is the exact
join to the transcript, so never invent one.

**Always set `harness`, `model`, and `host` explicitly** — they default to
`unknown` / `unknown` / the machine's nodename. Get the key names right:
**`harness`** and **`model`**, not `origin_harness` — a silently-ignored key
leaves the brief stamped `unknown` with no error to notice it by.

Full schema, bounds (list caps, 600-char field clips, truncation reporting):
`session-brief/README.md`.

### 2. Write it — directly, no proposal step

A brief is a snapshot of state already decided, mutates nothing the operator
owns, and is trivially deletable — this is a direct write, not a
propose-then-ask step:

```bash
printf '%s' "$payload" | python3 <ROOT>/session-brief/session_brief.py write
```

If a truncation note prints to stderr, read it — if something load-bearing
got cut, shorten that field yourself rather than letting the 600-char cap
choose what survives.

### 3. Tell the operator, one line

Report the brief's path and its one-line goal. The next session resumes it
with:

```bash
python3 <ROOT>/session-brief/session_brief.py resume --id latest
```

— paste that output as the opening context of the new session (any
harness), or wire it into your harness's session-start hook so it loads
automatically. Don't ask the operator to run the writer themselves; the
point of this being a skill is removing that friction.

## Relationship to the rest of the system

- **`/capture`** — the fuller ritual (memory notes + knowledge notes +
  brief). Its last step is this same mechanic; reach for `/capture` when
  memory destinations are also in play, `/freeze` when the brief is the
  only thing worth writing.
- **`/recall`** — reads the brief store as one of its sources, so a
  question like "where did we leave the X work" finds the brief.
- **`session_brief.py resume --id latest`** — the thaw half; not this
  skill's job to invoke.
