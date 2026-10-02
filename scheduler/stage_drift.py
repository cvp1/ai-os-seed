#!/usr/bin/env python3
"""Close the Monday loop: turn measurable scheduler<->freshness drift into
staged, hash-bound proposals the human applies via install.py.

Finds jobs in scheduler/manifest.yml with no observability/freshness.json
entry and stages an additive proposal under .cc-seed/staged/proposals/.
Unscheduled freshness entries are reported, never removed. Silent when clean.

    stage_drift.py [--root INSTALL_ROOT]
"""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REL_MANIFEST = "scheduler/manifest.yml"
REL_FRESHNESS = "observability/freshness.json"


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_manifest_jobs(manifest_text: str):
    """(name, schedule) pairs from `- name:` / `schedule:` lines, skipping comments."""
    jobs, name = [], None
    for raw in manifest_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("- name:"):
            name = line[len("- name:"):].strip()
        elif line.startswith("schedule:") and name is not None:
            jobs.append((name, line[len("schedule:"):].strip().strip('"')))
            name = None
    return jobs


def max_age_for(schedule: str):
    """max_age = interval plus slack; None for unsupported cron shapes (left for a human)."""
    fields = schedule.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, mon, dow = fields
    if re.fullmatch(r"\*/\d+", minute) and (hour, dom, mon, dow) == ("*", "*", "*", "*"):
        return f"{2 * int(minute[2:])}m"          # every N min -> 2N slack
    if minute.isdigit() and hour == "*" and (dom, mon, dow) == ("*", "*", "*"):
        return "2h"                                # hourly
    if minute.isdigit() and hour.isdigit() and (dom, mon, dow) == ("*", "*", "*"):
        return "26h"                               # daily
    if minute.isdigit() and hour.isdigit() and dom == "*" and mon == "*" and dow.isdigit():
        return "8d"                                # weekly
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                    help="the install root (default: the install this file is in)")
    args = ap.parse_args()
    root = args.root

    manifest_text = (root / REL_MANIFEST).read_text(encoding="utf-8")
    freshness_text = (root / REL_FRESHNESS).read_text(encoding="utf-8")
    freshness = json.loads(freshness_text)
    registered = freshness.get("jobs", {})
    scheduled = read_manifest_jobs(manifest_text)

    additions, unparseable = {}, []
    for name, schedule in scheduled:
        if name in registered:
            continue
        age = max_age_for(schedule)
        if age is None:
            unparseable.append((name, schedule))
            continue
        additions[name] = {
            "max_age": age,
            "label": f"{name} (cron: {schedule}) — cadence staged by stage_drift.py",
        }

    unscheduled = sorted(set(registered) - {n for n, _ in scheduled})

    for name, schedule in unparseable:
        print(f"NOTE: {name} ({schedule!r}) needs a hand-written cadence — "
              f"schedule shape not derived")
    if unscheduled:
        print(f"NOTE: registered but not scheduled (left alone, removal is "
              f"a human call): {', '.join(unscheduled)}")

    if not additions:
        return 0  # edge-trigger: nothing mechanical to stage

    merged = dict(freshness)
    merged["jobs"] = {**registered, **additions}
    after = json.dumps(merged, indent=2) + "\n"

    staged_dir = root / ".cc-seed" / "staged" / "proposals"
    staged_dir.mkdir(parents=True, exist_ok=True)

    # Dated slug; install.py refuses reuse of an applied slug, so suffix -2, -3, ...
    # until free in both the receipt and the staged dir.
    applied = set()
    receipt_path = root / ".cc-seed" / "receipt.json"
    if receipt_path.exists():
        try:
            applied = set(json.loads(receipt_path.read_text(encoding="utf-8"))
                          .get("applied_proposals", {}))
        except ValueError:
            pass  # unreadable receipt: install.py will refuse the apply anyway
    base = "freshness-drift-" + datetime.now(timezone.utc).strftime("%Y%m%d")
    slug, n = base, 1
    while slug in applied or (staged_dir / f"{slug}.json").exists():
        n += 1
        slug = f"{base}-{n}"
        if n > 99:  # bound the loop; 100 stagings in a day is a malfunction
            print("ERROR: could not find a free proposal slug — investigate")
            return 2
    for existing in staged_dir.glob("*.json"):
        try:
            prior = json.loads(existing.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if prior.get("target") == REL_FRESHNESS and prior.get("after_content") == after:
            print(f"FINDINGS: drift already staged as '{existing.stem}' — nothing new")
            return 0

    proposal = {
        "target": REL_FRESHNESS,
        "before_sha256": _sha256(freshness_text),
        "before_content": freshness_text,
        "after_content": after,
        "rationale": (f"register freshness cadence for {len(additions)} scheduled "
                      f"job(s) with no entry: {', '.join(sorted(additions))}"),
    }
    (staged_dir / f"{slug}.json").write_text(json.dumps(proposal, indent=2),
                                             encoding="utf-8")
    print(f"FINDINGS: staged proposal '{slug}' — {len(additions)} missing cadence "
          f"entr{'y' if len(additions) == 1 else 'ies'}: {', '.join(sorted(additions))}")
    print(f"review: python3 {root}/install.py --target {root} --review-proposals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
