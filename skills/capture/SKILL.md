---
name: capture
description: Close the write-back loop at the end of a substantive session —
  distill what was decided and *why* and file it into the right destination
  (a memory note for agent-facing rules, a knowledge note for durable
  decisions and domain understanding, a session brief for work left
  unfinished). Use when the operator says "/capture", "capture this
  session", "write that back", "log this decision", "hand this off", or at
  the end of a session where real thinking happened — or real work was left
  open — that would otherwise evaporate. The sibling of /improve (which is
  memory-only) — /capture routes to all three.
---

# /capture — write the session's thinking back into the right brain

Real thinking happens *in the session*. When the context window closes, the
reasoning — the *why* behind what was decided — is lost unless it's written
down. `/improve` captures the agent-facing half (preferences, corrections,
gotchas → `memory/`). `/capture` is the **full** loop: it also routes durable
**decisions and domain knowledge** where the operator will find them again,
so each session compounds instead of leaking.

Those two hold what is *settled*. A substantive session usually also leaves
work that is **not** settled — a goal half-met, threads still open, a next
action obvious only to whoever was in the room. That evaporates the same way,
and costs more to rebuild. So `/capture` has a third destination: a **session
brief**, the harness-neutral artifact another agent (another harness, another
provider, a local model) can resume from cold.

This skill **reuses `/improve`'s judgment** (harvest → filter hard → check
for an existing note → write → show). Its additions are the **router** and
the brief.

## When to reach for it
- The operator runs `/capture` (optionally scoped: `/capture the routing
  decision`).
- **Proactively, at the end of a substantive session** — one where a
  decision was made, an approach was chosen with a reason, or domain
  understanding advanced. Offer it; don't wait to be asked. Skip trivial
  sessions.

## Procedure

### 1. Harvest + filter (same as /improve)
Re-read the session. Pull every **durable, cross-session-useful** item:
decisions, chosen approaches + the *reason*, corrections, stated
preferences, domain insights, project facts. **Filter hard** — drop one-off
specifics, anything already in `CLAUDE.md` / the repo / `git log`, anything
you can't state as a concrete applicable rule or a decision with a
rationale.

**Hard cap: 1–3 items.** The cap forces selection — capture the
*load-bearing* thinking, not a transcript.

### 1.5 Earn the write
Step 1 asks whether an item already has a home. This asks whether it
deserves a *new* one.

- **Is it already forced by a principle?** If `PRINCIPLES.md` already
  compels the same behaviour with nothing workspace-specific left over,
  drop it — a note that restates a principle is duplication that also
  freezes it. A measured gotcha that *illustrates* a principle without
  being implied by it is exactly what memory is for; keep those.
- **Is only the general half durable?** Keep the part specific to this
  workspace; drop what any capable model already knows.
- **Does it belong somewhere that isn't a brain?** `CLAUDE.md` for a
  workspace-wide property, a code comment for a one-file gotcha, a script
  for a measurement worth running twice. If so, **name the right home to
  the operator and write no memory** — that's a hand-off, not a capture.

A capture that proposes nothing and names what it rejected, with reasons,
is a good capture.

### 2. Route each item (the new part)
The discriminator: **is this a rule for the *agent*, or knowledge for the
*operator*?**

| The item is… | → Destination | Form |
|---|---|---|
| How to work / a preference / a correction | `memory/` note, type `feedback` | atomic one-liner + **Why:** + **How to apply:** |
| Who the operator is | `memory/` note, type `user` | atomic |
| A fact about the workspace, a tool, a harness gotcha | `memory/` note, type `project` / `reference` | atomic |
| **A decision + its rationale** ("chose X over Y because…") | **knowledge note** — see below | dated, narrative, the why in full |
| **Domain synthesis** (understanding that builds over time) | **knowledge note** — the page it extends | prose |
| A decision you'll also *act on as an agent* AND revisit | **both** — one-line pointer in `memory/`, full rationale in the knowledge note | linked |
| **Work that isn't finished** (open threads, an obvious next action) | **session brief** — `session-brief/briefs/` | one brief per goal |

**Knowledge notes.** This system ships no notes vault. If the operator has
one (an Obsidian vault, a wiki, a `docs/decisions/` directory — ask once,
then record the answer as a `reference` memory note so you never ask
again), the decision goes there as a dated entry. If they don't, write the
decision as a `project` memory note with the **Why:** in full — it's
searchable by `/recall` either way, and it can be moved when a vault
appears. Never let "no vault yet" become "the decision was not written".

The brief is orthogonal to the other two, not a fourth kind of lesson:
memory and knowledge notes hold what is *settled*, the brief holds what is
*live*. A session can produce all three, or only a brief (real work, no new
insight), or only memory (a correction, nothing left open). Route
independently — never skip the brief because the lessons were thin, or
vice versa.

Test for knowledge-note-not-memory: *does it have a why that won't compress
to a rule, and would the operator browse / link / build on it later?*

### 3. Check for an existing note before writing
Search `memory/` (filename, then descriptions, then bodies) for a note that
already covers the ground; **update it** rather than creating a
near-duplicate — `/improve` step 2 is the rule and the reason. For a
knowledge note, search the operator's notes the same way and extend the
existing page.

### 4. Show, then write
**Show the operator, for each item: destination · type · the exact text.**
They approve / edit / drop each. Then:
- **Memory notes**: write them exactly as `/improve` steps 3–5 describe —
  correct frontmatter, the **Why:** line, an `emit.py --kind lesson` event
  so the mesh picks it up, and never a hand-edit of `MEMORY.md`.
- **Knowledge notes** in the operator's own vault or docs are **written
  only after the operator says to** — they are the operator's content in the
  operator's space, and a wrong entry there is theirs to find and undo. A
  `project` memory note standing in for a missing vault is written
  directly, same as any memory note.

### 5. Freeze what isn't finished (the brief)
**Do this last**, after the memory writes, so the brief describes final
state rather than mid-session state. This step is also available standalone
as **`/freeze`** — reach for that directly when a brief is the only thing
worth writing; the payload schema is the same either way
(`session-brief/README.md` is canonical for both).

Offer a brief when the session leaves real work open: threads not closed, a
next action that is obvious to you and to nobody else, or anything you'd
have to re-explain to pick back up. Skip it when the work genuinely finished
— a brief whose next action is "nothing" is noise.

```bash
printf '%s' "$payload" | python3 <ROOT>/session-brief/session_brief.py write
```

`goal` and `next_action` are **required** (the writer refuses without them).
Then `constraints`, `decisions`, `failed_paths`, `state`, `files`,
`open_threads`, `provenance`, plus `harness` / `model` / `host` — set those
three explicitly; they default to `unknown`. Three fields fail a specific way
if you get lazy:

- **`constraints`** — restate them in full even though they're in
  `CLAUDE.md`; a brief may be resumed by a harness that never loaded it.
  Phrase each so that degrading it fails safe — a smaller model compresses,
  and compression turns a trailing qualifier into permission.
- **`provenance`** — what you *verified*, not what you believe; the resume
  header tells the reader everything absent from this list is unconfirmed.
- **`decisions`** — the *why*, not the what.

Lists are capped and fields clip at 600 chars; **every truncation is
reported**. Read those notes and shorten the load-bearing field yourself
rather than letting the cap choose.

Briefs are **written directly, not proposed** — a brief is a snapshot of
state you already have, mutates nothing the operator owns, and is trivially
deletable. Show the path and the one-line goal after writing.

### 6. Report
One short block: what was written where (note filenames, event ids, the
brief path), what was rejected and why, and — if a knowledge note is
staged — the exact text waiting on the operator's word.
