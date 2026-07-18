#!/usr/bin/env bash
# One-time, host-level fscrypt enablement. Run this FIRST, in a real terminal
# (it needs sudo and may prompt). Idempotent: safe to re-run.
#
#   sudo bash ~/Github/CC/keyvault/01-setup-fscrypt.sh
#
# What it does:
#   1. installs the `fscrypt` package
#   2. verifies the root ext4 fs can hold encrypted dirs (256B inodes)
#   3. enables the ext4 `encrypt` feature flag if missing (online, no reboot on
#      modern e2fsprogs; aborts with guidance if it can't be set live)
#   4. runs `fscrypt setup` (system) and `fscrypt setup /` (per-filesystem)
# It does NOT touch ~/.key — that's step 02, run as your normal user.
set -euo pipefail

DEV="$(findmnt -no SOURCE /)"
echo "Root device: $DEV"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo: sudo bash $0" >&2; exit 1
fi

echo "== 1. install fscrypt =="
if ! command -v fscrypt >/dev/null; then
  apt-get update -qq && apt-get install -y fscrypt
else
  echo "   already installed: $(fscrypt --version 2>&1 | head -1)"
fi

echo "== 2. check inode size (need 256B) =="
ISIZE="$(dumpe2fs -h "$DEV" 2>/dev/null | awk -F: '/Inode size/{gsub(/ /,"",$2);print $2}')"
echo "   inode size: ${ISIZE:-unknown}"
if [ "${ISIZE:-0}" -lt 256 ]; then
  echo "   ABORT: 128B inodes can't hold encryption metadata. ~/.key fscrypt is" >&2
  echo "   not possible on this fs without a reformat. Stop here." >&2
  exit 2
fi

echo "== 3. ext4 'encrypt' feature =="
FEATS="$(dumpe2fs -h "$DEV" 2>/dev/null | awk -F: '/Filesystem features/{print $2}')"
if echo "$FEATS" | grep -qw encrypt; then
  echo "   already enabled"
else
  echo "   enabling encrypt feature on $DEV (online)..."
  if tune2fs -O encrypt "$DEV"; then
    echo "   enabled"
  else
    echo "   ABORT: could not set 'encrypt' online. Enable it from rescue media:" >&2
    echo "     sudo tune2fs -O encrypt $DEV   (with / unmounted)" >&2
    echo "   then re-run this script." >&2
    exit 3
  fi
fi

echo "== 4. fscrypt setup =="
# Global config /etc/fscrypt.conf.
fscrypt setup --force 2>/dev/null || true

# Per-filesystem metadata at the mount root. --all-users lets your NON-root
# login create/unlock the vault's protector (metadata dir becomes world-
# writable + sticky; it holds only wrapped key material, useless without the
# passphrase). v2 policy / fs keyring means one unlock then covers cron too.
if fscrypt setup / --all-users --force 2>/dev/null; then
  echo "   filesystem set up (all-users)"
else
  # "already setup" — fscrypt won't re-run, so verify it actually allows non-root.
  if [ "$(stat -c '%A' /.fscrypt 2>/dev/null | cut -c9)" = "w" ]; then
    echo "   already set up (all-users)"
  else
    # Root-only. Safe to recreate ONLY if no real vault metadata exists yet.
    nprot="$(find /.fscrypt/protectors -type f 2>/dev/null | wc -l)"
    npol="$(find /.fscrypt/policies -type f 2>/dev/null | wc -l)"
    if [ "$nprot" -eq 0 ] && [ "$npol" -eq 0 ]; then
      echo "   converting root-only setup to all-users (metadata empty, safe)..."
      rm -rf /.fscrypt
      fscrypt setup / --all-users --force
    else
      echo "   ABORT: / is set up root-only and already has protectors/policies." >&2
      echo "   Don't wipe them. Fix perms manually: sudo chmod -R o+rwt /.fscrypt" >&2
      exit 5
    fi
  fi
fi

echo "   /.fscrypt perms: $(stat -c '%A %U' /.fscrypt)  (world-writable = all-users OK)"

echo
echo "DONE. fscrypt is ready. Next, as your normal user (NOT root):"
echo "  bash ~/Github/CC/keyvault/02-migrate-key.sh"
