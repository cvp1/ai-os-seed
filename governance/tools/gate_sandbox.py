#!/usr/bin/env python3
"""Drive profile_gate.py standalone with a synthetic PreToolUse event — no
live Claude Code session required (SEED-062). Used by tests and by the
conformance suite's mechanical mode (SEED-063).

    gate_sandbox.py --tool Bash --input '{"command":"git push origin main"}' \\
        --classification seed-src/governance/out/classification.json \\
        --staged-dir /tmp/staged --audit-dir /tmp/audit

Prints the hook's raw stdout and exits with the hook's exit code, exactly as
Claude Code would observe it — this is a thin subprocess wrapper, not a
reimplementation, so what passes here is what the real hook does.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "profile_gate.py"


def run_gate(tool_name: str, tool_input: dict, classification_path: Path,
             staged_dir: Path, audit_dir: Path, spend_usd: float = 0.0,
             session_id: str = "sandbox-session"):
    event = {"session_id": session_id, "cwd": str(Path.cwd()), "tool_name": tool_name, "tool_input": tool_input}
    env = dict(os.environ)
    env["SEED_GOVERNANCE_CLASSIFICATION"] = str(classification_path)
    env["SEED_GOVERNANCE_STAGED_DIR"] = str(staged_dir)
    env["SEED_GOVERNANCE_AUDIT_DIR"] = str(audit_dir)
    env["SEED_GOVERNANCE_SPEND_USD"] = str(spend_usd)
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event),
        capture_output=True, text=True, env=env,
    )
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tool", required=True)
    ap.add_argument("--input", default="{}", help="JSON tool_input")
    ap.add_argument("--classification", required=True)
    ap.add_argument("--staged-dir", required=True)
    ap.add_argument("--audit-dir", required=True)
    ap.add_argument("--spend-usd", type=float, default=0.0)
    args = ap.parse_args()

    result = run_gate(
        args.tool, json.loads(args.input), Path(args.classification),
        Path(args.staged_dir), Path(args.audit_dir), args.spend_usd,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
