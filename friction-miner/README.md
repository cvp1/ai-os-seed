# friction-miner — the generator that proposes generators

A weekly, zero-LLM pass over work you already do by hand, surfacing **at most
one** automation candidate per run. It never builds anything; its whole output
is one row in `observability/data/friction-miner/candidates.jsonl` and a
`FINDINGS:` line in the run log.

What it reads (on this machine only, nothing leaves it):

| Detector | Evidence |
|---|---|
| D1 repeated command | the same command shape in 3+ sessions (a Claude Code `!` command or a dated terminal day), or 4+ times in undated shell history |
| D2 sequence | the same two-step pair, minutes apart, in 3+ sessions |
| D3 approve-with-edit | latent until a proposal audit trail records edits |
| D4 recurring hand edit | the same file hand-committed on 3+ separate days |

Shell history comes from `~/.bash_history` and `~/.zsh_history` (zsh's
extended `: <epoch>:<dur>;cmd` lines are dated), or from `FRICTION_HISTORY`
(paths separated by `:`). A command carrying anything secret-shaped is dropped
whole, never redacted-and-kept; evidence is command *shapes* and counts.

    python3 friction-miner/miner.py run --dry-run   # see what it would raise
    python3 friction-miner/miner.py status          # every candidate + its outcome
    python3 friction-miner/miner.py show <fp>       # evidence for one

To schedule it, uncomment the `friction_miner` example in
`scheduler/manifest.yml` and run `scheduler/sync.sh`. A fingerprint is raised
once, ever — a dismissed idea never comes back.
