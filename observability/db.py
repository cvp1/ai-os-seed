"""Shared SQLite helpers for the CC observability store.

The DB path defaults to <this dir>/data/runs.db and can be overridden with the
CC_OBS_DB environment variable (e.g. to point the status-site container at a
mounted copy). Stdlib only; safe to run under /usr/bin/python3.
"""
import os
import sqlite3
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DEFAULT_DB = _HERE / "data" / "runs.db"
_SCHEMA = _HERE / "schema.sql"


def db_path() -> Path:
    """Resolve the store path. DEFAULT_DB is always absolute; a CC_OBS_DB
    override MUST be absolute too — a relative value opens (and creates) a stray
    DB at the caller's cwd (Story 026: a relative CC_OBS_DB in an interactive/test
    session dropped a 0-byte runs.db at the repo root). Fail loud instead."""
    override = os.environ.get("CC_OBS_DB")
    if override is None:
        return DEFAULT_DB
    if not os.path.isabs(override):
        raise ValueError(
            f"CC_OBS_DB must be an absolute path, got {override!r} — a relative path "
            f"would create a stray DB at {os.getcwd()!r}. Use an absolute path.")
    return Path(override)


def connect() -> sqlite3.Connection:
    """Open the DB for read/write, creating the file + schema on first use.

    When CC_OBS_READONLY is set (used by the status-site container, which mounts
    the repo :ro), open immutably instead: no schema creation, no locks, no
    journal files — so it works on a read-only filesystem. The DB must already
    exist in that mode.
    """
    path = db_path()
    if os.environ.get("CC_OBS_READONLY"):
        uri = f"file:{path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")  # tolerate the 5-min push job overlap
    conn.executescript(_SCHEMA.read_text())
    _migrate(conn)
    return conn


# Additive column migrations: CREATE TABLE IF NOT EXISTS never alters an existing
# table, so columns added after a DB was first created must be back-filled with
# ALTER TABLE here. SQLite has no ADD COLUMN IF NOT EXISTS, so guard on the live
# schema. Each entry is (column, DDL-type); all are nullable (old rows stay NULL).
_ADDED_COLUMNS = [
    ("cache_read_tokens", "INTEGER"),
    ("cache_creation_tokens", "INTEGER"),
    ("model", "TEXT"),
]


def _migrate(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for col, decl in _ADDED_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
    conn.commit()
