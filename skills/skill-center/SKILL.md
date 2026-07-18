---
name: skill-center
description: Find, create, and audit Craig's Claude Code skills. Use when Craig
  says "/skill-center", wants to know whether a skill already exists for a need
  ("is there a skill for X", "do I have a skill that…", "find a skill for…"),
  wants to build or scaffold a new skill ("make a skill for X", "turn this into a
  skill", "create a skill", "let's build a skill"), or wants to check his skills
  follow best-practices ("audit my skills"). Always searches the public skill
  ecosystem AND Craig's local inventory before building anything new, and
  scaffolds following Anthropic's authoring best-practices plus Craig's repo +
  symlink conventions.
---

# /skill-center — the hub for Craig's skill lifecycle

One skill, three modes: **find**, **create**, **audit**. The job is to stop
reinvention (find before you build) and to make every new skill trigger reliably
and land in the right place (best-practices + Craig's conventions, enforced by
scripts so they aren't re-derived each time).

Two dependency-free scripts under `~/Github/CC/cc-skills/skill-center/`, run with
`/usr/bin/python3`:
- `audit.py` — lints local skills; `--find "query"` ranks them by relevance.
- `scaffold.py` — plan-validate-execute creation of a new skill (dry-run default).

## Craig's conventions (every created skill must follow these)

- **Canonical-in-repo + symlink.** The real `SKILL.md` lives in a git repo under
  `~/Github/CC` (private, {{REDACTED}}, HTTPS). `~/.claude/skills/<name>/SKILL.md` is a
  **symlink** to it. Simple/workflow skills go in the shared `{{REDACTED}}/cc-skills`
  repo; skills with real code get their own repo.
- **Scripts stay in the repo**, referenced by absolute path:
  `/usr/bin/python3 ~/Github/CC/<repo>/run.py "…"`. The skill dir only holds the
  symlinked `SKILL.md`.
- **Secrets from `~/.key/`** (fscrypt vault, locked after reboot) — never
  hardcode or echo. Shared secrets/ha/mail helpers via `_lib`/cc-lib bootstrap.
- **Scheduled skills** register a hermes cron shim — a **real file** in
  `~/.hermes/scripts/` (the scheduler rejects symlinks/abs paths).
- **Hot-load:** a new `SKILL.md` is usable this session; a new *agent* needs a
  restart. Validate the underlying pipeline, not the dispatch.

---

## Mode: FIND — does this already exist?

When Craig describes a need, **search before building**:

1. **Local inventory** — `/usr/bin/python3 ~/Github/CC/cc-skills/skill-center/audit.py --find "the need"`.
   Also check the skills list in the session system-prompt.
2. **Public ecosystem** — WebSearch the `anthropics/skills` repo and skill
   marketplaces (e.g. "anthropics skills <need>", "claude code skill <need>").
   Many needs (PDF, pptx, docx, code-review) already have a maintained skill.
3. **Report decisively**, one of:
   - *You already have `X`* — and how to invoke it.
   - *Public skill `Y` exists* — link it; offer to vendor it into a repo + symlink.
   - *Nothing fits* — go to CREATE.

Don't build what already exists. Vendoring a public skill still follows the
repo+symlink convention so it stays version-controlled.

---

## Mode: CREATE — scaffold a new skill

1. **Interview** (keep it short, infer what you can):
   - What capability? What's the single-sentence job?
   - **When should it trigger?** Exact phrases Craig would say. This becomes the
     description — the only thing the runtime matches on.
   - Output format? Read-only or does it change state (mail/cron/files)?
   - Does it need secrets/endpoints (→ `~/.key`, cc-lib) or its own repo (→ has
     real code) vs the shared cc-skills repo?
   - Scheduled (→ hermes cron) or interactive-only?

2. **Plan + validate** (dry-run):
   ```
   /usr/bin/python3 ~/Github/CC/cc-skills/skill-center/scaffold.py \
     --name <kebab> --desc "<what it does>. Use when Craig says \"/<name>\", …" \
     [--repo-path ~/Github/CC/<name>] [--with-scripts]
   ```
   It validates the description (third-person, has triggers, not vague) and
   prints exactly what it will create. Fix any VALIDATION FAILED issues first.

3. **Execute**: re-run with `--commit`. Creates the repo dir, skeleton
   `SKILL.md`, the `~/.claude/skills/<name>/` symlink, and (with `--with-scripts`)
   a `scripts/` dir.

4. **Fill the body** following the best-practices checklist below. Write any
   helper scripts into the repo (don't make the skill generate them at runtime).

5. **Validate + register**:
   - `audit.py` — confirm the new skill lints clean.
   - Commit to the repo with Craig's identity:
     `git -C ~/Github/CC/<repo> -c user.name='{{REDACTED}}' -c user.email='{{REDACTED}}@gmail.com' commit …`
   - Write an auto-memory file + a one-line `MEMORY.md` pointer.
   - If scheduled: add the hermes cron shim (real file) + manifest entry.
   - **Validate live once** end-to-end before calling it done.

---

## Mode: AUDIT — lint existing skills

`/usr/bin/python3 ~/Github/CC/cc-skills/skill-center/audit.py`

Flags vague/first-person descriptions, missing triggers, bodies over 500 lines,
and broken or non-version-controlled symlinks. Use periodically, or after editing
a skill.

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
