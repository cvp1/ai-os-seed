#!/usr/bin/env bash
# Encrypt ~/.key in place with fscrypt, using a passphrase protector.
# Run as your NORMAL user (not root), in a real terminal — fscrypt will prompt
# you to set the vault passphrase:
#
#   bash ~/Github/CC/keyvault/02-migrate-key.sh
#
# fscrypt can only encrypt an EMPTY dir, so this does a safe copy-swap:
#   plaintext ~/.key  ->  temp backup  ->  fresh encrypted ~/.key  ->  copy back
#   ->  shred the temp backup. A pre-encryption tarball is kept until you've
#   confirmed everything reads, then you delete it (the script tells you how).
set -euo pipefail

KEY="$HOME/.key"
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK="$HOME/.key.plain.$STAMP"          # working copy (plaintext, shredded at end)
TAR="$HOME/.key.pre-fscrypt.$STAMP.tar"  # safety tarball (you delete after verify)
CANARY=".vault_unlocked"

[ "$(id -u)" -eq 0 ] && { echo "Run as your normal user, not root." >&2; exit 1; }
command -v fscrypt >/dev/null || { echo "Run 01-setup-fscrypt.sh first." >&2; exit 1; }
[ -d "$KEY" ] || { echo "No $KEY to migrate." >&2; exit 1; }

# Already encrypted? (fscrypt status succeeds on an encrypted dir)
if fscrypt status "$KEY" >/dev/null 2>&1; then
  echo "$KEY is already an fscrypt vault. Nothing to do."; exit 0
fi

echo "== backing up current secrets =="
cp -a "$KEY" "$BAK"
tar -C "$HOME" -cf "$TAR" "$(basename "$KEY")"
chmod 700 "$BAK"; chmod 600 "$TAR"
SRC_N="$(find "$BAK" -maxdepth 1 -type f | wc -l)"
echo "   $SRC_N secret files backed up -> $BAK (working) and $TAR (safety)"

echo "== creating fresh encrypted ~/.key =="
rm -rf "$KEY"; mkdir -m 700 "$KEY"
# Prompts for the NEW vault passphrase (source: custom_passphrase).
fscrypt encrypt "$KEY" --source=custom_passphrase --name="key-vault"

echo "== restoring secrets into the encrypted dir =="
cp -a "$BAK"/. "$KEY"/
printf 'ok\n' > "$KEY/$CANARY"        # plaintext-named canary for vault_locked()
chmod 700 "$KEY"; find "$KEY" -type f -exec chmod 600 {} +
DST_N="$(find "$KEY" -maxdepth 1 -type f ! -name "$CANARY" | wc -l)"
echo "   restored $DST_N files (expected $SRC_N)"
[ "$DST_N" -eq "$SRC_N" ] || { echo "COUNT MISMATCH — backup kept at $BAK, investigate." >&2; exit 4; }

echo "== shredding the working plaintext copy =="
find "$BAK" -type f -exec shred -u {} + 2>/dev/null || rm -rf "$BAK"
rm -rf "$BAK"

cat <<EOF

DONE — ~/.key is now an encrypted fscrypt vault (currently UNLOCKED).

Verify a secret still reads, e.g.:
  python3 -c "import sys;sys.path.insert(0,'$HOME/Github/CC');from _lib import secrets;print(bool(secrets.load_secret('X','~/.key/ha_token','ha token')))"

Once you've confirmed jobs work, delete the safety tarball:
  shred -u $TAR

AFTER EVERY REBOOT the vault starts LOCKED. Unlock it with:
  bash ~/Github/CC/keyvault/unlock.sh
(The freshness check will flag any cron job that ran while it was still locked.)
EOF
