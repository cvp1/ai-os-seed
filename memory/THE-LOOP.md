# The loop

Why this system ships more than cron: an incident becomes a memory note,
a memory note becomes a convention, a convention becomes the default —
so the same problem doesn't cost a full diagnosis twice. This is CC's own
real practice, described here generically (the actual example: a vendor's
cloud dashboard once said a device was "not reporting" when the device
was fine locally and only the cloud uplink had wedged — after that
happened once, "distrust the cloud status, check locally first" became a
standing rule instead of a lesson re-learned on the next false alarm).

## The five arrows, and which shipped piece is which

```
   jobs run  ──log_run.py──▶  runs.db  ──/status──▶  operator
                                  │
                                  ▼
                          weekly.py (facts → NOW.md)

   incident / correction  ──/improve──▶  memory notes  ──/recall──▶  session
```

- **jobs → runs.db** (`observability/log_run.py`) — every scheduled run
  becomes a row: what ran, when, whether it worked. The floor everything
  else stands on.
- **runs.db → operator** (`/status`) — the one-verb answer to "how is my
  system doing", read-only, distrust-green by default.
- **incident → memory** (`/improve`) — a correction or a taught lesson
  this session becomes a note in `memory/`, not something re-explained
  next time.
- **memory → session** (`/recall`) — a later session asks "what do I know
  about X" and gets an answer with citations, not a shrug.
- **facts → derived views** (`views/weekly.py`) — a deterministic weekly
  projection over runs.db + git history, no LLM, the same "store facts,
  derive views" discipline the rest of this system follows.

## Why this is the whole point

A scheduler that runs jobs and a store that logs them is a monitoring
stack — useful, but it doesn't get smarter. The loop is what turns "cron
with receipts" into something that behaves like an operating system:
every session that uses `/improve` makes every later session that uses
`/recall` or `/status` a little better informed about *this* system,
specifically, without you re-explaining it.
