#!/usr/bin/env python3
"""Operator signing — turn Craig's judgement into a cryptographic fact.

    # resolve a parked subject with signed truth (supersedes both sides)
    sign.py --subject ssh-route/{{REDACTED}} --polarity exists \
            --content "ssh st21 works" --home FLEET.md#reachability \
            --supersedes abc123,def456

    # promote an agent's proposal (the gardener path): agents may only emit
    # kind=propose-correct; ONLY this command turns one into signed truth
    sign.py --promote <proposal-id>

Signed events are the only truth the fold will defend: any unsigned event
disagreeing with a signed one on the same subject parks AND alarms. Agents
cannot sign — the key lives in Craig's fscrypt vault and this CLI is the only
caller. That is the whole point: a compromised session can add noise, it
cannot manufacture authority.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M


def append(ev, line):
    log = M.append_event_line(line)
    with M.repo_lock():
        M.git("add", str(log.relative_to(M.MESH_ROOT)))
        M.git("commit", "-q", "-m", f"sign {ev['kind']} {ev['subject']} {ev['id']}")


MEMORY_WRITE = Path(os.path.expanduser(
    "~/{{REDACTED}}/cc-skills/improve/memory_write.py"))


def reconcile_store(subject):
    """After a promotion, make it fact on the STORE surface too (2026-07-30).

    Craig's ruling, in his words: "if I promote it, that must be fact
    everywhere." Before this, promotion wrote only the mesh — so a memory came
    out of the mesh quarantine and started being served while its store file
    still said `lineage: contains-untrusted` and the store's own QUARANTINE.md
    still listed it. One fact, two surfaces, two different answers.

    The store's per-file `lineage:` is the fact; `memory_write.py retag` owns
    writing it (a hand edit is blocked by the write guard, and rightly). Note
    that retag's docstring points at `consolidate.py` to move the index line —
    that script NO LONGER EXISTS, so nothing has maintained the store's
    QUARANTINE.md since the fold took over index generation. That file is an
    orphan holding stale entries; the fold's divergence check (mesh_lib
    .store_quarantine_drift) is what makes the remaining gap loud instead of
    silent, and it is Craig's open decision whether that file becomes a
    fold-derived view or is retired.

    Failure here does NOT fail the promotion: the signed event is already
    committed and is the authority. But it must be LOUD, because a half-applied
    promotion is exactly the divergence this function exists to end.
    """
    if not subject.startswith("lesson/"):
        return                      # only lessons have store files
    slug = subject.split("/", 1)[1]
    if not (Path(M.store_dir()) / f"{slug}.md").exists():
        return                      # mesh-only subject; nothing to reconcile
    if not MEMORY_WRITE.exists():
        print(f"  STORE NOT RECONCILED: {MEMORY_WRITE} missing — the store copy "
              f"still reads lineage: contains-untrusted while the mesh serves "
              f"this fact. Retag it by hand-equivalent tooling.", file=sys.stderr)
        return
    r = subprocess.run([sys.executable, str(MEMORY_WRITE), "retag", slug,
                        "--lineage", "craig-direct", "--commit"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        print(f"  store reconciled: {slug} -> lineage: craig-direct")
    else:
        print(f"  STORE NOT RECONCILED for {slug} (the signed event stands and "
              f"is authoritative, but the store copy still says "
              f"contains-untrusted):\n{r.stdout}{r.stderr}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", help="proposal event id to promote to signed truth")
    ap.add_argument("--subject")
    ap.add_argument("--content")
    ap.add_argument("--polarity", default="n/a", choices=sorted(M.POLARITIES))
    ap.add_argument("--home")
    ap.add_argument("--supersedes", help="comma-separated event ids")
    ap.add_argument("--audience", default="operator", choices=sorted(M.AUDIENCES))
    ap.add_argument("--signer", default=M.SIGNER)
    ap.add_argument("--session", default="operator-sign")
    args = ap.parse_args()

    subject, content, home, polarity = (args.subject, args.content,
                                        args.home, args.polarity)
    supersedes = [s for s in (args.supersedes or "").split(",") if s]

    if args.promote:
        events, _ = M.read_all_events()
        prop = next((e for e in events if e["id"] == args.promote), None)
        if prop is None:
            sys.exit(f"sign: no event {args.promote!r} found")
        # Two things are promotable, and for the same reason: both are claims
        # this fold deliberately refuses to serve until the operator's key says
        # otherwise. A proposal (an agent's suggested correction) and a
        # quarantined fact (lineage contains-untrusted — written while working on
        # ingested content) are the same shape of withheld authority.
        promotable = (prop["kind"] == "propose-correct"
                      or prop.get("lineage") == "contains-untrusted")
        if not promotable:
            sys.exit(f"sign: {args.promote} is kind={prop['kind']} / "
                     f"lineage={prop.get('lineage')!r} — only proposals and "
                     f"quarantined (contains-untrusted) events are promoted; "
                     f"write others explicitly with --subject/--content")
        subject = subject or prop["subject"]
        content = content or prop["content"]
        home = home or prop.get("home")
        polarity = prop.get("polarity", "n/a") if args.polarity == "n/a" else polarity
        # Promoting resolves the whole park: supersede the proposal AND every
        # live claim on that subject, so one operator act clears the conflict.
        fold = M.fold_events(events, M.load_registry())
        parked = fold["parked"].get(subject, [])
        supersedes = sorted({args.promote, *supersedes,
                             *(e["id"] for e in parked)})
        # A promotion must not SILENTLY overwrite a fact already being served.
        # Superseding the parked set clears a conflict the operator is already
        # looking at; reaching past that into live truth is a different act, and
        # it needs to be named on the command line. Left unsaid, the fold's own
        # lesson rule parks the subject — loud, and recoverable — rather than the
        # newer claim quietly winning.
        clash = [e for e in fold["live"]
                 if e["subject"] == subject and e["content"] != content
                 and e["id"] not in supersedes]
        for e in clash:
            print(f"  WARNING: {e['id']} is SERVED on {subject} with different "
                  f"content and is NOT being superseded:\n"
                  f"    served:   {e['content'][:120]}\n"
                  f"    promoting:{content[:120]}\n"
                  f"  the fold will PARK this subject. Re-run with "
                  f"--supersedes {e['id']} if you mean to replace it.",
                  file=sys.stderr)

    if not subject or not content:
        sys.exit("sign: --subject and --content required (or --promote)")

    # B3 (2026-08-06, Grok-reviewed — memory-mesh/reviews/2026-08-06-grok-b3
    # -plan-review.md): bind the signature to the store file's ACTUAL bytes,
    # not just the short --content description. Covers both --promote and a
    # direct --subject/--content call, since both land here before
    # make_event(). A lesson subject with no store file to hash would sign
    # an unbound promotion — refused rather than silently signed without a
    # body_sha256, so no lesson/* promotion event is ever created that
    # nothing can be checked against later.
    body_sha256 = None
    if subject.startswith("lesson/"):
        slug = subject.split("/", 1)[1]
        store_file = Path(M.store_dir()) / f"{slug}.md"
        if not store_file.exists():
            sys.exit(
                f"sign: refusing to promote {subject!r} — no store file at "
                f"{store_file}.\n"
                "  A lesson subject with nothing to hash would sign an "
                "unbound promotion (B3). If this is a mesh-only subject "
                "with no store-file counterpart, that's expected — but it "
                "can't be promoted through this path.")
        body_sha256 = M.content_fingerprint(store_file.read_text())

    ev, _ = M.make_event("correct", subject, content, session=args.session,
                         polarity=polarity, home=home, audience=args.audience,
                         confidence="operator-stated",
                         supersedes=supersedes or None,
                         body_sha256=body_sha256)
    M.sign_event(ev, args.signer)          # raises loudly if vault locked
    if not M.verify_sig(ev):
        sys.exit("sign: signature did not verify against allowed_signers — "
                 "refusing to emit an event that the fold would alarm on")
    line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
    append(ev, line)
    print(f"signed {ev['id']} ({subject}) by {args.signer}")
    if supersedes:
        print(f"  supersedes: {', '.join(supersedes)}")
    if args.promote:
        reconcile_store(subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
