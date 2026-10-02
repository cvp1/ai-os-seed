"""Frontmatter parsing (stdlib-only): split "---\\nkey: value\\n---\\nbody".

Naive by design: no YAML, no quoting, no multi-line values, no lists.
session-brief keeps its own standalone copy; _lib/selftest.py asserts they agree.
"""


def parse(text):
    """Return (meta, body); meta splits each ``key: value`` line on the first colon.

    Text with no leading ``---`` returns ({}, text.strip()).
    """
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    _, fm, body = parts
    meta = {}
    for ln in fm.strip().splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body.strip()


def parse_file(path):
    """Read `path` and parse() it."""
    with open(path) as f:
        return parse(f.read())
