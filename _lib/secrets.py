"""Credential loading for the CC projects (stdlib-only).

Single source of truth for the ``env-var-fallback -> expanduser -> read().strip()``
pattern that was copy-pasted across ~8 scripts (solar-health, battery-health,
solar-direct, unifi-health, smarthub-usage, uptime-kuma, ...).
"""
import json
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


class SecretShielded(SecretError):
    """Raised when a routed credential is requested inside a shielded session."""


# --- session shield ----------------------------------------------------------
# When $EGRESS_SHIELD=1, any credential that an egress-proxy route already
# injects is REFUSED here — callers must go through the proxy socket instead, so
# the value never enters the process (and never the transcript). This is opt-in
# and OFF by default: cron jobs and jailed workers don't set it, so their
# behaviour is byte-identical. The interactive Claude Code session sets it.
#
# It is not containment — the session keeps full network access and could still
# read ~/.key directly. It removes the *ordinary* path by which a credential
# reaches a prompt-injectable context. See egress-proxy/SPEC-session-shield.md.
SHIELD_ENV = "EGRESS_SHIELD"
ROUTES_JSON = os.environ.get(
    "EGRESS_ROUTES",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "egress-proxy", "routes.json"))
_shield_cache = {"mtime": None, "map": None}


def shield_active():
    return os.environ.get(SHIELD_ENV, "").strip() == "1"


def _shield_map():
    """{identifier -> route key} for every credential an egress route injects.

    Identifiers are BOTH the expanded ~/.key file path and the env-var name, so
    a caller is shielded however it asks. Built from routes.json's inject blocks
    — the route table stays the single source of truth, and a route added there
    is shielded here without a second edit.

    When several routes carry the SAME credential (``ha`` reads and ``ha-write``
    writes with one token), the FIRST one in routes.json wins the hint. Order the
    table read-route-first so the message names the route a caller most likely
    wants; either way the refusal is identical, only the suggestion differs.

    Raises SecretShielded if the route table can't be read: shield mode was
    explicitly asked for, and if we can't tell which credentials are routed the
    safe answer is to refuse loudly, not to hand the value over.
    """
    try:
        mtime = os.path.getmtime(ROUTES_JSON)
    except OSError as e:
        raise SecretShielded(
            "🛡 %s=1 but the route table is unreadable (%s: %s) — refusing to "
            "hand out any credential. Restore it, or unset %s."
            % (SHIELD_ENV, ROUTES_JSON, e.strerror, SHIELD_ENV))
    if _shield_cache["mtime"] == mtime:
        return _shield_cache["map"]
    try:
        with open(ROUTES_JSON) as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        raise SecretShielded(
            "🛡 %s=1 but the route table is unparseable (%s: %s) — refusing to "
            "hand out any credential. Fix it, or unset %s."
            % (SHIELD_ENV, ROUTES_JSON, e.__class__.__name__, SHIELD_ENV))
    mapping = {}
    for key, route in raw.items():
        if key.startswith("_") or not isinstance(route, dict):
            continue
        inj = route.get("inject")
        if not isinstance(inj, dict):
            continue          # keyless pass-through route — nothing to shield
        for field in ("file", "user_file", "pass_file"):
            val = inj.get(field)
            if val:
                mapping.setdefault(os.path.expanduser(val), key)   # first route wins
        for field in ("env", "user_env", "pass_env"):
            val = inj.get(field)
            if val:
                mapping.setdefault(val, key)
    _shield_cache.update(mtime=mtime, map=mapping)
    return mapping


def _shielded_route(env_name, path):
    """Route key covering this credential, or None if it isn't routed."""
    mapping = _shield_map()
    if env_name and env_name in mapping:
        return mapping[env_name]
    if path:
        return mapping.get(os.path.expanduser(path))
    return None


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


def load_secret(env_name, path, what="secret", required=True, exit_on_error=True,
                allow_raw=False):
    """Return a credential string (or ``None``).

    Resolution order:
      1. environment variable ``env_name`` (if set and non-empty), stripped;
      2. the file at ``path`` (``~`` expanded), stripped.

    On a miss when ``required`` (the default): ``sys.exit()`` with a one-line
    message, matching the CLI scripts' fail-fast behaviour. Pass
    ``exit_on_error=False`` to raise :class:`SecretError` instead, or
    ``required=False`` to return ``None``.

    ``allow_raw=True`` opts a caller out of the session shield (see
    :data:`SHIELD_ENV`). Use it only where the raw value is genuinely required
    and no egress route can carry it — the proxy itself, and SMTP senders (the
    proxy is HTTP-only). Every such call site is a deliberate, greppable
    exception; prefer the route.
    """
    if not allow_raw and shield_active():
        route = _shielded_route(env_name, path)     # may raise (fails closed)
        if route:
            msg = (
                "🛡 %s is shielded in this session (%s=1) — the raw value is not "
                "handed to callers here.\n"
                "   Fetch it through the egress route instead:\n"
                "     curl --unix-socket \"$EGRESS_SOCK\" http://localhost/%s/<path>\n"
                "   If this caller genuinely needs the raw value (e.g. SMTP, which "
                "the HTTP proxy can't carry), pass allow_raw=True."
                % (what, SHIELD_ENV, route))
            if exit_on_error:
                sys.exit(msg)
            raise SecretShielded(msg)
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
