---
name: recall
description: Unified "what do I know about X?" front door — one query across memory, the notes vault(s), command work product, operational history and session briefs, returned as one fairly-ranked, cited answer. Read-only. Trigger on "/recall", "what do I know about X", "search everything / all of it", "what am I tracking", "have I decided or noted anything about…", "when did this last fail", "where did we leave off".
---

# /recall — everything-at-once recall

ONE query across every source this install actually has, returning one ranked
answer with citations. **Read-only — it never writes any store.** A recalled
fact is always cited; never state one as if it were common knowledge.
(`/wiki` stays the vault-only precedent + graph tool.)

## The sources

Each is used **if it exists here** and dropped silently if it does not — a
fresh install has memory and little else, and recall must still work. Say
which sources were searched in the Gaps footer, so an empty answer is never
confused with an absent source.

1. **Memory** — the memory-mesh store for this workspace, at
   `~/.claude/projects/<workspace path with / → ->/memory/*.md`. DERIVED,
   never typed; print it with
   `/usr/bin/python3 -c "import sys; sys.path.insert(0,'memory-mesh'); import mesh_lib; print(mesh_lib.store_dir())"`.
   `MEMORY.md` there is fold-generated. Cite as `[[slug]]`.
2. **Notes vault(s)** — `~/notes/**` and, where the install has one,
   `<workspace>/notes/**`. **Do not merge them — search both.** Cite as
   `<vault>/path > heading`. Skip archived agent logs and
   `_inbox/processed/**`: noise, not precedent.
3. **Work product** — durable knowledge produced by other commands, declared
   in this skill's own `manifest.yml`. READ THESE FILES LIVE at query time;
   never copy one into memory or the vault, because a copy drifts and a live
   read keeps the file the single source of truth. Cite as
   `command-folder/file`, and treat a work-product hit as LIVE CURRENT STATE,
   not durable precedent. Strip HTML to text, skip oversized dumps, and drop
   the source entirely if the manifest is missing.
4. **Operational history** — `observability/report.py --json` for the run log
   of every scheduled job, `observability/freshness.py --json` for current
   state. This is what answers "when did this last fail", "has X ever broken
   before". Cite as `runs.db > job, date`.
5. **Session briefs** — `session-brief/briefs/*.md`, listed by
   `session-brief/session_brief.py list`: work frozen by `/freeze` or
   `/capture` — the goal, the decisions and why, what failed, what is still
   open, the next action. This is what answers "where did we leave off".
   Cite as `brief <id>`.

## The engine

`memory-mesh/recall.py`, relative to the workspace root, does the memory tier
and any extra roots you hand it:

    /usr/bin/python3 memory-mesh/recall.py "<query>" --root ~/notes --limit 8
    /usr/bin/python3 memory-mesh/recall.py "<query>" --no-memory   # the /wiki shape
    /usr/bin/python3 memory-mesh/recall.py "<query>" --json

It serves memory through the same corpus and the same servable verdict the
per-turn retrieval channel uses, so recall and retrieval can never disagree
about what is live. Drive it rather than hand-rolling a grep — a second
search is a second answer, and this file's judgment is about how to READ the
result, not how to find it.

## Algorithm (rank fairly — the one real risk)

1. Keyword-match the query against each source independently; score each hit.
2. **Normalize within each source to 0–1**, so one source cannot drown the
   others on raw scale.
3. **Guarantee a per-source quota** (default 2 each) before filling the rest
   of the pack (default 10 total) by overall normalized rank. A vault is
   larger than a memory store by orders of magnitude; the quota is why it
   cannot drown it.
4. **Relational expansion (memory):** for the top memory hits, follow
   `[[slug]]` links in the body, the `MEMORY.md` index grouping, and any
   typed frontmatter relations, and show those in a separate **Related**
   block as context — never re-ranked into the pack.

## Answer contract

- Ground the answer ONLY in what was recalled.
- **Cite per source**, in that source's own form (above).
- Show a **Related** block when the memory graph has relevant context.
- End with an honest one-line **Gaps:** footer naming the sources searched,
  any uncovered terms, and any stale note leaned on. A memory is
  point-in-time: if the answer turns on a file, a flag or a path still
  existing, verify it before asserting it.

## Notes

- Plain keyword matching is the right engine at this scale. Leave a clear
  seam for an embedding reranker later — do NOT build one now.
- Read-only, and honor confidentiality: what is recalled stays in the
  session it was recalled into.
