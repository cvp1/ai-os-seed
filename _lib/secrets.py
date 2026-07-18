"""Credential loading for the CC projects (stdlib-only).

Single source of truth for the ``env-var-fallback -> expanduser -> read().strip()``
pattern that was copy-pasted across ~8 scripts (solar-health, battery-health,
solar-direct, unifi-health, smarthub-usage, uptime-kuma, ...).
"""
import os
import sys

# ~/.key is fscrypt-encrypted (see keyvault/). When locked (e.g. just after a
# reboot, before anyone ran `keyvault/unlock.sh`) the directory exists but its
# plaintext entries are inaccessible. This canary is a plaintext-named marker
# written inside the encrypted dir at migration time: readable only while the
# vault is unlocked, so its absence is a reliable "vault is locked" signal.
KEY_DIR = os.path.expanduser("~/.key")
VAULT_CANARY = os.path.join(KEY_DIR, ".vault_unlocked")


class SecretError(RuntimeError):
    """Raised by load_secret(required=True, exit_on_error=False) on a miss."""


def vault_locked():
    """True if ~/.key is an encrypted vault that is currently locked.

    Returns False when the vault is unlocked, or when fscrypt was never set up
    (plain ~/.key with no canary that still holds real files) — in that case the
    normal not-found path handles a genuine missing secret.
    """
    if not os.path.isdir(KEY_DIR):
        return False
    if os.path.exists(VAULT_CANARY):
        return False  # unlocked
    # No canary. Only call it "locked" if the dir looks encrypted (has entries
    # but none are readable plaintext) — avoids false alarms on a pre-fscrypt box.
    try:
        entries = os.listdir(KEY_DIR)
    except OSError:
        return True
    if not entries:
        return False
    return not any(os.path.isfile(os.path.join(KEY_DIR, e)) for e in entries)


def load_secret(env_name, path, what="secret", required=True, exit_on_error=True):
    """Return a credential string (or ``None``).

    Resolution order:
      1. environment variable ``env_name`` (if set and non-empty), stripped;
      2. the file at ``path`` (``~`` expanded), stripped.

    On a miss when ``required`` (the default): ``sys.exit()`` with a one-line
    message, matching the CLI scripts' fail-fast behaviour. Pass
    ``exit_on_error=False`` to raise :class:`SecretError` instead, or
    ``required=False`` to return ``None``.
    """
    env = os.environ.get(env_name)
    if env and env.strip():
        return env.strip()
    if path:
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded):
            with open(expanded) as fh:
                val = fh.read().strip()
            if val:
                return val
    if not required:
        return None
    if vault_locked():
        msg = ("🔒 ~/.key is locked (fscrypt). Run `~/Github/CC/keyvault/unlock.sh` "
               "to unlock the secret vault, then retry — needed %s." % what)
    else:
        msg = "No %s: set $%s or populate %s." % (what, env_name, path)
    if exit_on_error:
        sys.exit(msg)
    raise SecretError(msg)
