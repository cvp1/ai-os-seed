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
  - a vendored (`provenance: third-party`) skill has had its supervised first
    run recorded (`observed: true`) — see --mark-observed below
The why: the description is the ONLY thing the runtime matches against a user
prompt, so a vague or first-person one silently fails to trigger.

Supply-chain note (audits/2026-08-05-continuous-verification Epic D, D1/D2):
a static SKILL.md review cannot see what a skill's referenced scripts do at
runtime (SkillCloak-class payloads defeat >90% of static scanners). D1's
2026-08-05 audit found zero third-party skills installed via this path — the
cheapest point to land a control is before the first one arrives, not after.
So: `--vendor NAME` marks a freshly-vendored skill `provenance: third-party`
+ `observed: false`; the audit flags it FIX until a human has supervised one
real run and checked it against D1's own checklist (dynamic-context `!`
lines, eval/exec/os.system, curl|sh, decode-then-execute) and run
`--mark-observed NAME`. This is a lint gate, not a runtime sandbox — Claude
Code has no per-skill execution jail to hook here; a real sandboxed-execution
control is D3 (routed to ai-os-pm, not built in this repo).
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


def load_skills(skills_dir=SKILLS_DIR):
    out = []
    if not os.path.isdir(skills_dir):
        return out
    for name in sorted(os.listdir(skills_dir)):
        path = os.path.join(skills_dir, name, "SKILL.md")
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
    if rec["meta"].get("provenance") == "third-party" and rec["meta"].get("observed") != "true":
        issues.append("UNOBSERVED third-party skill — supervise one real run (check "
                       "dynamic-context `!` lines, eval/exec/os.system, curl|sh, "
                       "decode-then-execute per D1's checklist), then run "
                       f"`audit.py --mark-observed {rec['name']}`")
    return issues


def _set_frontmatter_key(text, key, value):
    """Set a top-level `key: value` in a SKILL.md's frontmatter, byte-preserving
    everything else (same rewrite-not-regenerate discipline as
    memory_write.py's set_lineage() — a vendoring/observation stamp must never
    become an excuse to touch the body a reviewer is about to read)."""
    if not text.startswith("---"):
        raise ValueError("no `---` frontmatter block — refusing to guess")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("frontmatter block never closes — refusing to guess")
    fm, rest = text[3:end], text[end:]
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    line = f"{key}: {value}"
    if pattern.search(fm):
        fm = pattern.sub(line, fm, count=1)
    else:
        # After `name:` if present, else at the top of the block.
        anchor = re.search(r"^name:.*$", fm, re.M)
        fm = (fm[: anchor.end()] + "\n" + line + fm[anchor.end():]
              if anchor else "\n" + line + fm)
    return "---" + fm + rest


def _find_skill(skills, name):
    for rec in skills:
        if rec["name"] == name:
            return rec
    return None


def cmd_vendor(skills, name):
    rec = _find_skill(skills, name)
    if rec is None or not rec["exists"]:
        print(f"error: no skill '{name}' at ~/.claude/skills/{name}/SKILL.md — "
              "vendor it in first (repo + symlink, per the FIND-mode convention), "
              "then run --vendor to stamp it.", file=sys.stderr)
        return 1
    if rec["meta"].get("provenance") == "third-party":
        print(f"'{name}' is already marked provenance: third-party "
              f"(observed: {rec['meta'].get('observed', 'false')})")
        return 0
    new_text = _set_frontmatter_key(rec["text"], "provenance", "third-party")
    new_text = _set_frontmatter_key(new_text, "observed", "false")
    with open(rec["real"], "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"marked '{name}' provenance: third-party, observed: false -> {rec['real']}")
    print("Before unrestricted use: supervise one real run (dynamic-context `!` "
          "lines, eval/exec/os.system, curl|sh, decode-then-execute — D1's "
          f"checklist), then: audit.py --mark-observed {name}")
    return 0


def cmd_mark_observed(skills, name):
    rec = _find_skill(skills, name)
    if rec is None or not rec["exists"]:
        print(f"error: no skill '{name}' at ~/.claude/skills/{name}/SKILL.md", file=sys.stderr)
        return 1
    if rec["meta"].get("provenance") != "third-party":
        print(f"error: '{name}' is not marked provenance: third-party — "
              "nothing to observe (run --vendor first if it should be)", file=sys.stderr)
        return 1
    if rec["meta"].get("observed") == "true":
        print(f"'{name}' is already marked observed: true — nothing to do")
        return 0
    new_text = _set_frontmatter_key(rec["text"], "observed", "true")
    with open(rec["real"], "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"marked '{name}' observed: true -> {rec['real']}")
    return 0


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
    ap.add_argument("--vendor", metavar="NAME",
                     help="stamp a freshly-vendored skill provenance: third-party, observed: false")
    ap.add_argument("--mark-observed", metavar="NAME",
                     help="record that a third-party skill's supervised first run is done")
    ap.add_argument("--dir", metavar="PATH", default=SKILLS_DIR,
                     help="skills directory to audit (default: ~/.claude/skills)")
    args = ap.parse_args()
    skills = load_skills(os.path.expanduser(args.dir))
    if args.find:
        return cmd_find(skills, args.find)
    if args.vendor:
        return cmd_vendor(skills, args.vendor)
    if args.mark_observed:
        return cmd_mark_observed(skills, args.mark_observed)
    return cmd_audit(skills)


if __name__ == "__main__":
    sys.exit(main())
