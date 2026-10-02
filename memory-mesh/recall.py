#!/usr/bin/env python3
"""recall: answer "what do I know about X?" with citations, from any harness.

The memory tier uses retrieve.corpus/score filtered by the fold's servable
manifest; extra Markdown roots are searched and cited separately. Read-only;
absent sources are named in the Gaps footer.

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

# Bounds on files walked and bytes read per file.
MAX_FILES_PER_ROOT = 4000
MAX_FILE_BYTES = 200_000
SNIPPET = 240
WORD = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.I)


def _terms(query):
    return {w.lower() for w in WORD.findall(query or "")} - R.STOP


def memory_hits(query, limit, store=None, manifest=None):
    """Return (hits, reason) from the servable memory tier; reason is set when nothing can be served."""
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
    # Centre the snippet on the first matching term.
    low = joined.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, at - SNIPPET // 3)
    out = joined[start:start + SNIPPET].strip()
    return ("…" if start else "") + out + ("…" if len(joined) > start + SNIPPET else "")


def root_hits(query, roots, limit):
    """Keyword-score Markdown files under extra roots; cited by path and labelled by root."""
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
    """Combine memory and root hits, normalized per source, into one result pack."""
    notes = []
    mem, why = ([], None) if not with_memory else memory_hits(query, limit)
    if why:
        notes.append(why)
    others = root_hits(query, roots, limit)
    searched = (["memory"] if with_memory else []) + [
        str(Path(r).expanduser()) for r in roots if Path(r).expanduser().is_dir()]
    missing = [str(r) for r in roots if not Path(r).expanduser().is_dir()]
    # Normalize per source: tf-idf and term-count scores are not comparable.
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
