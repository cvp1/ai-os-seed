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
| `google-connector` | Gmail + Calendar + Drive (**fleet hosts**) | *itself* (`google-connector/`) | draft-only |
| `gmail-mcp` | Gmail (claude.ai MCP — **phone/web only**) | `triage` | draft-only |
| `outlook-composio-personal` | hotmail personal (Composio) | `triage` | draft-preferred |
| `gcal-mcp` | Google Calendar (claude.ai MCP — **phone/web only**) | `triage` | gated writes |
| `drive` | Google Drive (claude.ai MCP — **phone/web only**) | `triage` | read, opportunistic |
| `proton-bridge` | Proton Mail (local Bridge) | `proton-mail` | dry-run-then-send |
| `outlook-composio-work` | {{REDACTED}} M365 (Composio) | `{{REDACTED}}` | **read-only** |

A skill's `connectors.md` documents only the realizations it owns; this table is
the cross-skill map.

### Google on a fleet host: `google-connector`, not the claude.ai MCP

*(2026-09-16, `decisions/google-capability-harness-wide-adopted-2026-09-16.md`.)*

On {{REDACTED}} and every other fleet host, Google is
**`python3 -m google_connector <tool>`** — one dispatch path that every surface
(CLI, stdio MCP, Claude Code skill, scheduled Python) is a generated view of.
`triage` and anything else that wants Gmail on a fleet host calls that, not the
hosted connector.

| Contract verb | Realized by |
|---|---|
| `list(filter)` | `google_mail_recent --count N` |
| `read(id)` | `google_mail_read --id gm-<id>` |
| `search(query)` | `google_mail_search --query "<gmail syntax>"` |
| `draft(id, body)` | `google_mail_draft_create --to … --subject … --body …` |
| `send(id, body)` | **not realized, and not realizable** — omitted from the tool table and denied by name. Mail sending is `_lib/mail.py` behind its two-factor scheduled gate. |
| `archive(id)` | **not realized** — same reason |
| `events(range)` | `google_calendar_list --days N` |
| calendar `create`/`update` | `google_calendar_create` / `_update` (etag-conditional; guest mail explicit, default none) |
| calendar `delete` | **not realized** — cancelling a meeting is a human decision with an audience |

Posture is `draft-only` and it is structural, not a manifest setting: there is
no send verb in the connector's vocabulary for a posture to be relaxed around.

**The claude.ai Gmail/Calendar/Drive connectors stay** — they are the
**phone and web** realization, where no local process exists. They are not the
fleet contract, and a fleet-host skill reaching for them is reaching past the
gate, the schema and the audit line.

The table above is hand-maintained; the connector's own half is generated —
`python3 -m _lib.connector.scaffold google --skill` renders the live tool list
into `cc-skills/google-connector/SKILL.md`, which is the authority on flags and
bounds if this table and that file ever disagree.
