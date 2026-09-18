#!/usr/bin/env python3
"""declare — set a subject's SPEC-v4 residency tier (Craig's declaration).

    declare.py --residency state  home/fleet-md ssh-route/{{REDACTED}} ...
    declare.py --residency doctrine lesson/sms-stop-must-be-advertised ...
    declare.py --residency state --from-file batch.txt --commit

Residency is a human declaration, so this tool only carries one Craig has
already made; it invents nothing. It emits a fresh event on each subject that
SUPERSEDES the live ones, carrying the same kind/content plus the tier — so the
declaration is auditable, replayable, and reversible by another declaration,
exactly like every other belief change in the mesh.

Lesson subjects carry their store body into the event (SPEC v4: the event is
the fact), which also satisfies the ghost gate. Pointer subjects
(`home/...`, `ssh-route/...`) have no store file and carry content only.

Dry-run by default. Stdlib + emit.py; targets /usr/bin/python3.
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
    live = {e["id"]: e for e in events}
    store = M.harness_store()
    done, skipped = [], []

    for subj in subjects:
        ids = M.unsuperseded_ids(subj, events)
        if not ids:
            skipped.append((subj, "no live event on this subject"))
            continue
        # The tip is the newest FACT event, never a pin — and pins are never
        # superseded by a declaration. Found live 2026-08-13: on 5 pinned
        # subjects ids[-1] was the pin, so this tool re-emitted a PIN as the
        # fact and superseded the real lesson — the served row vanished and
        # the fold alarmed "PIN protects nothing". A pin is an overlay on the
        # subject, not its content; residency work must leave it standing.
        fact_ids = [i for i in ids if live[i]["kind"] != "pin"]
        if not fact_ids:
            skipped.append((subj, "only pin events live on this subject"))
            continue
        ids = fact_ids
        tip = live[ids[-1]]
        body = None
        if subj.startswith("lesson/") and store:
            f = store / f"{subj.split('/', 1)[1]}.md"
            if f.exists():
                body = f.read_text(encoding="utf-8")
                # An oversized body cannot ride the event (MAX_EVENT_BYTES:
                # "split it or point at a doc"). The store FILE satisfies the
                # ghost gate by itself, so point at the doc: declare hook-only
                # and leave the body in its home. Found 2026-08-13: the batch
                # skipped `local-llm-harness-over-model` (store file > 8 KB).
                if len(body.encode("utf-8")) + 1500 > M.MAX_EVENT_BYTES:
                    body = None
        if not args.commit:
            print(f"would declare {args.residency:8s} {subj} "
                  f"(supersedes {len(ids)}, body={'yes' if body else 'no'})")
            done.append(subj)
            continue
        # --carry-forward: a declaration re-emits the tip's content VERBATIM —
        # exactly the legacy-stump/over-length escape that flag documents.
        # Without it any subject whose live content predates the admission
        # gate can never be declared (found 2026-08-13: batch declare skipped
        # `inferred-writes-propose-only-boundary`, whose content the gate
        # refused as "trails off mid-sentence" — a stump this tool did not
        # mint and must not be blocked by).
        cmd = [sys.executable, str(HERE / "emit.py"), "--no-nudge",
               "--carry-forward",
               "--kind", tip["kind"], "--subject", subj,
               "--content", tip.get("content") or subj,
               "--session", args.session,
               "--residency", args.residency,
               "--lineage", tip.get("lineage", "operator-direct"),
               "--audience", tip.get("audience", "operator"),
               "--supersedes", ",".join(ids)]
        # `home` is required on an assert (one home per fact) — carry the tip's
        # forward rather than dropping it, or the re-emit fails validation for
        # a reason that has nothing to do with residency.
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
