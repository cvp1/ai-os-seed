#!/usr/bin/env python3
"""recall — "what do I know about X?", answered with citations, from any harness.

SEED-080 Step 4b. Recall existed twice: as prose in `recall/SKILL.md` that
only a skill-loading harness could follow, and as `corral/aios_memory.py`, a
separate grep over Markdown that never consulted the fold's verdict — so a
codex or grok pane asking `/recall` got a different answer than a Claude
session, from a different corpus, with quarantined and superseded subjects
eligible in one and not the other. Capability belongs in the repo with a
`python -m`-shaped entry point; the harness gets a thin shim (PRINCIPLES 16).

The memory tier is served through the SAME path the per-turn channel uses —
`retrieve.corpus` filtered by the fold's `_servable.json` manifest, scored by
`retrieve.score` — so recall and retrieval can never disagree about what is
servable. Extra Markdown roots (a notes vault, an install's own notes) are
searched alongside and cited separately; they carry no lifecycle verdict, so
they are labelled as what they are.

Read-only. It writes nothing, and it never invents a hit: a source that is
absent is dropped and named in the Gaps footer, because "no matches" and "I
did not look there" are different answers.

    recall.py "wombat telemetry"
    recall.py "where did we leave off" --root ~/notes --limit 5
    recall.py "grid outage" --json

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402
import retrieve as R  # noqa: E402

# Bounds up front (PRINCIPLES 8): a recall that walks an unbounded vault on a
# busy host is a hang wearing a search's clothes.
MAX_FILES_PER_ROOT = 4000
MAX_FILE_BYTES = 200_000
SNIPPET = 240
WORD = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.I)


def _terms(query):
    return {w.lower() for w in WORD.findall(query or "")} - R.STOP


def memory_hits(query, limit, store=None, manifest=None):
    """The mesh tier — same corpus, same verdict, same scorer as the per-turn
    channel. Returns [] with a stated reason rather than a guess when the fold
    has published no manifest."""
    store = store or M.harness_store() or M.store_dir()
    allow = R.servable(path=manifest)
    if allow is None:
        return [], ("memory: the fold has published no servable manifest — "
                    "recall SUPPRESSED rather than served unfiltered")
    docs = R.corpus(store, allow)
    if not docs:
        return [], "memory: the store is empty here"
    out = []
    for score, slug, desc in R.score(query, docs, limit):
        out.append({"source": "memory", "score": round(score, 4), "slug": slug,
                    "cite": f"[[{slug}]]", "path": str(store / f"{slug}.md"),
                    "title": desc, "snippet": _snippet(store / f"{slug}.md", query)})
    return out, None


def _snippet(path, query):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
    except OSError:
        return ""
    terms = _terms(query)
    body, fm = [], False
    for line in text.splitlines():
        s = line.strip()
        if s == "---":
            fm = not fm
            continue
        if fm or not s or s.startswith("#"):
            continue
        body.append(s)
    joined = " ".join(body)
    # Centre the snippet on the first term that actually occurs, so the reader
    # sees WHY this was a hit rather than the note's opening pleasantries.
    low = joined.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, at - SNIPPET // 3)
    out = joined[start:start + SNIPPET].strip()
    return ("…" if start else "") + out + ("…" if len(joined) > start + SNIPPET else "")


def root_hits(query, roots, limit):
    """Plain keyword scoring over extra Markdown roots (a vault, an install's
    own notes). These carry NO lifecycle verdict — nothing supersedes or
    quarantines a vault page — so they are cited by path and labelled by root,
    never merged into the memory tier."""
    terms = _terms(query)
    out = []
    if not terms:
        return out
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        seen = 0
        scored = []
        for path in root.rglob("*.md"):
            seen += 1
            if seen > MAX_FILES_PER_ROOT:
                break
            parts = set(path.parts)
            if "_inbox" in parts or ".git" in parts or "node_modules" in parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
            except OSError:
                continue
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            score += 5 * len(_terms(path.stem) & terms)   # the name is a strong signal
            if score:
                scored.append((score, path, text))
        scored.sort(key=lambda r: (-r[0], str(r[1])))
        for score, path, text in scored[:limit]:
            title = next((l[2:].strip() for l in text.splitlines()
                          if l.startswith("# ")), path.stem.replace("-", " "))
            out.append({"source": str(root), "score": float(score), "slug": path.stem,
                        "cite": f"{path} > {title}", "path": str(path),
                        "title": title, "snippet": _snippet(path, query)})
    return out


def recall(query, roots=(), limit=8, with_memory=True):
    """One pack, quota'd per source so a large vault cannot drown the store."""
    notes = []
    mem, why = ([], None) if not with_memory else memory_hits(query, limit)
    if why:
        notes.append(why)
    others = root_hits(query, roots, limit)
    searched = (["memory"] if with_memory else []) + [
        str(Path(r).expanduser()) for r in roots if Path(r).expanduser().is_dir()]
    missing = [str(r) for r in roots if not Path(r).expanduser().is_dir()]
    # Normalize within each source before interleaving: raw scores are not
    # comparable across a tf-idf cosine and a term count.
    pack = []
    for group in (mem, others):
        top = max((h["score"] for h in group), default=0) or 1
        for h in group:
            pack.append(dict(h, norm=h["score"] / top))
    pack.sort(key=lambda h: -h["norm"])
    return {"query": query, "hits": pack[:limit], "searched": searched,
            "absent": missing, "notes": notes}


def render(result):
    if not result["hits"]:
        lines = [f"**Recall** — no matches for `{result['query']}`."]
    else:
        lines = [f"**Recall** — `{result['query']}`", ""]
        for i, h in enumerate(result["hits"], 1):
            lines.append(f"{i}. **{h['title']}**")
            lines.append(f"   `{h['cite']}`")
            if h["snippet"]:
                lines.append(f"   {h['snippet']}")
    gaps = "Gaps: searched " + ", ".join(result["searched"])
    if result["absent"]:
        gaps += "; not present here: " + ", ".join(result["absent"])
    for n in result["notes"]:
        gaps += f"; {n}"
    gaps += (". Memory is point-in-time — verify anything load-bearing "
             "against live state before acting on it.")
    lines += ["", gaps]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="+")
    ap.add_argument("--root", action="append", default=[],
                    help="extra Markdown root to search (repeatable)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-memory", action="store_true",
                    help="search only the extra roots (the /wiki shape)")
    args = ap.parse_args(argv)
    result = recall(" ".join(args.query), args.root, max(1, args.limit),
                    with_memory=not args.no_memory)
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
