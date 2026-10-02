#!/usr/bin/env python3
"""effectiveness: measure the retrieval channel from retrieval-log.ndjson.

Reports delivery (fire rate, hits/turn, saturation at top_k), windowed coverage
and dead-weight slugs, and always-on vs on-demand byte cost. Does not measure
precision, recall, or behavioural effect. High saturation is flagged, not
treated as proof of precision.

    python3 memory-mesh/effectiveness.py
    python3 memory-mesh/effectiveness.py --window 2000
    python3 memory-mesh/effectiveness.py --dry-run
    python3 memory-mesh/effectiveness.py --selftest

Stdlib; _lib.influx optional (the write is skipped with a stderr note if absent).
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import mesh_lib as M          # noqa: E402

MEASUREMENT = "cc_memory_effectiveness"
DEFAULT_WINDOW = 1000
OVEREXPOSED_FRAC = 0.20     # a slug in >20% of fired window turns is worth a look
DEAD_WEIGHT_SHOW = 20       # cap the printed list; the count is the real number


def _read_log(path):
    """Parse the ndjson log, skipping corrupt lines."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def _servable(path=None):
    path = path or M.servable_manifest_path()
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return set(doc.get("slugs") or [])
    except Exception:
        return None


def snapshot(log_path=None, manifest_path=None, store=None, window=DEFAULT_WINDOW):
    """Return a report dict, or {'error': ...} if inputs are missing."""
    log_path = log_path or (M.MESH_ROOT / "state" / "retrieval-log.ndjson")
    records = _read_log(log_path)
    if not records:
        return {"error": "no retrieval log yet — channel has not fired, or "
                          "the log path is wrong"}

    servable = _servable(manifest_path)
    if servable is None:
        return {"error": "no servable manifest — dead-weight/coverage are "
                          "unknowable until the fold publishes one"}

    total = len(records)
    win = records[-window:] if window else records
    n_win = len(win)

    fired = 0
    saturated = 0
    hits_sum = 0
    top_scores = []
    pulls = Counter()
    top_k = None
    for r in win:
        hits = r.get("hits") or []
        hits_sum += len(hits)
        if hits:
            fired += 1
            top_scores.append(hits[0]["score"])
            if top_k is None or len(hits) > top_k:
                top_k = len(hits)
            for h in hits:
                pulls[h["slug"]] += 1
    # Use the observed max hit count as the channel's live top_k.
    top_k = top_k or 0
    saturated = sum(1 for r in win if len(r.get("hits") or []) == top_k) if top_k else 0

    top_scores.sort()
    def pct(vals, p):
        if not vals:
            return None
        i = min(len(vals) - 1, int(len(vals) * p))
        return round(vals[i], 3)

    pulled = set(pulls)
    dead = sorted(servable - pulled)
    overexposed = sorted(
        ((slug, n, round(n / max(1, fired), 3)) for slug, n in pulls.items()
         if slug in servable and n / max(1, fired) >= OVEREXPOSED_FRAC),
        key=lambda t: -t[1])

    # Cost split: always-on index bytes vs on-demand memory file bytes.
    always_on_bytes = None
    ondemand_bytes = 0
    ondemand_files = 0
    if store is None:
        store = M.harness_store()
    if store is not None:
        idx_path = os.path.join(str(store), "MEMORY.md")
        try:
            always_on_bytes = os.path.getsize(idx_path)
        except OSError:
            always_on_bytes = None
        for slug in M.ondemand_slugs(store):
            fp = os.path.join(str(store), f"{slug}.md")
            try:
                ondemand_bytes += os.path.getsize(fp)
                ondemand_files += 1
            except OSError:
                continue

    return {
        "log_total_turns": total,
        "window": n_win,
        "top_k_observed": top_k,
        "fire_rate": round(fired / n_win, 3) if n_win else None,
        "saturation_rate": round(saturated / fired, 3) if fired else None,
        "avg_hits_per_turn": round(hits_sum / n_win, 2) if n_win else None,
        "top_score_median": pct(top_scores, 0.5),
        "top_score_p10": pct(top_scores, 0.1),
        "servable_total": len(servable),
        "coverage_window": round(len(pulled & servable) / len(servable), 3)
            if servable else None,
        "dead_weight_count": len(dead),
        "dead_weight_sample": dead[:DEAD_WEIGHT_SHOW],
        "overexposed": overexposed[:10],
        "always_on_bytes": always_on_bytes,
        "ondemand_bytes": ondemand_bytes,
        "ondemand_files": ondemand_files,
    }


def render(snap):
    if "error" in snap:
        return f"effectiveness: {snap['error']}"
    lines = [
        f"effectiveness: window={snap['window']} of {snap['log_total_turns']:,} "
        f"logged turns, top_k={snap['top_k_observed']}",
        f"  delivery: fire_rate={snap['fire_rate']} "
        f"saturation={snap['saturation_rate']} "
        f"avg_hits={snap['avg_hits_per_turn']} "
        f"top_score(median/p10)={snap['top_score_median']}/{snap['top_score_p10']}",
    ]
    if snap["saturation_rate"] is not None and snap["saturation_rate"] > 0.9:
        lines.append(
            "  FLAG: saturation >90% — the channel fills to top_k on almost "
            "every turn. That is consistent with good matching AND with weak "
            "matching padded to k; this instrument cannot tell them apart "
            "(see docstring). Treat fire_rate/saturation as a floor, not "
            "proof of precision.")
    lines.append(
        f"  coverage: {snap['coverage_window']} of {snap['servable_total']} "
        f"servable slugs pulled in-window; dead weight "
        f"{snap['dead_weight_count']} (never pulled in-window)")
    if snap["dead_weight_sample"]:
        shown = ", ".join(snap["dead_weight_sample"])
        more = snap["dead_weight_count"] - len(snap["dead_weight_sample"])
        tail = f" (+{more} more)" if more > 0 else ""
        lines.append(f"    e.g. {shown}{tail}")
    if snap["overexposed"]:
        ov = ", ".join(f"{s} {frac:.0%}" for s, n, frac in snap["overexposed"])
        lines.append(f"  overexposed (>={OVEREXPOSED_FRAC:.0%} of fired turns): {ov}")
    if snap["always_on_bytes"] is not None:
        total = snap["always_on_bytes"] + snap["ondemand_bytes"]
        pct_hot = round(100 * snap["always_on_bytes"] / total, 1) if total else None
        lines.append(
            f"  cost: always-on {snap['always_on_bytes']:,} B vs on-demand "
            f"{snap['ondemand_bytes']:,} B across {snap['ondemand_files']} files "
            f"— {pct_hot}% of known corpus weight still on the hot path")
    else:
        lines.append("  cost: host not opted into a fold-generated index — skipped")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help=f"most recent N logged turns to score (default {DEFAULT_WINDOW})")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write influx")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    snap = snapshot(window=args.window)
    print(render(snap))
    if "error" in snap:
        return 0  # a quiet channel isn't this job's failure to report as one

    if args.dry_run:
        print("-- dry run: NOT written to influx")
        return 0

    import time
    ts = int(time.time() * 1e9)
    fields = {k: v for k, v in snap.items()
              if isinstance(v, (int, float)) and v is not None}
    try:
        # Lazy import: _lib.influx may be absent.
        from _lib import influx
    except ImportError as e:
        print(f"effectiveness: influx write skipped — _lib.influx unavailable ({e})",
              file=sys.stderr)
        return 0
    try:
        influx.write_points([(MEASUREMENT, {"host": os.uname().nodename}, fields, ts)])
    except influx.InfluxError as e:
        print(f"effectiveness: influx write skipped — {e}", file=sys.stderr)
    return 0


def _selftest():
    """Check the metrics on a known fixture and that missing inputs return a labeled error."""
    import tempfile
    fails = []

    def ok(cond, label):
        print("  %-56s %s" % (label, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        log = d / "retrieval-log.ndjson"
        # 4 turns: 3 fire (2 saturated at k=2, 1 partial at k=1), 1 misses.
        # slug 'a' pulled 3/3 fired turns -> overexposed at the 20% bar.
        # slug 'c' is servable but never pulled -> dead weight.
        rows = [
            {"chars": 10, "hits": [{"slug": "a", "score": 5.0}, {"slug": "b", "score": 3.0}]},
            {"chars": 10, "hits": [{"slug": "a", "score": 4.0}, {"slug": "b", "score": 2.0}]},
            {"chars": 10, "hits": [{"slug": "a", "score": 1.0}]},
            {"chars": 10, "hits": []},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        manifest = d / "servable.json"
        manifest.write_text(json.dumps({"slugs": ["a", "b", "c"]}))

        empty_store = d / "empty-store"
        empty_store.mkdir()
        snap = snapshot(log_path=log, manifest_path=manifest, store=empty_store,
                        window=100)
        ok(snap["log_total_turns"] == 4, "total turns counted")
        ok(snap["fire_rate"] == 0.75, "fire rate 3/4")
        ok(snap["top_k_observed"] == 2, "observed top_k is the max hit-count seen")
        ok(snap["saturation_rate"] == round(2 / 3, 3), "2 of 3 fired turns hit k=2")
        ok(snap["coverage_window"] == round(2 / 3, 3), "a,b pulled of a,b,c servable")
        ok(snap["dead_weight_count"] == 1 and snap["dead_weight_sample"] == ["c"],
           "c is dead weight")
        ok(any(s == "a" for s, n, f in snap["overexposed"]),
           "a is overexposed (pulled in all 3 fired turns)")
        ok(snap["always_on_bytes"] is None and snap["ondemand_bytes"] == 0,
           "empty store -> cost section is None/0, not a fake number")

        # missing log -> labeled error, not a crash or a fake all-zero report
        missing = snapshot(log_path=d / "nope.ndjson", manifest_path=manifest)
        ok("error" in missing, "missing log returns a labeled error")

        # missing manifest -> labeled error even though the log is fine
        missing2 = snapshot(log_path=log, manifest_path=d / "nope.json")
        ok("error" in missing2, "missing manifest returns a labeled error")

        # render() must not crash on either error path
        try:
            render(missing)
            render(missing2)
            render(snap)
            ok(True, "render() handles error and normal snapshots")
        except Exception as e:  # noqa: BLE001
            ok(False, f"render() raised: {e}")

    print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
