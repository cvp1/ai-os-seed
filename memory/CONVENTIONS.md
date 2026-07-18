# Memory conventions

A durable, file-based memory store your agent builds up across sessions —
so the next session starts smarter than this one, instead of from zero.

## Where this lives

Your coding agent's memory location is keyed to the **exact working
directory** it launches from. If you run your agent from more than one
directory (or from a subdirectory of this install), check where it's
actually reading and writing memory before you assume a note landed here —
a note filed from the wrong cwd silently lands in a different store and
won't show up next time you ask from this one. Confirm the real path with
your agent at first use; don't assume.

## One fact per file

Each note is its own file, named for what it's about (kebab-case, e.g.
`deploy-doesnt-run-tests.md`). Don't bundle unrelated facts into one file —
a note should be small enough to load and re-read in isolation.

## Frontmatter

Every note file starts with:

```markdown
---
name: {{kebab-case-slug, matches the filename}}
description: {{one line — specific enough to judge relevance without opening the file}}
metadata:
  type: {{user, feedback, project, reference}}
---

{{the note body}}
```

## The four types

- **user** — who the operator is, their role, goals, what they already
  know. Tailor future answers to this, don't just record it.
- **feedback** — corrections and confirmations about *how to work*: what
  the operator told the agent to stop doing, or explicitly approved doing
  again. Lead with the rule, then a **Why:** line (the reason given) and a
  **How to apply:** line (when this kicks in). The why is what lets a
  future session judge edge cases the rule didn't spell out.
- **project** — facts about ongoing work: decisions, deadlines, who's
  doing what. These decay fast — convert relative dates ("Thursday") to
  absolute ones when you write the note, or it stops making sense once
  time passes.
- **reference** — pointers to where something lives in another system
  (a dashboard, a ticket tracker, a channel) — not the information itself,
  just where to find current information.

## Update, don't duplicate

Before writing a new note, check whether an existing one already covers
the same fact — if it does, update that file rather than creating a
near-duplicate. A memory store with five overlapping notes on the same
rule is worse than one that's slightly out of date, because it's no
longer clear which version is current.

## What NOT to store here

Anything derivable by reading the current state of your system: code,
file paths, recent changes (that's what `git log` is for), or a fix's
mechanics (the fix is in the code; put the *why* here if it's non-obvious,
not the *what*). Memory is for facts that would otherwise be lost between
sessions — not a second copy of things already written down elsewhere.

## The index (`MEMORY.md`)

`MEMORY.md` in this same directory is a **loader**, not a store — short
bullets pointing at notes, grouped by topic, capped in size. Add an
index bullet for always-on notes (behavioral rules your agent should
apply without being asked); collapse on-demand/reference notes to a
name-only line under their topic instead. Keep the index itself short —
if it's growing long, you're promoting too much to always-on.

## cwd matters, again

Worth repeating because it's the one gotcha that silently breaks this
whole system: memory is keyed to the working directory your agent
launches from. Two different launch directories can mean two different,
non-overlapping memory stores on the same machine. If a note you expect
to be there isn't, check the cwd before you assume the store is empty.
