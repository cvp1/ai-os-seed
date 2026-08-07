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
| **Substrate** | secrets vault · scheduler · run history · freshness backstop · event-sourced memory · skills conventions | **AI-OS Seed** (this repo) |
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
> `https://github.com/cvp1/ai-os-seed` (tag `v0.2.6-alpha`) into
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
| **5 · Memory** | writes your first memory note by doing it rather than describing it, then starts the memory mesh underneath it (`memory-mesh/install.sh`) so the index becomes generated rather than hand-kept |
| **5.5 · Governance** | nothing to run — the permission-matrix layer is withheld in this release, and the phase says why rather than pretending it was never there |
| **6 · First win** | schedules the demo job for real, on crontab or launchd |
| **7 · Hand back** | tells you what you now have and what to build next |

Two rules the installer keeps: **every mutating command is shown before it
runs**, and **only scripts move bytes** — your install is byte-identical to
this repo, never agent-transcribed.

### Step 3 — the commands, if you'd rather drive

`AGENT-INSTALL.md` is written for an agent, but every step is a plain
command. The short version:

    git clone --branch v0.2.6-alpha https://github.com/cvp1/ai-os-seed ~/tools/ai-os-seed
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
hand: your `CLAUDE.md`, your first memory note, `bash <ROOT>/memory-mesh/install.sh`
to start the event log under it, and scheduling the demo job for real.

### Undo

    python3 install.py --target <ROOT> --uninstall

De-schedules the jobs, then removes only what the seed added. In a
workspace it joined, everything of yours stays exactly where it was. It
refuses to touch a tree that isn't ours.

**The mesh is not covered by it.** If you ran the Phase 5 bootstrap,
`--uninstall` removes `memory-mesh/` but leaves the two timers it
installed and your event log — so clean those up by hand, in this order:

    # Linux
    systemctl --user disable --now memory-fold.timer memory-home-watch.timer
    rm ~/.config/systemd/user/memory-{fold,home-watch}.{service,timer}
    # macOS
    launchctl unload ~/Library/LaunchAgents/local.memory-{fold,home-watch}.plist
    rm ~/Library/LaunchAgents/local.memory-{fold,home-watch}.plist

`~/memory-events` is left alone on purpose — it's your memory, not the
seed's, and deleting it is your call.

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
| `memory-mesh/` | the layer underneath the notes: an append-only event log (plain git, your machine only) that folds into the index, parks contradictions instead of letting them coexist, and can replay the exact view your agent had at any past instant |
| `skills/improve` | corrections and preferences you teach become durable memory notes |
| `skills/recall` | "what do I know about X" over your notes and run history, with citations |
| `skills/status` | one honest screen answering "how is my system doing" — read-only, distrust-green by design |
| `skills/skill-center` | authoring conventions plus a scaffold/audit tool, for the skills you build |
| `views/weekly.py` | a weekly `NOW.md` derived from your own run history and git activity — "store facts, derive views," made concrete |

The last six rows are **the cognitive spine**: the loop that makes this an
operating system rather than cron with logging. Jobs produce facts, facts
become memory, memory makes the next session smarter.
`memory/THE-LOOP.md` maps which piece serves which arrow.

### Memory that has a history, not just a current state

Most agent memory is a file the agent rewrites. That loses the thing you
most want during an incident: *when did it start believing this, and what
did it believe before?* Since v0.2.6 the seed ships `memory-mesh/`
underneath the notes, and Phase 5 of the install turns it on.

What changes for you:

- **Lessons are emitted, not hand-edited.** `/improve` appends an event;
  a fold every five minutes regenerates `MEMORY.md` from the log. The
  index carries a `GENERATED` header from then on — edit it by hand and
  the next fold overwrites you. (Your pre-mesh index is preserved at
  `MEMORY.md.pre-mesh`.)
- **Re-teaching supersedes rather than duplicates.** Say it differently
  next month and the new event replaces the old wording on that subject,
  so the index doesn't silt up with three versions of one rule.
- **Contradictions park loudly.** Two live claims that disagree about the
  same subject stop being served and get flagged, instead of the agent
  quietly picking one.
- **You can replay.** `replay.py` reconstructs the exact view any agent
  had at any past timestamp — the difference between "the agent got that
  wrong" and "the agent was working from what it knew at 03:14."

It runs **solo by default** — one machine, a local git repo at
`~/memory-events`, no server, no network, nothing to enroll in. If you
later run a second machine, `memory-mesh/ENROLL.md` walks you through
making them share one set of beliefs peer-to-peer over ssh (fetch-only,
still no server). The design contract and its proof obligations are
`memory-mesh/SPEC.md` and `memory-mesh/drill.py` — the drill stands up a
three-node mesh in a temp directory against real git and runs the spec's
guarantees as tests, so you can check the claims above rather than trust
them.

One honest edge: the mesh's **signed-event** path — where an operator
signature makes a claim authoritative and clears a park — is built around
the upstream fleet's key layout (an Ed25519 key in `~/.key/signing/` and a
signer registry). Solo installs never need it, and everything above works
without signing. If you go multi-machine and want it, read `sign.py` and
`ENROLL.md` first; expect to adapt paths.

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
- **Its memory gains a history.** The mesh folds your notes from an
  append-only log, so re-teaching supersedes instead of duplicating and
  you can replay what it believed on any past day.

Your `CLAUDE.md` and your files are left exactly as they are. The install
refuses rather than touch anything of yours, and an existing `memory/`
counts as *satisfied*, not colliding — your live memory already is the
thing the empty scaffold exists to start.

**The one exception, stated plainly:** the Phase 5 mesh bootstrap does
change how your memory index is maintained. It copies your existing
`MEMORY.md` to `MEMORY.md.pre-mesh`, backfills its entries into the event
log, and from then on regenerates the index from that log — so it is no
longer a file you edit by hand. Your notes and their content are kept;
the *authoring* moves to `/improve` and `emit.py`. It is one marker file
to undo (`rm <store>/.mesh-generated`, restore the `.pre-mesh` copy), and
if you'd rather keep hand-authored memory, skip Phase 5 step 3 entirely —
everything else in the seed works without the mesh. Note also that the
mesh schedules its own fold and home-watch timers directly on systemd /
launchd; those are separate from `scheduler/manifest.yml` and won't show
up in `/status`.

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
"should work." **What it doesn't cover:** CI calls `install.py` directly —
it never drives `AGENT-INSTALL.md` through an actual LLM agent, so the
primary install path's real trust boundary (an agent reading and obeying
"show every mutating command," "only scripts move bytes," "stop means
stop") has no automated coverage. One live sandbox pass with a real agent
is recorded above; that's the only data point, not a standing check.

**The two highest-stakes agent-authored writes are now bytes-bound, not
just instructed.** `install.py` writes a receipt at every state-changing
step (a pre-write baseline of anything already at your target, and a record
of the two gated writes below); the agent never has an Edit/Write call on
`CLAUDE.md` or runs the memory-mesh bootstrap directly — `install.py
--approve` does, in one atomic step with the human. `install.py --audit
--package <clone>` then compares live state against the receipt and this
repo's own manifest (never against the installed tree — a same-uid agent
rewriting the tree can't rewrite what the audit compares against). Live
regression-tested: content, mode-bit, and symlink tamper on shipped files;
a tampered CLAUDE.md region after approval; an unexpected file; pre-existing
content modified without an approval record — all correctly flagged.
**What it still doesn't cover:** a determined adversarial agent (same
residual as memory-mesh signing's "one honest edge" — this raises the bar
against drift and confusion, not against compromise), and CI does not yet
run this chain end to end (tracked, not yet built). It also does not audit
your whole machine — see the audit's own perimeter disclaimer, printed at
the top of every run.

**The cognitive spine is live-verified end to end.** In one fresh sandbox:
the demo job logged its row, `/status` reported it honestly — and in a
second run with its command tools withheld, correctly refused to fabricate
a green answer rather than guess — a planted correction became a memory
note via `/improve`, `/recall` found and cited that note from a
plain-language question, and `views/weekly.py` wrote a `NOW.md` whose
numbers matched `report.py --stats` by hand. Every skill passes this
repo's own `skill-center/audit.py`.

**The memory mesh is drilled, not just tested.** `memory-mesh/drill.py`
stands up a three-node mesh in a temp directory against real git and runs
the `SPEC.md` guarantees as executable drills — single-writer append,
deterministic fold, contradiction parking, non-fast-forward refusal,
supersede chains, torn-tail healing, replay. Run it yourself; it needs
nothing but Python and git. Its bootstrap on a fresh install (log created,
index flipped to generated, existing notes backfilled, `/improve`'s event
landing in the next fold) was verified end to end in a sandbox. **Not yet
verified:** a real multi-machine mesh installed from *this repo* by
someone other than the author. The upstream fleet runs one; your enrollment
is the unproven path, and `ENROLL.md` is where to start if you try it.

**A control was withheld from this release, deliberately.** A governance
layer — a permission matrix your agent is held to, compiled into a
PreToolUse hook, with a conformance suite — was built and then pulled
before shipping. Its informed-approval control (Principle 17: show what
you're asking to approve) did not hold: an *allowed* `Bash` call could
rewrite both a staged proposal and the audit record it was checked
against, and the verifier still reported the proposal unchanged. Three
rounds of fixes failed the same way, because the protection was bound to
the spelling of a write rather than to "an agent wrote governance state."
A control that reads stronger than it is, shipped to strangers, is worse
than no control — so Principle 17 still ships as doctrine, and the
enforcement does not ship at all. Phase 5.5 of the install says the same
thing. It comes back when the binding rests on a boundary an agent cannot
cross from inside the same user account.

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
