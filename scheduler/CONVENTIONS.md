# Scheduler conventions

Two rules make a plain-crontab/launchd scheduler behave like a fleet instead
of a pile of unrelated jobs. Both are distilled from the same convention
running CC's own hermes-backed scheduler; neither is hermes-specific.

## 1. Exit semantics — found work vs breakage

A job that **succeeds at finding or handling a problem** — a watchdog that
heals something, a drift checker that reports drift, `freshness.py` itself —
should exit **0** and print a first stdout line beginning `FINDINGS: <one-line
summary>`, with detail below it if useful. **Non-zero is reserved for
genuine breakage**: a crash, an unreachable dependency, a heal action that
itself failed.

Why this matters more here than it might look: without the convention,
found-work jobs and genuinely broken jobs are indistinguishable in
`runs.db` — both show up as "something happened," and `freshness.py`'s
FAILING status (triggered by a non-zero exit) can't tell them apart. With
the convention, `ok=0` in `runs.db` means *actually broken*; a `FINDINGS:`
line means *the job worked and found something worth telling you about*.
Nothing downstream — freshness, a report, a future dashboard — needs to be
taught the difference per job; `ok` just tells the truth.

## 2. Jobs must self-notify

`crontab` only emails you on non-empty stdout if `MAILTO` is set in the
crontab AND local mail delivery actually works on this machine — neither is
true by default on most fresh installs. `launchd` doesn't notify you at all
by default. **Neither scheduler is a notification channel** — if a job needs
to reach you (a real alert, not just a `runs.db` row), it must send that
notification itself, from inside its own script, over whatever channel you
actually check (email, a webhook, a Telegram bot — your choice, not the
scheduler's).

The corollary: `runs.db` + `freshness.py` are your **pull** channel (you or
a dashboard checks them), and self-notify is the **push** channel (the job
tells you unprompted). A job that matters usually wants both — log the run
so history exists, and notify so you don't have to go looking.

## The one demo job

`demo/hello_fleet.py` follows both conventions in miniature: it always
prints exactly one summary line (there's nothing for it to find or heal, so
it always exits 0 with a plain status line rather than a `FINDINGS:` one),
and it deliberately does NOT self-notify — it's a heartbeat you check via
`freshness.py`, not an alert. Point `scheduler/manifest.yml` at it, run
`sync.sh`, and you have a live example of both ends of the spine before you
write your first real job.
