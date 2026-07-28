#!/usr/bin/env python3
"""One-time backfill (cutover phase 7): every always-on memory in the store's
index becomes a mesh `lesson` event, so the mesh's day-one corpus is the
curated behavioral ruleset rather than an empty log.

Idempotent by SUBJECT, not by run: a slug that already has a live lesson
event is skipped (re-running after new store writes only adds the new ones —
the dual-write in memory_write.py handles those going forward anyway).

Reads the INDEX (one line per always-on memory) — the operative hook line is
exactly what sessions see, so it is exactly what the mesh should carry.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

# Hand-built indexes carry an on-demand catalog as continuation lines
# ("↳ _on-demand (/recall):_ slug · slug …"). Those slugs aren't lessons —
# they belong in _index-exclude.txt so the generated index's on-demand
# appendix keeps advertising them (found by the AI-OS upgrade sandbox,
# 2026-07-28: without this, the catalog silently vanished at cutover).
ONDEMAND_LINE = re.compile(r"on-demand[^:]*:_?\s*(.+)$")

# One derivation for the store path fleet- and seed-wide: mesh_lib.store_dir()
# keys it off the workspace root (wherever memory-mesh/ lives).
INDEX = M.store_dir() / "MEMORY.md"
LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<slug>[a-z0-9-]+)\.md\)\s+—\s+(?P<hook>.+)$")


def main():
    apply = "--commit" in sys.argv
    events, _ = M.read_all_events()
    have = {e["subject"] for e in events if e["kind"] == "lesson"}
    todo = []
    for line in INDEX.read_text().splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        subj = f"lesson/{m['slug']}"
        if subj in have:
            continue
        todo.append((subj, f"{m['title']}: {m['hook']}"[:900]))
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
    if not apply:
        for s, c in todo[:8]:
            print(f"  {s} — {c[:80]}")
        print("(dry run — pass --commit)")
        return 0
    for subj, content in todo:
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
