#!/usr/bin/env python3
"""skill-center: audit + find over Craig's local Claude Code skills.

Two jobs, no dependencies (/usr/bin/python3):

  audit.py                 # lint every local skill against best-practices
  audit.py --find "query"  # rank local skills by relevance to a need

A skill lives at ~/.claude/skills/<name>/SKILL.md, which is a symlink into a
git repo ({{REDACTED}}/cc-skills or the skill's own repo). We read through the symlink
so we lint the canonical file.

Best-practice checks (Anthropic skill-authoring guide):
  - description present, has trigger phrases, third-person (no "I "/"you ")
  - description not vague ("helps with", "processes data", ...)
  - SKILL.md body < 500 lines (progressive disclosure)
  - symlink resolves to a real file inside a git repo
The why: the description is the ONLY thing the runtime matches against a user
prompt, so a vague or first-person one silently fails to trigger.
"""
import argparse
import os
import re
import sys

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
VAGUE = ("helps with", "help you", "processes data", "various tasks",
         "utility", "general purpose", "does things")


def parse_frontmatter(text):
    """Return (meta_dict, body_lines) from a SKILL.md. Minimal YAML: name/description."""
    if not text.startswith("---"):
        return {}, text.splitlines()
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text.splitlines()
    fm = text[3:end]
    body = text[end + 4:]
    meta, key = {}, None
    for line in fm.splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            meta[key] = m.group(2).strip()
        elif key and line.startswith(("  ", "\t")):  # folded continuation
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, body.splitlines()


def load_skills():
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for name in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        rec = {"name": name, "path": path, "exists": os.path.exists(path),
               "real": os.path.realpath(path) if os.path.exists(path) else None}
        if rec["exists"]:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            rec["meta"], body = parse_frontmatter(text)
            rec["body_lines"] = len(body)
            rec["text"] = text
        out.append(rec)
    return out


def lint(rec):
    issues = []
    if not rec["exists"]:
        return ["SKILL.md missing or broken symlink"]
    desc = rec["meta"].get("description", "")
    if not desc:
        issues.append("no description (skill will never trigger reliably)")
    else:
        low = desc.lower()
        if not re.search(r'"/?[\w-]+"|use when|use this when|when (craig|the user|you)', low):
            issues.append("description has no explicit trigger phrases")
        # Quoted trigger phrases are the user's own voice ("how do I grow X") and
        # may legitimately be first/second person — only the narration must be 3rd.
        narration = re.sub(r'"[^"]*"|\'[^\']*\'', " ", desc)
        if re.search(r"\b(I |I'?ll|I'?m|you can|you should|your )", narration):
            issues.append("description not third-person (injected into system prompt)")
        if any(v in low for v in VAGUE):
            issues.append("description is vague — be specific about what + when")
        if len(desc) > 1400:
            issues.append(f"description very long ({len(desc)} chars; ~500 words max)")
    if not rec["meta"].get("name"):
        issues.append("no name in frontmatter")
    if rec.get("body_lines", 0) > 500:
        issues.append(f"body {rec['body_lines']} lines > 500 — split via progressive disclosure")
    if rec["real"] and "/Github/" not in rec["real"]:
        issues.append("canonical file not in a git repo under ~/Github (not version-controlled)")
    return issues


def cmd_audit(skills):
    bad = 0
    for rec in skills:
        issues = lint(rec)
        flag = "ok " if not issues else "FIX"
        if issues:
            bad += 1
        link = "->%s" % rec["real"].replace(os.path.expanduser("~"), "~") if rec["real"] else "(broken)"
        print(f"[{flag}] {rec['name']:<22} {link}")
        for i in issues:
            print(f"        - {i}")
    print(f"\n{len(skills)} skills, {bad} with issues.")
    return 0


def cmd_find(skills, query):
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored = []
    for rec in skills:
        if not rec["exists"]:
            continue
        desc = rec["meta"].get("description", "").lower()
        body = rec.get("text", "").lower()
        # description matches weigh 3x — that's what the runtime matches on
        score = sum(3 * desc.count(t) + body.count(t) for t in terms)
        if score:
            scored.append((score, rec))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        print(f"No local skill matches '{query}'.")
        print("→ Search the public ecosystem (anthropics/skills, marketplaces), "
              "then scaffold one: scaffold.py --name ...")
        return 0
    print(f"Local skills relevant to '{query}':\n")
    for score, rec in scored[:5]:
        desc = rec["meta"].get("description", "")[:160]
        print(f"  ({score:>3}) {rec['name']:<20} {desc}…")
    print("\nIf none of these truly fit, search the public ecosystem before building new.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--find", metavar="QUERY", help="rank local skills by relevance")
    args = ap.parse_args()
    skills = load_skills()
    if args.find:
        return cmd_find(skills, args.find)
    return cmd_audit(skills)


if __name__ == "__main__":
    sys.exit(main())
