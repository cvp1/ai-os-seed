# Proposals — human-applied exact-diff repairs

A middle tier between an agent quietly running things and a full governed
action broker: your agent writes an exact, hash-bound intent to a file and
stops; you review it and apply it with one command. Nothing executes inside
the agent's own turn — `install.py` is the only thing that ever writes the
target file, same as `--approve claude-md` and `--approve mesh-bootstrap`.

## When your agent should propose one

Today, scoped narrowly on purpose: **scheduler-entry changes** —
`scheduler/manifest.yml` only. If your agent notices scheduler drift (a job
missing an entry, a stale schedule, a command that needs fixing) and wants
to repair it, it writes a proposal instead of editing the file directly.

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
it before running anything. Widening this beyond scheduler entries to
other reversible filesystem actions is a deliberate later step (see
`BACKLOG.md` SEED-073 in the cc-seed repo, if you have it), not something
this build does — this is the pattern proving itself first.
