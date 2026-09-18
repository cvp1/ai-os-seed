#!/usr/bin/env python3
"""repair_from_description — heal event/file drift at the SOURCE the file holds.

The 2026-07-27 backfill composed event content from the legacy index line, an
already-truncated derivative, while the lossless `description:` sat in the same
file. The result is 60-odd rows whose event is a STUMP of their own file
(projection_drift calls these file-richer), plus a handful the head-strip left
disagreeing outright (disjoint). This tool re-emits those events with the
description as content, so the event — which SPEC v4 makes the fact's home —
finally says what the file always said.

Three refusals, because a repair that quietly degrades is the bug it repairs:

* content that `admission_reject` refuses (over the index ceiling, or trailing
  off) is REPORTED, never truncated. A rule that does not fit must be rewritten
  by a human at the source; machine-cutting it is how the stumps happened.
* the render is simulated before anything is emitted, and a repair that would
  EVICT an index row or change residency membership is refused as a set. Rows
  grow when a stump is healed, and growth inside a byte-budgeted tier is
  zero-sum ([[promotion-into-a-fixed-budget-evicts]]).
* only LESSON events are superseded. `--supersedes-live-on` is kind-blind and
  swept two of Craig's pins on 2026-07-31; the supersede list here is built by
  filtering on kind, and pins are left standing.

Dry-run by default. Stdlib + emit.py; targets /usr/bin/python3.

    repair_from_description.py --disjoint          # what the head-strip left
    repair_from_description.py --file-richer       # the legacy stumps
    repair_from_description.py --subjects lesson/foo lesson/bar --commit
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

HERE = Path(__file__).resolve().parent
DESC = re.compile(r"^description:\s*(.*)$", re.M)


def file_description(store, subject):
    """The store file's one-line essence, collapsed — or None."""
    f = store / (subject.split("/", 1)[1] + ".md")
    if not f.exists():
        return None
    m = DESC.search(f.read_text(encoding="utf-8"))
    if not m:
        return None
    return " ".join(m.group(1).strip().strip('"').split()) or None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subjects", nargs="*", default=[])
    ap.add_argument("--disjoint", action="store_true")
    ap.add_argument("--file-richer", action="store_true")
    ap.add_argument("--session", default="drift-repair")
    ap.add_argument("--fit", action="store_true",
                    help="instead of refusing the whole set, repair the "
                         "largest subset that evicts nothing (cheapest growth "
                         "first) and NAME what was left behind")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    events, _ = M.read_all_events()
    by_id = {e["id"]: e for e in events}
    fold = M.fold_events(events, M.load_registry())
    store = M.harness_store()
    if store is None:
        sys.exit("no harness store on this host")

    drift = M.projection_drift(fold, store)
    want = list(args.subjects)
    if args.disjoint:
        want += drift["disjoint"]
    if args.file_richer:
        want += drift["file_richer"]
    want = sorted(dict.fromkeys(want))
    if not want:
        return ap.error("nothing selected (--disjoint / --file-richer / --subjects)")

    live_lesson = {e["subject"]: e for e in fold["live"] if e["kind"] == "lesson"}
    todo, refused, nochange = [], [], []
    for subj in want:
        ev = live_lesson.get(subj)
        if ev is None:
            refused.append((subj, "no live lesson event"))
            continue
        desc = file_description(store, subj)
        if desc is None:
            refused.append((subj, "no store file / no description:"))
            continue
        if desc == ev["content"]:
            nochange.append(subj)
            continue
        # Both funnel refusals, pre-screened so a refusal is REPORTED (this
        # tool's contract) rather than surfacing as a FAIL line from emit.py
        # after the batch has started. The fact-shape gate (2026-09-16) does
        # not exempt on `home`, so neither does this — pointing is not pasting.
        why = M.admission_reject(desc) or M.fact_refusal(desc)
        if why:
            refused.append((subj, why))
            continue
        # Kind-filtered: a pin on this subject is Craig's word and outranks a
        # mechanical repair, so it is never superseded (2026-07-31 incident).
        ids = [i for i in M.unsuperseded_ids(subj, events)
               if by_id[i]["kind"] == "lesson"]
        todo.append((subj, ev, ids, desc))

    # ── the zero-eviction gate, measured on the assembled artifact ──────────
    def simulate(sel):
        proposed = {s: d for s, _, _, d in sel}
        sim = [dict(e) for e in events]
        for e in sim:
            if e["subject"] in proposed and e["kind"] == "lesson":
                e["content"] = proposed[e["subject"]]
        sim_fold = M.fold_events(sim, M.load_registry())
        text, report = M.render_harness_memory(sim_fold, store)
        return (text, report, M.residency_delta(store / "MEMORY.md", text),
                M.delivery_breach(text))

    def safe(sel):
        _, report, delta, breach = simulate(sel)
        return not delta and not breach and report["rows"] >= report["rows_total"]

    deferred = []
    if args.fit and todo and not safe(todo):
        # Cheapest growth first: this is the frontier of what the budget can
        # absorb without spending residency, and the ORDER is what makes the
        # result deterministic rather than dependent on the drift set's order.
        # Not "best" — cheapest. What it leaves behind is named below, because
        # a silent cap reads as full coverage ([[bound-every-loop-and-output]]).
        ranked = sorted(todo, key=lambda t: len(t[3]) - len(t[1]["content"]))
        kept = []
        for cand in ranked:
            if safe(kept + [cand]):
                kept.append(cand)
            else:
                deferred.append(cand)
        todo = kept
    text, report, delta, breach = simulate(todo)

    for subj, why in refused:
        print(f"  REFUSED {subj}: {why}")
    for subj in nochange:
        print(f"  already aligned {subj}")
    print(f"\n{len(todo)} events to re-emit from their file description "
          f"({len(refused)} refused, {len(nochange)} already aligned)")
    if deferred:
        print(f"\nDEFERRED {len(deferred)} — the budget cannot absorb these "
              f"without evicting a row; they stay stumps until curation frees "
              f"bytes:")
        for subj, ev, _, desc in deferred:
            print(f"  ~ {subj} (+{len(desc) - len(ev['content'])}B)")
    print(f"render: {report['rows']}/{report['rows_total']} rows | "
          f"residency delta: {delta or 'none'} | breach: {breach or 'none'}")

    if delta or breach or report["rows"] < report["rows_total"]:
        sys.exit("REFUSED as a set: this repair would evict a row, change "
                 "always-on membership, or breach the loader ceiling. "
                 "Curate first — a repair may not spend residency.")
    if not args.commit:
        for subj, ev, _, desc in todo[:6]:
            print(f"\n  {subj}\n    - {ev['content'][:90]}\n    + {desc[:90]}")
        print("\n(dry run — pass --commit)")
        return 0

    for subj, ev, ids, desc in todo:
        cmd = [sys.executable, str(HERE / "emit.py"), "--no-nudge",
               "--kind", "lesson", "--subject", subj, "--content", desc,
               "--session", args.session,
               "--lineage", ev.get("lineage", "operator-direct"),
               "--audience", ev.get("audience", "operator"),
               "--confidence", ev.get("confidence", "inferred")]
        if ids:
            cmd += ["--supersedes", ",".join(ids)]
        if ev.get("home"):
            cmd += ["--home", ev["home"]]
        if ev.get("polarity") and ev["polarity"] != "n/a":
            cmd += ["--polarity", ev["polarity"]]
        if ev.get("residency"):
            cmd += ["--residency", ev["residency"]]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(("  ok   " if r.returncode == 0 else "  FAIL "), subj,
              (r.stderr or r.stdout).strip()[:100])
    print(f"\nre-emitted {len(todo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
