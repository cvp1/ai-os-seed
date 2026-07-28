#!/usr/bin/env python3
"""Audit surface (SEED-064) — derive a monthly one-pager + SIEM export from
the gate hook's append-only JSONL log (SEED-062).

    audit_report.py --audit-dir ~/.seed/audit --month 2026-07
    audit_report.py --audit-dir ~/.seed/audit --month 2026-07 --export siem --out spend.csv

Everything here is a DERIVED VIEW, recomputed from the log on every run —
never a frozen number (store facts, derive views doctrine). The report
never touches or deletes the log; retention/rotation is the org's own
records policy.

Edge-trigger tone: an ordinary month reads as ordinary. Anomalies (a budget
flip, a self-protect/MFA hit, an outsized refusal share) lead the page —
nothing routine is buried under nothing anomalous, and nothing anomalous is
buried under routine detail.

Stdlib only.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

CATEGORY_LABELS = {
    "allowed": "allowed (ACT/ACT_NOTIFY)",
    "staged": "staged for human application (PROPOSE)",
    "budget_downgrade": "staged because the monthly budget was exceeded",
    "draft_only_deny": "denied outright — human must send/sign/commit directly (DRAFT_ONLY)",
    "default_deny": "denied — no classification rule matched (unclassified)",
    "self_protect": "denied — attempted to modify the governance files themselves",
    "mfa_deny": "denied — an MFA/OTP-shaped request (always goes to a human)",
}
ANOMALY_CATEGORIES = {"self_protect", "mfa_deny", "budget_downgrade"}


def load_month(audit_dir: Path, month: str) -> list:
    path = audit_dir / f"{month}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a corrupt line is skipped, not fatal — the report degrades, doesn't crash
    return records


def load_spend_state(audit_dir: Path, month: str) -> dict:
    path = audit_dir / "spend_state.json"
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if state.get("month") == month else {}


def load_completions(audit_dir: Path, month: str) -> list:
    """Optional signal: a first-win skill MAY log completions to
    completions.jsonl ({"ts": "...", "task": "..."}). Absent by default —
    the report says so honestly rather than fabricating a count."""
    path = audit_dir / "completions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("ts", "").startswith(month):
            out.append(rec)
    return out


def staged_pending_count(records: list, staged_dir: Path) -> tuple:
    """Cross-references each STAGE record's undo_path against the current
    staged/ directory: still present = pending; gone = resolved (applied or
    manually discarded — this artifact can't tell which, and says so)."""
    staged_records = [r for r in records if r.get("decision") == "STAGE" and r.get("undo_path")]
    pending = sum(1 for r in staged_records if Path(r["undo_path"]).exists())
    resolved = len(staged_records) - pending
    return pending, resolved


def detect_anomalies(records: list, spend_state: dict) -> list:
    anomalies = []
    cat_counts = {}
    for r in records:
        cat = r.get("category", "unknown")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for cat in ANOMALY_CATEGORIES:
        if cat_counts.get(cat, 0) > 0:
            anomalies.append(f"{cat_counts[cat]} event(s) in category '{cat}' ({CATEGORY_LABELS.get(cat, cat)})")
    total = len(records)
    denies = sum(1 for r in records if r.get("decision") == "DENY")
    if total >= 10 and denies / total > 0.3:
        anomalies.append(f"refusal share is {denies}/{total} ({denies*100//total}%) — unusually high")
    if spend_state.get("budget_flipped"):
        anomalies.append(f"monthly budget was exceeded (${spend_state.get('usd', 0):.2f})")
    return anomalies


def render_report(month: str, records: list, spend_state: dict, completions: list,
                   staged_pending: int, staged_resolved: int, budget_usd: float = None) -> str:
    anomalies = detect_anomalies(records, spend_state)
    lines = [f"# Governance audit — {month}", ""]

    if anomalies:
        lines.append("**This month is NOT ordinary — leading with what changed:**\n")
        for a in anomalies:
            lines.append(f"- {a}")
        lines.append("")
    else:
        lines.append(f"**Ordinary month.** {len(records)} governed action(s), no budget flip, "
                      "no self-protection or MFA events, refusal share within normal range.\n")

    lines.append("## Actions by category\n")
    cat_counts = {}
    for r in records:
        cat = r.get("category", "unknown")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for cat, count in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {CATEGORY_LABELS.get(cat, cat)}: **{count}**")
    if not cat_counts:
        lines.append("- (no governed actions recorded this month)")

    lines.append("\n## Proposals (PROPOSE-tier staging)\n")
    lines.append(f"- Pending (still awaiting human application): **{staged_pending}**")
    lines.append(f"- Resolved (applied or manually discarded — this log can't distinguish "
                  f"which, only that the staged file is gone): **{staged_resolved}**")

    lines.append("\n## Spend vs budget\n")
    if spend_state:
        budget = budget_usd if budget_usd is not None else "?"
        lines.append(f"- Spend this month: **${spend_state.get('usd', 0):.2f}** of ${budget} budget")
        lines.append(f"- Budget exceeded: **{spend_state.get('budget_flipped', False)}**")
    else:
        lines.append("- no spend state recorded for this month yet")

    lines.append("\n## Task completions / cost-per-task\n")
    if completions:
        spend = spend_state.get("usd", 0.0)
        per_task = spend / len(completions) if completions else None
        lines.append(f"- Tasks completed: **{len(completions)}**")
        if per_task is not None:
            lines.append(f"- $/successful-task: **${per_task:.2f}**")
    else:
        lines.append("- no task-completion signal present this month (no first-win skill logged "
                      "completions to `completions.jsonl`) — this line is honestly blank, not zero")

    return "\n".join(lines) + "\n"


def export_siem(records: list, out_path: Path):
    """Flat CSV with stable field names — the org's SIEM plumbing takes it
    from there (non-goal: this tool doesn't integrate, only emits)."""
    fields = ["ts", "tool_name", "args_hash", "surface", "reversibility", "tier", "decision", "category", "undo_path"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({k: r.get(k, "") for k in fields})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audit-dir", required=True)
    ap.add_argument("--staged-dir", default=None,
                     help="default: <audit-dir's sibling>/staged (the ~/.seed/{audit,staged} convention)")
    ap.add_argument("--month", default=None, help="YYYY-MM, default = current UTC month")
    ap.add_argument("--budget-usd", type=float, default=None, help="known monthly budget, for the report line")
    ap.add_argument("--export", choices=["siem"], default=None)
    ap.add_argument("--out", default=None, help="output path (report .md, or CSV for --export)")
    args = ap.parse_args()

    audit_dir = Path(args.audit_dir)
    month = args.month or time.strftime("%Y-%m", time.gmtime())
    records = load_month(audit_dir, month)

    if args.export == "siem":
        out_path = Path(args.out) if args.out else audit_dir / f"{month}-export.csv"
        export_siem(records, out_path)
        print(f"exported {len(records)} record(s) to {out_path}")
        return 0

    spend_state = load_spend_state(audit_dir, month)
    completions = load_completions(audit_dir, month)
    staged_dir = Path(args.staged_dir) if args.staged_dir else audit_dir.parent / "staged"
    pending, resolved = staged_pending_count(records, staged_dir)
    report = render_report(month, records, spend_state, completions, pending, resolved, args.budget_usd)

    out_path = Path(args.out) if args.out else audit_dir / f"audit-report-{month}.md"
    out_path.write_text(report)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
