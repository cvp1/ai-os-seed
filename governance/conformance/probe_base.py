"""Shared probe interface (SEED-063). A probe is a small, self-describing
unit: a plain-English description (what a reviewer should see happen) plus a
run() that returns a ProbeResult. See run_conformance.py for how the fixed
roster (12 cell + 5 prohibition + 3 dynamic) is assembled and executed.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ProbeResult:
    status: str          # PASS / FAIL / UNENFORCED
    detail: str           # what actually happened, plain sentence


@dataclass
class ProbeContext:
    """What a probe needs to act. In mechanical mode, `call` drives
    gate_sandbox directly. In --live mode (a curated subset — see
    run_conformance.py), `call` is replaced by a real-session driver and
    `live` is True."""
    classification_path: Path
    staged_dir: Path
    audit_dir: Path
    call: Callable  # (tool_name, tool_input, spend_usd=0.0) -> gate_sandbox.run_gate-shaped result
    policy_path: Path = None    # the REAL policy.yml (not the temp compile output)
    report_path: Path = None    # the REAL COMPILE-REPORT.md next to policy_path's compile
    live: bool = False


@dataclass
class Probe:
    id: str
    category: str          # "cell" | "prohibition" | "dynamic"
    description: str        # reviewer-facing: what was attempted, plain English
    run: Callable[[ProbeContext], ProbeResult]
    live_eligible: bool = True  # some probes only make sense mechanically


def decision_of(result) -> str:
    """Extract the permissionDecision from a gate_sandbox result, or 'allow'
    if the hook printed nothing (its allow contract)."""
    if not result.stdout.strip():
        return "allow"
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["permissionDecision"]


def reason_of(result) -> str:
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["permissionDecisionReason"]
