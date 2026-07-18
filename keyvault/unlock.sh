#!/usr/bin/env bash
# Unlock the ~/.key fscrypt vault after a reboot. Prompts for the vault
# passphrase. Run in a real terminal:  bash ~/Github/CC/keyvault/unlock.sh
# Idempotent — a no-op if already unlocked.
set -euo pipefail
KEY="$HOME/.key"

if [ -f "$KEY/.vault_unlocked" ]; then
  echo "Vault already unlocked."; exit 0
fi
if ! fscrypt status "$KEY" >/dev/null 2>&1; then
  echo "$KEY is not an fscrypt vault (run 02-migrate-key.sh first)." >&2; exit 1
fi
fscrypt unlock "$KEY"
if [ -f "$KEY/.vault_unlocked" ]; then
  echo "Unlocked. Secret-dependent cron jobs will work until the next reboot."
  # The hermes gateway auto-starts at boot while the vault is still locked, so it
  # comes up without HASS_TOKEN/TELEGRAM_BOT_TOKEN (now vault-only, see
  # /etc/systemd/system/hermes-gateway.service.d/vault-secrets.conf). Restart it
  # now that ~/.key/hermes.env is readable so HA + Telegram re-activate. Needs sudo.
  if systemctl list-unit-files hermes-gateway.service >/dev/null 2>&1; then
    echo "Restarting hermes-gateway to load its vault secrets..."
    if sudo systemctl restart hermes-gateway; then
      echo "  gateway restarted."
    else
      echo "  WARN: restart failed — run: sudo systemctl restart hermes-gateway" >&2
    fi
  fi
else
  echo "WARNING: unlock reported success but canary missing — check the vault." >&2
  exit 2
fi
