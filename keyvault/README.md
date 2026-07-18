# keyvault — encryption & rotation for `~/.key`

All CC secrets live in `~/.key/` (read via `_lib/secrets.py`). This dir holds the
tooling that protects them **at rest** and the runbook to **rotate** them.

## Threat model (why this exists, and its limits)

This host has **no TPM** and the root fs (`/dev/nvme0n1p2`, ext4) had **no
full-disk encryption** — so a stolen/imaged/RMA'd NVMe exposed every secret in
plaintext. That offline-exposure gap is what fscrypt closes.

It does **not** protect against a live attacker who is already running as
`{{REDACTED}}` while the vault is unlocked — cron needs the secrets unattended, so
between unlock and reboot they're readable by this user's processes by design.
Per-file GPG/age was rejected for exactly this reason: with no hardware root of
trust, the auto-decrypt key would sit next to the data and add fragility for no
real gain. fscrypt's passphrase is **never stored on disk**, so offline media is
useless without it.

## Layout

| File | Run as | What |
|---|---|---|
| `01-setup-fscrypt.sh` | **root** (sudo) | install fscrypt, enable ext4 `encrypt`, `fscrypt setup / --all-users`. One time. (`--all-users` makes `/.fscrypt` world-writable so your non-root login can create the vault; it stores only wrapped key material, useless without the passphrase.) |
| `02-migrate-key.sh` | you | copy-swap `~/.key` into an encrypted vault w/ a passphrase. One time. |
| `unlock.sh` | you | unlock the vault **after every reboot**. |
| `ROTATION.md` | — | how to rotate each credential if one is ever exposed. |

## First-time setup (in a real terminal — these prompt for sudo + passphrase)

```bash
sudo bash ~/Github/CC/keyvault/01-setup-fscrypt.sh
bash      ~/Github/CC/keyvault/02-migrate-key.sh      # set a STRONG vault passphrase; store it in your password manager
# verify a job, then:  shred -u ~/.key.pre-fscrypt.*.tar
```

## After every reboot

The vault starts **locked**. Until you unlock it, any secret-reading job fails
with a clear `🔒 ~/.key is locked` message (from `_lib/secrets.py`), and the
**freshness check** (`cron` job `826de76e370d`) flags the stalled jobs.

```bash
bash ~/Github/CC/keyvault/unlock.sh
```

Reboots here are ~weekly, so this is a roughly-weekly chore. The key lands in the
filesystem keyring (fscrypt **v2** policy on kernel 6.17), so a single unlock
covers all of this user's processes — including hermes cron — until the next
reboot. No per-job change needed.

## Login reminder (in `~/.bashrc`)

So a post-reboot lock never surprises you, interactive shells print a warning
when the vault is locked. `~/.bashrc` isn't tracked in a repo, so the snippet is
kept here for recovery:

```bash
# --- keyvault: warn at login when the ~/.key fscrypt vault is locked ---
if [[ $- == *i* ]] && [ -d "$HOME/.key" ] && [ ! -e "$HOME/.key/.vault_unlocked" ] && [ -n "$(ls -A "$HOME/.key" 2>/dev/null)" ]; then
    printf '\033[33m🔒 ~/.key vault is LOCKED\033[0m — cron secrets unavailable until you run: \033[36mbash ~/Github/CC/keyvault/unlock.sh\033[0m\n'
fi
```

## Detecting reads (removed — was too noisy)

There used to be an audit alerter (`audit_alert.py` on a 10-min systemd timer +
an auditd `keyvault_read` watch) that Telegram-paged on "theft-shaped" reads of
`~/.key`. **Removed 2026-07-03 — too noisy** (even with the allowlist heuristic;
its worst run logged ~393k false positives in 4 days when status-site's container
mounts shared the key inode). fscrypt still protects the bytes **at rest**, which
is this dir's actual job; live read-detection while unlocked was never a strong
control on a no-TPM host anyway. The source is recoverable from git history if a
tuned version is ever wanted.

Unrelated leftover that's still good hygiene: containers get secrets via a
read-only `/run/<name>` mount + a `*_FILE` env var (see
`status-site/docker-compose.yml`) — keeps host and container secret paths clean.

## Don't undo the protection

- Never copy `~/.key` (or an unlocked snapshot) into git, `~/notes` (Syncthing),
  or a backup that leaves the host. The vault only protects the bytes on *this*
  disk.
- The vault passphrase is the single point of failure — keep it in your password
  manager, not on this box.
