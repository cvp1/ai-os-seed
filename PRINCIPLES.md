# First principles

The deep, generative rules behind the CC fleet — a single-operator home-automation
and monitoring system run largely by an AI-OS. The conventions in `CLAUDE.md` and
the tactics in per-project auto-memory follow from these. When a decision isn't
covered by a specific convention, reason from here.

## Truth over status
1. **Distrust green.** A passing homegrown healthcheck is a claim, not proof —
   flap-and-self-heal reads HEALTHY at any sampling instant. When the operator says
   it's down, believe him and check ground truth (local-API freshness, the device
   itself), not the dashboard. A vendor "device offline" usually means the cloud
   uplink wedged while the device is locally fine.
2. **Lead with what's refuted.** Foreground the unverified and the disconfirming as
   prominently as the confirmed, and spend the next step closing the biggest
   unknown — not re-confirming what you already believe.
3. **Check what exists before naming a gap.** The fleet is mature; grep the code and
   the run history before building or flagging something "missing." Most gaps are
   already filled.

## Fail safe
4. **Degrade toward safety.** Missing, corrupt, or locked state resolves to the safe
   default — the seed tier when the ledger is unreadable, a clear "🔒 locked" error
   when the vault is sealed — never a silent dangerous path. A broken ledger must
   never downtier.
5. **Self-heal over lock.** For races and drift, rebuild the contended file as a
   projection of collision-safe sources rather than guarding it with locks. Resolve
   on failure — cached and zero-cost on the happy path, active discovery (e.g.
   IP-by-MAC) only when something actually breaks — not eagerly.
6. **Automate asymmetrically by risk.** Automate the reversible, quality-safe
   direction; propose-only the risky or human-owned one. The tier loop auto-applies
   a REVERT (cheap→default) but only proposes a PROMOTE (default→cheap). Mutations of
   human-owned state are proposals, not actions.

## Signal, not noise
7. **Edge-trigger.** Alert on change and anomaly; stay silent in steady state.
   Allow-normal beats deny-unknown — model what ordinary looks like and page only on
   the deviation. A no-op cron emits nothing.
8. **Bound every loop and output.** Guarantee termination and cap size up front; an
   unbounded inline loop once wrote a 1.7 GB file.

## Data and cost
9. **Store facts, derive views.** Persist physical measurements plus an
   effective-dated rate table; compute money and other derived numbers at read time.
   Never freeze a dollar figure — rates change and history must still re-derive
   correctly.
10. **Right-size to the turn.** Run the cheapest tier that passes a zero-LLM
    structural gate; escalate up the ladder only on failure; reserve the frontier
    model for hard or tool-using turns. Let data, not code, hold the assignment — and
    prove non-inferiority before trusting it.
11. **First-party for sensitive; never a third party.** One data-class taxonomy
    everywhere — `public` < `internal` < `sensitive`. Sensitive data (e.g. {{REDACTED}}
    work email/calendar) may reach only **first-party** providers: the local Ollama
    node and Claude/Anthropic (decided 2026-07-08 — Claude counts as trusted first-party,
    so a `.21` outage falls back to Claude rather than going dark). It NEVER reaches a
    third party (DeepSeek, Gemini, OpenRouter), even under an explicit pin. Unknown
    data-class fails loud, never open.

## Architecture
12. **Small sharp tools on a shared spine.** Independent repos, one concern each,
    over a thin shared spine (`_lib`, the key vault, the cron manifest). stdlib-first
    for system scripts. A workspace, not a monorepo.
13. **Validate live.** A new agentic harness isn't done until it has run end-to-end
    once for real. Synthetic and isolated tool-call tests don't predict multi-step
    agentic fitness; the live shootout decides.

## Secrets
14. **Secrets never touch the transcript.** Read them from the fscrypt vault at the
    point of use, inject at egress, fail closed when the vault is locked. Never
    hardcode, echo, or commit a secret value.

## Working with the operator
15. **Recommend, don't poll.** For design and sequencing calls, give a decision and
    proceed; reserve questions for forks only the operator can resolve. Keep
    interactive answers tight — decision first; long-form goes to the vault.
