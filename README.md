# AI-OS Seed

A self-installing seed of a working personal AI operating system — the
substrate layer that turns "a machine with Claude Code on it" into an
operated system: a secrets vault, a manifest-driven scheduler, run
observability with a liveness backstop, memory discipline, and skills
conventions. Extracted live, by a leak-audited build pipeline, from
a system that runs a high-desert Arizona ranch — solar, water, cameras, a
small machine fleet, and the operator's working life. The system is young
and says so: built in the open since late May 2026, it grew to ~80 small
projects running ~17,000 scheduled jobs a week on exactly this substrate.
This repo *is* that pipeline's output; nothing here is hand-maintained
prose about a system, it's the system's own OS layer, genericized.

**Status: public alpha.** Honest ledger of what's proven and what isn't is
below. If you expect polish, come back later; if you're an operator who
reads source, welcome — you're exactly who this is for.

## Who this is for

An **operator**: comfortable in a terminal, owns a machine that stays on,
has something real worth monitoring. You'll end the install with a
scheduled job running green and reporting into your own observability
store — then you grow your own system on the substrate. (If you want a
personal AI assistant without the terminal, this isn't the edition for
you — start with [AI-OS Core](https://craigvandeputte.com) instead.)

## Install (agent-driven — the primary path)

Open Claude Code on the machine that will run the system and paste:

> Set up AI-OS Seed for me. Clone
> `https://github.com/cvp1/ai-os-seed` (tag `v0.2.0-alpha`) into
> `~/tools/ai-os-seed`, then read `AGENT-INSTALL.md` inside the clone and
> follow it exactly. Show me every command before you run it.

Your agent runs the readiness checks, interviews you, installs via the
deterministic `install.py` (the agent orchestrates; only scripts move
bytes — your install is byte-identical to this repo, never
agent-transcribed), verifies, and finishes with the demo job green in your
own runs.db. Everything it does is shown before it runs, and
`install.py --uninstall` is the whole undo path.

Prefer to drive yourself? `AGENT-INSTALL.md` is written for an agent but
every step is a plain command — follow it by hand.

## What's in the seed

| Component | What it gives you |
|---|---|
| `PRINCIPLES.md` | 15 generative operating principles the rest reasons from |
| `_lib/` | stdlib-only spine: lock-aware secrets loader, event bus, report builder, import self-test |
| `keyvault/` | encrypted-at-rest secrets dir pattern (fscrypt on Linux), fail-closed when locked |
| `scheduler/` | manifest-as-source-of-truth job scheduling for plain crontab (Linux) / launchd (macOS), with drift detection |
| `observability/` | one SQLite row per scheduled run + a freshness backstop that catches jobs that silently stop |
| `demo/hello_fleet.py` | the first win: one heartbeat job proving the whole spine end-to-end |
| `memory/` | two-tier memory scaffold (`MEMORY.md` index + one-fact-per-file notes) — your agent writes the first note itself, at install |
| `skills/improve` | corrections and taught preferences this session become durable memory notes |
| `skills/recall` | "what do I know about X" over your memory notes + run history, with citations |
| `skills/status` | one-verb honest answer to "how is my system doing" — read-only, distrust-green by design |
| `skills/skill-center` | skill-authoring conventions + a scaffold/audit tool, for skills you build yourself |
| `views/weekly.py` | scheduled, stdlib-only: a weekly `NOW.md` derived from your own run history + git activity — "store facts, derive views" made concrete |

The last five rows are **the cognitive spine** (Wave 1.5): the loop that
makes this an operating system rather than cron with logging — jobs
produce facts, facts become memory, memory makes the next session
smarter. `memory/THE-LOOP.md` (written at install) is the one-page map of
which piece serves which arrow.

## The honest ledger (alpha)

**Verified on real hardware, both OSes.** I've installed this myself on a
real Mac — launchd job live, freshness green, the full spine confirmed —
in addition to my own daily-driver Linux box. **CI also runs the full
install on every push** — [![install
test](https://github.com/cvp1/ai-os-seed/actions/workflows/install-test.yml/badge.svg)](https://github.com/cvp1/ai-os-seed/actions/workflows/install-test.yml)
— on `ubuntu-latest` and `macos-latest`: install, selftest, the demo job
through `log_run.py`, `freshness.py` reporting OK, a REAL crontab (Linux)
/ launchd (macOS) install with drift-check, then `--uninstall` removing
the whole footprint. If that badge is green, the install worked on both
OSes as of the last push — not "should work," observed.

The first real-hardware install caught and reported a genuine bug
([#1](https://github.com/cvp1/ai-os-seed/issues/1), fixed same day) — that's
the whole point of shipping this as an alpha instead of waiting for
imagined completeness.

What CI doesn't cover: your specific machine's quirks. Live-verified in
an isolated sandbox beyond CI:
- STALE detection (backdating a run and confirming `freshness.py` flags it)
- crontab content-drift and orphan-job detection, and that your
  pre-existing crontab entries survive untouched

Known rough edge: Homebrew Python on modern macOS refuses a bare `pip
install pyyaml` (PEP 668) — caught live on real hardware and by this
project's own CI. AGENT-INSTALL.md's readiness phase has the fix (a
venv, or `--break-system-packages`).

`CLAUDE.md.template` / `README.md.template` are structural references
only — your agent drafts your actual `CLAUDE.md` fresh at install
instead of copying them.

**The cognitive spine (v0.2.0-alpha, live-verified end to end.)** In one
fresh sandbox install: the demo job ran and logged a row, `/status`
reported it honestly (and, in a second run with its command tools
withheld, correctly refused to fabricate a green answer rather than guess
— exactly the doctrine it's supposed to enforce), a planted correction
became a memory note via `/improve`, `/recall` found and cited that note
from a plain-language question, and `views/weekly.py` wrote a `NOW.md`
whose numbers matched `report.py --stats` by hand. Each skill also passes
this repo's own `skill-center/audit.py`.

No telemetry, no network calls, no accounts. The optional "confirm back"
at the end of install is you choosing to open a GitHub issue saying it
worked — that's the entire mechanism.

## Provenance

This substrate is extracted from a live system by a parity-gated,
leak-audited build pipeline (the pipeline stays upstream — it necessarily
knows the private names it scrubs). The extraction is re-run against the
live tree and drift-checked before every release: accuracy here is a
pipeline property, not an editorial promise. Built by the operator of the
ranch it runs — more at [craigvandeputte.com](https://craigvandeputte.com).

## License

MIT. A gift, like the rest of the family — no tiers, no paid layer.
