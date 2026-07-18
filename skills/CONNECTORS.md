# Mail-connector contract  (CORE)

The shared spine for every comms skill. A *connector* is an adapter that binds one
real mail/calendar provider to this contract; a skill's engine speaks only the
contract and never names a vendor. To add a provider, write a realization (in the
owning skill) and register it below — the engines don't change.

This is Core: it names no vendor, no host, no account. See [`LAYERS.md`](LAYERS.md)
for how skills sort into Core / adapter.

## The contract

| Verb | Meaning | Discipline |
|---|---|---|
| `list(filter)` | messages → `{id, from, subject, gist, date}` | **read-only**; default filter = unread + last ~24h |
| `read(id)` | full headers + plain-text body of one message | **read-only**; SELECT folders read-only — never mark `\Seen` |
| `search(query)` | messages matching subject/from/body | read-only |
| `draft(id, body)` | create a reviewable draft | never auto-sends |
| `send(id, body)` | deliver a reply | **gated** — confirm recipient/subject/body, never bulk |
| `archive(id)` | move out of inbox | reversible; still list items before acting |

Calendar is a parallel capability on providers that have one: `events(range)` (read),
and gated `create`/`update`/`delete` (echo details before writing).

**`mutate` posture** (set per-account in a skill's manifest): `read-only` (no
writes at all), `draft-only` (stop at a draft), `dry-run-then-send` (preview, then
send on confirm), `draft-preferred` (draft unless told to send).

## Realization registry

Each connector is **owned by exactly one skill**, which holds its provider-specific
verb mapping. Engines reference connectors by name.

| Connector | Provider | Owner skill | Posture |
|---|---|---|---|
| `gmail-mcp` | Gmail (claude.ai MCP) | `triage` | draft-only |
| `outlook-composio-personal` | hotmail personal (Composio) | `triage` | draft-preferred |
| `gcal-mcp` | Google Calendar (claude.ai MCP) | `triage` | gated writes |
| `drive` | Google Drive (claude.ai MCP) | `triage` | read, opportunistic |
| `proton-bridge` | Proton Mail (local Bridge) | `proton-mail` | dry-run-then-send |
| `outlook-composio-work` | {{REDACTED}} M365 (Composio) | `cognizant` | **read-only** |

A skill's `connectors.md` documents only the realizations it owns; this table is
the cross-skill map.
