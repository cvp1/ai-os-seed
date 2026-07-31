#!/usr/bin/env python3
"""Compile a validated policy.yml into enforceable Claude Code config
(SEED-061) — the static half of governance enforcement.

    compile_profile.py policy.yml --out out/
    compile_profile.py policy.yml --out out/ --check   # drift gate

Emits, into --out:
  settings-fragment.json  — a `permissions` block (allow/ask/deny), merge-
                             ready into the recipient's .claude/settings.json
  COMPILE-REPORT.md        — every matrix cell + every prohibition, with the
                             enforcement layer it actually bound at; any
                             UNENFORCED line names its compensating control
  SCOPES.md                 — per-connector: which send/write scopes to
                             withhold, config snippet where known, checklist
                             where the platform holds the toggle

Design rule (the enforcement ladder — see docs/governance-profile.md):
every binding is reported at the STRONGEST layer this compiler can actually
prove — capability > network > harness > instruction. A cell or prohibition
this compiler can only describe, not enforce, is marked UNENFORCED, loudly,
never silently downgraded to "instruction" and called done.

Classification engine: classify_defaults.yml (shipped pattern library). A
tool/command pattern with NO entry there is UNCLASSIFIED regardless of
policy.yml's own descriptive surface defaults — default-deny, and named in
the report (see classify_defaults.yml's header for why these are separate).

Stdlib + PyYAML (same dependency as validate_policy.py / the scheduler).
"""
import argparse
import filecmp
import json
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GOV_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import validate_policy as vp  # noqa: E402

REVERSIBILITY = ["reversible", "costly", "irreversible"]

TIER_TO_ACTION = {
    "ACT": "allow",
    "ACT_NOTIFY": "allow",
    "PROPOSE": "ask",
    "DRAFT_ONLY": "deny",
    "NEVER": "deny",
}

# Self-protection targets — files whose modification would let the agent
# escape or rewrite its own governance. Always denied regardless of policy
# content (no_self_modification, bound at harness — this IS the binding).
SELF_PROTECT_PATTERNS = [
    "Edit(.claude/settings.json)",
    "Write(.claude/settings.json)",
    "Edit(.claude/hooks/profile_gate.py)",
    "Write(.claude/hooks/profile_gate.py)",
    "Edit(governance/policy.yml)",
    "Write(governance/policy.yml)",
    "Edit(governance/out/**)",
    "Write(governance/out/**)",
]

# no_mfa_handling's concrete binding — any tool-name shape that looks like an
# auth-challenge/OTP submission is denied outright, independent of policy
# content. Fixed in v1 (not policy-configurable), same status as self-protect.
MFA_DENY_PATTERNS = [
    "*otp*", "*mfa*", "*2fa*", "*verification_code*", "*auth_challenge*", "*totp*",
]

# Prohibition enforcement layer determination — see docs/governance-profile.md
# "Honest constraints". A generic, environment-agnostic compiler cannot bind
# network or connector-capability layers itself; it reports those honestly
# and produces the artifact (SCOPES.md) a human uses to close the gap.
PROHIBITION_LAYER = {
    "secrets_vaulted": ("harness", "BOUND",
        "hook denies/redacts reads outside the vault path pattern and never echoes matched values"),
    "no_mfa_handling": ("harness", "BOUND",
        "hook denies any tool call matching an OTP/MFA-shaped request pattern"),
    "egress_first_party_only": ("network", "UNENFORCED",
        "this compiler has no live network layer to bind; configure an org-side egress "
        "allowlist restricting model-provider traffic to contracted endpoints. Harness-layer "
        "fallback: MCP connectors not in the org's approved list can be denied wholesale in "
        "settings — do this by hand until an org-specific allowlist is supplied"),
    "no_self_modification": ("harness", "BOUND",
        "settings.json + hook deny Edit/Write against the governance files themselves (see self-protection block)"),
    "informed_approval": ("harness", "BOUND",
        "every STAGE-tier call writes the full action plus an approval_digest over its exact "
        "bytes (profile_gate.write_staged); --verify-staged re-derives that digest and refuses "
        "a proposal altered after review, and a proposal with no digest fails closed"),
    "no_impersonation": ("harness", "BOUND-VIA-MATRIX",
        "the external row's DRAFT_ONLY tier already denies direct-send tool patterns at the "
        "harness layer for every cell; capability-layer reinforcement (withhold send scope "
        "entirely) is documented per-connector in SCOPES.md — verify there, this compiler "
        "cannot revoke OAuth scopes itself"),
}


def load_classify_defaults(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    for e in entries:
        if e["reversibility"] not in REVERSIBILITY:
            sys.exit(f"classify_defaults.yml: entry {e['pattern']!r} has unknown reversibility {e['reversibility']!r}")
    return entries


def compile_settings(policy: dict, classify_entries: list):
    """Returns (fragment, bindings) where bindings is a list of
    (pattern, surface, reversibility, tier, action, note) for the report."""
    matrix = policy["matrix"]
    allow, ask, deny = [], [], []
    bindings = []

    for e in classify_entries:
        surface, reversibility = e["surface"], e["reversibility"]
        if surface not in matrix:
            sys.exit(f"classify_defaults.yml: entry {e['pattern']!r} references "
                      f"unknown surface {surface!r} — not in policy.yml matrix")
        tier = matrix[surface][reversibility]
        action = TIER_TO_ACTION[tier]
        {"allow": allow, "ask": ask, "deny": deny}[action].append(e["pattern"])
        bindings.append((e["pattern"], surface, reversibility, tier, action, e.get("note", "")))

    # Self-protection: always denied, independent of any classify entry —
    # this is the concrete binding for no_self_modification.
    for pattern in SELF_PROTECT_PATTERNS:
        deny.append(pattern)
    # MFA/OTP shapes: always denied — the concrete binding for no_mfa_handling.
    for pattern in MFA_DENY_PATTERNS:
        deny.append(pattern)

    fragment = {"permissions": {"allow": sorted(set(allow)), "ask": sorted(set(ask)), "deny": sorted(set(deny))}}
    return fragment, bindings


def render_compile_report(policy: dict, bindings: list, unclassified_examples: list) -> str:
    lines = ["# COMPILE-REPORT.md — governance profile compile output", "",
             "Generated by compile_profile.py. Every matrix cell and every prohibition is",
             "listed with the layer it actually bound at. `UNENFORCED` is loud on purpose —",
             "never a silent downgrade (degrade toward safety doctrine).", "",
             "**Merge-order requirement:** `settings-fragment.json`'s broad `allow` entries "
             "(bare `Edit`, `Write`) coexist with narrow `deny` entries for the governance "
             "files themselves (self-protection block). This relies on Claude Code's own "
             "deny-before-allow precedence — verify that holds for the target install's "
             "Claude Code version (SEED-063 probes this directly)."]

    lines.append("## Matrix cells\n")
    lines.append("Every cell binds at the **harness** layer (the strongest layer this static "
                  "compiler itself produces — see docs/governance-profile.md's enforcement "
                  "ladder). The runtime hook (SEED-062) adds staging/audit behavior on top of "
                  "the same classification; it does not change which layer a cell binds at.\n")
    lines.append("| Surface | Reversibility | Tier | Layer | Action | Bound patterns |")
    lines.append("|---|---|---|---|---|---|")
    matrix = policy["matrix"]
    for surface in matrix:
        for reversibility in REVERSIBILITY:
            tier = matrix[surface][reversibility]
            action = TIER_TO_ACTION[tier]
            patterns = [b[0] for b in bindings if b[1] == surface and b[2] == reversibility]
            pat_str = ", ".join(f"`{p}`" for p in patterns) if patterns else "_(no shipped pattern classifies here yet)_"
            lines.append(f"| {surface} | {reversibility} | {tier} | harness | {action} | {pat_str} |")

    lines.append("\n## Prohibitions\n")
    lines.append("| id | declared layer | actual status | detail |")
    lines.append("|---|---|---|---|")
    for p in policy["prohibitions"]:
        pid = p["id"]
        layer, status, detail = PROHIBITION_LAYER.get(
            pid, (p.get("enforce_at", "?"), "UNENFORCED", "no compiler rule for this prohibition id — treat as unbound until one is added")
        )
        lines.append(f"| {pid} | {p.get('enforce_at')} | **{status}** ({layer}) | {detail} |")

    lines.append("\n## Self-protection (no_self_modification binding)\n")
    for pat in SELF_PROTECT_PATTERNS:
        lines.append(f"- `{pat}` — denied")

    lines.append("\n## MFA/OTP shapes (no_mfa_handling binding)\n")
    for pat in MFA_DENY_PATTERNS:
        lines.append(f"- `{pat}` — denied")

    lines.append("\n## Default-deny (unclassified patterns)\n")
    if unclassified_examples:
        for ex in unclassified_examples:
            lines.append(f"- `{ex}` — no classify_defaults.yml entry; compiled to `ask`, per default-deny")
    else:
        lines.append("_(none exercised in this compile — see compile_profile's test suite for a live example)_")

    return "\n".join(lines) + "\n"


def render_scopes(policy: dict, bindings: list) -> str:
    lines = ["# SCOPES.md — connector send/write scopes to withhold", "",
             "Per the delivery model: draft-only is an API-scope FACT, not an instruction",
             "the model could ignore. For each connector below, the org must withhold",
             "send/write authority at the connector's own configuration — config snippet",
             "where this compiler can express one, a platform checklist item otherwise.", ""]
    external_patterns = sorted({b[0] for b in bindings if b[1] == "external"})
    if not external_patterns:
        lines.append("_(no external-surface patterns in this compile)_")
        return "\n".join(lines) + "\n"
    for pat in external_patterns:
        lines.append(f"## `{pat}`")
        lines.append("- Tier: DRAFT_ONLY (external row, all reversibilities)")
        lines.append("- Action required: withhold send/write scope from this connector's OAuth "
                      "grant or API credential; the agent should hold read/draft scope only")
        lines.append("- Verify: attempt the conformance suite's impersonation probe (SEED-063) "
                      "against this connector and confirm it refuses at the connector layer, "
                      "not just the harness layer\n")
    return "\n".join(lines) + "\n"


def probe_unclassified(entries: list, probe_patterns: list) -> list:
    """Given a list of candidate tool patterns an org wants classified
    (e.g. their real MCP tool inventory), return the subset with no
    classify_defaults.yml entry — the compile-time default-deny check."""
    known = {e["pattern"] for e in entries}
    return [p for p in probe_patterns if p not in known]


def render_classification(bindings: list, policy: dict) -> dict:
    """Machine-readable artifact for the runtime hook (SEED-062) — so the hook
    consumes a precompiled table instead of re-deriving policy interpretation
    (PyYAML-free at runtime; the hook only needs stdlib json)."""
    return {
        "version": 1,
        "entries": [
            {"pattern": pat, "surface": surface, "reversibility": rev, "tier": tier, "action": action, "note": note}
            for pat, surface, rev, tier, action, note in bindings
        ],
        "self_protect_deny": SELF_PROTECT_PATTERNS,
        "mfa_deny": MFA_DENY_PATTERNS,
        "delegation": policy["delegation"],
        "spend": policy["spend"],
    }


def compile_all(policy_path: Path, classify_path: Path, out_dir: Path, probe_patterns=None):
    data = vp.load(policy_path)
    vp.validate(data)  # refuse to compile an invalid policy — no partial output
    entries = load_classify_defaults(classify_path)
    fragment, bindings = compile_settings(data, entries)
    unclassified = probe_unclassified(entries, probe_patterns or [])
    if unclassified:
        fragment["permissions"]["ask"] = sorted(set(fragment["permissions"]["ask"]) | set(unclassified))
    report = render_compile_report(data, bindings, unclassified_examples=unclassified)
    scopes = render_scopes(data, bindings)
    classification = render_classification(bindings, data)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "settings-fragment.json").write_text(json.dumps(fragment, indent=2) + "\n")
    (out_dir / "COMPILE-REPORT.md").write_text(report)
    (out_dir / "SCOPES.md").write_text(scopes)
    (out_dir / "classification.json").write_text(json.dumps(classification, indent=2) + "\n")
    return fragment, report, scopes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("policy", help="path to policy.yml")
    ap.add_argument("--classify", default=str(GOV_ROOT / "classify_defaults.yml"),
                     help="path to classify_defaults.yml (default: shipped copy)")
    ap.add_argument("--out", default=str(GOV_ROOT / "out"), help="output directory")
    ap.add_argument("--probe", default=None,
                     help="optional file, one tool pattern per line — patterns with no "
                          "classify_defaults.yml entry are flagged unclassified in the report")
    ap.add_argument("--check", action="store_true",
                     help="recompile into a temp dir and diff against --out, exit 1 on drift")
    args = ap.parse_args()

    policy_path, classify_path, out_dir = Path(args.policy), Path(args.classify), Path(args.out)
    probe_patterns = None
    if args.probe:
        probe_patterns = [ln.strip() for ln in Path(args.probe).read_text().splitlines() if ln.strip()]

    if args.check:
        if not out_dir.exists():
            print(f"no committed {out_dir} yet — nothing to diff (run without --check first)")
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            tmp_out = Path(tmp) / "out"
            compile_all(policy_path, classify_path, tmp_out, probe_patterns)
            cmp = filecmp.dircmp(out_dir, tmp_out)
            drift = cmp.diff_files + cmp.left_only + cmp.right_only
            if drift:
                print(f"DRIFT: {len(drift)} path(s) differ between committed {out_dir} and a fresh compile:")
                for d in drift:
                    print(f"  {d}")
                return 1
            print(f"OK — {out_dir} matches a fresh compile of {policy_path}")
            return 0

    fragment, report, scopes = compile_all(policy_path, classify_path, out_dir, probe_patterns)
    print(f"compiled {policy_path} -> {out_dir}/")
    print(f"  settings-fragment.json  ({len(fragment['permissions']['allow'])} allow, "
          f"{len(fragment['permissions']['ask'])} ask, {len(fragment['permissions']['deny'])} deny)")
    print("  COMPILE-REPORT.md")
    print("  SCOPES.md")
    print("  classification.json  (consumed by the runtime hook, SEED-062)")
    unenforced = report.count("**UNENFORCED**")
    if unenforced:
        print(f"\nNOTE: {unenforced} prohibition(s) report UNENFORCED at their declared layer — "
              f"see COMPILE-REPORT.md for the compensating control before treating this as signed-off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
