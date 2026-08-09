---
name: skill-center
description: Find, create, and audit this workspace's Claude Code skills. Use
  when the operator says "/skill-center", wants to know whether a skill
  already exists for a need ("is there a skill for X", "do I have a skill
  that…", "find a skill for…"), wants to build or scaffold a new skill ("make
  a skill for X", "turn this into a skill", "create a skill", "let's build a
  skill"), or wants to check skills follow best-practices ("audit my
  skills"). Always searches the public skill ecosystem AND the local
  inventory before building anything new, and scaffolds following
  Anthropic's authoring best-practices plus this workspace's canonical-file
  + symlink convention.
---

# /skill-center — the hub for this workspace's skill lifecycle

One skill, three modes: **find**, **create**, **audit**. The job is to stop
reinvention (find before you build) and to make every new skill trigger
reliably and land in the right place (best-practices + this workspace's
conventions, enforced by scripts so they aren't re-derived each time).

Two dependency-free scripts under `skills/skill-center/`, run with
`/usr/bin/python3`:
- `audit.py` — lints local skills; `--find "query"` ranks them by relevance.
- `scaffold.py` — plan-validate-execute creation of a new skill (dry-run default).

## This workspace's conventions (every created skill must follow these)

- **Canonical-in-workspace + symlink.** The real `SKILL.md` lives under this
  workspace's `skills/<name>/` (version-controlled with everything else here).
  `.claude/skills/<name>/SKILL.md` is a **symlink** to it — Claude Code
  discovers project-level skills by walking up from the working directory
  looking for `.claude/skills/`, so this works from anywhere inside the
  workspace. A skill with real code that deserves its own repo can pass
  `--repo-path` to scaffold.py instead; the symlink convention is unchanged
  either way.
- **Scripts stay next to the canonical `SKILL.md`**, referenced by absolute
  path: `/usr/bin/python3 <path-to-skill-dir>/run.py "…"`. The `.claude/skills`
  entry only holds the symlinked `SKILL.md`.
- **Secrets from `~/.key/`** — never hardcode or echo. Shared helpers via
  this workspace's `_lib`.
- **Scheduled skills** register through `scheduler/manifest.yml` (see
  `scheduler/CONVENTIONS.md`) — a real scheduled job, not something the skill
  invents at runtime.
- **Hot-load:** a new `SKILL.md` is usable this session; a new *agent*
  (subagent type) needs a restart. Validate the underlying pipeline, not the
  dispatch.

---

## Mode: FIND — does this already exist?

When the operator describes a need, **search before building**:

1. **Local inventory** — `/usr/bin/python3 skills/skill-center/audit.py --find "the need"`.
   Also check the skills list in the session system-prompt.
2. **Public ecosystem** — WebSearch the `anthropics/skills` repo and skill
   marketplaces (e.g. "anthropics skills <need>", "claude code skill <need>").
   Many needs (PDF, pptx, docx, code-review) already have a maintained skill.
3. **Report decisively**, one of:
   - *You already have `X`* — and how to invoke it.
   - *Public skill `Y` exists* — link it; offer to vendor it in (canonical
     file + symlink, same as any locally-authored skill).
   - *Nothing fits* — go to CREATE.

Don't build what already exists. Vendoring a public skill still follows the
canonical-file + symlink convention so it stays version-controlled.

**Vendored skills get a supervised first run before unrestricted use.** A
static SKILL.md review can't see what a referenced script does at runtime.
The cheapest point to land this control is before the first third-party
skill arrives, not after. After the vendor step:
1. `audit.py --vendor <name>` — stamps `provenance: third-party` +
   `observed: false` in the skill's frontmatter. `audit.py`'s lint now flags
   it FIX until observed.
2. Run it once, supervised, on non-sensitive input, and check it for
   dynamic-context `!` lines, `eval`/`exec`/`os.system`, `curl|sh`,
   decode-then-execute payloads, unexpected secrets/network reach.
3. Clean → `audit.py --mark-observed <name>`. Something looks wrong → don't
   mark it; fix or drop the skill instead.
This is a lint gate, not a runtime sandbox — Claude Code has no per-skill
execution jail to hook here.

---

## Mode: CREATE — scaffold a new skill

1. **Interview** (keep it short, infer what you can):
   - What capability? What's the single-sentence job?
   - **When should it trigger?** Exact phrases the operator would say. This
     becomes the description — the only thing the runtime matches on.
   - Output format? Read-only or does it change state (mail/cron/files)?
   - Does it need secrets/endpoints (→ `~/.key`) or its own repo (real code)
     vs living in this workspace's `skills/`?
   - Scheduled (→ `scheduler/manifest.yml`) or interactive-only?

2. **Plan + validate** (dry-run):
   ```
   /usr/bin/python3 skills/skill-center/scaffold.py \
     --name <kebab> --desc "<what it does>. Use when the operator says \"/<name>\", …" \
     [--repo-path ~/<own-repo>] [--with-scripts]
   ```
   It validates the description (third-person, has triggers, not vague) and
   prints exactly what it will create. Fix any VALIDATION FAILED issues first.

3. **Execute**: re-run with `--commit`. Creates the skill directory, skeleton
   `SKILL.md`, the `.claude/skills/<name>/` symlink, and (with `--with-scripts`)
   a `scripts/` dir.

4. **Fill the body** following the best-practices checklist below. Write any
   helper scripts into the skill directory (don't make the skill generate
   them at runtime).

5. **Validate + register**:
   - `audit.py` — confirm the new skill lints clean.
   - Commit it to this workspace's repo with your own identity.
   - Write a memory note + pointer (see `memory/CONVENTIONS.md`).
   - If scheduled: add the manifest entry (`scheduler/CONVENTIONS.md`).
   - **Validate live once** end-to-end before calling it done.

---

## Mode: AUDIT — lint existing skills

`/usr/bin/python3 skills/skill-center/audit.py`

Flags vague/first-person descriptions, missing triggers, bodies over 500
lines, and broken or un-version-controlled symlinks. Use periodically, or
after editing a skill.

---

## Best-practices checklist (Anthropic skill-authoring guide)

- **Description is everything.** Third person (it's injected into the system
  prompt — no "I"/"you"). State *what* AND *when*, with concrete trigger phrases.
  Be a little "pushy" — Claude tends to *under*-trigger skills.
- **Name** in gerund/kebab form; avoid "helper", "utils", "tools".
- **Body < 500 lines.** Past that, split into reference files one level deep and
  add a table of contents to any file over 100 lines (partial reads otherwise).
- **Explain the why**, not bare ALWAYS/NEVER — reasoning improves adherence.
- **One default path**, not a menu of equivalent options; give escape hatches.
- **Provide utility scripts, don't generate them** — more reliable, fewer tokens.
  Make intent explicit: "Run `x.py`" (execute) vs "See `x.py`" (reference).
- **Plan-validate-execute** for anything destructive (as scaffold.py does).
- **No time-sensitive text, no magic numbers, forward-slash paths only.**

## Gotchas
- Don't symlink the whole skill *dir* — symlink `SKILL.md` into a dir you mkdir.
- `scaffold.py` is dry-run until `--commit`; it refuses to overwrite an existing
  `SKILL.md`.
- The vault/secrets may be locked after reboot — a skill that reads `~/.key`
  should fail loud, not silently.
