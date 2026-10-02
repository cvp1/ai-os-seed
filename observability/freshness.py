#!/usr/bin/env python3
"""Freshness / liveness check over the observability store.

Reads expected cadence from freshness.json and, for each scheduled job, reports:
  OK       newest run is recent and succeeded
  STALE    newest run is older than max_age (job has gone silent)
  MISSING  job is configured but has never logged a run
  FAILING  newest run is recent but exited non-zero
  SOFTFAIL every recent run "succeeded" but most wrote to stderr — the job is
           swallowing its own errors and exiting 0 (see soft_failure below)

Prints only problems, led by a `FINDINGS:` line, and exits 0; non-zero means
the checker itself crashed. --all also prints healthy jobs; --json emits JSON.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import repo_hygiene
import switches

_HERE = Path(__file__).resolve().parent
_CONFIG = _HERE / "freshness.json"
# Drift checker: first existing of cron/sync.sh, scheduler/sync.sh.
_SYNC_CANDIDATES = [_HERE.parent / "cron" / "sync.sh", _HERE.parent / "scheduler" / "sync.sh"]
_DUR = re.compile(r"^\s*(\d+)\s*([dhm])\s*$")

# Written by --write-findings; the size caps bound the whole composed file.
_FINDINGS = _HERE / "data" / "FINDINGS.md"
_FINDINGS_MAX_LINES = 60
_FINDINGS_MAX_BYTES = 16000

# Ack ledger: a finding matching an ack (substring of the rendered line) is
# hidden until the ack's `until` date, then returns marked EXPIRED-ACK.
_ACKS = _HERE / "control" / "findings_ack.json"
_ACK_MAX_DAYS = 90

# Fleet-only scanners and their instruments. On a seed install (receipt present)
# a missing instrument is skipped; elsewhere its absence is a finding.
_SEED_INSTALL = (_HERE.parent / ".cc-seed" / "receipt.json").exists()
_FLEET_INSTRUMENTS = {
    "prices": "observability/gen_prices.py",
    "models": "_lib/model_catalog.py",
    "keys": "keyvault/keys.py",
    "leaks": "keyvault/leak_scan.py",
    "inventory": "keyvault/inventory_check.py",
    "ontology": "ontology/ontology.py",
}


def fleet_scanner_applies(name, root=None, seed=None):
    """True when the named fleet-only scanner should run on this tree."""
    root = Path(root) if root else _HERE.parent
    seed = _SEED_INSTALL if seed is None else seed
    return not seed or (root / _FLEET_INSTRUMENTS[name]).exists()


def load_acks(now=None):
    """Return (active, expired) ack lists; raises on a malformed ledger."""
    if not _ACKS.exists():
        return [], []
    data = json.loads(_ACKS.read_text(encoding="utf-8"))
    today = (now or datetime.now(timezone.utc)).date()
    active, expired = [], []
    for a in data.get("acks", []):
        until = datetime.strptime(a["until"], "%Y-%m-%d").date()
        (active if until >= today else expired).append(a)
    return active, expired


def apply_acks(findings, now=None):
    """Partition rendered finding lines into (live, acked).

    live  — unmatched lines, plus expired-ack matches (prefixed EXPIRED-ACK).
    acked — (line, ack) pairs hidden until `until`.
    """
    try:
        active, expired = load_acks(now)
    except Exception as e:  # noqa: BLE001 — a bad ledger must surface, not hide
        return [f"[LEDGER] findings_ack.json unreadable: {e}"] + list(findings), []
    live, acked = [], []
    for ln in findings:
        hit = next((a for a in active if a["match"] in ln), None)
        if hit:
            acked.append((ln, hit))
            continue
        old = next((a for a in expired if a["match"] in ln), None)
        if old:
            ln = f"[EXPIRED-ACK {old['until']} {old.get('owner', '?')}] {ln}"
        live.append(ln)
    return live, acked


def edit_acks(add=None, remove=None):
    """--ack / --unack: bounded, reversible edits to the ledger."""
    data = json.loads(_ACKS.read_text(encoding="utf-8")) if _ACKS.exists() else {"acks": []}
    acks = [a for a in data.get("acks", []) if not (remove and a["match"] == remove)]
    if add:
        until = datetime.strptime(add["until"], "%Y-%m-%d").date()
        horizon = datetime.now(timezone.utc).date() + timedelta(days=_ACK_MAX_DAYS)
        if until > horizon:
            raise SystemExit(f"--until {add['until']} is more than {_ACK_MAX_DAYS} days out; "
                             "a longer park is a switch or a fix, not an ack")
        acks = [a for a in acks if a["match"] != add["match"]] + [add]
    _ACKS.parent.mkdir(parents=True, exist_ok=True)
    _ACKS.write_text(json.dumps({"_comment": "Findings disposition ledger — see freshness.py "
                                 "load_acks(). match=substring of the FINDINGS.md line; "
                                 "until=YYYY-MM-DD (<=90d); owner=craig|agent; why=required.",
                                 "acks": sorted(acks, key=lambda a: (a["until"], a["match"]))},
                                indent=1) + "\n", encoding="utf-8")
    return acks


def write_findings(lines, now, acked=(), switched=()):
    """Write data/FINDINGS.md when there are findings; delete it when clean.

    Returns the path if written, else None. Never raises.
    """
    try:
        if not lines:
            _FINDINGS.unlink(missing_ok=True)
            return None
        _FINDINGS.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        # generated-at first, so a stale file shows the checker stopped.
        head = [f"generated-at: {stamp}", "",
                f"# Findings — {len(lines)} live item(s)"
                + (f", {len(acked)} acknowledged (listed at the end)" if acked else ""), ""]
        body, dropped = list(lines), 0
        if len(body) > _FINDINGS_MAX_LINES:
            dropped = len(body) - _FINDINGS_MAX_LINES
            body = body[:_FINDINGS_MAX_LINES]
        out = "\n".join(head + [f"- {ln}" for ln in body])
        if dropped:
            out += f"\n- …and {dropped} more finding(s) truncated"
        if acked:
            # Acknowledged items, one compact row per ack.
            out += "\n\n## Acknowledged — hidden from the live list until their date\n"
            seen = {}
            for _ln, a in acked:
                seen[a["match"]] = (a, seen.get(a["match"], (a, 0))[1] + 1)
            for a, n in seen.values():
                out += (f"\n- until {a['until']} ({a.get('owner', '?')}): {a['match']}"
                        f"{f' ×{n}' if n > 1 else ''} — {a.get('why', '')}")
        if switched:
            # Paused jobs, so a quiet job is not mistaken for a healthy one.
            out += "\n\n## Switched off — these jobs are paused, not healthy\n"
            for ln in switched:
                out += f"\n- {ln}"
        out += "\n"
        # Enforce the byte cap on the composed file.
        if len(out.encode("utf-8")) > _FINDINGS_MAX_BYTES:
            keep, acc = [], len("\n".join(head).encode("utf-8"))
            for ln in body:
                enc = len(f"- {ln}\n".encode("utf-8"))
                if acc + enc > _FINDINGS_MAX_BYTES - 200:
                    break
                keep.append(ln)
                acc += enc
            dropped = len(lines) - len(keep)
            out = "\n".join(head + [f"- {ln}" for ln in keep])
            out += f"\n- …and {dropped} more finding(s) truncated\n"
        tmp = _FINDINGS.with_suffix(".tmp")
        tmp.write_text(out, encoding="utf-8")
        tmp.replace(_FINDINGS)
        return _FINDINGS
    except Exception as e:  # noqa: BLE001 — never fail the run over the sidecar
        print(f"[WARN   ] could not write findings file: {e}", file=sys.stderr)
        return None


def repo_hygiene_problems():
    """Repo hygiene findings (7-day grace for dirty/unpushed). Never raises."""
    try:
        return [f"{p['kind']}: {p['repo']}: {p['detail']}" if p['repo'] != '-'
                else f"{p['kind']}: {p['detail']}"
                for p in repo_hygiene.problems(days=7)]
    except Exception as e:  # noqa: BLE001
        return [f"repo_hygiene failed to run: {e}"]


def model_drift_problems():
    """Model catalog drift findings; an unreachable provider is a finding.
    Never raises.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _lib import model_catalog
        # NO-INSTRUMENT gaps are permanent, so they are excluded here.
        return ["%s: %s" % (cls, text.strip())
                for cls, text in model_catalog.findings()
                if cls != "NO-INSTRUMENT"]
    except Exception as e:  # noqa: BLE001
        return ["frontier drift check failed to run: %s" % e]


def shim_drift():
    """Run `sync.sh --check`; return DRIFT lines (empty = in sync). A missing or
    failing checker is reported as a line."""
    sync = next((p for p in _SYNC_CANDIDATES if p.exists()), None)
    if sync is None:
        candidates = " or ".join(str(p) for p in _SYNC_CANDIDATES)
        return [f"sync.sh missing at {candidates}"]
    try:
        r = subprocess.run(["bash", str(sync), "--check"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — never let the backstop crash the job
        return [f"sync.sh --check failed to run: {e}"]
    if r.returncode == 0:
        return []
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("DRIFT:")]
    return lines or [f"sync.sh --check exit {r.returncode}: {(r.stderr or r.stdout).strip()[:120]}"]


def prices_projection_drift():
    """Run `gen_prices.py --check`: prices.json must match its generated
    projection. Never raises."""
    gen = _HERE / "gen_prices.py"
    if not gen.exists():
        return [f"gen_prices.py missing at {gen}"]
    try:
        r = subprocess.run([sys.executable, str(gen), "--check"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001 — never let the backstop crash the job
        return [f"gen_prices.py --check failed to run: {e}"]
    if r.returncode == 0:
        return []
    lines = [ln.strip() for ln in r.stderr.splitlines() if ln.startswith("DRIFT:")]
    return lines or [f"gen_prices.py --check exit {r.returncode}: "
                     f"{(r.stderr or r.stdout).strip()[:120]}"]


def key_registry_problems():
    """Run `keyvault/keys.py --check`. Coverage gaps fold into one count line;
    other findings are itemised. A locked vault is skipped. Never raises."""
    keys = _HERE.parent / "keyvault" / "keys.py"
    if not keys.exists():
        return [f"keys.py missing at {keys}"]
    try:
        r = subprocess.run([sys.executable, str(keys), "--check"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 -- never let the backstop crash the job
        return [f"keys.py --check failed to run: {e}"]
    if r.returncode != 0:
        if "locked" in (r.stderr + r.stdout):
            return []
        return [f"keys.py --check exit {r.returncode}: {(r.stderr or r.stdout).strip()[:120]}"]
    items = [ln.strip()[2:] for ln in r.stdout.splitlines() if ln.strip().startswith("- ")]
    uncovered = [i for i in items if i.startswith("no ROTATION.md row: ")]
    other = [i for i in items if not i.startswith("no ROTATION.md row: ")]
    out = []
    if uncovered:
        out.append(f"{len(uncovered)} vault file(s) without a ROTATION.md row "
                   f"(keyvault/keys.py --check lists them; add a row per keyvault/ROTATION.md new-key checklist)")
    return out + other


_LEAK_STATE = _HERE / "data" / "leak_scan.json"
_LEAK_MAX_AGE = timedelta(hours=30)   # daily job: one missed run


def key_leak_problems(path=None, now=None):
    """Read the leak-scan state file: one line per file holding a vault value,
    plus a coverage line when the scan was partial. Paths and entry names only,
    never values. A missing or stale state file is itself a finding."""
    path = Path(path) if path else _LEAK_STATE
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return [f"leak scan: no state at {path} — cron/leak_scan.sh has never written it"]
    try:
        j = json.loads(path.read_text())
        gen = _parse_iso(str(j["generated_at"]))
    except Exception as e:  # noqa: BLE001
        return [f"leak scan: state unreadable ({e})"]
    age = now - gen
    if age > _LEAK_MAX_AGE:
        return [f"leak scan: state is {_fmt_age(age)} old (max {_fmt_age(_LEAK_MAX_AGE)}) — the job stopped"]
    home = str(Path.home())
    out = [f"{f['path'].replace(home, '~')} holds {', '.join(f['vault_entries'])}" for f in j.get("findings", [])]
    cov = j.get("coverage", {})
    if cov.get("bounded") or cov.get("uncovered"):
        out.append(f"scan coverage: {cov.get('scanned')} files scanned, {cov.get('uncovered')} uncovered"
                   + (f", BOUND HIT: {cov['bounded']}" if cov.get("bounded") else "")
                   + " (keyvault/leak_scan.py --list-uncovered)")
    return out


_INVENTORY_STATE = _HERE / "data" / "inventory_check.json"
_INVENTORY_MAX_AGE = timedelta(days=8)   # weekly job: one missed run


def key_inventory_problems(path=None, now=None):
    """Read the key-inventory state file: hosts holding unlisted or differing
    vault keys, or that could not be listed. Never values or hashes."""
    path = Path(path) if path else _INVENTORY_STATE
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return [f"inventory check: no state at {path} — cron/inventory_check.sh has never written it"]
    try:
        j = json.loads(path.read_text())
        gen = _parse_iso(str(j["generated_at"]))
    except Exception as e:  # noqa: BLE001
        return [f"inventory check: state unreadable ({e})"]
    age = now - gen
    if age > _INVENTORY_MAX_AGE:
        return [f"inventory check: state is {_fmt_age(age)} old (max {_fmt_age(_INVENTORY_MAX_AGE)}) — the job stopped"]
    return [str(f) for f in j.get("findings", [])]


_ONTOLOGY_STATE = _HERE.parent / "ontology" / "data" / "last.json"
# Daily job: 30h means one missed run.
_ONTOLOGY_MAX_AGE = timedelta(hours=30)


def ontology_problems(state_path=None, now=None):
    """One line when the ontology check's finding set gained a finding; a
    missing or stale state file is also a line. Never raises."""
    path = Path(state_path) if state_path else _ONTOLOGY_STATE
    now = now or datetime.now(timezone.utc)
    try:
        if not path.exists():
            return [f"ontology: no check state at {path} — the daily "
                    f"ontology_check job has never written one (has it stopped?)"]
        d = json.loads(path.read_text(encoding="utf-8"))
        gen = _parse_iso(str(d["generated_at"]))
        age = now - gen
        if age > _ONTOLOGY_MAX_AGE:
            return [f"ontology: check state is {_fmt_age(age)} old (max "
                    f"{int(_ONTOLOGY_MAX_AGE.total_seconds() // 3600)}h) — the daily "
                    f"ontology_check job has stopped writing it"]
        if int(d.get("n_new") or 0) <= 0:
            return []
        changed = _fmt_age(now - _parse_iso(str(d.get("changed_at") or d["generated_at"])))
        return [f"ontology: {int(d.get('n', 0))} findings "
                f"({int(d['n_new'])} new), set changed {changed} ago"]
    except Exception as e:  # noqa: BLE001 -- never let the sidecar crash the job
        return [f"ontology: check state at {path} is unreadable: "
                f"{type(e).__name__}: {e}"]


def job_findings_problems(conn, now, cfg=None):
    """One line per job with `"surface_findings": true` whose latest successful
    run's summary starts with `FINDINGS:`. Never raises."""
    cfg = cfg if cfg is not None else json.loads(_CONFIG.read_text())
    out = []
    try:
        for job, spec in cfg["jobs"].items():
            if not spec.get("surface_findings"):
                continue
            rows = conn.execute(
                "SELECT started_at, ok, summary FROM runs WHERE job=? "
                "ORDER BY started_at DESC LIMIT 60", (job,)).fetchall()
            if not rows or not rows[0]["ok"] \
                    or not (rows[0]["summary"] or "").startswith("FINDINGS:"):
                continue
            streak = 0
            for r in rows:
                if not (r["ok"] and (r["summary"] or "").startswith("FINDINGS:")):
                    break
                streak += 1
            since = _fmt_age(now - _parse_iso(rows[streak - 1]["started_at"]))
            summ = rows[0]["summary"][len("FINDINGS:"):].strip()
            out.append(f"{spec.get('label', job)}: {summ[:160]} "
                       f"(every run for {since}, {streak}{'+' if streak == 60 else ''} run(s))")
    except Exception as e:  # noqa: BLE001 -- never let the backstop crash the job
        return [f"job findings check failed to run: {type(e).__name__}: {e}"]
    return out


def parse_age(s: str) -> timedelta:
    m = _DUR.match(s)
    if not m:
        raise SystemExit(f"freshness.json: bad duration {s!r} (expected e.g. 26h, 20m, 8d)")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{secs}s"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h"
    return f"{hrs // 24}d"


# --- soft-failure detection --------------------------------------------------
# A job that exits 0 but writes stderr on most recent runs is soft-failing.
# Jobs that log to stderr normally set "stderr_ok": true in freshness.json.
SOFT_WINDOW = 12       # most recent runs considered
SOFT_MIN_RUNS = 4      # don't judge a job with less history than this
SOFT_RATIO = 0.75      # this share of them writing stderr = persistent


# Connector audit lines on stderr are telemetry: a run whose entire stderr is
# status=ok audit lines (tail covers every byte) is not noisy.
_AUDIT_OK = re.compile(r"^connector: tool=\S+ caller=\S+ status=ok cred=\S+ ms=\d+$")


def _noisy(row):
    n = row["stderr_bytes"] or 0
    if n <= 0:
        return False
    tail = row["error_tail"] or ""
    lines = [l for l in tail.splitlines() if l.strip()]
    if lines and len(tail.encode()) + 2 >= n and all(_AUDIT_OK.match(l.strip()) for l in lines):
        return False
    return True


def soft_failure(conn, job):
    """Detail string if `job` is soft-failing now, else None. Never raises.

    Requires both: the latest run wrote stderr, and at least SOFT_RATIO of the
    recent window did. The note quotes the latest run.
    """
    try:
        rows = conn.execute(
            "SELECT ok, stderr_bytes, error_tail FROM runs WHERE job=? "
            "ORDER BY started_at DESC LIMIT ?", (job, SOFT_WINDOW)).fetchall()
    except Exception:  # noqa: BLE001 — a backstop must not crash the job
        return None
    if len(rows) < SOFT_MIN_RUNS:
        return None
    if any(not r["ok"] for r in rows):
        return None        # a real failure in the window — FAILING already covers it
    if not _noisy(rows[0]):
        return None        # condition 1: latest run is clean -> not failing now
    noisy = [r for r in rows if _noisy(r)]
    if len(noisy) / len(rows) < SOFT_RATIO:
        return None
    tail = (rows[0]["error_tail"] or "").strip().splitlines()
    note = tail[-1][:120] if tail else f"{rows[0]['stderr_bytes']} bytes, text not captured"
    return (f"exit 0 but wrote stderr on its last run and {len(noisy)}/{len(rows)} "
            f"recent ones: {note}")


_FLAP_MAX_AGE = timedelta(hours=6)


def _flapping(conn, job, max_age):
    """True when a frequent job (max_age <= 6h) failed its latest run but passed
    the one before; two failures in a row still page."""
    if max_age > _FLAP_MAX_AGE:
        return False
    prev = conn.execute(
        "SELECT ok FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1 OFFSET 1",
        (job,)).fetchone()
    return bool(prev and prev["ok"])


def switch_state(now):
    """``(active, expired, err)`` from the strict switch reader. Expired switches
    are watched again; nothing here changes a switch."""
    today = now.date() if hasattr(now, "date") else now
    d, err = switches.load_strict()
    if err:
        return {}, {}, err
    return switches.active(today), switches.expired(today), None


def switch_problems(now):
    """One line when the switch file is unreadable; every job is then evaluated."""
    err = switch_state(now)[2]
    if not err:
        return []
    return [f"switches.json unreadable: {err} — no job was treated as switched off"]


def switched_off_lines(now):
    """Paused-job lines: job, until, owner, why."""
    active, expired_map, err = switch_state(now)
    if err:
        return []
    out = []
    for job, r in sorted({**active, **expired_map}.items()):
        r = r or {}
        why = r.get("why") or "(no reason recorded)"
        until = r.get("until") or "(no expiry recorded)"
        gone = " EXPIRED —" if job in expired_map else ""
        out.append(f"{job} until {until}{gone} ({r.get('owner') or '?'}): {why}")
    return out


def evaluate(conn, now):
    cfg = json.loads(_CONFIG.read_text())
    results = []
    active, expired_map, _err = switch_state(now)
    for job, spec in cfg["jobs"].items():
        if job in active:
            continue  # switched off
        max_age = parse_age(spec["max_age"])
        label = spec.get("label", job)
        row = conn.execute(
            "SELECT started_at, ok, exit_code, summary, error_tail "
            "FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1", (job,)
        ).fetchone()
        if row is None:
            results.append({"job": job, "label": label, "status": "MISSING",
                            "detail": "never run", "age": None})
            continue
        age = now - _parse_iso(row["started_at"])
        if age > max_age:
            results.append({"job": job, "label": label, "status": "STALE",
                            "detail": f"last run {_fmt_age(age)} ago "
                                      f"(max {spec['max_age']})", "age": _fmt_age(age)})
        elif not row["ok"] and _flapping(conn, job, max_age):
            results.append({"job": job, "label": label, "status": "OK",
                            "detail": f"last run failed {_fmt_age(age)} ago but the one "
                                      "before passed (single miss on a frequent job)",
                            "age": _fmt_age(age)})
        elif not row["ok"]:
            tail = (row["error_tail"] or "").splitlines()
            note = tail[-1] if tail else f"exit {row['exit_code']}"
            # Age goes in the detail: FINDINGS.md renders detail only.
            results.append({"job": job, "label": label, "status": "FAILING",
                            "detail": f"last run failed {_fmt_age(age)} ago: {note[:120]}",
                            "age": _fmt_age(age)})
        else:
            soft = None if spec.get("stderr_ok") else soft_failure(conn, job)
            results.append({"job": job, "label": label,
                            "status": "SOFTFAIL" if soft else "OK",
                            "detail": soft or f"last run {_fmt_age(age)} ago",
                            "age": _fmt_age(age)})
    # Annotate jobs whose switch expired with the original pause reason.
    for r in results:
        exp = expired_map.get(r["job"])
        if exp is not None or r["job"] in expired_map:
            exp = exp or {}
            r["detail"] += (f" — switch expired {exp.get('until', '?')}: "
                            f"{exp.get('why') or '(no reason recorded)'}")
    return results


def fixture_only():
    """True when FRESHNESS_FIXTURE_ONLY=1 (tests only): skip live-estate scanners."""
    return os.environ.get("FRESHNESS_FIXTURE_ONLY") == "1"


def main():
    ap = argparse.ArgumentParser(description="Freshness/liveness check over the run log.")
    ap.add_argument("--all", action="store_true", help="also print healthy/never-run jobs")
    ap.add_argument("--strict", action="store_true",
                    help="treat MISSING (never run) as a paging problem too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--write-findings", action="store_true",
                    help="also write/remove data/FINDINGS.md so an agent finds "
                         "it at session start (SEED-074; off by default)")
    ap.add_argument("--ack", metavar="MATCH",
                    help="park a finding: substring of its FINDINGS.md line; "
                         "needs --until and --why")
    ap.add_argument("--until", metavar="YYYY-MM-DD", help="ack expiry (<=90 days out)")
    ap.add_argument("--why", help="one line: what was decided and who owns it")
    ap.add_argument("--owner", default="agent", choices=("craig", "agent"))
    ap.add_argument("--unack", metavar="MATCH", help="remove an ack by its exact match")
    ap.add_argument("--acks", action="store_true", help="list the disposition ledger")
    args = ap.parse_args()

    if args.ack or args.unack or args.acks:
        if args.ack and not (args.until and args.why):
            ap.error("--ack needs --until and --why")
        add = ({"match": args.ack, "until": args.until, "why": args.why,
                "owner": args.owner, "added": datetime.now(timezone.utc).date().isoformat()}
               if args.ack else None)
        acks = edit_acks(add=add, remove=args.unack) if (add or args.unack) else load_acks()[0]
        for a in sorted(acks, key=lambda a: a["until"]):
            print(f"until {a['until']} ({a.get('owner', '?')}): {a['match']} — {a.get('why', '')}")
        return 0

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        results = evaluate(conn, now)
        found = job_findings_problems(conn, now)
    if fixture_only():
        # These scanners read the live estate, so fixture runs silence them.
        # A new scanner must go inside the else-branch.
        drift = repo = prices = models = keys = onto = sw = leaks = inv = []
        skipped = []
    else:
        # See fleet_scanner_applies.
        skipped = [n for n in _FLEET_INSTRUMENTS if not fleet_scanner_applies(n)]
        def fleet(name, scan):
            return [] if name in skipped else scan()
        drift = shim_drift()
        repo = repo_hygiene_problems()
        prices = fleet("prices", prices_projection_drift)
        models = fleet("models", model_drift_problems)
        keys = fleet("keys", key_registry_problems)
        onto = fleet("ontology", ontology_problems)
        sw = switch_problems(now)
        leaks = fleet("leaks", key_leak_problems)
        inv = fleet("inventory", key_inventory_problems)

    # MISSING (never run) pages only with --strict.
    paging = ({"STALE", "FAILING", "SOFTFAIL", "MISSING"} if args.strict
              else {"STALE", "FAILING", "SOFTFAIL"})
    problems = [r for r in results if r["status"] in paging]

    # Compose findings before any early return so --write-findings also runs
    # on the clean path (to delete the file) and under --json.
    triage = ""  # "— N live, M parked" when acks were applied; "" otherwise
    if args.write_findings:
        findings = [f"[{r['status']}] {r['label']}: {r['detail']}" for r in
                    sorted(problems, key=lambda x: x["job"])]
        findings += [f"[DRIFT] cron shim reconcile: {d}" for d in drift]
        findings += [f"[REPO] git hygiene: {rp}" for rp in repo]
        findings += [f"[DRIFT] price table projection: {p}" for p in prices]
        findings += [f"[MODEL] frontier drift: {m}" for m in models]
        findings += [f"[KEYS] key registry: {k}" for k in keys]
        findings += [f"[LEAK] secret outside the vault: {l}" for l in leaks]
        findings += [f"[INVENTORY] key on a fleet host: {i}" for i in inv]
        findings += [f"[ONTO] {o}" for o in onto]
        findings += [f"[FOUND] {f}" for f in found]
        findings += [f"[SWITCH] {w}" for w in sw]
        live, acked = (findings, []) if fixture_only() else apply_acks(findings, now)
        write_findings(live, now, acked, [] if fixture_only() else switched_off_lines(now))
        # Counts below include parked items; report the live/parked split.
        triage = f" — {len(live)} live, {len(acked)} parked"

    if args.json:
        print(json.dumps({"checked_at": now.isoformat(timespec="seconds"),
                          "problems": len(problems), "results": results,
                          "shim_drift": drift, "repo_hygiene": repo,
                          "prices_drift": prices, "model_drift": models,
                          "key_registry": keys, "ontology": onto,
                          "switches": sw, "leaks": leaks, "job_findings": found,
                          "skipped_not_installed": skipped,
                          "switched_off": [] if fixture_only() else switched_off_lines(now)},
                         indent=2))
        return 0

    shown = results if args.all else problems
    if not shown and not drift and not repo and not prices and not models and not keys and not onto and not sw and not leaks and not found:
        # Silent success: nothing printed, nothing found.
        return 0
    if problems or drift or repo or prices or models or keys or onto or sw or leaks or found:
        # Found work is success: lead with a FINDINGS: line and exit 0.
        print(f"FINDINGS: {len(problems)} job problem(s), "
              f"{len(drift)} shim drift, {len(repo)} repo hygiene, "
              f"{len(prices)} price drift, {len(models)} model drift, {len(keys)} key registry, "
              f"{len(onto)} ontology, {len(sw)} switch store, {len(leaks)} leak, "
              f"{len(found)} job finding{triage}")
    for r in sorted(shown, key=lambda x: (x["status"] == "OK", x["job"])):
        print(f"[{r['status']:7}] {r['label']}: {r['detail']}")
    for d in drift:
        print(f"[{'DRIFT':7}] cron shim reconcile: {d}")
    for rp in repo:
        print(f"[{'REPO':7}] git hygiene: {rp}")
    for p in prices:
        print(f"[{'DRIFT':7}] price table projection: {p}")
    for m in models:
        print(f"[{'MODEL':7}] frontier drift: {m}")
    for k in keys:
        print(f"[{'KEYS':7}] key registry: {k}")
    for l in leaks:
        print(f"[{'LEAK':7}] secret outside the vault: {l}")
    for o in onto:
        print(f"[{'ONTO':7}] {o}")
    for w in sw:
        print(f"[{'SWITCH':7}] {w}")
    for f in found:
        print(f"[{'FOUND':7}] {f}")
    if args.all and skipped:
        print(f"[{'SKIP':7}] fleet-only scanners not installed on this seed install: "
              f"{', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
