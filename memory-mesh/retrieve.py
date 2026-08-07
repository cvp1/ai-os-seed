#!/usr/bin/env python3
"""retrieve — score the memory corpus against a turn and return the top-k.

THE POINT. Always-on memory is capped at ~24 KB by the consumer, the corpus
grows every session, and `/recall` is manual — an agent that does not know a
rule exists cannot think to look for it. So everything important got crammed
into the one channel that always fires, and 142 rows fought over one budget.
That fight is a DELIVERY problem wearing a storage problem's clothes.

This is the other delivery channel: given the text of a turn, surface the
memories that turn actually needs. Corpus size stops touching the always-on
budget, so growth stops requiring a curation session.

LIFECYCLE. This tier serves only what the fold still stands behind, filtered
through the `_servable.json` manifest (live, non-superseded, non-quarantined,
non-parked). It did NOT until 2026-07-31: it globbed the store and consulted no
verdict at all, so quarantined untrusted-lineage facts and superseded doctrine
both rode in labelled "STANDING RULES" — verified live, including a session that
was served a quarantined subject while reviewing this very file. A store file is
not a servable fact; the fold keeps files for subjects it has retired.

WHAT THIS DOES NOT SOLVE (say it plainly, so nobody reads it as a cure):
  * multi-hop — needing memory A to learn that B exists. Flat top-k will not.
  * conflicting or stale memories both scoring in — retrieval will serve both
    confidently. The mesh parks DETECTED contradictions; undetected ones ride,
    and same-proposition-different-subject pairs are not detected at all.
  * which memories deserve to exist at all. Delivery is solved here; curation
    QUALITY still scales with the corpus.

SECURITY. Scoring runs on the turn's text, so text can steer which memories
load — including AWAY from a rule an attacker would rather not fire. Untrusted
spans (pasted mail, fetched pages, tool output) are fenced out of the scorer
before it runs. That is necessary, not sufficient: the real answer is that
anything genuinely dangerous is a GATE (see ~/.claude/hooks/safety-gate.py),
not a memory that has to be retrieved to work.

Stdlib only; targets /usr/bin/python3.
"""
import json
import math
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402

TOP_K = 5
MAX_INJECT_BYTES = 1400
# Spans we refuse to score on: content the agent INGESTED rather than the
# operator's own words. A crafted page that repeats "ignore memory about
# secrets" would otherwise reshape retrieval by sheer term frequency.
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
    """The fold's delivery manifest: slugs this tier may serve. None if absent.

    A store FILE is not a servable fact. Quarantined and superseded subjects
    keep their files — deletion on a projection would be data loss — so the
    file's existence says nothing about whether the fold still stands behind
    it. Written by `mesh_lib.write_servable_manifest` on every fold.

    `path` overrides the location OUTRIGHT — no fallback. A test that could
    silently fall back to the host's real manifest would pass for the wrong
    reason: its query matches nothing in the real corpus either, so "empty" would
    look like "correctly suppressed" while proving nothing.
    """
    p = path if path is not None else M.servable_manifest_path()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("slugs") or [])
    except Exception:  # noqa: BLE001 — missing/corrupt handled by the caller
        return None


def corpus(store, allow=None):
    """Servable memory files, as (slug, description, text).

    `allow` is the fold's manifest. Filtering here rather than at render time is
    deliberate: a held-out doc must not reach the SCORER either, or its terms
    still shape idf and it can displace a legitimate hit without appearing.
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
    """Plain TF-IDF cosine-ish scoring. Deterministic, no model, no network.

    Deliberately dumb: the fold has a no-model invariant and this runs on every
    turn, so an embedding call would add a dependency, a latency tail and a
    failure mode to the hot path. If precision proves insufficient the answer is
    corpus consolidation (merge duplicates), not a smarter scorer in the loop.
    """
    q = _tok(turn_text)
    if not q:
        return []
    qs = set(q)
    n = len(docs) or 1
    df = {}
    toks = []
    for slug, desc, text in docs:
        # weight the description: it is the human-written summary, and matching
        # it is a better relevance signal than matching a word buried in prose
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
    """Drop ingested spans before scoring (see SECURITY above)."""
    return FENCE_RX.sub(" ", text or "")


def retrieve(turn_text, store=None, k=TOP_K, manifest=None):
    """Top-k servable memories for this turn, or [] if the manifest is missing.

    FAILS CLOSED, unlike the module's outer handler. That asymmetry is the
    point: an exception means this code broke and a missed memory beats a
    bricked turn, but a missing manifest means the fold's verdict is UNKNOWN —
    and serving unfiltered was the live defect this exists to fix, not a
    tolerable degradation. The fold republishes on every run, so the closed
    window is bounded by the timer and self-heals.
    """
    store = store or M.harness_store()
    allow = servable(path=manifest)
    if allow is None:
        if store is not None:
            print("retrieve: no servable manifest — recall SUPPRESSED until "
                  "the next fold publishes one", file=sys.stderr)
        return []
    return score(fence(turn_text), corpus(store, allow), k)


def log_injection(turn_text, hits, path=None):
    """Record what was injected and why.

    Without this, 'why did it do that' is unanswerable a week later, and a
    poisoned memory that shaped a turn leaves no trace. The log is the audit
    surface for a channel that otherwise operates invisibly.
    """
    path = path or (M.MESH_ROOT / "state" / "retrieval-log.ndjson")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"chars": len(turn_text or ""),
               "hits": [{"slug": s, "score": round(sc, 4)} for sc, s, _ in hits]}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass          # logging must never break a turn


def render(hits):
    if not hits:
        return ""
    # Framing is load-bearing and was MEASURED, not guessed (P1b, 2026-07-31):
    # "relevant memories ... lower authority than always-on" scored 8/10 on the
    # unnatural-behaviour probe; the directive framing below scored 9/10. Same
    # rule, same model, same n. Retrieved rules still yield to the user and to
    # always-on, but hedging the DELIVERY cost compliance.
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
        # FAIL OPEN, loudly. A retrieval hook that can break a turn is worse
        # than one that occasionally misses — a gate that bricks sessions gets
        # deleted, and then there is no gate at all.
        print(f"retrieve: failed open ({e.__class__.__name__}: {e})",
              file=sys.stderr)
        sys.exit(0)
