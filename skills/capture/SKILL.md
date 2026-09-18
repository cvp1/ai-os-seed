---
name: capture
description: Close the write-back loop at the end of a substantive session — distill what was decided and *why* and file it into the right destination (auto-memory for agent-facing rules, the vault for durable decisions/knowledge, a session brief for work left unfinished). Use when Craig says "/capture", "capture this session", "write that back", "log this decision", "freeze this", "hand this off", or at the end of a session where real thinking happened — or real work was left open — that would otherwise evaporate. The sibling of /improve (which is auto-memory-only) — /capture routes to all three.
---

# /capture — write the session's thinking back into the right brain

Real thinking happens *in the session*. When the context window closes, the
reasoning — the *why* behind what was decided — is lost unless it's written
down. `/improve` captures the agent-facing half (preferences, corrections,
gotchas → auto-memory). `/capture` is the **full** loop: it also routes durable
**decisions and domain knowledge** into the vault, so each session compounds
instead of leaking.

Those two brains both store what is *settled*. A substantive session usually
also leaves work that is **not** settled — a goal half-met, threads still open,
a next action obvious only to whoever was in the room. That evaporates the same
way, and costs more to rebuild. So `/capture` has a third destination: a
**session brief**, the harness-neutral artifact another agent (or another
provider, or a local model) can resume from cold.

This skill **reuses `/improve`'s engine** (harvest → filter-hard → classify →
dedup → stage). Its additions are the **router**, a vault write-path, and the
brief.

**Auto-memory store:** `~/.claude/projects/<workspace path with / → ->/memory/`
— DERIVED from the workspace root, never typed. Print this install's:
`/usr/bin/python3 -c "import sys; sys.path.insert(0,'memory-mesh'); \
import mesh_lib; print(mesh_lib.store_dir())"`
(index `MEMORY.md`). **Vault:** `~/notes` (`$OBSIDIAN_VAULT_PATH`).
**Briefs:** `session-brief/briefs/`, relative to the workspace root.

**The engine (Story D2 — mechanics in the repo, judgment here):**
- `memory-mesh/pipeline.py` — mechanical stages as a CLI
  (`harvest` / `dedup` / `sections` / `stage`), JSON out, read-only.
- `memory-mesh/memory_write.py` — the **one sanctioned
  writer** into auto-memory. `pipeline.py stage` only proposes its invocation.

Both paths are **relative to the workspace root** (the directory
holding `memory-mesh/`) — `cd` there first, or spell the path from it.
That is the one door on every harness and every install, which is why
it is not an absolute path to anyone's home.
- `session-brief/session_brief.py` — the **one sanctioned writer**
  for briefs (`write` / `resume` / `list` / `show`). Never hand-format a brief;
  the caps, the mandatory-field checks and the truncation notes only hold if
  everything goes through it.
- The vault side has no writer CLI on purpose — vault writes stay propose-only
  prose (see below).

## When to reach for it
- Craig runs `/capture` (optionally scoped: `/capture the enrich decision`).
- **Proactively, at the end of a substantive session** — one where a decision
  was made, an approach was chosen with a reason, or domain understanding
  advanced. Offer it; don't wait to be asked. (Skip trivial/one-off sessions.)

## Procedure

### 1. Harvest + filter (same as /improve — judgment)
Re-read the session. Pull every **durable, cross-session-useful** item:
decisions, chosen approaches + the *reason*, corrections, stated preferences,
domain insights, project/context facts. **Filter hard** — drop one-off
specifics, anything already in `CLAUDE.md`/repo/git, anything you can't state as
a concrete applicable rule or a decision with a rationale. (Working from
transcript *files* instead of live context? `pipeline.py harvest <file>...`
pre-scans for signal lines — a pre-filter, not a verdict.)

**Hard cap: 1–3 items.** The cap forces selection — capture the *load-bearing*
thinking, not a transcript. (A judgment cap, kept prose-side on purpose.)

### 1.5 Earn the write (judgment — run BEFORE routing)
Step 1 asks whether an item already has a home. This asks whether it deserves a
*new* one. The bar is the standing rule in `CLAUDE.md` — read it there, it is
not restated here.

- **Is it FORCED by a principle for this case?** Not "does it resemble one" —
  resembling is fine and common. A measured gotcha that *illustrates* a
  principle without being implied by it is exactly what memory is for. Drop only
  when `PRINCIPLES.md` already compels the same behaviour with no fleet-specific
  residual left over.
- **Is only the general half durable?** Keep the part specific to this
  workspace; drop what any capable model already knows.
- **Does it belong somewhere that isn't a brain?** `CLAUDE.md` for a
  workspace-wide property, a code comment for a one-file gotcha, a script for a
  measurement worth running twice (PRINCIPLES 16). If so: **name the right home
  to Craig and write no memory.** Proposing that line is a hand-off, not a
  capture deliverable — don't count it as one.

`stage` ENFORCES this: it refuses without `--not-implied-by`, which must name
the residual concretely. The prose above is how to answer it, not the gate.

A capture that proposes nothing and names what it rejected, with reasons, is a
good capture.

### 2. Route each item (the new part — judgment)
The discriminator: **is this a rule for the *agent*, or knowledge for *Craig*?**

| The item is… | → Destination | Form |
|---|---|---|
| How to work / a preference / a correction | auto-memory `feedback` | atomic one-liner |
| Who Craig is | auto-memory `user` | atomic |
| A fleet/infra fact or harness gotcha | auto-memory `project`/`reference` | atomic |
| **A decision + its rationale** ("chose X over Y because…") | **vault** `06 Logs/Decisions/` + link to the canonical project/area note | dated, narrative |
| **Domain synthesis** (ranch, energy, career, AI strategy) | **vault** — the PARA note it extends | prose, builds over time |
| A decision you'll also *act on as an agent* AND revisit | **both** — one-line pointer in memory, full rationale in vault | linked |
| **Work that isn't finished** (open threads, an obvious next action) | **session brief** — `session-brief/briefs/` | one brief per goal |

The brief is orthogonal to the other two, not a fourth kind of lesson: memory and
vault hold what is *settled*, the brief holds what is *live*. A session can
produce all three, or only a brief (real work, no new insight), or only memory
(a correction, nothing left open). Route independently — never skip the brief
because the lessons were thin, or vice versa.

Test for vault-not-memory: *does it have a why that won't compress to a rule,
and would Craig browse / link / build on it later?* → vault.

### 3. Dedup before writing
- Auto-memory (mechanical):
  ```bash
  /usr/bin/python3 memory-mesh/pipeline.py dedup \
    --slug <proposed-slug> --keywords "<3-8 topical words>"
  ```
  Update an existing file rather than duplicating; when a belief *changed*,
  supersede rather than delete (full semantics: `/improve` step 4).
- Vault: search for an existing note on the topic (`wiki/ask.py "<topic>"`);
  extend it rather than spawning a near-duplicate.

### 4. Stage + propose, then write
For each **auto-memory** item, run it through the structural filter, which
emits the exact writer invocation as a proposal (executes nothing):
```bash
/usr/bin/python3 memory-mesh/pipeline.py stage \
  --slug <kebab-slug> --type feedback|user|project|reference \
  --description "<one-line description>" --lineage craig-direct|contains-untrusted \
  --rule "<the lesson>" [--why "<...>" --how "<...>"] \
  --hook "<short MEMORY.md hook>" --section "<MEMORY.md section header>" \
  --not-implied-by "<what PRINCIPLES.md does not force for this case>" \
  [--supersedes old-slug]
```
**Set `lineage:` honestly (Story 029, required — no default, by design):**
`contains-untrusted` if the lesson traces back to email/web/mesh content the
session ingested — the writer then files it in `QUARANTINE.md`, not always-on
`MEMORY.md`, until Craig promotes it. Classify by WHO AUTHORED THE CLAIM
(external assertion vs first-party observation). `/capture` is *especially*
exposed here (it routes session content), so when in doubt,
`contains-untrusted`. Only you can set this honestly — the tool can only check
it's present and valid.

**Show Craig, for each item: destination · type · lineage · the exact text.**
He approves / edits / drops each. Then:
- **Auto-memory writes**: run the staged `preview_command` (memory_write.py dry
  run), then the `commit_command`. memory_write.py stays the only writer —
  never hand-format the frontmatter/index line.
- **Vault writes stage as a proposal** Craig applies (mirrors `/wiki enrich`;
  the vault is 2-way Syncthing'd, so don't fight it) — UNLESS he explicitly says
  to apply/write it, then write directly (the hourly backup commits it). This
  gate is prose because the vault deliberately has no memory-style writer CLI.

### 5. Freeze what isn't finished (the brief — judgment)
**Do this last**, after the memory/vault writes, so the brief describes final
state rather than mid-session state. This step's mechanics are also
available standalone as **`/freeze`** — reach for that directly when a brief
is the only thing worth writing (nothing durable enough for memory or the
vault); come back through `/capture` when memory/vault destinations are also
in play. Same payload schema either way (`session-brief/README.md` is
canonical for both).

Offer a brief when the session leaves real work open: threads not closed, a next
action that is obvious to you and to nobody else, or anything you'd have to
re-explain to pick back up. Skip it when the work genuinely finished — a brief
whose next action is "nothing" is noise, and briefs are recall surface.

```bash
printf '%s' "$payload" | /usr/bin/python3 session-brief/session_brief.py write
```

Payload keys: `goal` and `next_action` are **required** (the writer refuses
without them — no goal means it can't be resumed, no next action means it's a
summary). Then `constraints`, `decisions`, `state`, `files`, `open_threads`,
`provenance`, plus `harness` / `model` / `host`.

**Always set `harness`, `model` and `host` explicitly** — they default to
`unknown` / `unknown` / the machine's nodename, and a nodename is not a
routable name in any fleet vocabulary (one fleet host answers to `iMac`).
Use the host's fleet slug. The resume preamble prints all three, so a brief
that lies about its origin misleads whoever picks it up.

Three of those carry most of the value, and each fails a specific way if you get
lazy:

- **`constraints`** — restate them in full, every time, even though they're in
  `AGENTS.md`. Doctrine does not travel with capability; a brief may be resumed
  by a harness that never loaded any of it. And phrase each so that *degrading*
  it fails safe — a smaller model compresses, and compression turns a trailing
  qualifier into permission (`[[handoff-constraints-must-degrade-safe]]`).
- **`provenance`** — what you actually *verified*, not what you believe. The
  resume header instructs the reader to treat everything absent from this list as
  unconfirmed. Omitting it hands the next agent your assumptions with more
  confidence than you had.
- **`decisions`** — the *why*, not the what. "Chose X over Y because Z" so the
  next agent doesn't re-litigate a settled call, or worse, silently reverse it.

Lists are capped and fields clip at 600 chars; **every truncation is reported**
to stderr and in the brief. Read those notes — if something load-bearing got cut,
shorten it yourself rather than letting the cap choose.

Briefs are **written directly, not proposed** — a brief is a snapshot of state
you already have, mutates nothing Craig owns, and is trivially deletable. Show
him the path and the one-line goal after writing. To hand one to another host,
post it as a signed `resume` task (see `session-brief/README.md`).

## Vault write conventions (when applying)
- **Decisions** land in `06 Logs/Decisions/<YYYY-MM-DD> <slug>.md` with
  frontmatter `tags: [decision]` + a `Decision / Why / What changed / Links`
  body. The dated log is thin; it **links out** to the canonical PARA note
  (don't duplicate that note's content).
- **Minimal metadata contract, new/active notes only** (second-brain eval,
  2026-08-06 — no mass backfill, this is the write path that keeps new notes
  from joining the ~87% with no frontmatter): every note this skill writes or
  meaningfully edits carries `created` (date, set once), `last_verified`
  (date, bump on any substantive edit — not on a passing link fix), and
  `lifecycle` (`live` | `stale` | `archived` — three explicit states, not an
  overloaded `status` string). Set `lifecycle: live` on write; a note only
  becomes `stale`/`archived` by a deliberate edit, never inferred silently.
- **Wikilinks resolve by FILENAME, not the H1 title** — use `[[stem|Nice Title]]`
  when they differ (em-dash titles, kebab-case Synthesis notes), or the link
  silently dangles. See auto-memory `[[vault-bulk-edit-safety]]`.

## Guardrails
- One fact per memory file; one decision per vault log entry; one goal per brief.
- **Never store secrets** — credential values live in `~/.key/` (`stage` also
  refuses secret-shaped text, but rephrase rather than rely on the scanner).
- Propose-only into the vault by default; auto-memory is direct **only after
  Craig's per-item approval**, and only through memory_write.py. Briefs are
  direct — they record state rather than mutating anything Craig owns.
- This is the *capture* half. *Applying* a captured lesson needs nothing extra —
  auto-memory recall is automatic; vault recall is `/recall` or `/wiki`; a brief
  is thawed with `session_brief.py resume --id latest`.

## Relationship to the rest of the system
- **`/improve`** = capture → auto-memory only. `/capture` = capture → all three
  destinations. When only a behavioral rule is in play, `/improve` is fine; when a
  *decision, domain insight, or unfinished work* is in play, use `/capture`.
- **`session-brief`** is the fifth transferable asset in the modular-harness
  goal — tools, memory, connectors and doctrine already move between providers;
  work-in-progress did not until it became a file. Validated 2026-07-28 against
  opencode/grok-4.5 and a local gemma4-e4b, both resuming cold from a brief
  alone. `cc-handoff`'s signed `resume` verb moves one between hosts.
- **`/ingest`** writes the vault from `_inbox` raw sources; `/capture` writes it
  from *session reasoning*. Different inputs, complementary.
- **Phase 2 (LIVE since 2026-06-30):** a gated Stop hook
  (`~/.claude/hooks/capture-nudge.py`, wired in `settings.json`) auto-prompts
  `/capture` at session end when ≥3 file mutations happened and no capture signal
  is present. Fail-open, fires once (no loop), opt-out via
  `~/.claude/.capture-skip/<session_id>`. It's the **backstop**; the norm above
  is still the primary trigger (the hook can't see pure-discussion decisions —
  only file-mutation-heavy sessions).
