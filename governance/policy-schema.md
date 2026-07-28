# policy.yml schema — the signed matrix as data

SEED-060. This is the one input every governance tool in this wave consumes
(`validate_policy.py`, `compile_profile.py`, the runtime gate, the
conformance suite). It is deliberately **harness-neutral**: nothing here
names a Claude Code concept (no tool names, no hook types). Only the
compiler (SEED-061/062) translates policy into harness mechanics — which
keeps a second harness backend a new compiler target, not a schema fork.

Source of truth for the *content*: vault `01 Ideas/AI Agent Permission
Matrix - One-Page Governance Artifact.md` (v0.2), the signed page. This
schema is that page, typed.

## Top-level keys

```yaml
version: 1

org:                  # free text, informational only — never scrubbed/validated
  name: string
  policy_owner: string
  security_owner: string
  signed_date: string   # ISO date; informational, not enforced

surfaces: {...}         # §1
tiers: [...]             # §2
matrix: {...}            # §3
prohibitions: [...]      # §4
read_scope: {...}        # §5
delegation: {...}        # §6
spend: {...}             # §7
overlays: {...}          # §8 (optional)
```

### §1 `surfaces`

The rows of the matrix. Four ship in the reference policy; an org may add
custom rows (never remove the four — the validator rejects a policy missing
any reference surface).

```yaml
surfaces:
  private_workspace:
    label: "Private workspace"
    description: "own files, drafts, scratch analysis"
    classify:                     # patterns that route an action to this surface
      - kind: path                # {path, tool, connector} — the compiler owns matching
        pattern: "**"              # default: everything not otherwise classified
        default: true
  team_shared:
    label: "Team-shared"
    description: "tickets, wikis, repos with review gates"
    classify:
      - kind: connector
        pattern: "chat.dm.*"
        note: "a DM to one colleague — team-shared, not private (v0.2 binding call)"
      - kind: repo
        pattern: "*"
        requires: "review_gate=true"
  system_of_record:
    label: "System of record"
    description: "prod config, master data, ledgers, HR/CRM"
    classify:
      - kind: repo
        pattern: "*"
        requires: "review_gate=false"
        note: "an ungated repo IS a system of record — merge is truth (v0.2 binding call)"
      - kind: connector
        pattern: "db.prod.*"
  external:
    label: "External"
    description: "outbound mail, publishing, spend, anything a customer sees"
    classify:
      - kind: connector
        pattern: "mail.send"
      - kind: connector
        pattern: "publish.*"
      - kind: connector
        pattern: "spend.*"
        note: "money is always external — a third party is on the other end (v0.2)"
      - kind: connector
        pattern: "calendar.invite.external"
        note: "an external attendee promotes a calendar action to external (v0.2)"
```

Custom rows follow the same shape and slot into `matrix` like the four
reference rows.

### §2 `tiers`

Fixed vocabulary — the validator rejects any tier name outside this set.

```yaml
tiers: [ACT, ACT_NOTIFY, PROPOSE, DRAFT_ONLY, NEVER]
```

- `ACT` — executes, logs.
- `ACT_NOTIFY` — executes, then reports the change + undo path.
- `PROPOSE` — stages the exact change; a human applies it deliberately.
- `DRAFT_ONLY` — produces the artifact; a human sends/signs/commits it under
  their own identity. The agent never holds send authority here.
- `NEVER` — not in the v0.2 page's named tiers, but legal in the schema for
  a cell an org wants hard-blocked regardless of reversibility (stricter
  than DRAFT_ONLY is not expressible on the page's 4-tier ladder otherwise).

### §3 `matrix`

Every `surface × reversibility` cell, explicit — the validator rejects any
missing cell (a matrix with holes fails closed, per the artifact's
default-deny operating rule).

```yaml
matrix:
  private_workspace:
    reversible: ACT
    costly: ACT_NOTIFY
    irreversible: PROPOSE
  team_shared:
    reversible: ACT_NOTIFY
    costly: PROPOSE
    irreversible: PROPOSE
  system_of_record:
    reversible: PROPOSE
    costly: PROPOSE
    irreversible: DRAFT_ONLY
  external:
    reversible: DRAFT_ONLY
    costly: DRAFT_ONLY
    irreversible: DRAFT_ONLY
```

### §4 `prohibitions`

The five standing ones from v0.2, each carrying an `enforce_at` hint the
compiler must honor or report as a weaker binding (the enforcement ladder —
see `docs/governance-profile.md`).

```yaml
prohibitions:
  - id: secrets_vaulted
    text: "Secrets are read from the vault at point of use; never in prompts, transcripts, logs, or outputs."
    enforce_at: harness        # hook redacts/blocks; connector-level where possible
  - id: no_mfa_handling
    text: "No MFA/OTP handling. Authentication challenges go to a human, always."
    enforce_at: harness
  - id: egress_first_party_only
    text: "Enterprise data reaches only contracted, enterprise-tier model providers. Unknown data classification fails closed."
    enforce_at: network        # strongest available layer for this one
  - id: no_self_modification
    text: "The agent cannot alter its own permissions, this matrix, or its automation hooks without human sign-off."
    enforce_at: harness
  - id: no_impersonation
    text: "External communication carries a human's deliberate send, or it does not go."
    enforce_at: connector      # strongest — send scope withheld entirely
```

A policy may not delete a reference prohibition; it may only add org-specific
ones (validator-enforced).

### §5 `read_scope`

Points at the org's *existing* data-classification policy — this schema
creates no new taxonomy (per the artifact's design note).

```yaml
read_scope:
  approved_classes: [public, internal]      # org-defined labels
  excluded_classes: [restricted, regulated]  # org-defined labels
  unclassified: fail_closed                  # only legal value in v1
```

### §6 `delegation`

```yaml
delegation:
  terminal_cell_governs: true   # the only legal value in v1 — see design note
```

### §7 `spend`

```yaml
spend:
  monthly_budget_usd: 500
  alert_threshold_pct: 80
  on_exceed: propose_all        # only legal value in v1 — degrade loudly, never silent
```

### §8 `overlays` (optional)

Named, business-unit-scoped patches. **Tighten-only** — the validator
rejects any overlay cell that names a tier looser than the base matrix for
that cell (ACT looser than ACT_NOTIFY looser than PROPOSE looser than
DRAFT_ONLY looser than NEVER; this ordering is what "tighten" means).

```yaml
overlays:
  finance_team:
    matrix:
      system_of_record:
        reversible: DRAFT_ONLY   # tightened from PROPOSE — legal
```

## What "valid" means

`validate_policy.py` (SEED-060) enforces, in order: schema shape (all
required keys, known tiers, known surface/classify kinds) → matrix
completeness (every reference surface × reversibility cell present) →
prohibition completeness (all five reference IDs present, text unchanged
in `id`) → overlay tighten-only → spend has both a budget and an
`on_exceed`. First violation found halts with a specific, line-referenced
reason — an unparseable or incomplete policy is a **refused** policy, never
a partially-applied one.
