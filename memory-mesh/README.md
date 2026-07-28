# memory-mesh

Distributed, event-sourced memory for the fleet's AI agents. Coordinator-free
(git p2p over ssh, fetch-only full mesh), vendor-agnostic (no model in any hot
path), plain text throughout. Design contract: `SPEC.md`. Proof obligations:
`drill.py` (all passing 2026-07-27).

    emit.py    append one event to THIS host's log (single-writer), commit, nudge
    fold.py    fetch peers (FF-guarded), merge, contradiction rules, materialize views
    replay.py  the exact view any agent had at any past instant
    drill.py   3-node mesh in a tempdir, runs the SPEC drills against real git
    install.sh idempotent join (Linux systemd / macOS launchd)

Events repo: `~/memory-events` (separate from this code repo; NOT on GitHub —
LAN-only mesh; covered by the standby sync + weekly snapshot like everything
else). Views (`views/`, `view.version`) are derived, per-host, never committed.

Join a host: clone this repo, then `MESH_HOST=<fleet-slug> ./install.sh`
(MESH_HOST only needed where slug ≠ hostname). Prereq: outbound ssh aliases to
both peers per `mesh.toml`.

Phase status vs SPEC build order: 1–4 built and drilled (schema/registry/emit,
fold+views+alerts, mesh transport, sync/kill/non-FF hardening). Phase 7 DONE
on {{REDACTED}} (2026-07-28, Craig's go): staleness hook installed, store
backfilled, and MEMORY.md flipped to fold-generated — per-host opt-in via a
`.mesh-generated` marker in the store (`memory_write.py flip-generated`);
memory_write/consolidate/reconcile key off the marker and stand down from
index editing. {{REDACTED}}/cvptp flip after backfilling their own corpus.
Phase 6 DONE (2026-07-28): replay, gardener promote, and the home watcher —
`home_watch.py` (hourly timer on EVERY host; first-noticer-wins suppression)
hashes the registered canonical homes (`[[homes]]`) and emits a superseding
`update-pointer` on change/missing; edge-triggered, emit-confirmed-or-retry.
v1.7 (Grok review 4, priorities 1+2): lessons chain by explicit `supersedes`
(emit resolves; blind cross-host revisions PARK — drill 7), and torn log
tails are healed on append instead of eating the next event.
Pending: 5 only (compaction+checkpoints — logs are years from the size cap).

## Signing (v1.3)

Signed events are the only truth the fold defends: any unsigned event
disagreeing with a signed one on the same subject PARKS and ALARMS. Signing
uses the fleet's existing machinery — `ssh-keygen -Y` over the Ed25519 key in
`~/.key/signing/`, verified against `cc-handoff/allowed_signers` (one signer
registry for the whole fleet) — under the namespace `memory-mesh`, distinct
from cc-handoff's so a signed task can never replay as a signed event.

    sign.py --subject S --content C --home H --supersedes id1,id2   # signed truth
    sign.py --promote <proposal-id>                                 # gardener path

**Agents cannot sign.** The key is passphrase-protected inside the fscrypt
vault, so signing requires Craig at a terminal — that passphrase IS the
authority boundary. Agents may emit `propose-correct` events; only
`sign.py --promote` turns one into truth, and promotion supersedes every live
claim on that subject, so one operator act clears a whole park.

Verified at fold on every host independently (a forged `sig` string is caught
everywhere, not trusted because it looks signed) and drilled: drill 6 proves
an unsigned contradiction of signed truth parks fleet-wide, and that tampering
with signed content is detected and alarmed.
