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

1. **Where should the system live?** Default suggestion: `~/aios` — any
   absolute path is fine. Call it `<ROOT>` below.
2. **What's the first real thing you'd want to watch or automate?** (You
   won't build it now — knowing it lets you tailor the wrap-up advice.)
3. **Name to use in their CLAUDE.md** (optional; skip if they prefer).

## Phase 2 — Install (deterministic)

Show, then run:

    python3 install.py --target <ROOT>

This copies the substrate (`_lib/`, `keyvault/`, `scheduler/`,
`observability/`, `demo/`, `skills/`, `PRINCIPLES.md`, the two
`.template` reference files) into `<ROOT>` and refuses to overwrite a
non-empty existing target. On failure: show the error, stop.

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

## Phase 5 — Memory (the first note, demonstrated not described)

`<ROOT>/memory/` shipped in Phase 2 with an empty `MEMORY.md` index and a
`CONVENTIONS.md` explaining the note format (four types, one fact per
file, frontmatter schema — read it if you haven't). Two things now:

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

- The undo path: `python3 install.py --target <ROOT> --uninstall` (removes
  the scheduled jobs it manages, then the tree — shown before run, like
  everything else).
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
