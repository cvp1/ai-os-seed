# memory-mesh — SPEC v3 (2026-07-27)

Distributed, event-sourced memory for the fleet's AI agents. Vendor-agnostic,
coordinator-free, plain-text + git-native. Designed against the failure modes
of 2026-07-27 (the ssh split-brain, the blocked-write knowledge loss, the
0-byte destruction, staleness-served-as-truth) and hardened by three
adversarial design reviews (Grok, 2026-07-27).

## Goals and non-goals

**Goals.** (1) An operator correction lands once and can never be outvoted by
a stale copy. (2) Parallel sessions on parallel hosts cannot silently hold
contradictory beliefs — contradictions are *detected, parked, and alerted*
within one replication+fold cycle. (3) Any surviving host can rebuild
everything. (4) The whole system survives an LLM-vendor swap: no model in any
hot path, all state plain text in git.

**Non-goals.** Not a fact store — **one home per fact** stands (FLEET.md seam
rule 1): facts live in their canonical artifact; events carry *assertions and
pointers*. Not a vector DB, not a graph DB, not millions of items.

## Invariants (constitutional)

1. **Single writer per log file.** `events/<host>.ndjson` is written by that
   host only, through the sanctioned producer only. Enforced three ways: the
   memory-write guard hook (only door), fold validation (event.host must
   match filename), and git itself (a merge conflict = the invariant broke —
   loud, never silent).
2. **Append-only.** No event is ever edited or removed; belief change is a
   new event with `supersedes`. Compaction preserves anything referenced.
3. **Fold reads committed blobs, never the working tree.** `git show
   HEAD:events/x.ndjson` — the hash chain guards what the fold consumes. (The
   0-byte incident corrupted a working tree while reporting clean; committed
   objects are the only checksummed surface.)
4. **Resolution is explicit, never temporal.** `supersedes: <event-id>` — a
   later timestamp never wins an argument by being later. Clock skew can
   misorder narrative, never correctness.
5. **No model in the fold.** The fold is deterministic Python: same logs in,
   same views out, on every host, forever. This is what makes replay real and
   the system vendor-proof.

## Event schema

```json
{"id":"<sha256 of host|session|ts|content, first 16 hex>",
 "ts":"2026-07-27T21:30:04Z","host":"{{REDACTED}}","session":"479df445",
 "kind":"assert|correct|lesson|denial|retract|propose-correct|update-pointer",
 "subject":"ssh-route/{{REDACTED}}","polarity":"exists|absent|n/a",
 "content":"...","home":"FLEET.md#reachability|null",
 "lineage":"operator-direct|contains-untrusted",
 "audience":"operator|family|shared",
 "confidence":"operator-stated|verified-live|inferred",
 "supersedes":"<event-id>|null","sig":"<ed25519|null>"}
```

- `id` makes the producer **idempotent**: a retried append is a duplicate id;
  the fold dedups by id. (Kafka's idempotent-producer property, done cheaply.)
- Per-writer order = **line number** in the host's own file (Kafka offsets).
  Cross-host display order = `(ts, host, line)`; `SUSPECT` flag on future
  timestamps; ordering is never load-bearing (invariant 4).

## Subject registry

`subjects.toml`, in this repo, with a test suite. Controlled vocabulary of
classes (`ssh-route/`, `endpoint/`, `install/`, `policy/`, `lesson/`, …) and
entity normalization (host slugs from FLEET.md). The fold parks any event
whose subject doesn't parse against the registry as `UNNORMALIZED` — naming
drift becomes visible instead of silently defeating contradiction detection.

## Producer — `memory_write.py emit`

Extends the existing sanctioned CLI (the one door; hook-enforced).
1. Runs the existing gates: fact-shape (operative fields only — `rule`/
   `description`/`hook`; evidence fields may cite literals), lineage,
   secret-scan.
2. Appends one line to `events/<host>.ndjson`; commits locally. Local disk +
   local commit — succeeds during any partition.
3. Best-effort nudge: `ssh <peer> systemctl --user start memory-fold` —
   garnish, timeout-bounded, never load-bearing.
4. `--sync` (operator corrections): after commit, block until at least one
   peer confirms the event id in its fetched log (bounded wait, loud
   failure). This is opt-in `acks=all` for the events that matter most.
5. A classifier-denied write emits a `denial` event — metadata only, never
   the denied content (the ledger must not become the bypass).

## Transport — git peer-to-peer over ssh, fetch-only full mesh

- Each host: working clone + remotes for both peers. **Nobody pushes.**
  Peers fetch via `git-upload-pack` over the verified ssh routes.
- **Fast-forward guard:** the fold asserts every fetched ref fast-forwards
  its last-seen sha (stored in `state/last-seen.json`). Non-FF = a peer
  rewrote history → refuse to merge, alarm. `receive.denyNonFastForwards`
  set everywhere anyway.
- Repo hygiene pinned: `.gitattributes` `* text eol=lf`, `core.ignorecase
  false` checks on the macOS node, `gc.pruneExpire=never` on events repos
  (peers may lag; nothing unreachable is ever pruned), `core.fsync=
  committed` (see Integrity).
- Latency rungs: fold timer per host (floor, minutes) → emit nudge (seconds)
  → `--sync` (confirmed replication).
- Build prerequisite: the three **reverse** ssh routes ({{REDACTED}}→{{REDACTED}},
  {{REDACTED}}→cvptp, cvptp→{{REDACTED}}); outbound-from-{{REDACTED}} three are
  verified live 2026-07-27.

## Consumer — `memory_fold.py` (timer + session-start-if-stale)

1. `git fetch` both peers; FF-guard; merge (cannot conflict; conflict =
   tripwire).
2. Read all logs **from HEAD blobs**; dedup by id; validate host↔filename,
   subject registry, schema.
3. Apply supersedes chains; **hold back untrusted lineage**; then the
   **contradiction rule pass** (deterministic, ~200 lines, no LLM):
   - `lineage: contains-untrusted` → **quarantined**: rendered to
     `QUARANTINE.md`, never served, and — the ordering matters — held out
     *before* the rule pass, so an untrusted claim can never park a served
     fact. If it could, one crafted page would silence any memory just by
     disagreeing with it. It still **alarms** when it disagrees with served
     content. Unknown/absent lineage quarantines and alarms.
     Promotion needs the operator's passphrase-gated key — `sign.py --promote
     <id>`, or a signature on the event itself. An agent cannot promote.
     *(Built 2026-07-30. Enforced write-side since Story 029 and ignored here
     for the whole life of the feature: the first untrusted fact ever written
     went straight into the always-on index while this document and the
     write-guard hook both described a QUARANTINE.md that did not exist.)*
   - opposing polarity, same subject → park both
   - >1 live non-identical `assert` on one subject → park all
   - >1 live non-identical `lesson` on one subject → park all (a lesson
     revision must supersede its predecessors — the producer resolves the
     chain automatically; identical restatements collapse to the earliest,
     never park). Lessons obey invariant 4 like everything else.
   - live event contradicting an operator-**signed** event → park + alert.
     Compared **across kinds**: until 2026-07-30 this rule only ever compared
     asserts with asserts and lessons with lessons, so a signed `correct` and
     an unsigned `lesson` disagreeing on one subject both served silently — and
     since lessons are 123 of 141 events, the tripwire was blind to the
     dominant kind. Found by drill 10.
   - `supersedes` pointing at a missing id (compaction bug tripwire) → alarm
4. Materialize per-audience views (operator sees `operator+shared`; family
   sees `family+shared`):
   - `MEMORY.md` — generated, never hand-edited; behavioral rules + pointers;
     ≤20 KB budget with strict eviction order: operator-pinned →
     operator-signed → correction-history → breadth (distinct sessions per
     subject; raw recall counts are gameable) → recency last.
   - `CONFLICTS.md` — parked subjects, served only as UNRESOLVED.
   - `QUARANTINE.md` — untrusted-lineage facts, not served, with the promotion
     command. Rendered TWICE from one verdict set, exactly as the served index
     is: once as a mesh view and once into the store beside the harness index.
     One writer, so they cannot disagree — a rendering, not a duplicate. Slugs
     and one-line hooks only; bodies never appear in either.
   - A nonzero quarantine count prints one line in the harness `MEMORY.md`, and
     quarantined slugs are dropped from the on-demand appendix. **Withheld must
     never be indistinguishable from absent, and advertising a withheld fact is
     serving it in the weak sense** — both halves, or the control fails in one
     direction or the other.
   - `/recall` serves a TOMBSTONE for a quarantined slug, never the body: it
     stays retrievable (its one-line description is kept, so a query still finds
     it and the operator learns it exists) while the prose an attacker controls
     at length never reaches the context. Guarding the force-loaded index alone
     is not quarantine — `/recall` is the path an agent uses *deliberately*.

**Lineage epistemology (state it, or the next writer gets it wrong):**
**events are the fact.** A memory file's `lineage:` frontmatter and every
`QUARANTINE.md`/index listing are fold PROJECTIONS of the event log.
`memory_write.py` may set frontmatter only as an optimistic mirror of the event
it emits in the same breath. A tool that writes frontmatter and skips the event
leaves the fold blind and the memory served. *(This paragraph exists because a
2026-07-30 design proposal asserted the opposite — "frontmatter is the fact" —
and Grok 4.5's review caught that the sentence itself would produce the next
bypass.)*
   - denial ledger view (review / force-accept queue).
   - `view.version` — sha256 of folded state (the staleness contract).
5. Edge-triggered: new parks or alarms page via owner-alerts; a no-op fold
   emits nothing.

## Reads

Recall serves local views (no network). Fact subjects: fetch the **home**
live and serve that — the copy is never the answer. Parked subjects:
`UNRESOLVED`, never truth. Everything age-stamped.

**Staleness enforcement (needs operator authorization — hook):** a
PreToolUse hook compares `view.version` mtime/hash against a threshold and
triggers a re-fold when stale. Closes the mid-session window mechanically;
"agents should re-read" is not a mechanism.

## The three differentiators

1. **Time-travel replay.** `memory_replay --at <ts> [--subject S]`: check out
   the events repo at that moment, fold events ≤ ts, print the exact view any
   agent had. Free byproduct of git + deterministic fold; mutable-DB systems
   (Letta/Zep class) cannot do this.
2. **Gardener via `propose-correct`.** Maintenance agents may only propose;
   the operator's Ed25519 signature (cc-handoff signed-verb machinery)
   converts a proposal to a `correct`. Agents never mutate truth directly.
3. **Home change detector.** A timer on EVERY host hashes the canonical
   homes (FLEET.md, workspace CLAUDE.md, registered OPS.md files); on change
   the first noticer emits `update-pointer` (superseding prior notices) and
   the rest adopt the live event silently — mechanical enforcement of
   one-home-per-fact's weak edge (home moved, pointers stale) with no
   single-watcher coordinator.

## Integrity — where this falls short of Kafka, stated honestly

| Kafka guarantee | This design | Gap + mitigation |
|---|---|---|
| `acks=all`: write acked only after N replicas hold it | Ack after **local** append+commit (acks=1). Host disk dies before any peer fetches → those events are gone | The one real durability gap. Window = fold interval, seconds with nudge. Mitigation: `--sync` for operator corrections (block until a peer confirms); hourly cold backup. Accepted for lessons; not accepted for corrections — hence `--sync` |
| Idempotent producer (no dupes on retry) | Not native — a retried CLI append could double-write | Closed by content-derived `id` + fold dedup |
| Per-partition total order, broker-enforced | Per-writer order by line number — equivalent | No gap. Cross-partition order: Kafka doesn't promise it either — parity |
| Record-batch CRC verified broker & consumer | Git SHA-1 chain on **committed** objects — cryptographically stronger in transit/at rest | Two edges: (1) the working file between append and commit is unchecksummed — why invariant 3 reads HEAD blobs only, and why `core.fsync=committed` is required (git's default fsync is weaker than Kafka's replication-backed durability; power loss can eat the newest commit on default configs, esp. macOS); (2) SHA-1 collisions are academic-practical — repos can be `sha256` object-format if warranted |
| Broker-side log compaction with protocol-defined semantics | Compaction is an application script — a bug diverges views silently | Closed by **checkpoint verification**: compaction emits a snapshot event carrying the sha256 of the folded state at the cut; every peer re-folds raw events and must reproduce that hash before honoring the truncation. Divergence = alarm, not drift |
| Consumer offsets tracked durably (at-least-once) | Stateless full replay every fold | **Stronger** at this scale: no offset bugs possible; cost O(corpus), fine for thousands of events |
| Controlled membership (controller quorum) | Static 3-host config; joining = manual | Accepted: membership changes are operator acts in this fleet by design |
| Availability during broker loss via leader election | Multi-master: every host always writes locally | **Stronger** on availability; the price is the acks=1 window above — that's the trade, named |

Net: the one gap that matters is **ack-before-replication**; `--sync` exists
precisely to buy Kafka-grade durability for the events that deserve it, and
everything else is parity or better at this scale — with two Kafka-beating
properties (deterministic full-replay consumers, cryptographic history).

## Drills (nothing is done until these pass live)

1. **Split-brain replay:** emit the real 14:30 `correct` and 15:19 `assert`
   pair on two hosts; prove both park on all three hosts + one alert fires.
2. **Partition:** disconnect a host, emit on all three, reconnect → converge,
   no loss, no conflict.
3. **Kill test:** `kill -9` the producer mid-emit; re-run; prove no
   duplicate (id dedup) and no torn line (append is one `write()`).
4. **Non-FF attack:** rebase one host's events repo; prove every peer
   refuses + alarms.
5. **Replay determinism:** fold the full corpus on all three hosts; byte-
   identical `view.version` everywhere.
6. **Compaction checkpoint:** compact on one host; prove peers verify the
   hash before truncating; corrupt the snapshot; prove alarm.
7. **Lineage quarantine:** emit an untrusted-lineage fact that contradicts a
   served one; prove it is absent from `INDEX.md`, present in `QUARANTINE.md`,
   cannot park the subject it contradicts, still alarms — then promote it with
   the key and prove it serves fleet-wide, and that promoting *over* a served
   fact warns and parks rather than winning quietly. (`drill.py 10`.)

## Build order

1. Schema + subject registry + `emit` (with id, gates, denial events).
2. Fold: fetch/FF-guard/merge, dedup, rule pass, per-audience views,
   `view.version`, alerts. Drill 1 + 5.
3. Mesh: reverse ssh routes (operator hands), remotes, timers. Drill 2.
4. `--sync`, kill/non-FF hardening. Drills 3 + 4.
5. Compaction + checkpoints. Drill 6.
6. Replay, gardener (`propose-correct` + signatures), home watcher.
7. Staleness hook + cutover: today's store becomes the first materialized
   corpus; `MEMORY.md` flips to generated-only.

## What this replaces / keeps

Keeps: one-home-per-fact, the write-guard hook, the fact-shape gate (field-
scoped), lineage quarantine, recall's home-fetching. Replaces: hand-edited
MEMORY.md (generated), ad-hoc cross-host memory drift (the mesh), silent
classifier denials (denial events), trust-by-prose (audience views).
