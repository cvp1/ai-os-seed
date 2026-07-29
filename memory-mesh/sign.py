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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_lib as M


def append(ev, line):
    log = M.append_event_line(line)
    with M.repo_lock():
        M.git("add", str(log.relative_to(M.MESH_ROOT)))
        M.git("commit", "-q", "-m", f"sign {ev['kind']} {ev['subject']} {ev['id']}")


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
        if prop["kind"] != "propose-correct":
            sys.exit(f"sign: {args.promote} is kind={prop['kind']}, not propose-correct "
                     f"(only proposals are promoted; write others explicitly)")
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

    if not subject or not content:
        sys.exit("sign: --subject and --content required (or --promote)")

    ev, _ = M.make_event("correct", subject, content, session=args.session,
                         polarity=polarity, home=home, audience=args.audience,
                         confidence="operator-stated",
                         supersedes=supersedes or None)
    M.sign_event(ev, args.signer)          # raises loudly if vault locked
    if not M.verify_sig(ev):
        sys.exit("sign: signature did not verify against allowed_signers — "
                 "refusing to emit an event that the fold would alarm on")
    line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
    append(ev, line)
    print(f"signed {ev['id']} ({subject}) by {args.signer}")
    if supersedes:
        print(f"  supersedes: {', '.join(supersedes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
