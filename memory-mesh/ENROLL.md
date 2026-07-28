# Enrolling more machines in the memory mesh

Out of the box the mesh runs SOLO: one machine, one event log, a fold timer,
and a generated memory index. Everything below is optional and becomes
relevant only when you add a second machine and want them to share one set
of beliefs.

## What enrollment gives you

- Every machine carries every machine's events (peer-to-peer git fetch over
  ssh — nobody pushes, there is no server).
- A contradiction between machines PARKS on all of them within one fold
  cycle instead of coexisting silently.
- Any surviving machine can rebuild everything.

## Prerequisites

1. ssh works both directions between every pair of machines, by alias
   (`~/.ssh/config` entries; a dedicated key is fine — see below).
2. This repo (the seed's `memory-mesh/` component) is present on the new
   machine (it arrives with a normal seed install, or copy the directory).

## Enroll a machine

1. On EVERY machine, edit `memory-mesh/mesh.toml`: add one `[[hosts]]` entry
   per mesh member — including the machine it's on. Keep names stable; they
   become event ownership (`events/<name>.ndjson`, single-writer).
2. On the NEW machine:

       SEED=<an-existing-host> ./memory-mesh/install.sh

   This clones the events repo from the seed host (a shared root commit is
   what makes "unrelated histories" a refusable attack), wires peer remotes,
   and schedules the fold timer.
3. On the EXISTING machines, re-run `./memory-mesh/install.sh` once — it is
   idempotent and only adds the new peer remote.
4. Verify: after one fold cycle, `view.version` (in the events repo root)
   is byte-identical on every machine. If it isn't, a machine is missing
   events or code — compare `git -C ~/memory-events log` across hosts.

## Optional: signed truth

Unsigned, the mesh still detects and parks contradictions — it just can't
defend one side as authoritative. To enable operator-signed events:

1. Generate a passphrase-protected Ed25519 identity key (NOT your ssh auth
   key): `ssh-keygen -t ed25519 -f <keydir>/signing_ed25519`
2. Publish the `.pub` half in an `allowed_signers` file (one line:
   `<your-id> <pubkey>`), synced to every machine, and point
   `MESH_ALLOWED_SIGNERS` at it (or place it at a probed default — see
   `mesh_lib.py`).
3. Set your identity: `export MESH_SIGNER=<your-id>` and
   `MESH_SIGNING_KEY=<keydir>/signing_ed25519` (your id must match your
   `allowed_signers` line).
4. Sign resolutions with `sign.py`. The passphrase is the authority
   boundary: an agent can propose (`propose-correct`), only you can promote.
   For a bounded working window: `ssh-add -t 8h <keydir>/signing_ed25519`.

## Notes

- `emit.py --sync` (block until a peer confirms replication) is meaningful
  only with peers enrolled; solo it reports no confirmation.
- The drills (`drill.py`) build a disposable three-node mesh in a temp
  directory — they prove the multi-host behavior without touching your live
  events, and they run the same on a solo machine.
