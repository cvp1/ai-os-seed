# AGENT-INSTALL — instructions for the agent performing this install

You are an AI coding agent (Claude Code or similar) whose user asked you to
install **AI-OS Seed** from this cloned repo. Follow these phases in order.
Binding rules for the whole install:

1. **Show every mutating command before you run it**, and run it only after
   the user can see it. Read-only checks may run freely.
2. **Only deterministic tools move bytes.** Every file that lands in the
   user's install comes from `git clone`, `install.py`, or a shipped script
   you show and then run (`memory-mesh/install.sh`, `scheduler/sync.sh`) —
   never type out file contents yourself, with two labeled exceptions: Phase
   4's personalized `CLAUDE.md` and Phase 5's first memory note + its
   `MEMORY.md` index bullet — both new content you author *for this user*,
   both shown before you write them.
3. **Never fabricate.** If something is missing or fails, stop, show the
   real error, and say what the user can do — do not improvise around it.
4. **Stop means stop.** A failed readiness check ends the install with a
   clear message; don't push through.
5. **`<ROOT>` below is a placeholder.** Substitute the literal resolved
   path, quoted, in every command you actually run — an unquoted path with
   a space breaks the command and Rule 4 then applies.

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

Draft the CLAUDE.md content fresh for this user: who they are (Phase 1),
what this workspace is, and pointers to `PRINCIPLES.md` and the component
READMEs. Keep it short — it will grow with their system. Do NOT copy
`CLAUDE.md.template` — it's a leak-scrubbed export kept only as a
structural reference.

**If this was an `--into` install, `<ROOT>/CLAUDE.md` already exists and is
theirs.** Draft a short addition instead (what the seed added, where the
ops verbs live) — same stage-then-approve flow below, just shorter content.

**You do not write `<ROOT>/CLAUDE.md` yourself.** Stage the exact content,
show it to the user verbatim (not narrated), and only `install.py --approve`
moves it into place — this binds what the user saw to what actually lands,
closing a real gap in the old "show it, then write it" flow where nothing
checked the two matched (Wave 2H/SEED-069):

    mkdir -p <ROOT>/.cc-seed/staged
    # write the exact drafted content to <ROOT>/.cc-seed/staged/claude-md.proposed
    # show the user that file's content, then:
    python3 install.py --target <ROOT> --approve claude-md

`--approve` hashes the staged bytes, records that hash in the install
receipt, and moves them into `<ROOT>/CLAUDE.md` wrapped in
`<!-- cc-seed:start -->`/`<!-- cc-seed:end -->` markers, in the same step —
you never get an Edit/Write call on `<ROOT>/CLAUDE.md`. It prints the hash
back; if a second `--approve claude-md` refuses because a region already
exists, that's the "no nesting, no silent overwrite" guard — resolve by hand.

## Phase 5 — Memory (the first note, demonstrated not described)

`<ROOT>/memory/` shipped in Phase 2 with an empty `MEMORY.md` index and a
`CONVENTIONS.md` explaining the note format (four types, one fact per
file, frontmatter schema — read it if you haven't). **If Phase 2 skipped
`memory/` because the workspace already had one:** the user's existing
memory conventions govern, not the seed's — read *their* index, follow
*their* format for the note below, and change nothing else about how
their memory works. **One exception, stated plainly, same as the README:**
step 3 below (the mesh bootstrap) converts `MEMORY.md` from a file the
user hand-edits into one this fold regenerates — a real, visible change if
they don't want it. Tell them that before running it, and if they'd rather
keep hand-authored memory, skip step 3 entirely; steps 1-2 and everything
else in this install still stand without it. Three things now:

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
   `<ROOT>/memory-mesh/SPEC.md`). This is the other gated write
   (Wave 2H/SEED-069) — show the user the exact command below and have
   *them* run it, rather than running it yourself:

       python3 install.py --target <ROOT> --approve mesh-bootstrap

   `--approve` runs `memory-mesh/install.sh` itself (never you directly)
   and records the approval plus a before/after hash in the install
   receipt. Verify: the workspace's memory store now opens with a
   GENERATED header and carries the step-2 note — that store lives at
   `~/.claude/projects/<ROOT with / replaced by ->/memory/`, **not**
   `<ROOT>/memory/`. `<ROOT>/memory/` is the shipped scaffold/doc copy and
   this step never touches it — a real distinction, easy to assume away.
   From here, new durable lessons are emitted (`/improve` does this),
   never hand-indexed. More machines later: `<ROOT>/memory-mesh/ENROLL.md`.

## Phase 5.5 — Governance (withheld in this release)

The governance layer — permission matrix, compiled PreToolUse hook, and the
conformance suite — is **not shipping in this build.** Its informed-approval
control was unsound: an allowed `Bash` call could rewrite a staged proposal
and its audit anchor, and the verifier still reported the proposal unchanged.
Rather than ship a control that reads stronger than it is, it is withheld
until the binding rests on a boundary an agent cannot cross from inside the
same user account.

Nothing here to run. Skip to Phase 6. Principle 17 in `PRINCIPLES.md` — show
what you're asking to approve — still stands; it was the doctrine that was
right and the enforcement that was not.

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

**Tell the user to open a fresh terminal and run the auditor themselves —
that, not anything you paste into this chat, is the completion signal**
(Wave 2H/SEED-068):

    python3 install.py --target <ROOT> --audit --package <path to this clone>

This compares the live install against the receipt `install.py` kept at
every state-changing step and against this clone's own manifest — never
against the installed tree, so it isn't grading its own homework. Show them
the command; you may run it yourself first if useful, but say plainly that
your own run doesn't count — an agent pasting audit output into chat is not
the completion signal, a human reading it themselves in a fresh terminal is.
A clean run prints `RESULT: PASS` and exits 0; anything else prints exactly
what it found and exits nonzero.

**Quote this to them — it's the audit's own perimeter disclaimer, and it
matters more than the PASS/FAIL line:** *"This audit verified `<ROOT>`, the
managed scheduler block/plists, (partially) keyvault's shipped scripts, and
— if mesh-bootstrap was approved — the one Claude Code memory-store path
that write deterministically targets. It did not scan shell rc files, SSH
config, other applications' config, or anything else outside `<ROOT>`. A
confused install session can still write there; this audit cannot see it."*
A clean audit means the install matches what was approved — it does not
mean the machine is safe.

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
- **Governance:** say plainly that this build ships none — the layer is
  withheld this release (see Phase 5.5).
- The undo path: `python3 install.py --target <ROOT> --uninstall` (removes
  the scheduled jobs it manages, then the seed's own files ONLY — anything
  the user or their agent created stays untouched. `memory/` in particular
  is only removed if it's still byte-identical to the shipped scaffold;
  one note or edit makes it theirs and it's kept. In an `--into` install
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
