# _lib — the shared spine

Small, stdlib-only modules every project in the workspace can import via
the sibling-path bootstrap (see `__init__.py`). One concern per module;
grow it as your system grows, and keep the stdlib-only invariant
(`selftest.py` enforces it).

| Module | What |
|---|---|
| `secrets.py` | env-var → file → strip credential loader; fails closed with a clear message when the key vault is locked |
| `event_bus.py` | append-only SQLite event/telemetry bus (local state, no network) |
| `report.py` | terminal-output-to-HTML archiver for jobs that produce reports |
| `selftest.py` | imports every module under `python3 -I -S` to enforce stdlib-only |

The seed ships only the universal spine. As you add domain modules (your
home-automation client, your provider SDK wrapper), they join this table —
that's the intended growth path, not a limitation.
