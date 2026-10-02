#!/usr/bin/env python3
"""canary: positive control for the per-turn retrieval channel.

Samples three servable memories (first, middle, last) and checks retrieve.score()
finds each from its own `description`. Passes on >=2 of 3; silent on pass,
exit 1 on fail. A missing manifest or empty corpus fails.

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
    """Return (ok, findings)."""
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
    """Check the canary passes a healthy fixture and fails on missing manifest or empty corpus."""
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
