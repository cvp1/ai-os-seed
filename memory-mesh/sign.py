#!/usr/bin/env python3
"""Operator signing: emit signed events the fold treats as authoritative.

    sign.py --subject <subject> --polarity exists --content "..." --supersedes id1,id2
    sign.py --promote <event-id>

Only this CLI holds the signing key, so agents cannot mint authority.
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


MEMORY_WRITE = Path(__file__).resolve().parent / "memory_write.py"


def reconcile_store(subject, approved_words=None):
    """Retag a promoted lesson's store file so the store agrees with the mesh.

    Failure is reported loudly but does not undo the signed event.
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
    cmd = [sys.executable, str(MEMORY_WRITE), "retag", slug,
           "--lineage", "craig-direct", "--commit"]
    # A verbal promotion carries the approval words through to the store.
    if approved_words:
        cmd += ["--operator-approved", approved_words]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        klass = M.PROMOTION_VERBAL if approved_words else M.PROMOTION_KEY
        print(f"  store reconciled: {slug} -> lineage: craig-direct ({klass})")
    else:
        print(f"  STORE NOT RECONCILED for {slug} (the signed event stands and "
              f"is authoritative, but the store copy still says "
              f"contains-untrusted):\n{r.stdout}{r.stderr}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", help="proposal event id to promote to signed truth")
    ap.add_argument("--promote-verbal", metavar="EVENT_ID",
                    help="promote an event on the owner's VERBAL approval instead "
                         "of the key. Requires --approved with the owner's "
                         "actual words. Weaker than --promote and stamped as such: "
                         "it buys `served`, never `pinned`/`doctrine`.")
    ap.add_argument("--approved", metavar="WORDS",
                    help="The owner's verbatim approval, recorded for audit. Required "
                         "by --promote-verbal.")
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

    # A promotion has exactly one class.
    if args.promote and args.promote_verbal:
        sys.exit("sign: --promote and --promote-verbal are mutually exclusive — "
                 "a promotion has one class, key-signed or verbally-signed.")
    if args.approved and not args.promote_verbal:
        sys.exit("sign: --approved is only meaningful with --promote-verbal. A "
                 "key-signed promotion is attested by the signature itself.")
    if args.promote_verbal:
        words = (args.approved or "").strip()
        if len(words) < M.MIN_APPROVAL_WORDS:
            sys.exit(
                f"sign: --promote-verbal requires --approved \"<the owner's actual "
                f"words>\" (at least {M.MIN_APPROVAL_WORDS} characters; got "
                f"{len(words)}).\n"
                "  The attestation IS the audit trail — it is the only thing that "
                "lets anyone later ask 'did you approve this?' and get a\n"
                "  checkable answer. An empty or token approval would serve an "
                "untrusted-lineage fact while recording nothing.")

    promote_id = args.promote or args.promote_verbal
    prop = None
    if promote_id:
        events, _ = M.read_all_events()
        prop = next((e for e in events if e["id"] == promote_id), None)
        if prop is None:
            sys.exit(f"sign: no event {promote_id!r} found")
        # Proposals and quarantined facts are both withheld until signed.
        promotable = (prop["kind"] == "propose-correct"
                      or prop.get("lineage") == "contains-untrusted")
        if not promotable:
            sys.exit(f"sign: {promote_id} is kind={prop['kind']} / "
                     f"lineage={prop.get('lineage')!r} — only proposals and "
                     f"quarantined (contains-untrusted) events are promoted; "
                     f"write others explicitly with --subject/--content")
        subject = subject or prop["subject"]
        content = content or prop["content"]
        home = home or prop.get("home")
        polarity = prop.get("polarity", "n/a") if args.polarity == "n/a" else polarity
        # Supersede the proposal and every parked claim on the subject.
        fold = M.fold_events(events, M.load_registry())
        parked = fold["parked"].get(subject, [])
        supersedes = sorted({promote_id, *supersedes,
                             *(e["id"] for e in parked)})
        # Never silently overwrite a served fact; the fold parks the subject instead.
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

    # Bind lesson signatures to the full body bytes, not just --content.
    body_sha256 = None
    shown_body = None
    if subject.startswith("lesson/"):
        slug = subject.split("/", 1)[1]
        # Prefer the body carried on the promoted event; the store is per-host.
        prop_body = prop.get("body") if prop else None
        if prop_body:
            body_sha256 = M.content_fingerprint(prop_body)
            shown_body = prop_body
        else:
            # Fall back to the local store file; refuse if there is nothing to hash.
            store_file = Path(M.store_dir()) / f"{slug}.md"
            if not store_file.exists():
                sys.exit(
                    f"sign: refusing to promote {subject!r} — no store file at "
                    f"{store_file} and the event carries no body to hash.\n"
                    "  A lesson subject with nothing to hash would sign an "
                    "unbound promotion (B3/B4). If this is a mesh-only subject "
                    "with no store-file counterpart, that's expected — but it "
                    "can't be promoted through this path.")
            body_sha256 = M.content_fingerprint(store_file.read_text())
            shown_body = store_file.read_text()

    # A verbal promotion keeps lineage `contains-untrusted`, which keeps it
    # distinguishable from a key-signed one and caps its residency at `served`.
    verbal = None
    if args.promote_verbal:
        verbal = {"words": args.approved.strip(),
                  "ts": M.datetime.datetime.now(M.datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")}
    # The signed event carries its body; oversized bodies fall back to hash-only.
    def _mk(body):
        return M.make_event("correct", subject, content, session=args.session,
                            polarity=polarity, home=home, audience=args.audience,
                            confidence="operator-stated",
                            lineage="contains-untrusted" if verbal else "operator-direct",
                            supersedes=supersedes or None,
                            body_sha256=body_sha256, body=body,
                            verbal_approval=verbal)
    carry = shown_body if (shown_body and args.audience in M.BODY_AUDIENCES) else None
    try:
        ev, _ = _mk(carry)
    except ValueError as e:
        if carry is None or "exceeds" not in str(e):
            raise
        print(f"note: body too large to carry ({len(carry.encode())}B) — "
              f"signing the hash only; peers recover the bytes by chain walk",
              file=sys.stderr)
        ev, _ = _mk(None)
    # Show exactly what is being attested before the signature is made.
    print(f"\n{'=' * 72}")
    print(f"ABOUT TO {'VERBALLY APPROVE' if verbal else 'SIGN'}: {subject}")
    print("=" * 72)
    print(shown_body if shown_body else content)
    print(f"{'=' * 72}\n")
    if verbal:
        # No signature; body_sha256 still records which bytes were approved.
        line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
        append(ev, line)
        print(f"VERBALLY signed {ev['id']} ({subject})")
        print(f"  approved: {verbal['words']!r}")
        print(f"  class: {M.PROMOTION_VERBAL} — served, but NOT pinned/doctrine.")
        print(f"  this is an AUDIT record, not a cryptographic gate. To make it "
              f"one, run: sign.py --promote {ev['id']}")
    else:
        M.sign_event(ev, args.signer)      # raises loudly if vault locked
        if not M.verify_sig(ev):
            sys.exit("sign: signature did not verify against allowed_signers — "
                     "refusing to emit an event that the fold would alarm on")
        line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
        append(ev, line)
        print(f"signed {ev['id']} ({subject}) by {args.signer}")
    if supersedes:
        print(f"  supersedes: {', '.join(supersedes)}")
    # Reconcile whenever the store still marks a signed subject untrusted.
    if subject.startswith("lesson/"):
        slug = subject.split("/", 1)[1]
        if promote_id or M.store_file_lineage(slug) == "contains-untrusted":
            reconcile_store(subject,
                            approved_words=verbal["words"] if verbal else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
