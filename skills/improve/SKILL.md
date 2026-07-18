---
name: improve
description: Distill this session's corrections and taught preferences into
  durable memory notes so the next session starts smarter. Use when the
  operator says "/improve", "remember this", "remember that", explicitly
  corrects how the agent should work ("no, don't do that", "always do X
  instead", "stop suggesting Y"), or confirms an unusual approach worked
  ("yes, that was right", "keep doing it that way"). Also worth running at
  the end of a session that had any back-and-forth correction in it, even
  without the operator asking by name.
---

# /improve — corrections become defaults

The compounding move of this whole system: a correction the operator gives
once should not need to be given again. This skill turns what happened in
*this* session into a note in `memory/` (see `memory/CONVENTIONS.md` for the
format this skill writes to) — so next session, the agent already knows.

## What counts as material

Scan the session (not just the latest message) for:

- **Explicit corrections** — "no, don't do X", "always use Y instead",
  "stop doing Z". These are unambiguous: write a `feedback` note.
- **Explicit confirmations** — the operator accepted or praised an unusual
  choice without pushback ("yes, exactly", "that's the right call"). Quieter
  than corrections, easy to miss, still worth recording — a memory store
  that only records mistakes drifts away from approaches already validated.
- **Implicit steering** — the operator redirected without spelling out a
  rule ("actually, do it the other way" with no further explanation). Note
  the redirect, but be honest that the *reason* is inferred, not stated —
  see "when unsure" below.
- **Facts about the operator, the project, or where things live** — these
  aren't corrections but are exactly the kind of thing worth keeping (the
  `user` / `project` / `reference` types in `memory/CONVENTIONS.md`).

Don't scan for things that belong somewhere other than memory: code
decisions that are already in the diff, ephemeral task state, anything
`git log` already answers. See CONVENTIONS.md's "what NOT to store" section.

## Procedure

1. **Identify candidate notes.** For each one, decide its type (user /
   feedback / project / reference — CONVENTIONS.md defines them) and draft
   the fact in one or two sentences.
2. **Check for an existing note first.** Search `memory/` for a note that
   already covers the same ground (by filename, then by grepping bodies).
   If one exists, **update it** — don't create a near-duplicate. This is
   the single most important rule here: a memory store with five
   overlapping notes on one rule is worse than one slightly-stale note,
   because nothing tells a future session which version to trust.
3. **Write the note(s)** with correct frontmatter (`name`, `description`,
   `metadata.type`). For `feedback` and `project` notes, structure the body
   as: the rule/fact first, then a `**Why:**` line (the reason given — this
   is what lets a future session judge edge cases the rule didn't spell
   out), then, for feedback notes, a `**How to apply:**` line (when this
   should kick in).
4. **Update the index.** Add or refresh the note's bullet in `MEMORY.md`
   under the right topic — always-on for behavioral rules, name-only under
   an "on-demand" line for reference material. Keep the index short; most
   notes are on-demand, not always-on (see MEMORY.md's own header comment).
5. **Show the operator exactly what was written** — the note file(s) and
   the index change — before treating this as done. No silent writes.

## When unsure

If it's unclear whether something was a one-off for this task or a
standing preference, **ask** rather than guess. A wrong always-on rule
actively makes future sessions worse; a missed one just means asking
`/improve` again later costs nothing. Never infer a rule stronger than
what the session actually supports — no fabricating a "why" the operator
didn't give.

## A note on the "only tools move bytes" rule

If this system was installed from a build pipeline that treats file
content as install payload (deterministic tools only, never model-authored
bytes), that rule governs the *install itself* — it does not apply here.
The memory notes this skill writes are the operator's own ongoing content,
authored by their agent doing ordinary work on their behalf, same as any
other file the agent writes for them. Don't let an installer-safety rule
be misread as "the agent can never write a memory file" — that would make
this skill impossible to use.
