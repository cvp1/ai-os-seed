#!/usr/bin/env python3
"""One-time backfill: emit a `lesson` event for each always-on memory in the store's MEMORY.md.

Idempotent by subject (slugs with an existing lesson event are skipped).
Dry run by default; pass --commit to write.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

# On-demand catalog continuation lines; their slugs go to _index-exclude.txt, not lessons.
ONDEMAND_LINE = re.compile(r"on-demand[^:]*:_?\s*(.+)$")

INDEX = M.store_dir() / "MEMORY.md"
LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<slug>[a-z0-9-]+)\.md\)\s+—\s+(?P<hook>.+)$")


FRONT_DESC = re.compile(r"^description:\s*(.*)$", re.M)


def compose_content(slug, title, hook):
    """Return (content, note) for a migrated memory, or (None, reason) if refused.

    Uses the file's `description:` collapsed to one line, not the possibly
    truncated index hook. A missing or empty description is refused rather
    than falling back to the hook.
    """
    f = M.store_dir() / f"{slug}.md"
    if not f.exists():
        return None, "no store file"
    m = FRONT_DESC.search(f.read_text(encoding="utf-8"))
    if not m:
        return None, "no description: in frontmatter"
    desc = " ".join(m.group(1).strip().strip('"').split())
    if not desc:
        return None, "empty description:"
    # Pre-screen make_event's admission and fact-shape refusals so they are
    # reported instead of raising mid-batch.
    reject = M.admission_reject(desc) or M.fact_refusal(desc)
    if reject:
        return None, reject
    return desc, ("unchanged" if desc == hook else
                  f"{len(desc) - len(hook):+d} chars vs legacy hook")


def main():
    apply = "--commit" in sys.argv
    events, _ = M.read_all_events()
    have = {e["subject"] for e in events if e["kind"] == "lesson"}
    todo, refused = [], []
    for line in INDEX.read_text().splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        subj = f"lesson/{m['slug']}"
        if subj in have:
            continue
        content, why = compose_content(m["slug"], m["title"], m["hook"])
        (todo if content else refused).append(
            (subj, content or m["hook"], why))
    ondemand = []
    seen_ex = set()
    exclude = INDEX.parent / "_index-exclude.txt"
    if exclude.exists():
        seen_ex = {l.split("#", 1)[0].strip()
                   for l in exclude.read_text().splitlines()}
    for line in INDEX.read_text().splitlines():
        m = ONDEMAND_LINE.search(line)
        if m:
            for slug in re.split(r"\s*·\s*", m.group(1).strip()):
                slug = slug.strip()
                if re.fullmatch(r"[a-z0-9-]+", slug) and slug not in seen_ex:
                    ondemand.append(slug)

    print(f"{len(todo)} index entries to backfill "
          f"({len(have)} lesson subjects already in the mesh); "
          f"{len(ondemand)} on-demand slugs to preserve in the exclude manifest")
    if refused:
        print(f"REFUSED {len(refused)} — not migrated, and NOT backfilled from "
              f"the legacy hook (a stump is not a memory):")
        for s, _, why in refused:
            print(f"  ! {s} — {why}")
    if not apply:
        for s, c, why in todo[:8]:
            print(f"  {s} [{why}] — {c[:80]}")
        print("(dry run — pass --commit)")
        return 0
    for subj, content, _ in todo:
        ev, line = M.make_event("lesson", subj, content,
                                session="backfill-2026-07-27",
                                lineage="operator-direct",
                                confidence="operator-stated")
        M.append_event_line(line)
    if ondemand:
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("".join(s + "\n" for s in ondemand))
        print(f"preserved {len(ondemand)} on-demand slugs -> {exclude.name}")
    if todo:
        M.git("add", "events")
        M.git("commit", "-q", "-m",
              f"backfill: {len(todo)} curated index rules as lesson events")
        print(f"emitted {len(todo)} events in one commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
