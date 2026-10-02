"""Credential loading (stdlib-only): env var, else a file under ~/.key, stripped."""
import json
import os
import sys

# ~/.key is fscrypt-encrypted; the canary is readable only while it is unlocked.
KEY_DIR = os.path.expanduser("~/.key")
VAULT_CANARY = os.path.join(KEY_DIR, ".vault_unlocked")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNLOCK_SH = os.path.join(_REPO_ROOT, "keyvault", "unlock.sh")


class SecretError(RuntimeError):
    """Raised by load_secret(required=True, exit_on_error=False) on a miss."""


class SecretShielded(SecretError):
    """Raised when a routed credential is requested inside a shielded session."""


# --- session shield ----------------------------------------------------------
# With $EGRESS_SHIELD=1, credentials an egress-proxy route injects are refused
# here; callers must use the proxy socket. Opt-in, off by default.
SHIELD_ENV = "EGRESS_SHIELD"
ROUTES_JSON = os.environ.get(
    "EGRESS_ROUTES",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "egress-proxy", "routes.json"))
_shield_cache = {"mtime": None, "map": None}


def shield_active():
    return os.environ.get(SHIELD_ENV, "").strip() == "1"


def _shield_map():
    """Map {file path or env-var name -> route key} from routes.json inject blocks.

    The first route listed wins when several share a credential. Raises
    SecretShielded if the route table can't be read (fails closed).
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

    False when unlocked or when ~/.key is a plain (non-fscrypt) directory.
    """
    if not os.path.isdir(KEY_DIR):
        return False
    if os.path.exists(VAULT_CANARY):
        return False  # unlocked
    # No canary: locked only if entries exist but none are readable files.
    try:
        entries = os.listdir(KEY_DIR)
    except OSError:
        return True
    if not entries:
        return False
    return not any(os.path.isfile(os.path.join(KEY_DIR, e)) for e in entries)


def load_secret(env_name, path, what="secret", required=True, exit_on_error=True,
                allow_raw=False):
    """Return a credential from $env_name, else the file at ``path`` (or ``None``).

    On a miss when ``required``: ``sys.exit()`` with a message, or raise
    :class:`SecretError` if ``exit_on_error=False``. ``allow_raw=True`` bypasses
    the session shield; use it only where no egress route can carry the value.
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
        msg = ("🔒 ~/.key is locked (fscrypt). Run `%s` "
               "to unlock the secret vault, then retry — needed %s."
               % (UNLOCK_SH, what))
    else:
        msg = "No %s: set $%s or populate %s." % (what, env_name, path)
    if exit_on_error:
        sys.exit(msg)
    raise SecretError(msg)
