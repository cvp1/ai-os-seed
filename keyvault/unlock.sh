#!/usr/bin/env bash
# Unlock the ~/.key fscrypt vault after a reboot. Prompts for the vault
# passphrase. Run in a real terminal:  bash ~/Github/CC/keyvault/unlock.sh
# Idempotent — a no-op if already unlocked.
set -euo pipefail
KEY="$HOME/.key"

# The hermes gateway auto-starts at boot while the vault is still locked, so it
# comes up without HASS_TOKEN/TELEGRAM_BOT_TOKEN (now vault-only, see
# /etc/systemd/system/hermes-gateway.service.d/vault-secrets.conf). Restart it
# now that ~/.key/hermes.env is readable so HA + Telegram re-activate. Needs sudo.
# Only on the real-unlock path: it costs a sudo prompt and a gateway blip, and
# once the vault is open a fresh gateway already reads its secrets.
restart_hermes_gateway() {
  if systemctl list-unit-files hermes-gateway.service >/dev/null 2>&1; then
    echo "Restarting hermes-gateway to load its vault secrets..."
    if sudo systemctl restart hermes-gateway; then
      echo "  gateway restarted."
    else
      echo "  WARN: restart failed — run: sudo systemctl restart hermes-gateway" >&2
    fi
  fi
}

# Containers that bind-mount files out of ~/.key cannot start while the vault is
# locked: Docker's mount setup fails with "required key not available" and the
# container exits 255. `restart: unless-stopped` does NOT save it — the failure is
# at container-create time, so Docker gives up with restartCount=0 and the service
# is simply gone. That is exactly how the ranch-status dashboard (status-site,
# :8088, 12 secret mounts) sat dead for 4 days after the 2026-07-27 reboot while
# every other container came back — none of the others mount ~/.key.
#
# Discriminator for "restart this": mounts ~/.key, not running, has a restart
# policy (so it was meant to be up), and exited NON-ZERO. The exit code is what
# keeps this from fighting Craig — a deliberate `docker stop` exits 0 or 137, a
# locked-vault mount failure exits 255. We never start something stopped on purpose.
restart_vault_containers() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || { echo "  (docker not reachable — skipping container repair)"; return 0; }

  local name mounts running code policy
  while read -r name; do
    [ -n "$name" ] || continue
    mounts=$(docker inspect "$name" --format '{{range .Mounts}}{{.Source}}
{{end}}' 2>/dev/null | grep -c "^$KEY/" || true)
    [ "${mounts:-0}" -gt 0 ] || continue
    running=$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || echo true)
    [ "$running" = "false" ] || continue
    code=$(docker inspect "$name" --format '{{.State.ExitCode}}' 2>/dev/null || echo 0)
    policy=$(docker inspect "$name" --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null || echo no)
    if [ "$policy" = "no" ] || [ "$policy" = "" ]; then
      echo "  · $name is down but has no restart policy — leaving it alone."
      continue
    fi
    if [ "$code" = "0" ] || [ "$code" = "137" ]; then
      echo "  · $name is down with exit $code (looks deliberately stopped) — leaving it alone."
      continue
    fi
    echo "Restarting $name (vault-mounted, exited $code)..."
    if docker start "$name" >/dev/null 2>&1; then
      echo "  ✓ $name started."
    else
      echo "  WARN: docker start $name failed — check: docker logs $name" >&2
    fi
  done < <(docker ps -a --format '{{.Names}}' 2>/dev/null)
}

# The container repair is re-drivable on purpose: if a restart failed, or a new
# casualty turns up hours later, re-running unlock.sh has to redo it — so the
# "already unlocked" path does the sweep too instead of exiting early. It needs
# no sudo and only touches containers that crashed, so it is safe to repeat.
if [ -f "$KEY/.vault_unlocked" ]; then
  echo "Vault already unlocked — re-running the container sweep."
  restart_vault_containers
  exit 0
fi
if ! fscrypt status "$KEY" >/dev/null 2>&1; then
  echo "$KEY is not an fscrypt vault (run 02-migrate-key.sh first)." >&2; exit 1
fi
fscrypt unlock "$KEY"
if [ -f "$KEY/.vault_unlocked" ]; then
  echo "Unlocked. Secret-dependent cron jobs will work until the next reboot."
  restart_hermes_gateway
  restart_vault_containers
else
  echo "WARNING: unlock reported success but canary missing — check the vault." >&2
  exit 2
fi
