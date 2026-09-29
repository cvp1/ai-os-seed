"""Frontmatter parsing (stdlib-only) — the one canonical home for the
"---\\nkey: value\\n---\\nbody" split, promoted 2026-08-08 after finding three
independent near-copies in this workspace: cc-skills/agy-bundle/build.py's
_frontmatter, cc-skills/improve/pipeline.py's _frontmatter_fields, and
session-brief/session_brief.py's parse_brief. The first two are now thin
callers of parse() below, preserving their own return shapes so nothing
downstream of them needs to change.

session-brief/session_brief.py deliberately KEEPS its own local copy rather
than importing this — it's designed to be standalone-portable, handed
verbatim to whatever harness or small local model is resuming a brief, with
zero repo dependencies (its own docstring: "stdlib-only, no harness API
anywhere in it"). The two are asserted to agree on a fixture in
_lib/selftest.py, so a change to one is caught if it silently drifts from
the other — a declared-and-tested duplicate rather than an undeclared one.

Naive by design, matching all three originals: no YAML, no quoting handled
here (callers strip a wrapping '"' themselves if their field expects one, as
agy-bundle and pipeline.py's originals did), no multi-line values, no lists.
A value containing ':' is fine; a value containing a newline is not. That
covers every consumer in this workspace and keeps this stdlib-only.
"""


def parse(text):
    """Return (meta: dict[str, str], body: str).

    meta holds every top-level ``key: value`` line between the leading
    ``---`` fences, split on the FIRST colon, values stripped of surrounding
    whitespace. body is everything after the second ``---``, stripped. Text
    with no leading ``---`` returns ({}, text.strip()) — same fallback
    session_brief.parse_brief and agy-bundle._frontmatter both use.
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
    """Convenience: read `path` and parse() it."""
    with open(path) as f:
        return parse(f.read())
