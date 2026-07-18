---
name: recall
description: Answer "what do I know about X" or "have I dealt with Y
  before" by searching this system's own memory notes and operational
  history. Use when the operator says "/recall", asks the agent to recall,
  remember, or look up something from a past session, or asks an
  operational question about job history ("when did this last fail",
  "has X ever broken before", "what happened with Y last week").
---

# /recall — memory reaches the session

Memory that nothing queries is a write-only diary. This skill searches the
two sources this system actually has and answers with citations — never a
recalled fact stated as if it were common knowledge.

## The two sources

1. **Memory notes** (`memory/*.md`, indexed by `memory/MEMORY.md`) — facts,
   preferences, decisions, corrections recorded by `/improve` or by hand.
   See `memory/CONVENTIONS.md` for the note format.
2. **Operational history** (`observability/report.py --json`, plus
   `observability/freshness.py --json` for current state) — the run log
   for every scheduled job: when it ran, whether it succeeded, what it said.

Nothing else. This system doesn't ship a vault, a wiki, or an embedding
index — as your system grows past these two sources, add them here.

## Procedure

1. **Classify the question.** A fact/preference/decision question ("what
   did we decide about X", "how does the operator like Y done") searches
   memory notes. An operational question ("when did X last run", "has Y
   failed before", "what's Z's status") queries `report.py`. Some
   questions need both — search both rather than guessing which one has
   the answer.
2. **Search memory notes.** Start with `MEMORY.md`'s index bullets (cheap,
   already loaded). If the index doesn't obviously answer it, grep note
   filenames and descriptions, then note bodies if still nothing.
   Plain-text search over `memory/` is enough at this scale — no ranking
   model needed for a few dozen files.
3. **Query operational history** when relevant:
   `observability/report.py --job <name> --json`,
   `--failures --json`, or `--since 7d --json` depending on the question.
   Read the actual rows; don't guess at what they'd say.
4. **Rank and answer.** Lead with the most directly relevant fact. Cite
   what you found: the note's filename for a memory fact, the run's
   `started_at` + job name for an operational one. A recall without a
   citation is indistinguishable from a guess — never give one.
5. **State what you did NOT find, plainly.** If the search comes up empty
   or partial, say so — "no memory note covers this" or "runs.db has no
   record before <date>" — rather than filling the gap with a plausible-
   sounding improvisation. A recall that hides its own gaps trains false
   confidence in the exact thing it exists to prevent.

## Example

> **Q:** "Has the demo job ever failed?"
> **A:** Checked `observability/report.py --job hello_fleet --failures
> --json` — 0 failing rows across 47 recorded runs, oldest 2026-07-01.
> No memory note references it either. As far as the record goes: no.
