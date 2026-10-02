#!/usr/bin/env bash
# Unlock the ~/.key fscrypt vault after a reboot. Prompts for the vault
# passphrase. Run in a real terminal:  bash ~/{{REDACTED}}/keyvault/unlock.sh
# Idempotent — a no-op if already unlocked.
set -euo pipefail
KEY="$HOME/.key"

# Restart {{REDACTED}}-gateway so it picks up its secrets from the now-readable vault.
# Needs sudo; only run on a real unlock.
restart_{{REDACTED}}_gateway() {
  if systemctl list-unit-files {{REDACTED}}-gateway.service >/dev/null 2>&1; then
    echo "Restarting {{REDACTED}}-gateway to load its vault secrets..."
    if sudo systemctl restart {{REDACTED}}-gateway; then
      echo "  gateway restarted."
    else
      echo "  WARN: restart failed — run: sudo systemctl restart {{REDACTED}}-gateway" >&2
    fi
  fi
}

# Restart containers that bind-mount ~/.key and failed to start while the vault
# was locked. Only containers with a restart policy that exited non-zero are
# touched; exit 0/137 means a deliberate stop and is left alone.
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

# The container sweep also runs when already unlocked, so re-running is safe.
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
  restart_{{REDACTED}}_gateway
  restart_vault_containers
else
  echo "WARNING: unlock reported success but canary missing — check the vault." >&2
  exit 2
fi
