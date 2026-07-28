#!/usr/bin/env python3
"""Validate a policy.yml against the governance schema (SEED-060).

    validate_policy.py policy.yml

Checks, in order (first failure halts — an unparseable or incomplete
policy is a REFUSED policy, never partially applied):
  1. parses as YAML at all
  2. required top-level keys present
  3. every tier used is in the known vocabulary
  4. every reference surface is present (org may ADD surfaces, never drop one)
  5. the matrix has all reversibility cells for every surface (no holes —
     a missing cell fails closed, per the artifact's default-deny rule)
  6. all five reference prohibitions present by id (org may add, not remove)
  7. read_scope.unclassified == fail_closed
  8. delegation.terminal_cell_governs == true
  9. spend has both a monthly_budget_usd and an on_exceed
  10. every overlay cell is >= as strict as the base matrix cell it patches
      (tighten-only — a looser overlay is rejected by name)

Exit 0 + "OK" on a clean policy. Exit 1 with a specific, referenced reason
on the first violation found. PyYAML required (same dependency as the
scheduler — see AGENT-INSTALL.md readiness).
"""
import sys
from pathlib import Path

import yaml

REFERENCE_SURFACES = ["private_workspace", "team_shared", "system_of_record", "external"]
REVERSIBILITY = ["reversible", "costly", "irreversible"]
KNOWN_TIERS = ["ACT", "ACT_NOTIFY", "PROPOSE", "DRAFT_ONLY", "NEVER"]
# Strictness order, loosest to strictest — an overlay may only move a cell
# rightward (or stay put), never leftward.
TIER_STRICTNESS = {t: i for i, t in enumerate(KNOWN_TIERS)}
REFERENCE_PROHIBITIONS = [
    "secrets_vaulted", "no_mfa_handling", "egress_first_party_only",
    "no_self_modification", "no_impersonation",
]
REQUIRED_TOP_KEYS = [
    "version", "surfaces", "tiers", "matrix", "prohibitions",
    "read_scope", "delegation", "spend",
]


class PolicyError(Exception):
    """A specific, halting validation failure — message is the whole point."""


def load(path: Path) -> dict:
    if not path.exists():
        raise PolicyError(f"policy file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PolicyError(f"cannot read {path}: {e}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"{path} is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise PolicyError(f"{path} must parse to a mapping at the top level, got {type(data).__name__}")
    return data


def check_required_keys(data: dict):
    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    if missing:
        raise PolicyError(f"missing required top-level key(s): {', '.join(missing)}")


def check_tiers(data: dict):
    declared = data.get("tiers")
    if not isinstance(declared, list) or not declared:
        raise PolicyError("'tiers' must be a non-empty list")
    unknown = [t for t in declared if t not in KNOWN_TIERS]
    if unknown:
        raise PolicyError(
            f"'tiers' names unknown tier(s) {unknown} — known vocabulary is {KNOWN_TIERS}"
        )


def check_surfaces(data: dict):
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, dict):
        raise PolicyError("'surfaces' must be a mapping")
    missing = [s for s in REFERENCE_SURFACES if s not in surfaces]
    if missing:
        raise PolicyError(
            f"policy is missing reference surface(s) {missing} — the four "
            f"reference surfaces may never be removed, only added to"
        )
    for name, spec in surfaces.items():
        if not isinstance(spec, dict) or "classify" not in spec:
            raise PolicyError(f"surfaces.{name} must be a mapping with a 'classify' list")


def check_matrix(data: dict, surfaces: dict):
    matrix = data.get("matrix")
    if not isinstance(matrix, dict):
        raise PolicyError("'matrix' must be a mapping")
    for surface in surfaces:
        if surface not in matrix:
            raise PolicyError(f"matrix is missing surface '{surface}' entirely — every surface needs all {REVERSIBILITY} cells")
        row = matrix[surface]
        if not isinstance(row, dict):
            raise PolicyError(f"matrix.{surface} must be a mapping of reversibility -> tier")
        missing_cells = [r for r in REVERSIBILITY if r not in row]
        if missing_cells:
            raise PolicyError(
                f"matrix.{surface} is missing cell(s) {missing_cells} — a missing "
                f"cell is an unclassified action and must fail closed, not be silently absent"
            )
        for reversibility, tier in row.items():
            if reversibility not in REVERSIBILITY:
                raise PolicyError(f"matrix.{surface} has unknown reversibility key '{reversibility}'")
            if tier not in KNOWN_TIERS:
                raise PolicyError(f"matrix.{surface}.{reversibility} = '{tier}' is not a known tier {KNOWN_TIERS}")


def check_prohibitions(data: dict):
    prohibitions = data.get("prohibitions")
    if not isinstance(prohibitions, list):
        raise PolicyError("'prohibitions' must be a list")
    ids = [p.get("id") for p in prohibitions if isinstance(p, dict)]
    missing = [p for p in REFERENCE_PROHIBITIONS if p not in ids]
    if missing:
        raise PolicyError(
            f"policy is missing reference prohibition id(s) {missing} — "
            f"the five standing prohibitions may never be removed"
        )
    for p in prohibitions:
        if not isinstance(p, dict) or "text" not in p or "enforce_at" not in p:
            raise PolicyError(f"prohibition entry {p!r} must have 'id', 'text', and 'enforce_at'")


def check_read_scope(data: dict):
    rs = data.get("read_scope")
    if not isinstance(rs, dict):
        raise PolicyError("'read_scope' must be a mapping")
    if rs.get("unclassified") != "fail_closed":
        raise PolicyError(
            "read_scope.unclassified must be 'fail_closed' — the only legal "
            "value in v1 (degrade toward safety, loudly)"
        )


def check_delegation(data: dict):
    d = data.get("delegation")
    if not isinstance(d, dict) or d.get("terminal_cell_governs") is not True:
        raise PolicyError(
            "delegation.terminal_cell_governs must be exactly true — the "
            "only legal value in v1 (no tier laundering through sub-agents)"
        )


def check_spend(data: dict):
    spend = data.get("spend")
    if not isinstance(spend, dict):
        raise PolicyError("'spend' must be a mapping")
    if "monthly_budget_usd" not in spend:
        raise PolicyError("spend.monthly_budget_usd is required")
    if spend.get("on_exceed") != "propose_all":
        raise PolicyError(
            "spend.on_exceed must be 'propose_all' — the only legal value "
            "in v1 (silent overrun or borrow-ahead is not permitted)"
        )


def check_overlays(data: dict):
    overlays = data.get("overlays") or {}
    if not isinstance(overlays, dict):
        raise PolicyError("'overlays' must be a mapping (or empty)")
    base_matrix = data.get("matrix", {})
    for overlay_name, overlay in overlays.items():
        overlay_matrix = (overlay or {}).get("matrix", {})
        for surface, row in overlay_matrix.items():
            if surface not in base_matrix:
                raise PolicyError(
                    f"overlay '{overlay_name}' patches unknown surface '{surface}'"
                )
            for reversibility, tier in (row or {}).items():
                base_tier = base_matrix[surface].get(reversibility)
                if base_tier is None:
                    raise PolicyError(
                        f"overlay '{overlay_name}'.{surface}.{reversibility} patches "
                        f"a cell absent from the base matrix"
                    )
                if tier not in TIER_STRICTNESS or base_tier not in TIER_STRICTNESS:
                    raise PolicyError(
                        f"overlay '{overlay_name}'.{surface}.{reversibility} = '{tier}' "
                        f"is not a known tier"
                    )
                if TIER_STRICTNESS[tier] < TIER_STRICTNESS[base_tier]:
                    raise PolicyError(
                        f"overlay '{overlay_name}' LOOSENS {surface}.{reversibility} "
                        f"from '{base_tier}' to '{tier}' — overlays are tighten-only"
                    )


def validate(data: dict):
    """Run every check in order; raises PolicyError on the first failure."""
    check_required_keys(data)
    check_tiers(data)
    check_surfaces(data)
    check_matrix(data, data["surfaces"])
    check_prohibitions(data)
    check_read_scope(data)
    check_delegation(data)
    check_spend(data)
    check_overlays(data)


def main():
    if len(sys.argv) != 2:
        print("usage: validate_policy.py policy.yml", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        data = load(path)
        validate(data)
    except PolicyError as e:
        print(f"REFUSED — {path}: {e}", file=sys.stderr)
        return 1
    print(f"OK — {path} is a valid governance policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
