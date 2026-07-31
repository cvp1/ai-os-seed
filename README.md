# AI-OS Seed

**The operations floor for a personal AI operating system.**

Install it on a machine you own and your AI agent stops being a chat
window. It runs jobs on a schedule while you sleep, keeps a record of
every run, notices when something quietly stops, holds secrets it can't
leak into a transcript, and turns your corrections into memory it still
has next month.

[![install test](https://github.com/cvp1/ai-os-seed/actions/workflows/install-test.yml/badge.svg)](https://github.com/cvp1/ai-os-seed/actions/workflows/install-test.yml)
&nbsp;·&nbsp; public alpha &nbsp;·&nbsp; MIT &nbsp;·&nbsp; no telemetry, no
accounts, no network calls

---

## What this is

A personal AI operating system has three layers. This repo is the bottom
one — the part that makes the other two dependable.

| Layer | What it does | Who ships it |
|---|---|---|
| **Your system** | the jobs, adapters and skills you build for your own life | you |
| **Substrate** | secrets vault · scheduler · run history · freshness backstop · memory discipline · skills conventions | **AI-OS Seed** (this repo) |
| **Assistant** | the agent you actually talk to — mail, calendar, questions | [AI-OS Core](https://craigvandeputte.com), or whatever you already use |

In one sentence: **AI-OS Seed turns a machine with an AI coding agent on
it into an operated system** — scheduled, observable, secret-safe, and
able to learn — owned entirely by you, with nothing phoning home.

It is not a SaaS, not a framework to code against, and not a chatbot. It
is a working substrate plus the doctrine that holds it together, extracted
live from a system that runs a high-desert Arizona ranch — solar, water,
cameras, a small machine fleet, and the operator's working life. That
system is young and says so: built in the open since late May 2026, it
grew to ~80 small projects running ~17,000 scheduled jobs a week on
exactly this substrate. This repo *is* the extraction pipeline's output.
Nothing here is hand-written prose about a system; it's the system's own
OS layer, genericized.

**Status: public alpha.** The honest ledger of what's proven is at the
bottom. If you expect polish, come back later; if you're an operator who
reads source, welcome — you're exactly who this is for.

## Who it's for

Someone comfortable in a terminal, who owns a machine that stays on and
has something real worth watching. You'll finish the install with a
scheduled job running green and reporting into your own run database —
then you grow your own system on top.

Want a personal AI assistant without the terminal? Start with
[AI-OS Core](https://craigvandeputte.com) instead; you can add this later.

---

## Install

### Before you start

| Requirement | Check |
|---|---|
| Linux or macOS | `uname` |
| Python 3.9+ | `python3 --version` |
| PyYAML | `python3 -c "import yaml"` |
| git | `git --version` |
| A machine that stays on | scheduled jobs don't run on a sleeping laptop |

On macOS, Homebrew Python refuses a bare `pip3 install pyyaml` (PEP 668).
Use a venv — `python3 -m venv ~/.venvs/aios-seed && ~/.venvs/aios-seed/bin/pip
install pyyaml` — then use `~/.venvs/aios-seed/bin/python3` everywhere below.
Your agent handles this for you on the agent-driven path.

### Step 1 — decide where it goes

**Already have a workspace your agent works in?** (An AI-OS Core install
counts.) The seed moves *into* it. One root, one memory, one agent — a
second directory is literally a second brain that can't see the first.

**Starting fresh?** It gets its own directory, `~/ai-os-seed` by default.

### Step 2 — let your agent install it (the primary path)

Open Claude Code on that machine and paste this:

> Set up AI-OS Seed for me. Clone
> `https://github.com/cvp1/ai-os-seed` (tag `v0.2.5-alpha`) into
> `~/tools/ai-os-seed`, then read `AGENT-INSTALL.md` inside the clone and
> follow it exactly. Show me every command before you run it.

That's the whole install. Your agent then works through
[`AGENT-INSTALL.md`](AGENT-INSTALL.md):

| Phase | What happens |
|---|---|
| **0 · Readiness** | checks the requirements above, and surveys the machine for a prior install (`install.py --detect`) before writing anything |
| **1 · Interview** | asks where it should live and what you actually want to watch |
| **2 · Install** | runs `install.py` — the only thing that moves bytes |
| **3 · Verify** | selftest, then the demo job through the real run logger |
| **4 · Personalize** | writes you a `CLAUDE.md` — the one file it authors |
| **5 · Memory** | writes your first memory note, by doing it rather than describing it |
| **6 · First win** | schedules the demo job for real, on crontab or launchd |
| **7 · Hand back** | tells you what you now have and what to build next |

Two rules the installer keeps: **every mutating command is shown before it
runs**, and **only scripts move bytes** — your install is byte-identical to
this repo, never agent-transcribed.

### Step 3 — the commands, if you'd rather drive

`AGENT-INSTALL.md` is written for an agent, but every step is a plain
command. The short version:

    git clone --branch v0.2.5-alpha https://github.com/cvp1/ai-os-seed ~/tools/ai-os-seed
    cd ~/tools/ai-os-seed
    python3 install.py --detect                      # read-only: any prior install?

    python3 install.py --target ~/ai-os-seed         # fresh directory
    # ...or, joining a workspace you already have:
    python3 install.py --target <your-workspace> --into

    python3 <ROOT>/_lib/selftest.py
    python3 <ROOT>/observability/log_run.py --job hello_fleet -- python3 <ROOT>/demo/hello_fleet.py
    python3 <ROOT>/observability/report.py --job hello_fleet     # one `ok` row
    python3 <ROOT>/observability/freshness.py --all              # [OK] hello-fleet

Then read Phases 4–6 of `AGENT-INSTALL.md` for the parts worth doing by
hand: your `CLAUDE.md`, your first memory note, and scheduling the demo
job for real.

### Undo

    python3 install.py --target <ROOT> --uninstall

De-schedules the jobs, then removes only what the seed added. In a
workspace it joined, everything of yours stays exactly where it was. It
refuses to touch a tree that isn't ours.

---

## What you get

| Component | What it gives you |
|---|---|
| `PRINCIPLES.md` | 17 generative operating principles the rest reasons from |
| `_lib/` | stdlib-only spine: lock-aware secrets loader, event bus, report builder, import selftest |
| `keyvault/` | encrypted-at-rest secrets directory (fscrypt on Linux), fail-closed when locked |
| `scheduler/` | manifest-as-source-of-truth job scheduling on plain crontab (Linux) / launchd (macOS), with drift detection |
| `observability/` | one SQLite row per scheduled run, plus a freshness backstop that catches jobs that silently stop |
| `demo/hello_fleet.py` | the first win: one heartbeat job proving the whole spine end to end |
| `memory/` | two-tier memory scaffold — `MEMORY.md` index, one fact per note |
| `skills/improve` | corrections and preferences you teach become durable memory notes |
| `skills/recall` | "what do I know about X" over your notes and run history, with citations |
| `skills/status` | one honest screen answering "how is my system doing" — read-only, distrust-green by design |
| `skills/skill-center` | authoring conventions plus a scaffold/audit tool, for the skills you build |
| `views/weekly.py` | a weekly `NOW.md` derived from your own run history and git activity — "store facts, derive views," made concrete |
| `governance/` | a permission matrix your agent is *held to*: policy → compiled hook → a 21-probe conformance suite that makes it refuse on camera |

The last five rows are **the cognitive spine**: the loop that makes this an
operating system rather than cron with logging. Jobs produce facts, facts
become memory, memory makes the next session smarter.
`memory/THE-LOOP.md` maps which piece serves which arrow.

### Approving what the agent can't do alone

Some actions the agent proposes instead of taking — that's the permission
matrix doing its job. Each one is written to your staged directory as a small
JSON file holding the **whole** action: the tool, every argument, and a
digest of those exact bytes.

Read it, then before you apply it:

```sh
python3 governance/hooks/profile_gate.py --verify-staged <the-file>.json
```

`OK` means the proposal is byte-for-byte what you read. `REFUSED` means it
changed after it was staged — don't apply it. A file with no digest is also
refused, because "nothing to check" must never read as "checked."

This exists because of a specific failure that is easy to build by accident:
an approval prompt that asks for a click, a PIN, or a signature without
showing what's being approved. That collects your *presence*, not your
*consent*, and hands whoever chose the content your authority. Principle 17,
and the conformance suite proves it — one probe stages a real action, mutates
it behind your back, and fails if the tamper isn't caught.

Watch the whole matrix refuse, on your own machine:

```sh
python3 governance/conformance/run_conformance.py   # 21 probes, no network, no API key
```

### If you already run AI-OS Core, this is its upgrade

The seed composes into the workspace you have (`install.py --target
<workspace> --into`) and your assistant gains an operations floor:

- **It acts on a schedule now, not just in conversation.** Jobs run while
  you're away; the manifest is the source of truth, and a drift check
  catches hand-edits before you find them the hard way.
- **It keeps a record of every run.** "Did the backup actually run last
  night?" becomes a query, not a feeling.
- **It notices silence** — the failure mode per-job checks can't see.
- **Secrets stay out of transcripts.** Encrypted at rest, read at point of
  use, fail-closed when locked.
- **New ops verbs.** `/status` answers honestly in one screen; `/improve`
  and `/recall` now ground themselves in run history as well as memory.

Your memory, your `CLAUDE.md`, your files are left exactly as they are.
The install refuses rather than touch anything of yours, and an existing
`memory/` counts as *satisfied*, not colliding — your live memory already
is the thing the empty scaffold exists to start.

---

## The honest ledger (alpha)

**Verified on real hardware, both OSes.** Installed by hand on a real Mac
— launchd job live, freshness green, the full spine confirmed — as well as
on a daily-driver Linux box.

**CI runs the whole install on every push**, on `ubuntu-latest` and
`macos-latest`: install, selftest, the demo job through `log_run.py`,
`freshness.py` reporting OK, a real crontab/launchd install with
drift-check, then `--uninstall` removing the entire footprint. Green badge
means the install worked on both OSes as of the last push — observed, not
"should work."

**The cognitive spine is live-verified end to end.** In one fresh sandbox:
the demo job logged its row, `/status` reported it honestly — and in a
second run with its command tools withheld, correctly refused to fabricate
a green answer rather than guess — a planted correction became a memory
note via `/improve`, `/recall` found and cited that note from a
plain-language question, and `views/weekly.py` wrote a `NOW.md` whose
numbers matched `report.py --stats` by hand. Every skill passes this
repo's own `skill-center/audit.py`.

**Live-verified beyond CI**, in an isolated sandbox: STALE detection
(backdate a run, confirm `freshness.py` flags it); crontab content-drift
and orphan-job detection; and that your pre-existing crontab entries
survive untouched.

**What isn't covered:** your specific machine's quirks. The first
real-hardware install found a genuine bug
([#1](https://github.com/cvp1/ai-os-seed/issues/1), fixed the same day) —
which is the point of shipping an alpha instead of waiting for imagined
completeness. If yours finds the next one, open an issue.

**No telemetry, no network calls, no accounts.** The optional "confirm
back" at the end of the install is you choosing to open a GitHub issue
saying it worked. That's the entire mechanism.

`CLAUDE.md.template` and `README.md.template` are structural references
only — your agent drafts your real `CLAUDE.md` fresh at install rather
than copying them.

## Provenance

This substrate is extracted from a live system by a parity-gated,
leak-audited build pipeline (the pipeline stays upstream — it necessarily
knows the private names it scrubs). The extraction is re-run against the
live tree and drift-checked before every release, so accuracy here is a
property of the pipeline, not an editorial promise. Built by the operator
of the ranch it runs — more at
[craigvandeputte.com](https://craigvandeputte.com).

## License

MIT. A gift, like the rest of the family — no tiers, no paid layer.
