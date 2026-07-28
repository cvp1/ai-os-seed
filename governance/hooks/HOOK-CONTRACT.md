# profile_gate.py — PreToolUse hook contract (SEED-062)

## Claude Code side (what we assume, and what we don't)

Claude Code's PreToolUse hook receives a JSON event on stdin and communicates
its decision back via stdout JSON and/or exit code. This hook targets the
`hookSpecificOutput.permissionDecision` form (`allow` / `deny` / `ask` +
`permissionDecisionReason`), with a fallback to the older `{"decision":
"block", "reason": ...}` shape for broader version compatibility. **This is a
best-effort implementation against the documented contract, not something
verified against every Claude Code release** — the `--live` conformance run
(SEED-063) against a real session is what actually proves it, per the wave's
validate-live gate. If the exact stdout schema drifts in a future Claude Code
version, this file is the one place to fix it.

**Input** (stdin, JSON):
```json
{
  "session_id": "...",
  "cwd": "...",
  "tool_name": "Bash",
  "tool_input": {"command": "git push origin main"}
}
```

**Output** (stdout, JSON) — one of:
```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}
```
or, for allow, print nothing and exit 0.

**Exit code:** 0 in all cases where stdout JSON carries the decision. A
non-JSON stdout + exit 0 is treated by Claude Code as "no opinion" (allow) —
which is exactly why the crash path below must NEVER just exit 0 silently.

## Our matching engine (documented simplification)

`classification.json` (SEED-061 output) lists `pattern -> {surface,
reversibility, tier, action}`. Patterns follow a small, explicit subset of
Claude Code's own permission-rule syntax:

- **Bare tool name** (`Edit`, `Read`, …) — matches any call to that tool.
- **`Tool(prefix:*)`** — matches if the tool's primary argument string
  (`command` for Bash, `file_path` for Edit/Write/NotebookEdit) starts with
  `prefix`.
- **`Tool(exact)`** — exact match (or suffix match, for file paths, so a
  relative path still matches an absolute one) against the primary argument.
- **Wildcard tool name** (`mcp__*mail*send*`) — no parens; `fnmatch` against
  `tool_name` itself, for connector families we can't enumerate by exact name.

This is **not** a claim to replicate Claude Code's own matcher exactly — it's
the seed's own, documented, testable subset. The conformance suite tests OUR
matcher's behavior because that's what ships.

## Decision ladder (in order)

1. **Self-protect** patterns (from `classification.json.self_protect_deny`)
   — always deny, checked first, independent of anything else.
2. **Classified match** — first pattern in `classification.json.entries`
   that matches wins; apply its `tier`:
   - `ACT` / `ACT_NOTIFY` → allow (write audit line; `ACT_NOTIFY` also queues
     a notify-digest entry)
   - `PROPOSE` → **deny the call**, write the staged artifact, tell the
     agent (via `permissionDecisionReason`) to surface the staged path and
     stop — never retry the call itself
   - `DRAFT_ONLY` / `NEVER` → deny, no staging (drafting happens through a
     *different*, already-allowed private-workspace tool — e.g. `Write` to
     a draft file — this tool call specifically is the one held back)
3. **Unclassified** (no pattern matches at all) → **deny**, reason names
   "default-deny: no classification rule for this call" (fail closed).

## Delegation — no override surface, by construction

`delegation.terminal_cell_governs: true` is enforced by NOT having a
mechanism for a parent call to relax the rules a child call is judged
against: every tool call — whether issued directly or via `Agent`/`Task`
fan-out — hits this same hook, reading the same `classification.json`. There
is no parent-context parameter that weakens a child's classification. A
sub-agent's own terminal action is judged exactly as if it were called
directly. (The `Agent`/`Task` tool call that *launches* a sub-agent is itself
classified normally too — typically `private_workspace`/`ACT` — but that
says nothing about what the sub-agent is later allowed to do.)

## Self-spend guard

State file `<audit_dir>/spend_state.json`: `{"month": "2026-07", "usd":
123.45, "budget_flipped": false}`. On each ACT/ACT_NOTIFY decision, the hook
adds the call's estimated cost (a caller-supplied stub in v1 — real token
costing is a harness integration point, out of scope here) to the running
total. Past `spend.alert_threshold_pct` → one-time stderr warning. The check
is **forward-looking, not retroactive**: the call that tips the month over
`spend.monthly_budget_usd` is itself still judged normally; every call
**after** that one is **downgraded to PROPOSE** (per `spend.on_exceed:
propose_all`), loudly, in the decision reason.

## Audit

Every decision (ALLOW / NOTIFY / STAGE / DENY) appends one JSONL line to
`<audit_dir>/<YYYY-MM>.jsonl`:
```json
{"ts": "...", "tool_name": "Bash", "args_hash": "sha256:...", "surface": "team_shared", "reversibility": "costly", "tier": "PROPOSE", "decision": "STAGE", "category": "staged", "undo_path": null}
```
`category` is a fixed, categorical slug (`allowed` / `staged` / `budget_downgrade` /
`draft_only_deny` / `default_deny` / `self_protect` / `mfa_deny`) — free-text
reasons are never logged, only this closed vocabulary, so the audit surface
(SEED-064) can report refusal breakdowns without risking data in the log.
Arguments are **hashed, never echoed** — the log must never carry secret
values. Append-only: the hook only ever opens the month file in append mode.

## Failure semantics

Any exception anywhere in the hook (missing classification.json, corrupt
JSON, unexpected stdin shape) is caught at the top level and converted to a
**deny** decision naming the failure — fail closed, never fail open. A
crashed hook must never be indistinguishable from "no opinion."

## Consent — this hook is only ever wired live at install time

This story builds and tests `profile_gate.py` entirely through
`gate_sandbox.py` (synthetic events, no real Claude Code session). It is
**never** added to this repository's own `.claude/settings.json`. Wiring it
into a recipient's settings, with their explicit consent shown as a diff, is
SEED-065's job.
