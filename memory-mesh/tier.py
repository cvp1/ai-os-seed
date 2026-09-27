#!/usr/bin/env python3
"""tier — replicate a subject's always-on / on-demand index tier as an event.

    tier.py --ondemand SUBJECT...          hold these out of the always-on index
    tier.py --always SUBJECT...            let these compete for always-on again
    tier.py --from-exclude                 emit `ondemand` for every entry of THIS
                                           host's _index-exclude.txt that no live
                                           tier event covers yet (migration)
    ... [--commit]                         dry run by default

Why (Craig 2026-09-27, "do the recommended for 2"): `_index-exclude.txt` was a
per-host file, so {{REDACTED}} and {{REDACTED}} disagreed on 38 always-on rows with
nothing to reconcile them. A `tier` event travels with the log; every host's
fold projects its file from the events (mesh_lib.project_index_exclude).

What it does NOT do: publish. Any always-on change a tier event causes is
STAGED by each host's fold behind the existing residency gate; Craig promotes
it (`fold.py --promote-residency`). A subject may be a bare lesson slug
(`one-home-per-fact` -> `lesson/one-home-per-fact`) or a full subject; one that
has no event in the log is refused, so a typo can never silently do nothing.
Stdlib only; one lock, one commit per run. Bounded: at most MAX_BATCH subjects.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

MAX_BATCH = 2000
SESSION = "tier"


def subject_of(name):
    return name if "/" in name else f"lesson/{name}"


def plan(targets, tier, events, fold):
    """-> (emit [(subject, supersede_ids)], skipped [(subject, why)])."""
    known = {e["subject"] for e in events}
    live_tiers = {}
    superseded = {s for e in events for s in
                  ([e["supersedes"]] if isinstance(e.get("supersedes"), str)
                   else (e.get("supersedes") or []))}
    for e in events:
        if e["kind"] == "tier" and e["id"] not in superseded:
            live_tiers.setdefault(e["subject"], []).append(e["id"])
    current = fold.get("tiers") or {}
    out, skipped = [], []
    for subj in targets:
        if subj not in known:
            skipped.append((subj, "no event on this subject in the log"))
        elif current.get(subj) == tier and len(live_tiers.get(subj, [])) == 1:
            skipped.append((subj, f"already {tier}"))
        else:
            out.append((subj, sorted(live_tiers.get(subj, []))))
    return out, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ondemand", nargs="+", metavar="SUBJECT")
    g.add_argument("--always", nargs="+", metavar="SUBJECT")
    g.add_argument("--from-exclude", action="store_true")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    events, _ = M.read_all_events()
    fold = M.fold_events(events, M.load_registry())
    if a.from_exclude:
        store = M.harness_store()
        if store is None:
            sys.exit("tier: no generated store on this host — nothing to migrate")
        covered = {M._tier_key(s) for s in (fold.get("tiers") or {})}
        names = sorted(M.file_ondemand_slugs(store) - covered)
        tier = "ondemand"
    else:
        names = a.ondemand or a.always
        tier = "ondemand" if a.ondemand else "always"
    targets = [subject_of(n) for n in names]
    if len(targets) > MAX_BATCH:
        sys.exit(f"tier: {len(targets)} subjects exceeds MAX_BATCH={MAX_BATCH}")
    todo, skipped = plan(targets, tier, events, fold)

    for subj, why in skipped:
        print(f"skip {subj}: {why}")
    print(f"{'would emit' if not a.commit else 'emitting'} {len(todo)} `{tier}` "
          f"tier event(s); {len(skipped)} skipped")
    if not a.commit or not todo:
        if not a.commit:
            print("(dry run — pass --commit to apply)")
        return 1 if (skipped and not todo and not a.from_exclude) else 0

    lines = []
    for subj, ids in todo:
        _, line = M.make_event("tier", subj, f"index tier: {tier}", session=SESSION,
                               lineage="operator-direct", audience="operator",
                               confidence="operator-stated",
                               supersedes=ids or None, tier=tier)
        lines.append(line)
    with M.repo_lock():
        for line in lines:
            log = M.append_event_line(line)
        M.git("add", str(log.relative_to(M.MESH_ROOT)))
        M.git("commit", "-q", "-m", f"tier: {len(lines)} subject(s) -> {tier}")
    print(f"emitted {len(lines)} tier event(s) -> {log.name}. Each host's next fold "
          f"projects its _index-exclude.txt and STAGES any always-on change for "
          f"Craig's --promote-residency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
