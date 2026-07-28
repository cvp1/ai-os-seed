# AGENT-INSTALL — instructions for the agent performing this install

You are an AI coding agent (Claude Code or similar) whose user asked you to
install **AI-OS Seed** from this cloned repo. Follow these phases in order.
Binding rules for the whole install:

1. **Show every mutating command before you run it**, and run it only after
   the user can see it. Read-only checks may run freely.
2. **Only deterministic tools move bytes.** Every file that lands in the
   user's install comes from `git clone` or `install.py` — never type out
   file contents yourself. (Exception: Phase 4's personalized CLAUDE.md,
   which is new content you author *for this user*, clearly labeled.)
3. **Never fabricate.** If something is missing or fails, stop, show the
   real error, and say what the user can do — do not improvise around it.
4. **Stop means stop.** A failed readiness check ends the install with a
   clear message; don't push through.

## Phase 0 — Readiness (read-only)

Check, and report each result plainly:

- **Prior installs first:** run `python3 install.py --detect` (read-only).
  It surveys this machine for an existing seed install — the scheduler's
  managed crontab block, `dev.cc-seed.*` launchd plists, and the
  directories a previous install or clone commonly leaves behind. If it
  reports anything, STOP and put the choice to the user plainly:
  - **Keep the existing install** — end here; if they wanted an update,
    point them at the existing install root instead of making a new one.
  - **Replace it** — run `--uninstall` against the OLD root first (shown
    before run, like everything else; it de-schedules that install's jobs
    then removes its tree), and only then continue to Phase 1.
  - **Never install twice on one machine.** The scheduler owns a single
    managed crontab block / launchd label set per machine; two installs
    silently fight over it. Don't offer side-by-side as an option.
  If `--detect` flags a directory that exists but *isn't* a seed layout,
  ask the user what it is rather than assuming. If it's **their agent's
  existing workspace** (say, one AI-OS Core built), the preferred move is
  for the seed to move IN, not to open a second directory — see Phase 1.
  If it's anything else (a repo checkout, someone else's files), leave it
  completely alone and never write into it.
- OS is Linux or macOS (`uname`) — anything else: stop, unsupported.
- `git` is installed.
- `python3` is 3.9+ (`python3 --version`).
- PyYAML is importable (`python3 -c "import yaml"`) — needed by the
  scheduler. If missing:
  - Linux (Debian/Ubuntu): `sudo apt install python3-yaml`.
  - macOS with Homebrew Python: a bare `pip3 install pyyaml` will likely
    refuse with `externally-managed-environment` (PEP 668, live-verified
    in this project's own CI). The clean fix is a venv: `python3 -m venv
    ~/.venvs/aios-seed && ~/.venvs/aios-seed/bin/pip install pyyaml`, then
    use `~/.venvs/aios-seed/bin/python3` for every command in this guide
    instead of bare `python3`. `pip3 install --break-system-packages
    pyyaml` also works but modifies the system Python — only suggest it if
    the user prefers that over a venv.
  Stop until this is resolved either way.
- Ask the user: **does this machine stay on?** A laptop that sleeps means
  scheduled jobs silently don't run — warn plainly and let them proceed
  informed, or pick a different machine.

## Phase 1 — Interview (conversational, write nothing yet)

Ask, one at a time:

1. **Where should the system live?** First ask: **do you already have a
   workspace your agent works in** (for example one AI-OS Core set up)?
   - **Yes → this install is an UPGRADE to that system, and say so.**
     Frame it in capabilities before asking to proceed, something like:
     *"You already have an AI-OS — this adds its operations floor. After
     this, it can run jobs on a schedule while you're away, keep a
     record of every run, catch jobs that silently stop, keep secrets
     out of transcripts, and answer `/status` honestly in one screen —
     and nothing about your current setup changes."* Their existing
     workspace is `<ROOT>`, and Phase 2 uses `--into`. One workspace,
     one memory, one agent — don't multiply directories. (This matters
     mechanically, not just aesthetically: agent memory is keyed to the
     working directory, so a second root is literally a second brain
     that can't see the first.)
   - **No → a fresh directory.** Default suggestion: `~/ai-os-seed`; any
     absolute path is fine. Keep it distinct from the clone directory
     you're reading this file in — the clone is the source, the install
     root is the live system. Call it `<ROOT>` below.
2. **What's the first real thing you'd want to watch or automate?** (You
   won't build it now — knowing it lets you tailor the wrap-up advice.)
3. **Name to use in their CLAUDE.md** (optional; skip if they prefer).

## Phase 2 — Install (deterministic)

Show, then run — fresh directory:

    python3 install.py --target <ROOT>

…or, joining an existing workspace (Phase 1 said yes):

    python3 install.py --target <ROOT> --into

Either way this copies the substrate (`_lib/`, `keyvault/`, `scheduler/`,
`observability/`, `demo/`, `skills/`, `memory/`, `memory-mesh/`, `views/`,
`PRINCIPLES.md`, the two `.template` reference files) into `<ROOT>`.
Without `--into` it refuses a non-empty target; with `--into` it refuses
if a name it would write already exists there — the user's own content is
never merged with or written over, and one collision stops the whole
install before any byte moves. The one deliberate exception: an existing
`memory/` doesn't collide, it *satisfies* — a workspace that already has
a live memory (every AI-OS Core does) already practices the discipline
the seed's empty scaffold exists to start, so the scaffold simply isn't
written and their memory stays exactly as it is. On failure: show the
error, stop.

## Phase 3 — Verify (deterministic)

Show, then run, in order:

    python3 <ROOT>/_lib/selftest.py
    python3 <ROOT>/observability/log_run.py --job hello_fleet -- python3 <ROOT>/demo/hello_fleet.py
    python3 <ROOT>/observability/report.py --job hello_fleet
    python3 <ROOT>/observability/freshness.py --all

Expected: selftest passes; the demo prints one alive-line; report shows
exactly one `ok` row; freshness shows `[OK] hello-fleet demo heartbeat`.
Any other outcome: stop and show it.

## Phase 4 — Personalize (the one thing you author)

Draft `<ROOT>/CLAUDE.md` fresh for this user: who they are (Phase 1), what
this workspace is, and pointers to `PRINCIPLES.md` and the component READMEs.
Keep it short — it will grow with their system. Mark it clearly as
generated-at-install so they know it's theirs to rewrite. Do NOT copy
`CLAUDE.md.template` — it's a leak-scrubbed export kept only as a
structural reference.

**If this was an `--into` install, `<ROOT>/CLAUDE.md` already exists and
is theirs.** Don't replace it — propose a short addition (what the seed
added, where the ops verbs live) and let the user approve the edit.

## Phase 5 — Memory (the first note, demonstrated not described)

`<ROOT>/memory/` shipped in Phase 2 with an empty `MEMORY.md` index and a
`CONVENTIONS.md` explaining the note format (four types, one fact per
file, frontmatter schema — read it if you haven't). **If Phase 2 skipped
`memory/` because the workspace already had one:** the user's existing
memory conventions govern, not the seed's — read *their* index, follow
*their* format for the note below, and change nothing about how their
memory works. Two things now:

1. Tell the user plainly: this only works if *your* memory (the agent
   running this install) is actually configured to read from
   `<ROOT>/memory/`. Confirm where your own memory store lives on this
   machine and whether it already points here — if you're not sure, say
   so rather than assuming; `CONVENTIONS.md`'s cwd-keying note explains
   why this can silently diverge.
2. Write the first note yourself, so the loop's first turn is
   demonstrated rather than explained. Show the file content before you
   write it (house rule). A `project`-type note is right for this: date,
   `<ROOT>`, and what Phase 2 installed — nothing about the user beyond
   what they told you in Phase 1's optional name. Add its index bullet to
   `MEMORY.md` in the same step.
3. Bootstrap the memory mesh — the event-sourced layer underneath the
   notes (append-only history, contradiction detection, replay; spec in
   `<ROOT>/memory-mesh/SPEC.md`). Show, then run:

       bash <ROOT>/memory-mesh/install.sh

   This starts a local event log (plain git, this machine only), schedules
   a 5-minute fold, backfills the index bullet from step 2 as the first
   event, and flips `MEMORY.md` to fold-generated (the pre-flip index is
   kept at `MEMORY.md.pre-mesh`). Verify: `MEMORY.md` now opens with a
   GENERATED header and carries the step-2 note. From here, new durable
   lessons are emitted (`/improve` does this), never hand-indexed. More
   machines later: `<ROOT>/memory-mesh/ENROLL.md`.

## Phase 5.5 — Governance (optional, opt-in)

Ask the user: **should this install be governed by a permission policy?**
Recommended for any shared, organizational, or client-facing use; optional
for a personal solo install. If **no**: say plainly "governance: none — the
personal/solo path" and skip straight to Phase 6. Nothing about the install
changes; `<ROOT>/governance/` is never created.

If **yes**:

1. **Enable the component.** Show, then run:

       python3 install.py --target <ROOT> --enable-governance

   Copies `governance/` into `<ROOT>` (opt-in only — never part of the
   default install, see `install.py`'s `OPTIONAL_COMPONENTS`).

2. **Choose the policy.** Default: the shipped reference at
   `<ROOT>/governance/policy.yml` (enterprise-neutral, matches vault "AI
   Agent Permission Matrix" v0.2). If the user has a workshop-produced org
   policy, copy it over the reference file (deterministic copy, don't
   retype it).

3. **Validate.** Show, then run:

       python3 <ROOT>/governance/tools/validate_policy.py <ROOT>/governance/policy.yml

   A refused policy STOPS here — show the exact error, fix the policy (or
   pick a different one), re-run. Do not proceed on a policy that fails
   validation for any reason, including a custom policy's overlay
   attempting to loosen a base cell.

4. **Compile.** Show, then run:

       python3 <ROOT>/governance/tools/compile_profile.py <ROOT>/governance/policy.yml --out <ROOT>/governance/out

   Read `<ROOT>/governance/out/COMPILE-REPORT.md` yourself and summarize
   for the user: how many cells bound at the harness layer, and — by
   name — any prohibition reporting `UNENFORCED`, with its compensating
   control. Never silently treat an `UNENFORCED` line as "handled."

5. **Consent, shown as a diff.** This is the gated step — wiring a hook is
   never silent (see PRINCIPLES.md's self-modification gate). Show the
   user the EXACT change about to be made to `<ROOT>/.claude/settings.json`:
   the `permissions` block from `governance/out/settings-fragment.json`,
   and a new `hooks.PreToolUse` entry running `governance/hooks/
   profile_gate.py` with the classification/staged/audit paths embedded
   directly in the command (inline env-var prefix, not relying on shell
   inheritance — durable across future sessions):

       SEED_GOVERNANCE_CLASSIFICATION=<ROOT>/governance/out/classification.json SEED_GOVERNANCE_STAGED_DIR=<ROOT>/.seed/staged SEED_GOVERNANCE_AUDIT_DIR=<ROOT>/.seed/audit python3 <ROOT>/governance/hooks/profile_gate.py

   If `<ROOT>/.claude/settings.json` already exists, show a proper
   before/after diff and merge (don't clobber unrelated keys — permissions
   arrays union, hooks.PreToolUse appends). If it doesn't exist, show the
   full file you're about to create. **Only write after the user says
   yes.** This is the one step in this phase that is genuinely
   irreversible-by-accident (a wired hook governs every future tool call)
   — do not skip showing the diff even if earlier steps went smoothly.

6. **Prove it — conformance.** Show, then run:

       python3 <ROOT>/governance/conformance/run_conformance.py --policy <ROOT>/governance/policy.yml --classify <ROOT>/governance/classify_defaults.yml --hook <ROOT>/governance/hooks/profile_gate.py --results <ROOT>/governance/conformance/RESULTS.md

   **A conformance FAIL stops the install here — do not proceed to Phase
   6.** Show the FAIL lines exactly as printed, and do not offer to
   "continue anyway." This is the wave's halt-before-capability rule:
   governance and capability arrive together, or not at all. If every
   probe is PASS or an honestly-reported UNENFORCED, continue.

7. **Confirm.** Tell the user plainly: governance is ON, name the policy
   in use, name any UNENFORCED prohibition and its compensating control
   again (repetition here is deliberate — this is the thing most likely
   to be forgotten later), and point at `governance/tools/audit_report.py`
   for the monthly one-pager once real usage accumulates.

## Phase 6 — First win (scheduling for real)

Show, then run:

    python3 install.py --target <ROOT> --enable-demo

This writes the `hello_fleet` entry (every 15 min) into
`<ROOT>/scheduler/manifest.yml`. Then show, then run:

    bash <ROOT>/scheduler/sync.sh

On Linux, confirm with the user via `crontab -l` that the marked cc-seed
block now exists (their pre-existing entries are untouched). On macOS,
`launchctl list | grep cc-seed`. The demo already ran once in Phase 3, so
freshness is green now and the scheduler keeps it green from here —
that's the whole spine live: **scheduler → job → runs.db → freshness.**

## Phase 7 — Hand back

Tell the user, concretely:

- **If this was an `--into` upgrade of an existing AI-OS: recap what
  their system can do now that it couldn't this morning**, in their
  terms, not component names — it runs jobs on a schedule without them,
  records every run, notices when something goes quiet, keeps secrets
  out of transcripts, and `/status` gives them the honest one-screen
  answer. Their memory and everything they had before is unchanged.
  Point them at `memory/THE-LOOP.md` for how the pieces feed each other
  — and note that on an `--into` install that file was deliberately not
  written into their memory, so read it from the clone
  (`<clone>/memory/THE-LOOP.md`) or copy it wherever they keep docs.
- **If Phase 5.5 was run:** recap governance status again here — on, which
  policy, any UNENFORCED prohibition and its compensating control. If it
  was skipped, say "governance: none" plainly so the user knows it's an
  available, not-yet-taken option (`install.py --target <ROOT>
  --enable-governance`, then Phase 5.5's steps, any time later).
- The undo path: `python3 install.py --target <ROOT> --uninstall` (removes
  the scheduled jobs it manages, then the seed's own files ONLY — anything
  the user or their agent created stays untouched. `memory/` in particular
  is only removed if it's still byte-identical to the shipped scaffold;
  one note or edit makes it theirs and it's kept. `governance/` follows the
  same rule — a policy.yml you've customized (org name, overlays) is kept,
  not deleted. In an `--into` install
  the pre-existing workspace survives minus exactly what the seed added.
  Shown before run, like everything else).
- Their next move: replace the demo with the real thing from Phase 1's
  answer — write the script, wrap it through `log_run.py` in a manifest
  entry, add a cadence line to `observability/freshness.json`, re-run
  `sync.sh`. The demo deletes cleanly whenever they're done with it.
- If it worked: opening a GitHub issue titled "first win" with their OS +
  what they'll monitor is the project's entire telemetry. Optional,
  appreciated, anonymous beyond what they choose to say.

## If this repo was unreachable or half-cloned

Stop. Tell the user to re-clone or download the release tarball from the
GitHub releases page. Do not reconstruct any file from memory.
