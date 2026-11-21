"""Credential loading (stdlib-only): env var, else ONE selected source, stripped.

Source selection (panel consensus 2026-11-02, reviews/2026-11-02-secrets-keyring-fallback/):
an explicit ``keyring="<account>"`` reads only that OS-keyring item; otherwise this
host's source map (``hosts/<fleet slug>/secret-sources.json``) may route an env name
to a keyring account, and then ONLY that item is read — the caller's file path is
never opened; otherwise the caller's file under ~/.key. No cascade between stores.
"""
import json
import os
import shutil
import subprocess
import sys

# ~/.key is fscrypt-encrypted; the canary is readable only while it is unlocked.
KEY_DIR = os.path.expanduser("~/.key")
VAULT_CANARY = os.path.join(KEY_DIR, ".vault_unlocked")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNLOCK_SH = os.path.join(_REPO_ROOT, "keyvault", "unlock.sh")


# --- OS keyring (opt-in) ---------------------------------------------------------
# The same service the AI-OS `bin/secret` broker stores under, so library reads and
# the broker name the same items. The source map carries ACCOUNTS only.
KEYRING_SERVICE = "ai-os"
KEYRING_TIMEOUT_S = 3
_HOSTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hosts")
SOURCES_FILE = "secret-sources.json"


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


class SecretSourceError(RuntimeError):
    """Keyring/source-map failure that is NOT a plain miss (timeout, denial, missing
    tool, corrupt map). Always raised — never degraded to a miss or a file read."""


def _no_dup_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise SecretSourceError("source map: duplicate key %r" % k)
        out[k] = v
    return out


def _host_slug():
    try:
        from _lib import fleet_host
    except ImportError:  # loaded as a bare module (e.g. seed layout)
        import fleet_host  # type: ignore
    return fleet_host.slug()


def source_map(hosts_dir=None, slug=None):
    """{ENV_NAME: account} for this host. No ``hosts/<slug>/`` dir = {} (files as
    before). If that dir EXISTS the map file is required and must be a JSON object
    of ENV_NAME -> {"keyring": "<account>"}; anything else raises SecretSourceError."""
    hosts_dir = hosts_dir or _HOSTS_DIR
    hdir = os.path.join(hosts_dir, slug or _host_slug())
    if not os.path.isdir(hdir):
        return {}
    path = os.path.join(hdir, SOURCES_FILE)
    try:
        with open(path) as fh:
            raw = json.load(fh, object_pairs_hook=_no_dup_keys)
    except SecretSourceError:
        raise
    except (OSError, ValueError) as e:
        raise SecretSourceError("source map %s unreadable (%s) — refusing every load "
                                "until fixed" % (path, type(e).__name__))
    if not isinstance(raw, dict):
        raise SecretSourceError("source map %s: not a JSON object" % path)
    out = {}
    for env, spec in raw.items():
        if (not isinstance(spec, dict) or set(spec) != {"keyring"}
                or not isinstance(spec["keyring"], str) or not spec["keyring"].strip()):
            raise SecretSourceError("source map %s: entry %r must be {\"keyring\": "
                                    "\"<account>\"}" % (path, env))
        out[env] = spec["keyring"]
    return out


def _keyring_cmd(account):
    if sys.platform == "darwin":
        tool = shutil.which("security")
        return tool, ([tool, "find-generic-password", "-s", KEYRING_SERVICE,
                       "-a", account, "-w"] if tool else None)
    if sys.platform.startswith("linux"):
        tool = shutil.which("secret-tool")
        return tool, ([tool, "lookup", "service", KEYRING_SERVICE,
                       "account", account] if tool else None)
    return None, None


def _is_not_found(rc, out, err):
    """Backend-specific, VERIFIED not-found signal only; anything else is a failure.
    macOS `security`: exit 44 (measured on {{REDACTED}} 2026-11-02). Linux `secret-tool`:
    exit 1 with empty stdout and stderr (documented; not yet measured on a fleet host)."""
    if sys.platform == "darwin":
        return rc == 44
    return rc == 1 and not out and not err.strip()


def _keyring_read(account, what):
    """The value, or None on a verified not-found. Raises SecretSourceError otherwise.
    Messages name service + account only — never output, never the value."""
    who = "%s (keyring %s/%s)" % (what, KEYRING_SERVICE, account)
    tool, cmd = _keyring_cmd(account)
    if not cmd:
        raise SecretSourceError("no OS keyring tool on this platform for %s" % who)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=KEYRING_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # run() kills + reaps the child
        raise SecretSourceError("keyring read timed out after %ss for %s"
                                % (KEYRING_TIMEOUT_S, who))
    except OSError as e:
        raise SecretSourceError("keyring tool failed to start (%s) for %s"
                                % (type(e).__name__, who))
    if r.returncode == 0:
        val = r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
        if not val:
            raise SecretSourceError("keyring returned an empty value for %s" % who)
        return val
    if _is_not_found(r.returncode, r.stdout, r.stderr):
        return None
    raise SecretSourceError("keyring read failed (exit %d) for %s" % (r.returncode, who))


def load_secret(env_name, path=None, what="secret", required=True, exit_on_error=True,
                allow_raw=False, keyring=None):
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
    if path and keyring:
        raise SecretSourceError("load_secret(%s): pass path OR keyring, not both"
                                % env_name)
    env = os.environ.get(env_name)
    if env and env.strip():
        return env.strip()
    account = keyring or source_map().get(env_name)
    if account:  # keyring selected: read ONLY that item — never the file
        val = _keyring_read(account, what)
        if val is not None:
            return val
        if not required:
            return None
        msg = ("No %s: keyring item %s/%s not found (set $%s, or store it with "
               "`bin/secret`)." % (what, KEYRING_SERVICE, account, env_name))
        if exit_on_error:
            sys.exit(msg)
        raise SecretError(msg)
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
