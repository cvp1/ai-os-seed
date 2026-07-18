<!--
  This is the memory INDEX — a loader, not a content store.

  Every note lives in its own file in this directory (one fact per file).
  This index exists so an agent starting a new session can see, in one
  short read, what it already knows without loading every note in full.

  Two tiers:
    - ALWAYS-ON notes (behavioral rules, standing facts your agent should
      apply on every session without being asked) get a bullet below,
      grouped under a topic heading.
    - ON-DEMAND / reference notes (useful but not worth loading every
      session) are named in an "on-demand" line under their topic instead
      of getting a full bullet — search for them by name when a task
      touches that topic.

  Keep this file SHORT. If it's growing past a couple hundred lines, you're
  promoting too much to always-on — most notes belong on-demand. See
  CONVENTIONS.md in this directory for the note format, the four memory
  types, and the frontmatter schema.

  This file is a scaffold: empty on install. Your agent (or you) populate
  it as you go — see memory/CONVENTIONS.md and skills/improve/SKILL.md.
-->

# Memory

## 🆕 Unsorted
<!-- New notes land here until you group them into topics that make sense
     for your own system. Recategorize freely — this index is yours. -->
