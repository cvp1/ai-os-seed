#!/usr/bin/env python3
"""declare: set a subject's residency tier by re-emitting its tip with the tier.

    declare.py --residency state  home/fleet-md ssh-route/HOST ...
    declare.py --residency doctrine lesson/some-slug ...
    declare.py --residency state --from-file batch.txt --commit

Each new event supersedes the live ones and carries the same kind/content, plus
the store body for lesson subjects. Pins and promoted tips are left alone.
Dry run by default.
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("subjects", nargs="*")
    ap.add_argument("--residency", required=True, choices=sorted(M.RESIDENCIES))
    ap.add_argument("--from-file", help="one subject per line (# comments ok)")
    ap.add_argument("--session", default="residency-declare")
    ap.add_argument("--commit", action="store_true",
                    help="apply (default: dry run)")
    args = ap.parse_args()

    subjects = list(args.subjects)
    if args.from_file:
        for ln in Path(args.from_file).read_text().splitlines():
            s = ln.split("#", 1)[0].strip()
            if s:
                subjects.append(s)
    if not subjects:
        return ap.error("no subjects given")

    events, _ = M.read_all_events()
    # Sets each event's `_signed`, which chain_body needs to find the authority.
    M.fold_events(events, M.load_registry())
    live = {e["id"]: e for e in events}
    store = M.harness_store()
    done, skipped = [], []

    for subj in subjects:
        ids = M.unsuperseded_ids(subj, events)
        if not ids:
            skipped.append((subj, "no live event on this subject"))
            continue
        # The tip is the newest non-pin event; pins are never superseded here.
        fact_ids = [i for i in ids if live[i]["kind"] != "pin"]
        if not fact_ids:
            skipped.append((subj, "only pin events live on this subject"))
            continue
        ids = fact_ids
        tip = live[ids[-1]]
        # Refuse a promoted tip: re-emitting cannot carry its signature or
        # approval and would demote it. Use tier.py instead.
        if tip.get("verbal_approval") or (tip.get("sig") and tip.get("_signed")):
            how = "verbally approved" if tip.get("verbal_approval") else "key-signed"
            skipped.append((subj, (
                f"tip {tip['id']} is {how} — declaring over it would strip the "
                f"promotion and quarantine a served fact; use tier.py for "
                f"residency, or re-promote after")))
            continue
        body = None
        if subj.startswith("lesson/") and store:
            f = store / f"{subj.split('/', 1)[1]}.md"
            if f.exists():
                body = f.read_text(encoding="utf-8")
                # Oversized bodies stay in the store file; declare without one.
                if len(body.encode("utf-8")) + 1500 > M.MAX_EVENT_BYTES:
                    body = None
        if body is None and subj.startswith("lesson/"):
            # No local file: re-carry the body from the supersede chain.
            chained = M.chain_body(tip, events)
            if chained and len(chained.encode("utf-8")) + 1500 <= M.MAX_EVENT_BYTES:
                body = chained
        if not args.commit:
            print(f"would declare {args.residency:8s} {subj} "
                  f"(supersedes {len(ids)}, body={'yes' if body else 'no'})")
            done.append(subj)
            continue
        # --carry-forward: content is re-emitted verbatim, so bypass the admission gate.
        cmd = [sys.executable, str(HERE / "emit.py"), "--no-nudge",
               "--carry-forward",
               "--kind", tip["kind"], "--subject", subj,
               "--content", tip.get("content") or subj,
               "--session", args.session,
               "--residency", args.residency,
               "--lineage", tip.get("lineage", "operator-direct"),
               "--audience", tip.get("audience", "operator"),
               # Carry confidence forward; emit's default would downgrade it.
               "--confidence", tip.get("confidence", "inferred"),
               "--supersedes", ",".join(ids)]
        # `home` is required on an assert; carry it forward.
        if tip.get("home"):
            cmd += ["--home", tip["home"]]
        if tip.get("polarity") and tip["polarity"] != "n/a":
            cmd += ["--polarity", tip["polarity"]]
        if body:
            cmd += ["--body", body]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            done.append(subj)
            print(f"declared {args.residency:8s} {subj}")
        else:
            skipped.append((subj, (r.stderr or r.stdout).strip()[:200]))

    print(f"\n{'declared' if args.commit else 'would declare'}: {len(done)} | "
          f"skipped: {len(skipped)}")
    for s, why in skipped:
        print(f"  skip {s}: {why}")
    if not args.commit:
        print("(dry run — pass --commit to apply)")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
