# Skill layers

Classifies every skill into the three layers from [`../VISION.md`](../VISION.md):
**Core** (the product — domain/provider-agnostic), **Adapter** (a swappable edge:
a comms connector or a domain pack), or proving-ground-only detail.

This registry is the first form of the tag. The eventual form is a `layer:` field
in each `SKILL.md` frontmatter — but a single table is cheaper to eyeball, safe
(touches no symlinked skill files), and is the right shape for a draft. Promote to
per-skill frontmatter once the split is settled.

## The test

- **Core** — names no vendor, no IP, no location, no ranch dataset; depends only on
  Core primitives (persistent memory, the notes vault, a *connector interface*).
  Ships in `{{REDACTED}}/ai-os` and works for anyone.
- **Adapter : comms** — binds the connector interface to one real mail/calendar
  provider (Proton, M365, Google). One realization of a Core contract.
- **Adapter : domain** — binds to a specific place, device, or dataset (the ranch:
  solar, mesh, cameras, network, weather, garden, fire).

A skill is an adapter the moment it hardcodes an endpoint, vendor, coordinate, or
corpus. The *pattern* can still be Core even when a given binding is an adapter —
see `triage`.

## Registry

| Skill | Layer | Note |
|---|---|---|
| `improve` | **core** | the learning loop — the product's heartbeat |
| `skill-center` | **core** | meta: finds/creates/audits the rest |
| `wiki` | **core** | precedent search over the vault |
| `ingest` | **core** | raw sources → maintained synthesis pages |
| `triage` | **core** · *bindings → adapter* | the pattern is Core; *which* inboxes (Gmail/Proton/personal Outlook) is adapter config to extract |
| `board` | **core** | decision tooling, vault-backed, domain-agnostic |
| `teach` | **core** | stateful tutor; general |
| `workflow-visualizer` | **core** | system description → HTML diagram |
| `proton-mail` | **adapter : comms** | Proton Bridge — one realization of the mail-connector contract |
| `cognizant` | **adapter : comms** | M365 work-mailbox binding (employer-specific) |
| `weather` | **adapter : domain (ranch)** | bias-corrected to the on-site station |
| `garden` | **adapter : domain (ranch)** | grounded in the 17-guide library |
| `firealert` | **adapter : domain (ranch)** | Twilio fire roster |
| `ipad-control` | **adapter : domain (home)** | UniFi client block/unblock |

Tally: **8 core · 2 comms adapters · 4 domain adapters.**

## Cross-check against the product

`{{REDACTED}}/ai-os` already advertises a Core starter set — `/triage`, `/wiki`, `/ingest`,
`/improve`, `/skill-center` (plus `/brief`, `/prep`, `/status`, `/weekly`,
`/memory-prune`, which CC realizes as crons/variants: `morning-brief`,
`home-digest`, `memory-prune`). Every one the product ships lands in **core** above
— independent confirmation the line is drawn in the right place.

## What this surfaces

- **Promoted to the product (`{{REDACTED}}/ai-os`):** `improve`, `skill-center`, `wiki`,
  `ingest` (already in the setup-prompt toolkit), plus `board`, `teach`,
  `workflow-visualizer` (added 2026-06-27). The product builds skills from its
  embedded prompt spec, not shipped files — "promote" = add the skill to PHASE 1
  (fresh installs build it; upgrade-mode adds it to existing installs).
- **Comms skills: factored.** `triage`, `cognizant`, and `proton-mail` now split a
  provider-blind engine from adapter bindings (`accounts.yml` / `account.yml` +
  per-skill `connectors.md`) over the shared contract in
  [`CONNECTORS.md`](CONNECTORS.md). The engines are Core; the manifests and
  connector realizations are the adapter layer.
- **Stay adapters by design:** the ranch domain skills (`weather`, `garden`,
  `firealert`, `ipad-control`). They are the proving-ground's hardest edges, not
  product debt.

> Live-validation gate (principle 12): **CLEARED 2026-06-27.** All three ran
> read-only end-to-end — `proton-bridge` (25-msg INBOX), `outlook-composio-work`
> (12 msgs + calendar, AZ/MST), and `triage` across all four connectors
> (`gmail-mcp` · `proton-bridge` · `outlook-composio-personal` · `gcal-mcp`) with
> window, `pins`, and `exclude` applied. Engines drove the right providers through
> the contract; behavior preserved. Not yet exercised: triage's HTML archive
> emitter (unchanged by the refactor) and a non-empty `outlook-composio-personal`
> read (inbox was empty).
