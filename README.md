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
> `https://github.com/cvp1/ai-os-seed` (tag `v0.1.3-alpha`) into
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
| `skills/` | skill-authoring conventions + a scaffold/audit tool |
| `memory` conventions | (via your agent, at install) two-tier memory index discipline |

## The honest ledger (alpha)

Live-verified, in an isolated sandbox, on Linux:
- demo job → `log_run.py` → runs.db row → `report.py` shows it
- `freshness.py` reports OK, and correctly flags STALE when a job goes silent
- crontab reconciliation: install, idempotent re-run, content-drift and
  orphan detection, preservation of your pre-existing crontab entries

Built and reviewed but **not yet run against real infrastructure**:
- macOS: launchd plist generation is unit-tested; never run on a real Mac
- the scheduler's `sync.py` needs PyYAML (`python3 -c "import yaml"` to
  check; readiness will tell you)
- `CLAUDE.md.template` / `README.md.template` are leak-scrubbed exports,
  not finished prose — your agent drafts your actual CLAUDE.md fresh at
  install instead

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
