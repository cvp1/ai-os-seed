# Proposals — human-applied exact-diff repairs

A middle tier between an agent quietly running things and a full governed
action broker: your agent writes an exact, hash-bound intent to a file and
stops; you review it and apply it with one command. Nothing executes inside
the agent's own turn — `install.py` is the only thing that ever writes the
target file, same as `--approve claude-md` and `--approve mesh-bootstrap`.

## When your agent should propose one

The allowlist (SEED-077 widening, 2026-09-02 — after three incident-free
weeks scoped to the manifest alone):

- `scheduler/manifest.yml` — scheduler drift: a missing entry, a stale
  schedule, a command that needs fixing.
- `observability/freshness.json` — cadence registration for jobs, so a
  manifest proposal's sibling cadence line travels the same governed lane.
- `.claude/settings.json` — self-modification (hooks, permissions, env).
  Allowlisted by Craig's explicit ruling (2026-09-02, verbatim: "I am
  approving the change to settings.json as long as the changes are
  clearly communicated to me before making them"), not by agent
  inference — the first submission's circular rationale was rejected in
  review. The condition is mechanical: settings.json is
  **single-apply-only** — `--apply-proposals` defers it; its full
  unified diff prints in `--review-proposals` (status `SOLO`) and again
  at `--apply-proposal` time; and the apply writes nothing unless it
  carries `--confirm TOKEN`, a prefix of the after-content hash that is
  printed only beside that diff. Showing and writing are two commands by
  construction, and the confirmation is bound to the exact bytes shown.

Named files, not a class: every entry must be reversible through this
mechanism, owned by the system itself (never human-owned state), and
validatable before the write. Each allowlisted target has a validity check
that its replacement bytes must pass **before** apply — JSON targets must
parse, the manifest must keep its `jobs:` key, contain no tabs, and have
no duplicate job names. A proposal that fails its check is refused, not
applied-then-discovered.

## The schema

A proposal is one JSON file at `<ROOT>/.cc-seed/staged/proposals/<slug>.json`:

```json
{
  "target": "scheduler/manifest.yml",
  "before_sha256": "sha256:<hex of the CURRENT file's exact bytes>",
  "before_content": "<the CURRENT file's exact text, so a revert is possible later>",
  "after_content": "<the FULL proposed replacement text>",
  "rationale": "one line: why this change"
}
```

`before_sha256` must be `sha256:` + the hex digest of `before_content`
encoded as UTF-8 — `install.py` recomputes it and refuses the proposal if
they don't match, and separately refuses to apply if the *live* file's hash
doesn't match `before_sha256` (someone or something changed it since the
proposal was written — the proposal is stale, not applied).

`after_content` is the **entire** file, not a patch. You see the exact
bytes that will land, the same discipline `--approve claude-md` already
uses — no diff-apply tool's own correctness to trust.

## Reviewing and applying the queue (SEED-077)

```
python3 install.py --target <ROOT> --review-proposals   # read-only: READY/HELD per proposal, rationale, diff size
python3 install.py --target <ROOT> --apply-proposals    # apply everything READY; refusals skip and report; exit 2 if any refused
python3 install.py --target <ROOT> --apply-proposal SLUG --confirm TOKEN   # a SOLO item, with the token its diff printed
```

Batch is a loop over the audited single-apply path — every guard runs per
item. Two READY proposals against the same file apply in slug order; the
second goes stale the moment the first lands and is refused (re-propose
against the new content). The Monday `proposal_feed` output plus one
`--review-proposals` read plus one `--apply-proposals` run is the intended
weekly rhythm.

## Applying one

```
python3 install.py --target <ROOT> --apply-proposal <slug>
```

Refuses if: the target isn't on the allowed list, the proposal is
malformed (its own before_content doesn't hash to its own before_sha256),
or the target file has changed since the proposal was written. Otherwise
writes `after_content`, records the before/after hashes and rationale in
`.cc-seed/receipt.json`, and archives the proposal file to
`.cc-seed/staged/proposals/applied/<slug>.json` (kept, not deleted — the
audit trail and the revert path both read it back).

## Reverting one

```
python3 install.py --target <ROOT> --revert-proposal <slug>
```

Restores `before_content`, but only if the target still matches what
`--apply-proposal` wrote (`after_sha256`) — if something else changed the
file since, it refuses rather than clobbering a newer edit.

## What this is not

No daemon, no new process, no standing permission. Every apply is one
explicit command a human types, over content they can read first — the
proposal file sits there, plain text, for as long as you want to look at
it before running anything. Batch apply changes the review granularity
(one sitting instead of one command per fix), never the covenant: the
agent still stops at writing the file, and install.py still performs
every write. Widening further (any "reversible filesystem actions"
class) stays deliberate: new targets are code changes to
`PROPOSAL_ALLOWED_TARGETS` + `_PROPOSAL_CHECKS` in install.py, reviewed
like any code — never something a proposal can grant itself.
