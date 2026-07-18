#!/usr/bin/env python3
"""skill-center: scaffold a new Claude Code skill following Craig's conventions.

Plan-validate-execute: runs DRY by default (prints the plan + validates the
description), only touches disk with --commit. The why: a skill that triggers
wrong or lands in the wrong place is annoying to unwind, so we validate the one
field that matters (description) before creating anything.

Craig's conventions enforced here:
  - canonical files live in a git repo under ~/Github/CC (version-controlled)
    * simple/workflow skills -> the shared {{REDACTED}}/cc-skills repo (default)
    * skills with real code   -> their own repo (pass --repo-path)
  - ~/.claude/skills/<name>/SKILL.md is a SYMLINK to the canonical file
  - helper scripts stay in the repo, called by ABSOLUTE path with /usr/bin/python3
  - secrets come from ~/.key (never hardcoded); shared helpers via _lib/cc-lib

Usage:
  scaffold.py --name solar-peek --desc "..."                 # dry-run plan
  scaffold.py --name solar-peek --desc "..." --with-scripts  # add scripts/ dir
  scaffold.py --name solar-peek --desc "..." --repo-path ~/Github/CC/solar-peek
  scaffold.py --name solar-peek --desc "..." --commit        # actually create
"""
import argparse
import os
import re
import sys

HOME = os.path.expanduser("~")
CC_SKILLS = os.path.join(HOME, "Github/CC/cc-skills")
SKILLS_DIR = os.path.join(HOME, ".claude/skills")

SKELETON = '''---
name: {name}
description: {desc}
---

# /{name} — {tagline}

<!-- Body stays under ~500 lines. Explain the WHY behind instructions; LLMs
     follow reasoning better than bare ALWAYS/NEVER rules. Reference helper
     scripts by absolute path, e.g.  /usr/bin/python3 {repo}/run.py "..."  -->

## What this does

{desc}

## How to run

1. ...

## Conventions
- Secrets from `~/.key/` — never hardcode or echo them.
- Internal HTTPS targets use self-signed certs (`curl -sk`).
'''


def validate(name, desc):
    """Return list of blocking problems with the proposed skill."""
    errs = []
    if not re.fullmatch(r"[a-z][a-z0-9-]+", name):
        errs.append(f"name '{name}' must be lower kebab-case (a-z, 0-9, -)")
    if len(desc) < 40:
        errs.append("description too short — say what it does AND when to trigger")
    low = desc.lower()
    if not re.search(r'"/?[\w-]+"|use when|use this when|when (craig|the user|you)', low):
        errs.append('description needs explicit triggers (e.g. \'Use when Craig says "/x", ...\')')
    # Ignore quoted trigger phrases (user's voice) — only the narration must be 3rd person.
    narration = re.sub(r'"[^"]*"|\'[^\']*\'', " ", desc)
    if re.search(r"\b(I |I'?ll|I'?m|you can|you should)", narration):
        errs.append("description must be third-person (it is injected into the system prompt)")
    return errs


def plan(name, desc, repo_dir, with_scripts, repo_label):
    skill_link = os.path.join(SKILLS_DIR, name, "SKILL.md")
    canonical = os.path.join(repo_dir, "SKILL.md")
    lines = [
        f"PLAN for skill '{name}'  (repo: {repo_label})",
        f"  create dir   {repo_dir.replace(HOME, '~')}/",
        f"  write        {canonical.replace(HOME, '~')}",
    ]
    if with_scripts:
        lines.append(f"  create dir   {repo_dir.replace(HOME, '~')}/scripts/")
    lines += [
        f"  mkdir        {os.path.dirname(skill_link).replace(HOME, '~')}/",
        f"  symlink      {skill_link.replace(HOME, '~')}  ->  {canonical.replace(HOME, '~')}",
        "",
        "AFTER --commit, still TODO by hand:",
        "  - fill in the SKILL.md body",
        f"  - audit.py            (lint the new skill)",
        f"  - git -C {os.path.dirname(repo_dir).replace(HOME,'~') if repo_label!='cc-skills' else '~/Github/CC/cc-skills'} add + commit (identity {{REDACTED}})",
        "  - write an auto-memory entry + MEMORY.md pointer",
        "  - if scheduled: register a hermes cron shim (real file, not symlink)",
    ]
    return "\n".join(lines)


def execute(name, desc, repo_dir, with_scripts):
    os.makedirs(repo_dir, exist_ok=True)
    if with_scripts:
        os.makedirs(os.path.join(repo_dir, "scripts"), exist_ok=True)
    canonical = os.path.join(repo_dir, "SKILL.md")
    if os.path.exists(canonical):
        print(f"refusing to overwrite existing {canonical}", file=sys.stderr)
        return 1
    tagline = desc.split(".")[0][:60]
    with open(canonical, "w", encoding="utf-8") as f:
        f.write(SKELETON.format(name=name, desc=desc, tagline=tagline,
                                repo=repo_dir.replace(HOME, "~")))
    link_dir = os.path.join(SKILLS_DIR, name)
    os.makedirs(link_dir, exist_ok=True)
    link = os.path.join(link_dir, "SKILL.md")
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(canonical, link)
    print(f"created {canonical}")
    print(f"symlinked {link} -> {canonical}")
    print("\nSKILL.md hot-loads this session; fill the body, then run audit.py.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--desc", required=True, help="frontmatter description (what + when-to-trigger)")
    ap.add_argument("--repo-path", help="own-repo path; default = shared cc-skills/<name>")
    ap.add_argument("--with-scripts", action="store_true", help="also create a scripts/ dir in the repo")
    ap.add_argument("--commit", action="store_true", help="actually create files (default is dry-run)")
    args = ap.parse_args()

    errs = validate(args.name, args.desc)
    if errs:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2

    if args.repo_path:
        repo_dir = os.path.expanduser(args.repo_path)
        repo_label = os.path.basename(repo_dir.rstrip("/"))
    else:
        repo_dir = os.path.join(CC_SKILLS, args.name)
        repo_label = "cc-skills"

    print(plan(args.name, args.desc, repo_dir, args.with_scripts, repo_label))
    if not args.commit:
        print("\n(dry-run — re-run with --commit to create)")
        return 0
    print()
    return execute(args.name, args.desc, repo_dir, args.with_scripts)


if __name__ == "__main__":
    sys.exit(main())
