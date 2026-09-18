#!/usr/bin/env python3
"""canary — positive control for the per-turn retrieval channel.

The 2026-08-13 memory-mesh tri-model review moved always-on weight onto
retrieval and named the cost: rules demoted to retrieval-only "simply stop
firing" if the retriever silently degrades, and nothing watched for that. This
is the watcher — the same positive-control discipline as everywhere else in
the fleet: a NEGATIVE result (no memories served) is only evidence if the
instrument can be shown to produce a positive.

Method: SELF-CUE. Sample three servable memories deterministically (first,
middle, last of the sorted corpus) and ask retrieve.score() to find each one
using its own frontmatter `description` as the turn text. A memory that cannot
be retrieved by its own human-written summary — the exact field the scorer
triple-weights — means the channel is degraded, whatever else looks green.
No dedicated canary memory: a real corpus row can't rot into a special case,
and the probe re-targets itself as the corpus changes.

Pass: >=2 of 3 self-cues hit (scoring nuance on one pathological doc must not
page daily). Silent on pass (Principle 7); loud + exit 1 on fail. A missing
manifest or empty corpus is a FAIL — that is the channel being dark, which is
the one thing this must never report as health.

Wired into the daily `cc-context-health` job (cron/MANIFEST.md).

    /usr/bin/python3 ~/{{REDACTED}}/memory-mesh/canary.py
    /usr/bin/python3 ~/{{REDACTED}}/memory-mesh/canary.py --selftest
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402
import retrieve as R  # noqa: E402

SAMPLES = 3
REQUIRED = 2


def run(store=None, manifest=None):
    """Returns (ok, findings)."""
    store = store or M.harness_store()
    allow = R.servable(path=manifest)
    if allow is None:
        return False, ["no servable manifest — the retrieval channel is dark, "
                       "not healthy"]
    docs = R.corpus(store, allow)
    if not docs:
        return False, ["servable corpus is EMPTY — retrieval cannot serve "
                       "anything"]
    idx = sorted({0, len(docs) // 2, len(docs) - 1})
    findings, hits = [], 0
    for i in idx:
        slug, desc, _text = docs[i]
        got = [s for _score, s, _d in R.score(desc, docs, k=R.TOP_K)]
        if slug in got:
            hits += 1
        else:
            findings.append(f"self-cue MISS: {slug!r} not in top-{R.TOP_K} "
                            f"for its own description ({desc[:60]!r})")
    if hits >= min(REQUIRED, len(idx)):
        return True, []
    findings.insert(0, f"retrieval canary: {hits}/{len(idx)} self-cues hit "
                       f"(need {min(REQUIRED, len(idx))}) — the per-turn "
                       "channel is degraded; rules held out of always-on are "
                       "not being delivered")
    return False, findings


def _selftest():
    """Prove the instrument can show the positive AND the negative."""
    import json
    import tempfile
    fails = []

    def ok(cond, label):
        print("  %-52s %s" % (label, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        for slug, desc in (("alpha-rule", "hydroponic nutrient dosing for the tower"),
                           ("beta-rule", "unifi controller backup restore procedure"),
                           ("gamma-rule", "propane tank telemetry alert thresholds")):
            (store / f"{slug}.md").write_text(
                f"---\nname: {slug}\ndescription: {desc}\n---\n\nBody about "
                f"{desc}.\n")
        man = store / "_servable.json"
        man.write_text(json.dumps({"slugs": ["alpha-rule", "beta-rule",
                                             "gamma-rule"]}))
        good, f = run(store=store, manifest=man)
        ok(good and not f, "healthy fixture passes")
        # manifest gone -> dark channel must FAIL, not pass
        man.unlink()
        dark, f = run(store=store, manifest=man)
        ok(not dark and "dark" in f[0], "missing manifest fails loud")
        # empty allow-list -> empty corpus must FAIL
        man.write_text(json.dumps({"slugs": []}))
        empty, f = run(store=store, manifest=man)
        ok(not empty and "EMPTY" in f[0], "empty corpus fails loud")
    print("\n%s (%d failed)" % ("PASS" if not fails else "FAIL", len(fails)))
    return 1 if fails else 0


def main():
    if sys.argv[1:2] == ["--selftest"]:
        return _selftest()
    good, findings = run()
    if good:
        return 0
    for f in findings:
        print(f"[CANARY ] {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
