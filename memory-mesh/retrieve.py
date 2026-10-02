#!/usr/bin/env python3
"""retrieve: score the memory corpus against a turn's text and inject the top-k.

Hook: reads the event JSON on stdin, prints a <memory-retrieved> block.
Serves only slugs in the fold's `_servable.json` manifest. Untrusted spans are
fenced out before scoring. Not multi-hop; does not resolve undetected conflicts.
Stdlib only.
"""
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402

TOP_K = 5
MAX_INJECT_BYTES = 1400
# Ingested spans excluded from scoring so they cannot steer retrieval.
FENCE_RX = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|```.*?```"
    r"|<untrusted>.*?</untrusted>", re.S)
STOP = set("""a an the and or of to in for on with is are was were be been it
this that these those i you he she they we my your our their as at by from if
then than so not no do does did done can could should would will just now new
use used using make made get got how what when where why which who whom into
out up down over under again more most other some such only own same too very
s t don should've now""".split())


def _tok(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
            if w not in STOP]


def servable(store=None, path=None):
    """Return the set of servable slugs from the fold's manifest, or None if missing/corrupt.

    `path` overrides the location with no fallback.
    """
    p = path if path is not None else M.servable_manifest_path()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("slugs") or [])
    except Exception:  # noqa: BLE001 — missing/corrupt handled by the caller
        return None


def corpus(store, allow=None):
    """Return servable memory files as (slug, description, text).

    Filtered by `allow` before scoring so excluded docs do not affect idf.
    """
    out = []
    if store is None:
        return out
    for f in sorted(store.glob("*.md")):
        if f.name in ("MEMORY.md", "MEMORY.md.shadow", "QUARANTINE.md") \
                or f.name.startswith("_"):
            continue
        if allow is not None and f.stem not in allow:
            continue                      # quarantined, superseded, or parked
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        out.append((f.stem, (m.group(1) if m else f.stem).strip(), text))
    return out


def score(turn_text, docs, k=TOP_K):
    """Return the top-k (score, slug, desc) by TF-IDF; deterministic, no model or network."""
    q = _tok(turn_text)
    if not q:
        return []
    qs = set(q)
    n = len(docs) or 1
    df = {}
    toks = []
    for slug, desc, text in docs:
        # Weight the description above body text.
        t = _tok(desc) * 3 + _tok(text)
        tf = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        toks.append((slug, desc, tf))
        for w in set(t) & qs:
            df[w] = df.get(w, 0) + 1
    scored = []
    for slug, desc, tf in toks:
        s = 0.0
        for w in qs:
            if w in tf:
                idf = math.log(1 + n / (1 + df.get(w, 0)))
                s += (1 + math.log(tf[w])) * idf
        if s > 0:
            scored.append((s / math.sqrt(sum(tf.values()) or 1), slug, desc))
    scored.sort(reverse=True)
    return scored[:k]


def fence(text):
    """Drop ingested spans before scoring."""
    return FENCE_RX.sub(" ", text or "")


def retrieve(turn_text, store=None, k=TOP_K, manifest=None):
    """Return top-k servable memories for this turn; [] if the manifest is missing (fails closed)."""
    store = store or M.harness_store()
    allow = servable(path=manifest)
    if allow is None:
        if store is not None:
            print("retrieve: no servable manifest — recall SUPPRESSED until "
                  "the next fold publishes one", file=sys.stderr)
        return []
    return score(fence(turn_text), corpus(store, allow), k)


def log_injection(turn_text, hits, path=None):
    """Append an audit record of injected hits to the retrieval log.

    Older lines may lack `ts`; readers treat that as unknown time, not epoch 0.
    """
    path = path or (M.MESH_ROOT / "state" / "retrieval-log.ndjson")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3),
               "chars": len(turn_text or ""),
               "hits": [{"slug": s, "score": round(sc, 4)} for sc, s, _ in hits]}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass          # logging must never break a turn


def render(hits):
    if not hits:
        return ""
    # Directive framing measured better compliance than hedged framing.
    lines = ["<memory-retrieved>",
             "STANDING RULES retrieved for this turn. Follow them exactly "
             "unless the user or an always-on rule overrides them:"]
    used = 0
    for sc, slug, desc in hits:
        line = f"- [{slug}] {desc}"
        if used + len(line) > MAX_INJECT_BYTES:
            break
        lines.append(line)
        used += len(line)
    lines.append("</memory-retrieved>")
    return "\n".join(lines)


def main():
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw)
    except Exception:
        ev = {}
    turn = (ev.get("prompt") or ev.get("user_prompt")
            or ev.get("tool_response") or raw or "")
    if isinstance(turn, (dict, list)):
        turn = json.dumps(turn)
    hits = retrieve(turn)
    log_injection(turn, hits)
    out = render(hits)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Fail open loudly: a broken hook must not break the turn.
        print(f"retrieve: failed open ({e.__class__.__name__}: {e})",
              file=sys.stderr)
        sys.exit(0)
