#!/usr/bin/env python3
"""Operator signing — turn Craig's judgement into a cryptographic fact.

    # resolve a parked subject with signed truth (supersedes both sides)
    sign.py --subject ssh-route/{{REDACTED}} --polarity exists \
            --content "ssh st21 works" --home FLEET.md#reachability \
            --supersedes abc123,def456

    # promote an agent's proposal (the gardener path): agents may only emit
    # kind=propose-correct; ONLY this command turns one into signed truth
    python3 sign.py --promote <proposal-id>

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
    "~/.claude/skills/improve/memory_write.py"))


def reconcile_store(subject, approved_words=None):
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
    cmd = [sys.executable, str(MEMORY_WRITE), "retag", slug,
           "--lineage", "craig-direct", "--commit"]
    # A verbal promotion has no signature for retag's own gate to verify, so it
    # must carry Craig's words through to the store the same way it carries
    # them into the mesh event — one promotion, one attestation, both surfaces.
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
                    help="promote an event on Craig's VERBAL approval instead of "
                         "his key (2026-08-12). Requires --approved with his "
                         "actual words. Weaker than --promote and stamped as such: "
                         "it buys `served`, never `pinned`/`doctrine`.")
    ap.add_argument("--approved", metavar="WORDS",
                    help="Craig's verbatim approval, recorded for audit. Required "
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

    # The two promotion classes are mutually exclusive on one invocation:
    # "which one promoted this?" must always have exactly one answer.
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
                f"sign: --promote-verbal requires --approved \"<Craig's actual "
                f"words>\" (at least {M.MIN_APPROVAL_WORDS} characters; got "
                f"{len(words)}).\n"
                "  The attestation IS the audit trail — it is the only thing that "
                "lets anyone later ask him 'did you approve this?' and get a\n"
                "  checkable answer. An empty or token approval would serve an "
                "untrusted-lineage fact while recording nothing.")

    promote_id = args.promote or args.promote_verbal
    prop = None
    if promote_id:
        events, _ = M.read_all_events()
        prop = next((e for e in events if e["id"] == promote_id), None)
        if prop is None:
            sys.exit(f"sign: no event {promote_id!r} found")
        # Two things are promotable, and for the same reason: both are claims
        # this fold deliberately refuses to serve until the operator's key says
        # otherwise. A proposal (an agent's suggested correction) and a
        # quarantined fact (lineage contains-untrusted — written while working on
        # ingested content) are the same shape of withheld authority.
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
        # Promoting resolves the whole park: supersede the proposal AND every
        # live claim on that subject, so one operator act clears the conflict.
        fold = M.fold_events(events, M.load_registry())
        parked = fold["parked"].get(subject, [])
        supersedes = sorted({promote_id, *supersedes,
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
    shown_body = None
    if subject.startswith("lesson/"):
        slug = subject.split("/", 1)[1]
        # B4 (2026-09-04): a --promote'd event already carries its full
        # store-file BODY in the synced mesh log -- verified 25/25
        # byte-identical to {{REDACTED}}'s own store files the same day
        # (corral consult, Grok: memory-mesh/reviews/2026-09-04-grok-mesh-
        # promote-body.md). B3 below instead re-reads a LOCAL store file,
        # which does not exist on {{REDACTED}} -- the ONLY host that can mint a
        # signature, since the store is per-host and only the event log
        # replicates. Every mesh promotion attempted from {{REDACTED}} refused
        # here, before the PIN prompt, for every quarantined lesson written
        # on a different host. Prefer the event's own carried body when
        # promoting: same bytes Craig is about to be shown below, signature
        # still binds to real content, just not a local re-read.
        prop_body = prop.get("body") if prop else None
        if prop_body:
            body_sha256 = M.content_fingerprint(prop_body)
            shown_body = prop_body
        else:
            # B3 (2026-08-06, Grok-reviewed — memory-mesh/reviews/2026-08-06
            # -grok-b3-plan-review.md): bind the signature to the store
            # file's ACTUAL bytes, not just the short --content description.
            # Still the only path for a direct --subject/--content call (no
            # --promote, no carried body) and for a promoted event that
            # somehow lacks one — refuse rather than sign unbound.
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

    # A VERBAL promotion keeps lineage `contains-untrusted` on purpose. The
    # source of the content did not change because Craig approved it — only
    # whether he vouches for it did. Emitting `operator-direct` here would
    # launder the source, serve the fact through the ordinary trusted branch,
    # and make the two promotion classes indistinguishable in the fold, which
    # is the one thing Craig asked for ("there is key signed and verbally
    # signed"). Keeping the lineage is also what caps residency for free:
    # effective_residency() refuses `pinned` to any unsigned event, so a verbal
    # promotion buys `served` and cannot reach doctrine off-host.
    verbal = None
    if args.promote_verbal:
        verbal = {"words": args.approved.strip(),
                  "ts": M.datetime.datetime.now(M.datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")}
    ev, _ = M.make_event("correct", subject, content, session=args.session,
                         polarity=polarity, home=home, audience=args.audience,
                         confidence="operator-stated",
                         lineage="contains-untrusted" if verbal else "operator-direct",
                         supersedes=supersedes or None,
                         body_sha256=body_sha256,
                         verbal_approval=verbal)
    # WYSIWYS (signing-window-plain-summary-above-pin, Craig): the PIN/touch
    # (or, for a verbal approval, the typed --approved words) IS the
    # signature, so what it attests to must be ON SCREEN before it happens,
    # not just a short --content slogan. Grok's review (2026-09-04) named
    # this gap directly: the charter path already prints full bytes before
    # signing; mesh printed only `content` and never the body. This prints
    # into whatever terminal is running sign.py -- on the mesh-card path
    # that IS the window Craig reads before touching the key.
    print(f"\n{'=' * 72}")
    print(f"ABOUT TO {'VERBALLY APPROVE' if verbal else 'SIGN'}: {subject}")
    print("=" * 72)
    print(shown_body if shown_body else content)
    print(f"{'=' * 72}\n")
    if verbal:
        # No signature by construction — that is what makes this the weaker
        # class. body_sha256 is still bound: it records WHICH bytes he approved,
        # so a later edit is detectable even without a signature over them.
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
    # Reconcile the store on the CONDITION, not on the verb (2026-09-12).
    #
    # This was `if promote_id:` — so the store file was retagged only when a
    # subject left quarantine via --promote. That is not how it happens in
    # practice: of the 28 half-applied promotions found this day, 28 arrived by
    # a signed `correct` superseding a quarantined lesson and ZERO by --promote.
    # A rule keyed on one verb cannot see the others, and the drift it leaves is
    # invisible to retrieval (the mesh is the serving authority) right up until
    # something rebuilds from store frontmatter and reads Craig's own signed
    # facts back as untrusted.
    #
    # So ask the store what it says. Any signature is Craig personally attesting
    # this subject; if the file still calls that untrusted, the two surfaces
    # disagree and this act is what resolves them — whichever verb got us here.
    if subject.startswith("lesson/"):
        slug = subject.split("/", 1)[1]
        if promote_id or M.store_file_lineage(slug) == "contains-untrusted":
            reconcile_store(subject,
                            approved_words=verbal["words"] if verbal else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
