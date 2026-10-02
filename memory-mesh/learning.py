#!/usr/bin/env python3
"""learning: detect relearned lessons (new lessons that near-duplicate prior ones).

Pairs each judged lesson with its closest strictly-prior lessons by token
Jaccard similarity. Tags:

  RELEARNED?         new lesson closely matches a prior one on a different
                     subject, with no supersedes link.
  RE-FILED           same subject re-emitted without a supersedes link.
  SERVED+REOFFENDED  one of the closest priors appears in the retrieval log
                     within the lookback window (no session attribution).

No default alarm threshold; --threshold reports a flagged count. Superseded
lessons are priors only; bulk-import days are excluded from the judged side.
Candidates need human judgment; paraphrased repeats are missed.

    python3 memory-mesh/learning.py                # full baseline report
    python3 memory-mesh/learning.py --days 30      # only judge recent lessons
    python3 memory-mesh/learning.py --top 10
    python3 memory-mesh/learning.py --selftest

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M          # noqa: E402

RETRIEVAL_LOG = M.MESH_ROOT / "state" / "retrieval-log.ndjson"
SERVED_LOOKBACK_DAYS = 7      # provisional, not measured — see --lookback
TOP_PRIORS = 5                # served-join checks this many closest priors
BURST_MIN = 30                # >= this many lessons on one UTC day = bulk import
_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
# Small and domain-specific on purpose: these words appear in nearly every
# lesson in this corpus, so they inflate every pairwise score equally and
# drown the signal. Derived by eyeballing the live corpus, not a general
# NLP stoplist — revisit against data, like every other number here.
_STOP = {"lesson", "never", "before", "check", "must", "always", "every",
         "does", "not", "the", "that", "this", "with", "from", "into",
         "when", "only", "them", "then", "than", "have", "should", "would",
         "which", "their", "there", "against", "after", "first"}


def _tokens(ev):
    text = " ".join(filter(None, [
        ev.get("subject", "").split("/", 1)[-1].replace("-", " "),
        ev.get("hook") or "",
        ev.get("content") or "",
    ])).lower()
    return frozenset(t for t in _TOKEN_RE.findall(text) if t not in _STOP)


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_ts(v):
    """Return epoch seconds from an epoch number or ISO 'Z' string, else None."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.datetime.strptime(
            str(v), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _sup_ids(ev):
    sup = ev.get("supersedes")
    if not sup:
        return set()
    items = sup if isinstance(sup, list) else str(sup).split(",")
    return {str(s).strip() for s in items if str(s).strip()}


def _superseded_ids(events):
    out = set()
    for ev in events:
        out |= _sup_ids(ev)
    return out


def burst_days(lessons, burst_min=BURST_MIN):
    """Return UTC days with >= burst_min lessons (bulk imports)."""
    per_day = Counter((e.get("ts") or "")[:10] for e in lessons)
    return {d for d, n in per_day.items() if d and n >= burst_min}


def relearn_pairs(priors_pool, judged, top_priors=TOP_PRIORS):
    """Return [(new_ev, [(prior_ev, sim), ...])] of each judged lesson's closest strictly-prior lessons.

    Skips explicitly superseded priors, and same-subject priors only when the
    new event has a supersedes link.
    """
    ptoks = [(e, _parse_ts(e.get("ts")), _tokens(e)) for e in priors_pool]
    out = []
    for new in judged:
        new_ts = _parse_ts(new.get("ts"))
        if new_ts is None:
            continue                          # can't make a temporal claim
        sup = _sup_ids(new)
        scored = []
        for prior, prior_ts, ptok in ptoks:
            if prior.get("id") == new.get("id"):
                continue
            if prior_ts is None or prior_ts >= new_ts:
                continue                      # strictly prior, parsed time
            if prior.get("id") in sup:
                continue                      # explicit, deliberate replacement
            if (prior.get("subject") == new.get("subject") and sup):
                continue                      # revision chain on this subject
            scored.append((prior, _jaccard(_tokens(new), ptok)))
        if scored:
            scored.sort(key=lambda x: -x[1])
            out.append((new, scored[:top_priors]))
    return out


def parse_served_lines(lines):
    """Return ({slug: [epoch, ...]}, lines_with_ts, total) from retrieval-log lines."""
    served, with_ts, total = {}, 0, 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue                          # time unknown
        with_ts += 1
        for hit in rec.get("hits", []):
            served.setdefault(hit.get("slug"), []).append(ts)
    return served, with_ts, total


def load_served_times():
    try:
        with open(RETRIEVAL_LOG, encoding="utf-8") as fh:
            return parse_served_lines(fh)
    except OSError:
        return {}, 0, 0


def served_prior(new, priors_scored, served_times,
                 lookback_days=SERVED_LOOKBACK_DAYS):
    """Return the first of the closest priors served within lookback before `new`, or None."""
    new_ts = _parse_ts(new.get("ts"))
    if new_ts is None:
        return None
    lo = new_ts - lookback_days * 86400
    for prior, sim in priors_scored:
        slug = (prior.get("subject") or "").split("/", 1)[-1]
        if any(lo <= t <= new_ts for t in served_times.get(slug, [])):
            return prior
    return None


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None,
                    help="only judge lessons emitted in the last N days "
                         "(priors are always the whole corpus)")
    ap.add_argument("--top", type=int, default=15, help="pairs to print")
    ap.add_argument("--threshold", type=float, default=None,
                    help="also report a flagged COUNT at this similarity — "
                         "set it from a measured baseline, never invented")
    ap.add_argument("--lookback", type=int, default=SERVED_LOOKBACK_DAYS,
                    help="served-join window in days (provisional default; "
                         "vary it to see sensitivity rather than trusting one)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    events, problems = M.read_all_events()
    lessons = [e for e in events if e.get("kind") == "lesson"]
    dead = _superseded_ids(events)
    bursts = burst_days(lessons)

    # Superseded lessons stay as priors but are not judged.
    judged = [e for e in lessons
              if e["id"] not in dead
              and (e.get("ts") or "")[:10] not in bursts]
    if args.days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=args.days)).timestamp()
        judged = [e for e in judged
                  if (_parse_ts(e.get("ts")) or 0) >= cutoff]

    pairs = relearn_pairs(lessons, judged)
    served_times, with_ts, total_lines = load_served_times()
    sims = sorted(p[1][0][1] for p in pairs)

    print(f"learning: {len(lessons)} lessons ({len(dead & {e['id'] for e in lessons})} "
          f"superseded — priors only), {len(pairs)} judged"
          + (f" (window: last {args.days}d)" if args.days else ""))
    if bursts:
        burst_n = sum(1 for e in lessons if (e.get("ts") or "")[:10] in bursts)
        print(f"  bulk-import day(s) excluded from the judged side "
              f"({burst_n} lessons — ts is import time, not learning time): "
              f"{', '.join(sorted(bursts))}")
    if problems:
        print(f"  ({len(problems)} unparseable/held-out event lines skipped)")
    print(f"  best-prior similarity: p50={_percentile(sims, 50):.2f}  "
          f"p90={_percentile(sims, 90):.2f}  p99={_percentile(sims, 99):.2f}  "
          f"max={sims[-1] if sims else 0:.2f}")
    print(f"  served-join: {with_ts}/{total_lines} retrieval-log lines usable; "
          f"checks the {TOP_PRIORS} closest priors; 'served' = present in "
          f"SOME session's window (no attribution), lookback {args.lookback}d")

    ranked = sorted(pairs, key=lambda p: -p[1][0][1])[:args.top]
    print(f"\n  top {len(ranked)} relearn candidates (human judgment decides — "
          f"overlap alone convicts nobody):")
    for new, scored in ranked:
        best, best_sim = scored[0]
        hit = served_prior(new, scored, served_times, args.lookback)
        if hit is not None:
            tag = "SERVED+REOFFENDED"
            shown = hit
        elif best.get("subject") == new.get("subject"):
            tag = "         RE-FILED"
            shown = best
        else:
            tag = "       relearned?"
            shown = best
        print(f"  {best_sim:.2f} {tag}  {str(new.get('ts', '?'))[:10]} "
              f"{new.get('subject', '?').split('/', 1)[-1]}")
        print(f"       ≈ {str(shown.get('ts', '?'))[:10]} "
              f"{shown.get('subject', '?').split('/', 1)[-1]}")

    if args.threshold is not None:
        flagged = [p for p in pairs if p[1][0][1] >= args.threshold]
        reoff = [p for p in flagged
                 if served_prior(p[0], p[1], served_times, args.lookback)]
        print(f"\n  at threshold {args.threshold:.2f}: {len(flagged)} flagged, "
              f"{len(reoff)} of those SERVED+REOFFENDED")
    else:
        print(f"\n  (no threshold set — this run IS the baseline; pick one "
              f"just past ordinary per the distribution above, then re-run "
              f"with --threshold)")
    return 0


def selftest():
    def ev(id_, ts, subject, content, supersedes=None):
        return {"id": id_, "ts": ts, "kind": "lesson", "subject": subject,
                "content": content, "hook": "", "supersedes": supersedes}

    a = ev("a1", "2026-08-01T00:00:00Z", "lesson/scrub-corrupts-identifiers",
           "the scrubber deny-list corrupts python identifiers producing invalid syntax tokens")
    b = ev("b2", "2026-08-10T00:00:00Z", "lesson/scrub-corrupts-url-values",
           "the scrubber deny-list corrupts python string values producing invalid runtime tokens")
    c = ev("c3", "2026-08-11T00:00:00Z", "lesson/horses-need-morning-water",
           "the horses water trough freezes overnight in january refill after sunrise")
    d = ev("d4", "2026-08-12T00:00:00Z", "lesson/scrub-corrupts-identifiers",
           "the scrubber deny-list corrupts python identifiers producing invalid syntax tokens",
           supersedes="a1")
    # same subject as a1, NO supersedes — a re-file, must be judged not skipped
    e = ev("e5", "2026-08-13T00:00:00Z", "lesson/scrub-corrupts-identifiers",
           "the scrubber deny-list corrupts python identifiers producing invalid syntax tokens")

    pool = [a, b, c, d, e]
    pairs = relearn_pairs(pool, pool)
    by_new = {p[0]["id"]: p for p in pairs}
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name)
        ok = ok and cond

    check("near-duplicate pair scores high (positive control)",
          by_new["b2"][1][0][0]["id"] == "a1" and by_new["b2"][1][0][1] > 0.5)
    check("unrelated lesson scores low (negative control)",
          by_new["c3"][1][0][1] < 0.2)
    check("explicit supersedes on the same subject is excluded",
          all(pr["id"] != "a1" and pr.get("subject") != d["subject"]
              for pr, _ in by_new["d4"][1]))
    check("same-subject re-file WITHOUT supersedes is judged, not skipped",
          by_new["e5"][1][0][1] > 0.9)
    check("superseded lesson still visible as a prior (regression watch)",
          any(pr["id"] == "a1" for pr, _ in by_new["e5"][1]))

    served = {"scrub-corrupts-identifiers": [_parse_ts("2026-08-09T00:00:00Z")]}
    check("served prior within lookback joins as REOFFENDED",
          served_prior(b, by_new["b2"][1], served) is not None)
    check("served outside lookback does not join",
          served_prior(b, by_new["b2"][1],
                       {"scrub-corrupts-identifiers":
                        [_parse_ts("2026-07-01T00:00:00Z")]}) is None)
    # served twin must not hide behind a closer unserved prior
    b_close = ev("f6", "2026-08-05T00:00:00Z", "lesson/scrub-corrupts-config-values",
                 "the scrubber deny-list corrupts python string values producing invalid runtime config")
    pairs2 = relearn_pairs([a, b_close, b], [b])
    check("served twin behind a closer unserved prior is still found",
          served_prior(b, pairs2[0][1],
                       {"scrub-corrupts-identifiers":
                        [_parse_ts("2026-08-09T00:00:00Z")]}) is not None)

    # the loader's ts parsing
    lines = [
        json.dumps({"ts": 1786766502.129, "chars": 10,
                    "hits": [{"slug": "float-ts-slug", "score": 1.0}]}),
        json.dumps({"ts": "2026-08-14T00:00:00Z", "chars": 10,
                    "hits": [{"slug": "iso-ts-slug", "score": 1.0}]}),
        json.dumps({"chars": 10, "hits": [{"slug": "no-ts-slug", "score": 1.0}]}),
        "not json at all",
    ]
    served2, with_ts, total = parse_served_lines(lines)
    check("loader accepts float epoch ts", "float-ts-slug" in served2)
    check("loader accepts ISO ts", "iso-ts-slug" in served2)
    check("loader holds out ts-less and garbage lines, counts honestly",
          "no-ts-slug" not in served2 and with_ts == 2 and total == 4)

    burst = burst_days([ev(f"x{i}", "2026-08-01T00:00:00Z", f"lesson/x{i}", "y")
                        for i in range(BURST_MIN)])
    check("a bulk-import day is detected", "2026-08-01" in burst)

    print("\nall checks passed" if ok else "\nFAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
