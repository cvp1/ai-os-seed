#!/usr/bin/env python3
"""triage: one screen, one approval, for every memory waiting on the owner.

Lists quarantined memories and store/event divergences, screens each, and
recommends a resolution. The owner approves the WHOLE list once; the agent
applies it. Nothing about the quarantine rule changes: every promotion is
still an explicit, verbatim-approved signed event (sign.py --promote-verbal).

Screening (a recommendation, never a verdict):
  PROMOTE  quarantined only because the WRITING SESSION touched web/mail
           (provenance: flagged-downgraded) and the body carries no
           external-claim markers (URLs, emails, phone numbers, forwarding
           or alerting instructions, "according to").
  REVIEW   authored-untrusted, or any external-claim marker — the owner reads
           it. Never applied by --apply.
  EVENT    divergence where the event is newer than the store file
           (a correction from another host that this store never took).
  FILE     divergence where the store file is newer (a local edit).

    triage.py                     # list + recommendations + digest
    triage.py --json
    triage.py --apply --digest <d> --approved "<owner's verbatim words>"

--apply refuses unless --digest matches the list as it stands NOW, so an
approval binds exactly the items the owner saw (one ceremony, bound by hash).
Stdlib only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_lib as M  # noqa: E402

MAX_ITEMS = 60
EXTERNAL = [
    (re.compile(r"https?://", re.I), "URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "email address"),
    (re.compile(r"(?<![\w.-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.-])"),
     "phone number"),
    (re.compile(r"(?<![\w-])(forward|send|notify|alert|cc|bcc)(?![\w-])[^.\n]{0,40}\b(to|at)\b", re.I),
     "routing instruction"),
    (re.compile(r"\baccording to\b|\bthe (article|post|email|page) (says|said)\b", re.I),
     "external attribution"),
]
DIVERGE_RX = re.compile(r"diverges from its UNSIGNED event and was LEFT ALONE: (\S+)")


def _body_text(e):
    body = e.get("body") or ""
    if body.startswith("---"):
        parts = body.split("---", 2)
        body = parts[2] if len(parts) == 3 else body
    return body


def _provenance(e):
    m = re.search(r"^provenance:\s*(\S+)", e.get("body") or "", re.M)
    return m.group(1) if m else ""


def screen_quarantined(e):
    text = (e.get("content") or "") + "\n" + _body_text(e)
    # Commands and paths are first-party; don't let a version string or a
    # `--cc` flag in a code span read as a phone number or routing order.
    scrubbed = re.sub(r"`[^`]*`", " ", text)
    # Dates, times, IPs, versions and ids are not phone numbers.
    scrubbed = re.sub(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?Z?)?"
                      r"|\b\d{1,3}(?:\.\d{1,3}){3}\b|\bv?\d+(?:\.\d+){1,3}\b", " ", scrubbed)
    urls = bool(EXTERNAL[0][0].search(scrubbed))
    # Filesystem paths are first-party (an account name inside a cloud-drive
    # folder path is not a contact address); drop them after the URL check.
    scrubbed = re.sub(r"\S*/\S*", " ", scrubbed)
    flags = sorted({why for rx, why in EXTERNAL[1:] if rx.search(scrubbed)} |
                   ({"URL"} if urls else set()))
    taint_only = _provenance(e) == "flagged-downgraded"
    if taint_only and not flags:
        rec = "PROMOTE"
        why = "session-taint only (writer had read web/mail); first-party claim, no external markers"
    else:
        rec = "REVIEW"
        why = ("authored as untrusted" if not taint_only else "session-taint") + \
              (f"; markers: {', '.join(flags)}" if flags else "")
    return {"kind": "quarantined", "id": e["id"], "slug": e["subject"].split("/", 1)[-1],
            "host": e.get("host"), "ts": e.get("ts"), "summary": (e.get("content") or "")[:200],
            "rec": rec, "why": why}


def divergences(fold, events, store):
    out = []
    if store is None:
        return out
    try:
        res = M.project_store(fold, store, apply=False, events=events)
    except Exception as ex:  # noqa: BLE001 — report, never crash the triage
        return [{"kind": "error", "id": "-", "slug": "-", "rec": "REVIEW",
                 "why": f"divergence scan failed: {ex}", "summary": ""}]
    live = {e["subject"]: e for e in fold.get("live", [])}
    for alarm in res.get("alarms", []):
        m = DIVERGE_RX.search(alarm)
        if not m:
            continue
        slug = m.group(1)
        e = live.get(f"lesson/{slug}")
        f = store / f"{slug}.md"
        if e is None or not f.exists():
            continue
        import calendar
        import time
        ets = calendar.timegm(time.strptime(e["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        newer_event = ets > f.stat().st_mtime
        out.append({"kind": "divergence", "id": e["id"], "slug": slug, "host": e.get("host"),
                    "ts": e.get("ts"), "summary": (e.get("content") or "")[:200],
                    "rec": "EVENT" if newer_event else "FILE",
                    "why": ("event (%s, %s) is newer than the store file — a correction this "
                            "store never took" if newer_event else
                            "store file is newer than event (%s, %s) — a local edit")
                           % (e.get("host"), e["ts"][:10])})
    return out


def collect():
    events, _ = M.read_all_events()
    fold = M.fold_events(events, M.load_registry())
    items = [screen_quarantined(e) for e in fold.get("quarantined", [])]
    items += divergences(fold, events, M.harness_store())
    items.sort(key=lambda i: (i["kind"], i["rec"], i["slug"]))
    return items[:MAX_ITEMS], len(items)


def digest(items):
    key = "\n".join(f"{i['kind']}|{i['id']}|{i['rec']}" for i in items)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def render(items, total):
    if not items:
        return "triage: nothing is waiting on the owner."
    lines = [f"triage: {total} item(s) waiting" + (f" (showing {len(items)})" if total > len(items) else "")]
    for n, i in enumerate(items, 1):
        lines.append(f"{n:2}. [{i['rec']}] {i['slug']} ({i['kind']}, {i.get('host')} {str(i.get('ts'))[:10]})")
        lines.append(f"      {i['summary']}")
        lines.append(f"      why: {i['why']}")
    auto = sum(1 for i in items if i["rec"] in ("PROMOTE", "EVENT", "FILE"))
    lines.append(f"digest {digest(items)} — approving applies the {auto} PROMOTE/EVENT/FILE "
                 f"item(s); REVIEW items are never auto-applied.")
    return "\n".join(lines)


def apply(items, words):
    done, failed = [], []
    for i in items:
        if i["rec"] in ("PROMOTE", "EVENT"):
            cmd = [sys.executable, str(HERE / "sign.py"), "--promote-verbal", i["id"],
                   "--approved", words]
        elif i["rec"] == "FILE":
            cmd = [sys.executable, str(HERE / "memory_write.py"), "adopt", i["slug"],
                   "--reconcile", "--commit"]
        else:
            continue
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        (done if r.returncode == 0 else failed).append(
            f"{i['slug']}" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout)[-200:]}"))
    return done, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--digest")
    ap.add_argument("--approved", help="the owner's verbatim approval, recorded on every promotion")
    args = ap.parse_args(argv)
    items, total = collect()
    if args.apply:
        if not args.approved or not args.approved.strip():
            print("triage: --apply needs --approved \"<owner's verbatim words>\"", file=sys.stderr)
            return 2
        if args.digest != digest(items):
            print(f"triage: REFUSED — the list changed since it was shown (digest now "
                  f"{digest(items)}, approved {args.digest}). Show it again and re-approve.",
                  file=sys.stderr)
            return 3
        done, failed = apply(items, args.approved.strip())
        print(f"triage: applied {len(done)}" + (f": {', '.join(done)}" if done else ""))
        if failed:
            print(f"triage: FAILED {len(failed)}: " + "; ".join(failed), file=sys.stderr)
            return 1
        return 0
    if args.json:
        print(json.dumps({"total": total, "digest": digest(items), "items": items}, indent=1))
    else:
        print(render(items, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
