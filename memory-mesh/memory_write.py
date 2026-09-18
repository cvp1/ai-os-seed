#!/usr/bin/env python3
"""memory_write.py — deterministic writer for Craig's auto-memory store.

Backs the `improve` and `capture` skills. Their prose already does the real
judgment (harvest, filter, classify, dedup, get Craig's approval) — this
script only handles the mechanical part that was previously hand-typed each
run: exact frontmatter formatting, the MEMORY.md one-line-per-memory index
(add/update/remove), and supersede bookkeeping (new file gets `supersedes:`,
old file's index line is dropped, old file itself is kept as history).

Store: ~/.claude/projects/-home-{{REDACTED}}-Github-CC/memory/ (MEMORY.md = index).
Lineage gate (Story 029, OWASP ASI06): `contains-untrusted` memories are written
to disk but never served — the memory-mesh FOLD holds them out of the generated
index, keeps them out of /recall's pack (tombstone, never the body), and
publishes both quarantine projections. Promotion needs a signed mesh event
(memory-mesh/sign.py --promote), which requires Craig's passphrase-gated key.

That enforcement lives entirely in the fold, so on a host without the mesh an
untrusted write is REFUSED rather than half-honoured — see
require_enforceable_quarantine(). Until 2026-07-30 this docstring described a
routing that the fold ignored and whose store-side owner (consolidate.py) no
longer existed; the description was true of nothing for the life of the feature.

Usage:
    # New or updated memory (same slug = in-place update)
    python3 memory_write.py write --slug foo-bar --type feedback \\
        --description "one-line description for recall relevance" \\
        --lineage craig-direct \\
        --rule "The lesson, stated as a concrete rule." \\
        --why "Why it matters." --how "Exactly what to do next time." \\
        --hook "short index hook" --section "Working Practices & Harness Lessons"

    # A belief changed: write the replacement, drop old-slug's index line,
    # keep old-slug's file on disk as history.
    python3 memory_write.py write --slug new-slug --type feedback \\
        --supersedes old-slug --rule "..." --why "..." --how "..." \\
        --hook "..." --section "..." --description "..."

    # user / reference memories: single paragraph, no --why/--how
    python3 memory_write.py write --slug craig-likes-x --type user \\
        --description "..." --rule "Single paragraph body." \\
        --hook "..." --section "..."

Two maintenance subcommands touch the index/frontmatter but never memory
CONTENT, so neither takes a --lineage of its own:

    # free always-on index headroom (fact file stays live for /recall)
    python3 memory_write.py demote slug-a slug-b --commit

    # Story 029 backfill: set lineage: on EXISTING notes, body byte-preserved
    python3 memory_write.py retag slug-a slug-b --lineage craig-direct --commit

Always prints the rendered file + the MEMORY.md diff and asks nothing — the
skill shows this to Craig for approval BEFORE calling with --commit. Without
--commit it's a dry run (prints what would be written, writes nothing).
Stdlib only — no deps.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ── where the mesh code lives ────────────────────────────────────────────────
# Resolved, never hardcoded. Until 2026-09-18 this was the literal
# `~/{{REDACTED}}/memory-mesh` in four places, which is one host's truth: on any
# install whose workspace is not `~/{{REDACTED}}` — i.e. EVERY seed recipient —
# `mesh_emit.exists()` was False, the emit was skipped, and the memory landed
# in the store while the event log never heard about it. Silently: the door
# printed nothing, and the next fold had nothing to project. Caught by
# memory-mesh/contract_test.py M3 against a fresh-install-shaped sandbox.
#
# Order: an explicit env override, then this file's own directory (after the
# engine moves into memory-mesh/ the emitter is its sibling), then the
# workspace root walking up from cwd — `<root>/memory-mesh/` is the one door
# convention every harness and every seed install shares — then the fleet's
# historical path, last, so a fleet host keeps working mid-migration.
def _mesh_code_dir():
    here = Path(__file__).resolve().parent
    cands = []
    env = os.environ.get("MESH_CODE_DIR")
    if env:
        cands.append(Path(env).expanduser())
    cands.append(here)
    cands.append(here.parent / "memory-mesh")
    cwd = Path.cwd().resolve()
    for root in (cwd,) + tuple(cwd.parents):
        cands.append(root / "memory-mesh")
    cands.append(Path(os.path.expanduser("~/{{REDACTED}}/memory-mesh")))
    for c in cands:
        if (c / "emit.py").is_file():
            return c
    return None


def _mesh_emit_path():
    """The emitter, or None — and None is said out loud by every caller."""
    d = _mesh_code_dir()
    return (d / "emit.py") if d else None


# Derived per host (homes differ: /home/{{REDACTED}} vs /Users/craigvandeputte) —
# the harness keys the store by the CC workspace path with / → -.
def _store():
    """The store the harness serves for THIS workspace. Walk up from cwd to the
    first directory whose cwd-keyed store carries the .mesh-generated marker
    ({{REDACTED}}: ~/ai-os; {{REDACTED}}/{{REDACTED}}: ~/{{REDACTED}}); fall back to the CC
    tree, the only path this ever knew until 2026-09-17 — when on {{REDACTED}} it
    wrote side-effect files into a store nothing folded."""
    override = os.environ.get("MEMORY_WRITE_STORE")
    if override:
        return Path(override).expanduser()
    # SEED-080: the workspace is where the CODE lives, which is the same rule
    # mesh_lib.store_dir() uses — ask it rather than deriving a second answer.
    # Until the engine moved into memory-mesh/ this file had no workspace
    # ancestry to reason from, so it walked up from cwd instead; that walk
    # survives below as a fallback, but it can no longer be the first answer.
    # It is why a fresh seed install failed M2: mesh_lib resolved the install's
    # own store while this file resolved ~/{{REDACTED}}'s, and the two halves of
    # one door disagreed about where memory lives.
    d = _mesh_code_dir()
    if d is not None:
        try:
            sys.path.insert(0, str(d))
            import mesh_lib
            return mesh_lib.store_dir()
        except Exception:
            pass
    d = Path.cwd().resolve()
    while True:
        cand = Path.home() / ".claude/projects" / str(d).replace("/", "-") / "memory"
        if (cand / ".mesh-generated").exists():
            return cand
        if d.parent == d:
            break
        d = d.parent
    return (Path.home() / ".claude/projects"
            / str(Path.home() / "Github" / "CC").replace("/", "-") / "memory")


STORE = _store()
INDEX = STORE / "MEMORY.md"
EXCLUDE = STORE / "_index-exclude.txt"
QUARANTINE = STORE / "QUARANTINE.md"
# Cutover phase 7 (memory-mesh, 2026-07-28): when this marker exists,
# MEMORY.md is GENERATED by the mesh fold — this writer must never edit it.
# Index changes flow through the mesh event (dual-write below) + the fold;
# the exclude manifest and QUARANTINE.md remain this writer's to maintain.
MESH_MARKER = STORE / ".mesh-generated"
# SPEC v4: the served index line is bounded at the door by rewrite. Kept in
# sync with mesh_lib.HOOK_MAX_CHARS — asserted at import below rather than
# imported, because this writer must keep working with the mesh absent.
HOOK_MAX_CHARS = 140
# mesh_lib.make_event's admission_reject() refuses any lesson content over
# mesh_lib.INDEX_CONTENT_CHARS — kept in sync the same way HOOK_MAX_CHARS is.
# Found 2026-08-08: the mesh dual-write below used to slice --content to
# [:1000], so any description over 200 chars ALWAYS failed emit.py with
# "make_event refused ... content is N chars" — every such write printed
# "mesh: EMIT FAILED (store write is safe; mesh will lag)" and silently
# never caught up, because the failure was a permanent refusal, not a
# transient lag. Caught by reproducing the exact emit.py call by hand after
# it fired twice in one session.
INDEX_CONTENT_CHARS = 200
# Bytes the generated index spends on things that are not rows: the header, the
# quarantine-count line, the on-demand stub. Held back so the door's budget is
# measured on the COMPOSED file rather than on the rows alone — a bound that
# measures a subsection is not a bound.
HARNESS_HEADER_RESERVE = 600


def index_is_generated():
    return MESH_MARKER.exists()


def proc_error(r, limit=400):
    """The most informative line of a failed subprocess — not the first one.

    emit.py surfaces an admission refusal as an uncaught exception, so the
    reason is the LAST line of the traceback ("make_event refused ...: content
    is 224 chars; the renderer cuts at 200"). Every mesh call site here used to
    keep `(stderr or stdout).strip()[:150]`, which clips the traceback HEADER
    and throws away the only line that says why. Measured 2026-08-09 on
    `adopt`: a permanent, actionable refusal reached the operator as a bare
    "skipped" plus a fragment of "Traceback (most recent call last):" — and
    the fold is what advises the operator to run adopt, so the dead end was
    one the tooling walked him into.

    Only reach past the head when there is actually a traceback to reach past:
    emit.py's own hand-written errors (the GHOST check) are already the
    message, and are multi-line by design.
    """
    text = (r.stderr or r.stdout or "").strip()
    if not text:
        return f"no output (exit {r.returncode})"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1 and lines[0].lstrip().startswith("Traceback"):
        return lines[-1].strip()[:limit]
    return text[:limit]


def require_enforceable_quarantine():
    """Refuse a contains-untrusted write on a host where quarantine is a fiction.

    Craig's call, 2026-07-30, closing the last finding on Grok 4.5's board.

    Quarantine has exactly one enforcer: the memory-mesh fold, which holds
    untrusted events out of the generated index, publishes the quarantine
    projection, and requires a signature to promote. Its per-host opt-in is
    MESH_MARKER. Without the marker there is no fold enforcing anything here —
    and the fallback that used to run instead was add-only: this writer appended
    to a list whose stated routing owner (consolidate.py) does not exist, so
    nothing could ever remove an entry and nothing could promote one. A seed
    recipient in that state accumulates memories that are neither served nor
    promotable nor removable, while a file named QUARANTINE.md implies a control
    that is not there. That implication is the whole failure this repaired.

    So the write is REFUSED rather than half-honoured. Deliberately no --force:
    an override would recreate exactly the surface it exists to remove, and the
    honest alternative is not a weaker memory — it is a document, which is where
    an unpromotable observation belongs anyway.
    """
    if index_is_generated():
        return
    raise SystemExit(
        "error: refusing a `contains-untrusted` memory on a host with no mesh "
        "quarantine.\n"
        f"  {MESH_MARKER} is absent, so no fold holds untrusted memories out of "
        "the index,\n"
        "  nothing can promote one (that needs a signed mesh event), and nothing "
        "removes an entry\n"
        "  from the store list — its owner consolidate.py no longer exists. A "
        "quarantine that\n"
        "  cannot be enforced or exited is a label, not a control.\n"
        "\n"
        "  Either:\n"
        "   - adopt the mesh on this host (memory-mesh/install.sh, then the "
        "marker appears), or\n"
        "   - write this observation as a DOC (vault note / repo README) instead "
        "of a memory.\n"
        "  A craig-direct memory is unaffected; only the untrusted class is "
        "refused.")


TYPES = {"feedback", "user", "project", "reference"}
LINEAGES = {"craig-direct", "contains-untrusted"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def on_demand_slugs():
    """Slugs deliberately kept OUT of the always-on MEMORY.md index.

    The two-tier index is a standing decision per memory (see auto-memory
    `memory-index-two-tier`), so an UPDATE to an on-demand memory must not
    silently promote it back to always-on.
    """
    if not EXCLUDE.exists():
        return set()
    return {l.strip() for l in EXCLUDE.read_text().splitlines()
            if l.strip() and not l.startswith("#")}


def resident_slugs():
    """Slugs the fold currently renders into the always-on index."""
    if not INDEX.exists():
        return set()
    return set(re.findall(r"^- \[lesson/([a-z0-9-]+)\]", INDEX.read_text(), re.M))


def default_on_demand(slug):
    """Enforce admission policy E at the PRODUCER: a NEW memory defaults to
    on-demand. Returns the paths it touched (for the commit).

    Craig ratified 2026-07-31: "New rules default to on-demand; promotion into
    always-on requires Craig's explicit word and a named displacement." Nothing
    enforced it at this door. The fold ranks every live lesson, so a memory
    written here became an always-on CANDIDATE the moment it existed and
    displaced a lower-ranked incumbent by score — a residency change nobody
    decided. Measured 2026-08-01: three memories a sibling session wrote in the
    evening were, within one fold, staged to evict three standing behavioural
    rules including `validate-the-instrument-before-trusting-silence`.

    The residency GATE caught it every time, which is why nothing was lost —
    but a gate that fires on routine writes is a gate the operator learns to
    click through, and the fix belongs where the artifact is created
    ([[fix-human-loop-races-at-the-producer]]).

    REFUSES on a slug that is already resident: removing a live rule from the
    always-on tier is a real demotion and stays Craig's call. That asymmetry is
    the whole safety argument — this may only ever keep a NEW memory out, never
    push an existing one out. (Applied by hand without this guard on
    2026-08-01, it demoted a resident rule; the gate held, and the guard is
    here so judgment is not the thing standing between a batch and Craig's
    index.)
    """
    if slug in resident_slugs():
        print(f"note: '{slug}' is already always-on — left resident. "
              f"Demoting a live rule is Craig's call, not a write's side effect.")
        return []
    if slug in on_demand_slugs():
        return []
    with open(EXCLUDE, "a", encoding="utf-8") as f:
        f.write(slug + "\n")
    print(f"on-demand by default: '{slug}' -> _index-exclude.txt "
          f"(admission policy E; promote by name + named displacement)")
    return [EXCLUDE]


def render_frontmatter(args):
    lines = ["---", f"name: {args.slug}", f"description: {args.description}"]
    if args.lineage:
        lines.append(f"lineage: {args.lineage}")
    # B2 (2026-08-06, quick-fix round per Grok review): the session-provenance
    # verdict travels WITH the file, not just in a stderr NOTE at write time —
    # PRINCIPLES 18, keep the artifact, not just the conclusion.
    # clean | unverified | flagged-downgraded. (flagged-override retired —
    # see the write subparser's --provenance-override removal note.)
    if getattr(args, "provenance", None):
        lines.append(f"provenance: {args.provenance}")
    if args.supersedes:
        lines.append(f"supersedes: [{args.supersedes}]")
    if args.contradicts:
        lines.append(f"contradicts: [{args.contradicts}]")
    # SPEC v4: an OPTIMISTIC MIRROR of the event, never the authority. The
    # event's lineage tip is the sole home of residency (A1); this line exists
    # so a human reading the file sees the tier, and so the fold can repair a
    # projection without a second lookup. A tool that writes this and skips the
    # event leaves the fold blind — the exact failure the SPEC's lineage
    # epistemology paragraph was written to prevent.
    if getattr(args, "residency", None):
        lines.append(f"residency: {args.residency}")
    if getattr(args, "doctrine_candidate", False):
        lines.append("doctrine_candidate: true")
    if getattr(args, "expires", None):
        lines.append(f"expires: {args.expires}")
    lines.append("metadata:")
    lines.append("  node_type: memory")
    lines.append(f"  type: {args.type}")
    if args.session_id:
        lines.append(f"  originSessionId: {args.session_id}")
    lines.append("---")
    return "\n".join(lines)


def render_body(args):
    body = args.rule.strip()
    if args.type in ("feedback", "project"):
        if not args.why or not args.how:
            raise SystemExit(f"error: type '{args.type}' requires --why and --how")
        body += f"\n\n**Why:** {args.why.strip()}"
        body += f"\n\n**How to apply:** {args.how.strip()}"
    else:
        if args.why or args.how:
            raise SystemExit(f"error: type '{args.type}' is a single paragraph — no --why/--how")
    return body


def render_file(args):
    return render_frontmatter(args) + "\n\n" + render_body(args) + "\n"


def index_line(args):
    title = args.slug.replace("-", " ").title()
    return f"- [{title}]({args.slug}.md) — {args.hook.strip()}"


def find_section(text, section):
    m = re.search(rf"^## .*{re.escape(section)}.*$", text, re.MULTILINE)
    return m


def insert_index_line(text, section, line, slug):
    # Already indexed? Replace that line in place.
    existing = re.search(rf"^- \[.*\]\({re.escape(slug)}\.md\).*$", text, re.MULTILINE)
    if existing:
        return text[: existing.start()] + line + text[existing.end():]

    m = find_section(text, section)
    if not m:
        # No matching section — append to the Unsorted section, or the end of
        # the file if even that is missing. Loud, not silent.
        print(f"warning: section '{section}' not found in MEMORY.md — filing under Unsorted", file=sys.stderr)
        m = find_section(text, "Unsorted")
        if not m:
            return text.rstrip("\n") + f"\n\n## Unsorted (auto-added by memory_write.py)\n{line}\n"
    # Insert before the on-demand recall continuation line if present, else
    # right after the section header, else before the next "## " heading.
    section_start = m.end()
    next_heading = re.search(r"^## ", text[section_start:], re.MULTILINE)
    section_end = section_start + next_heading.start() if next_heading else len(text)
    section_body = text[section_start:section_end]
    insert_at = section_start + len(section_body.rstrip("\n"))
    return text[:insert_at] + "\n" + line + text[insert_at:]


def remove_index_line(text, slug):
    pattern = rf"\n?^- \[.*\]\({re.escape(slug)}\.md\).*$"
    new_text, n = re.subn(pattern, "", text, flags=re.MULTILINE)
    if n == 0:
        print(f"note: no MEMORY.md line found for '{slug}' to remove (nothing to supersede-out)", file=sys.stderr)
    return new_text


def _git(*argv, check=False):
    """Run git in the memory store. Returns (rc, stdout+stderr)."""
    p = subprocess.run(("git", "-C", str(STORE)) + argv,
                       capture_output=True, text=True, timeout=120)
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        raise RuntimeError(out)
    return p.returncode, out


def git_commit(paths, args):
    """Commit ONLY the files this invocation touched, then push.

    The store is git-backed but nothing in the memory workflow ever called git,
    so every /improve and /capture wrote files that sat uncommitted until a
    human noticed. On 2026-07-25 that backlog was 64 files and 10 days deep,
    and it was found by accident. A memory that exists only in one working tree
    is not a memory that survives the disk.

    Scoped to `paths` on purpose: the store frequently holds unrelated in-flight
    edits, and `git add -A` here would sweep them into someone else's commit.

    Never fatal. The write already succeeded by the time we get here; failing
    the whole command would be a lie about what happened on disk. Degrade
    loudly instead — the operator needs to know it is only local.
    """
    if not (STORE / ".git").exists():
        print("note: memory store is not a git repo — nothing to commit", file=sys.stderr)
        return
    rel = [str(Path(p).relative_to(STORE)) for p in paths]
    try:
        rc, out = _git("add", "--", *rel)
        if rc != 0:
            print(f"⚠ memory NOT committed (git add failed): {out}", file=sys.stderr)
            return
        rc, out = _git("diff", "--cached", "--quiet", "--", *rel)
        if rc == 0:
            print("note: no content change — nothing to commit")
            return
        subject = f"memory: {args.slug} — {args.description}"
        if len(subject) > 100:
            subject = subject[:97] + "..."
        rc, out = _git("-c", "user.name={{REDACTED}}",
                       "-c", "user.email={{REDACTED}}@gmail.com",
                       "commit", "-q", "-m", subject, "--", *rel)
        if rc != 0:
            print(f"⚠ memory written but NOT committed: {out}", file=sys.stderr)
            return
        _, sha = _git("rev-parse", "--short", "HEAD")
        print(f"committed {sha} — {subject}")
    except Exception as e:  # noqa: BLE001 — never fail the write
        print(f"⚠ memory written but NOT committed: {e}", file=sys.stderr)
        return

    if args.no_push:
        print("note: --no-push — commit is local only")
        return
    rc, out = _git("push", "-q")
    if rc != 0:
        print(f"⚠ committed locally but PUSH FAILED — this memory exists on one disk only: {out}",
              file=sys.stderr)
    else:
        print("pushed")


# --- Fact-shape gate (2026-07-27, "one home per fact") -----------------------
# Infrastructure facts (hosts, routes, endpoints, install state) have exactly
# one home — FLEET.md, a CLAUDE.md, an OPS.md, the code — and memory POINTS at
# it. A restated fact in memory is a drift liability: on 2026-07-27 a memory
# asserting "no SSH key to .21 (Permission denied), verified" landed the same
# afternoon Craig corrected the opposite in a sibling session. This gate makes
# the conflict impossible at the only chokepoint instead of asking every future
# session to be careful. Surgical on purpose: an IPv4 literal is the strongest
# fact signal with near-zero overlap with behavioral lessons; per
# fix-the-discriminator, widen only on an observed miss, never speculatively.
# The ONE home for this list is mesh_lib.FACT_SHAPES (2026-09-16): make_event
# applies it at the funnel to every text field, so the same text can never be
# admitted by this door and refused by the emitter after the file has landed.
# This copy exists only so the door still bounds a write with the mesh absent;
# _mesh_lib() warns the moment the two diverge, as it does for HOOK_MAX_CHARS.
# `0.0.0.0` is excluded: the all-sources CIDR idiom, not a host — an observed
# false positive on 2026-09-16 ("never widen to 0.0.0.0/0").
FACT_SHAPES = [
    (re.compile(r"\b(?!0\.0\.0\.0\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     "an IPv4 address"),
    (re.compile(r"\blocalhost:\d{2,5}\b"), "a host:port endpoint"),
    (re.compile(r"\bno ssh\b|\bssh (?:works|fails|key)\b|permission denied \(publickey\)", re.I),
     "an SSH reachability claim"),
]
FACT_HOMES = ("vault 00 Meta/Fleet/FLEET.md (hosts/reachability) · the owning "
              "repo's CLAUDE.md/OPS.md/README (project facts) · the code itself "
              "(policy/config)")


def fact_shape(*texts):
    """First (match, label) found in any text, else None."""
    for t in texts:
        for rx, label in FACT_SHAPES:
            m = rx.search(t or "")
            if m:
                return m.group(0), label
    return None


def _session_provenance():
    """Import session_provenance.py lazily — same degrade-safe pattern as
    _mesh_lib(): its hooks are STAGED, not wired into ~/.claude/settings.json
    (see the module's own docstring), so this writer must keep working with
    it absent exactly as it keeps working with the mesh absent. A missing or
    broken import degrades the craig-direct check to UNVERIFIED, never to a
    silent pass framed as verified-clean (see _cmd_write)."""
    try:
        d = _mesh_code_dir()
        if d is None:
            return None
        sys.path.insert(0, str(d / "hooks"))
        import session_provenance
    except Exception:
        return None
    return session_provenance


def _mesh_lib():
    """Import mesh_lib lazily — the mesh is optional infrastructure.

    memory_write predates the mesh and must keep working without it (a mesh
    outage degrades to exactly the pre-mesh world, loudly). So every v4 feature
    that needs mesh_lib degrades to "no metering" rather than refusing a write.
    """
    try:
        d = _mesh_code_dir()
        if d is None:
            return None
        sys.path.insert(0, str(d))
        import mesh_lib
    except Exception:
        return None
    # HOOK_MAX_CHARS is duplicated here so the door still bounds the hook with
    # the mesh absent — which makes it a second home for one number. Rather
    # than trust the copies to stay equal, say so the moment they diverge: a
    # door that bounds at 140 feeding an emitter that refuses at 120 would
    # reject writes only AFTER the file landed.
    if mesh_lib.HOOK_MAX_CHARS != HOOK_MAX_CHARS:
        print(f"warning: HOOK_MAX_CHARS disagrees — memory_write "
              f"{HOOK_MAX_CHARS} vs mesh_lib {mesh_lib.HOOK_MAX_CHARS}; "
              f"the door and the emitter will refuse different writes",
              file=sys.stderr)
    if mesh_lib.INDEX_CONTENT_CHARS != INDEX_CONTENT_CHARS:
        print(f"warning: INDEX_CONTENT_CHARS disagrees — memory_write "
              f"{INDEX_CONTENT_CHARS} vs mesh_lib {mesh_lib.INDEX_CONTENT_CHARS}; "
              f"the mesh dual-write's truncation won't match what "
              f"make_event actually enforces", file=sys.stderr)
    # Same drift check for the fact-shape discriminator. Until 2026-09-16 this
    # door and emit.py each had their OWN list, and the emitter never looked at
    # the body at all — which is how a fact-copy entered the log through one
    # door and became unpromotable through the other. One list now (mesh_lib);
    # this mirror must match it, or the door admits a body the funnel refuses.
    if [rx.pattern for rx, _ in FACT_SHAPES] != \
            [rx.pattern for rx, _ in getattr(mesh_lib, "FACT_SHAPES", [])]:
        print("warning: FACT_SHAPES disagrees — memory_write's fact-shape gate "
              "and mesh_lib.make_event's will refuse different writes; the "
              "door could admit a body the funnel then refuses AFTER the file "
              "landed (mesh_lib older than 2026-09-16, or the lists drifted)",
              file=sys.stderr)
    return mesh_lib


def has_signed_promotion(slug):
    """(ok: bool, reason: str) — does `lesson/<slug>` have a live,
    cryptographically verified operator signature that covers its CURRENT
    content? The ONLY thing that may promote a memory to lineage:
    craig-direct via `retag` (2026-08-06, B2 quick-fix round; content
    binding added 2026-08-06, B3).

    Story: B1 proved the self-declared lineage tag gates nothing; B2 round 1
    added a session-provenance check to `_cmd_write`, but Grok's adversarial
    review (memory-mesh/reviews/2026-08-06-grok-b2-quickfix-convergence.md)
    found the actual chokepoint was fictional as long as `retag` had no check
    of its own: write as contains-untrusted (which pays the quarantine cost),
    wait for a later CLEAN session, `retag --lineage craig-direct` — session
    taint on retag would only see the retagging session, never the tainted
    one that actually authored the content. Session state is the wrong
    signal for retag/adopt: those tools don't author content this session,
    they promote something ALREADY WRITTEN — so the check has to be about
    the memory's history, not this session's.

    The fix reuses infrastructure Craig already trusts rather than building
    anything new: `memory-mesh/sign.py --promote` requires his
    passphrase-gated key (mesh_lib.sign_event's docstring: "an agent cannot
    promote" is a property of that key, not of this check) and produces a
    signed mesh event. `mesh_lib.fold_events()` independently RE-verifies
    every claimed signature — it never trusts a stored flag — and stamps the
    result as `_signed` on each live event (mesh_lib.py ~846-850), so this
    function only has to consume that, matching the codebase's own existing
    idiom (see e.g. mesh_lib.py:2025) rather than re-implementing signature
    verification.

    B3 (memory-mesh/reviews/2026-08-06-grok-b3-plan-review.md): a signed
    event used to bind only a short --content description string, not the
    store file's actual bytes — a promote on clean content followed by a
    LATER overwrite of the file body would still pass this check on the
    new, unreviewed bytes (TOCTOU). Closed by comparing the CURRENT file's
    `mesh_lib.content_fingerprint()` against the signed event's
    `body_sha256` (bound INSIDE the signature — sign.py computes it at
    promote time, before signing). A signed event from before B3 carries no
    `body_sha256` at all and is grandfathered (legacy — the only signer is
    Craig's passphrase-gated key, so grandfathering READS old events, it
    doesn't open a write path for new unbound ones).

    Degrades toward safety: no mesh, no readable log, any exception ->
    (False, reason), never a free pass — same posture as
    require_enforceable_quarantine().
    """
    M = _mesh_lib()
    if M is None:
        return False, "mesh not importable — cannot verify a promotion"
    try:
        events, _ = M.read_all_events()
        fold = M.fold_events(events, M.load_registry())
    except Exception as e:
        return False, f"mesh event log unreadable: {e}"
    subject = f"lesson/{slug}"
    signed = [e for e in fold.get("live", [])
              if e.get("subject") == subject and e.get("_signed")]
    if not signed:
        return False, "never promoted — no signed operator event on record"
    if any(e.get("body_sha256") is None for e in signed):
        return True, "legacy signature, no content binding (pre-B3)"
    target = STORE / f"{slug}.md"
    if not target.exists():
        return False, "signed event exists but the store file is gone"
    current = M.content_fingerprint(target.read_text())
    if any(e.get("body_sha256") == current for e in signed):
        return True, "signed and content matches"
    return False, ("signed content does not match the current file — it "
                    "was modified after promotion")


def door_lock():
    """Serialise budget-check + write across concurrent agents on this host.

    Grok round 2 caught the TOCTOU: two agents both read "doctrine tier has
    room", both pass the check, both write, and the tier is over budget with
    neither refused. `emit.py` already locks its append; that lock is held too
    late and too narrowly to cover the DECISION this door makes.

    Advisory flock on a lock file, not the store dir: the store is a git
    worktree that other tools legitimately touch, and an flock on a directory
    other processes open would deadlock work that has nothing to do with us.
    """
    import fcntl
    lock_path = STORE / ".door.lock"
    lock_path.touch(exist_ok=True)
    fh = open(lock_path, "r+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def doctrine_budget_state():
    """(used_bytes, cap_bytes, demotable_rows) for the doctrine tier, or None.

    Returns None when the mesh is absent or no memory has declared residency
    yet — during migration the tier does not exist, and a door that meters an
    undeclared tier would refuse every write for a budget nobody set.
    """
    M = _mesh_lib()
    if M is None:
        return None
    try:
        reg = M.load_registry()
        events, _ = M.read_all_events()
        fold = M.fold_events(events, reg)
    except Exception:
        return None
    rows = []
    for e in M.ranked_index(fold, "operator"):
        if M.effective_residency(e) == "doctrine":
            rows.append((M.line_bytes(M.index_row(e)), e["subject"]))
    if not rows:
        return None
    # Doctrine gets the delivered file minus what pins ACTUALLY USE — not minus
    # the pin CAP. Reserving the cap would hold ~8.7 KB idle for pins that do
    # not exist and refuse doctrine writes while a third of the file sat empty.
    # (Measured 2026-07-31: pins used 3,409 B of a 12,100 B cap, so the wrong
    # formula made 106 feedback rows look 7,986 B over budget when they in fact
    # fit with room to spare.) Pins remain the hard floor: when a new pin lands,
    # doctrine is what sheds, which is the priority order we want.
    pin_bytes = sum(M.line_bytes(M.index_row(e))
                    for e in M.ranked_index(fold, "operator")
                    if e.get("pin") or e.get("_pin"))
    cap = M.DELIVERY_BYTES - pin_bytes - HARNESS_HEADER_RESERVE
    return sum(b for b, _ in rows), cap, sorted(rows)


def _frontmatter_value(text, key):
    m = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.M)
    return m.group(1) if m else None


def cmd_adopt(args):
    return _locked(_cmd_adopt, args)


def _locked(fn, args):
    """Run a mutating verb under the door lock (no lock for a dry run)."""
    if not getattr(args, "commit", False):
        return fn(args)
    fh = door_lock()
    try:
        return fn(args)
    finally:
        fh.close()


def _cmd_adopt(args):
    """Carry existing store files into the event log as the fact's one home.

    Two jobs, and the second is the subtle one:

    1. Give each file a v4 event carrying its body, so the fold can project it
       on every host and /recall works fleet-wide.
    2. SUPERSEDE any pre-existing event for the same memory. Without this the
       log holds two live events per fact — the old content-only one and the
       new body-carrying one — which is a dual canonical, i.e. the one-home
       violation this whole design exists to close (Grok round 3, A3).

    Adopt is a trust-tier entry point: it turns an unverified file into
    canonical, replicated, signable state. So it carries the same bar as
    sign.py --promote — show the body, confirm per item — rather than the bar
    of a bulk edit.
    """
    M = _mesh_lib()
    if M is None:
        raise SystemExit("adopt: memory-mesh not importable — nothing to adopt into")
    mesh_emit = _mesh_emit_path()
    events, _ = M.read_all_events()

    adopted, skipped, touched = [], [], []
    for slug in args.slugs:
        f = STORE / f"{slug}.md"
        if not f.exists():
            skipped.append((slug, "no store file"))
            continue
        body = f.read_text(encoding="utf-8")
        raw_lineage = _frontmatter_value(body, "lineage")
        # B2 quick-fix (2026-08-06, Grok-reviewed): an UNTAGGED file used to
        # default silently to craig-direct — a file with literally no
        # lineage claim became trusted with zero check, the same class of
        # gap the retag signed-mesh gate closes for an explicit claim. Now
        # treated identically: an untagged file needs the same signed
        # operator promotion retag requires, or it stays contains-untrusted
        # (the safe default) rather than being waved through. A file that
        # already carries an explicit `lineage: craig-direct` tag is NOT
        # re-checked here — it already flowed through write/correct's own
        # session-provenance check or retag's signed-mesh check to get that
        # tag in the first place; re-deciding trust here would be a second,
        # redundant, and potentially conflicting judgment.
        if raw_lineage is None:
            ok, reason = has_signed_promotion(slug)
            if not ok:
                skipped.append((slug, f"untagged file, {reason} — "
                                      "defaulting to craig-direct is "
                                      "refused; retag it explicitly after "
                                      "`sign.py --promote`, or leave it "
                                      "contains-untrusted"))
                continue
        lineage = raw_lineage or "craig-direct"
        desc = _frontmatter_value(body, "description") or slug
        # DELIBERATELY NOT read from frontmatter. Residency is Craig's
        # declaration; frontmatter is a hand-editable mirror. Carrying
        # `residency: pinned` from a file into a log claim would let anything
        # that can write a store file declare its own tier — laundering an
        # edit into a privilege claim. Adopted memories arrive UNDECLARED and
        # are declared by the retag, deliberately.
        prior = M.unsuperseded_ids(f"lesson/{slug}", events)
        already = [e for e in events
                   if e["subject"] == f"lesson/{slug}" and e.get("body")]
        if already and not getattr(args, "reconcile", False):
            skipped.append((slug, "already carries a body in the log "
                                  "(divergence? use --reconcile)"))
            continue
        if len(body.encode()) > M.MAX_EVENT_BYTES - 1024:
            # Refuse, never truncate (A4): a half-body projected over a whole
            # file destroys the part that did not fit.
            skipped.append((slug, f"body {len(body.encode())}B too large — "
                                  f"split it or shorten before adopting"))
            continue

        print(f"\n=== {slug}  ({len(body.encode())} B, lineage={lineage})")
        print(f"    supersedes {len(prior)} prior event(s): "
              f"{', '.join(prior) if prior else 'none'}")
        if already:
            # Principle 17: the approval must SHOW what it replaces. A
            # reconcile overwrites a body that already exists in the log, so
            # printing only the file is presence, not consent.
            import difflib
            old_body = already[-1]["body"]
            print(f"    RECONCILE — event body {len(old_body.encode())} B "
                  f"-> file {len(body.encode())} B. Event text is REPLACED:")
            for l in list(difflib.unified_diff(
                    old_body.splitlines(), body.splitlines(),
                    fromfile="event (current)", tofile="file (wins)",
                    lineterm="", n=1))[:40]:
                print(f"      {l[:140]}")
        if not args.yes:
            print("--- body ---")
            print(body.rstrip())
            print("--- end ---")
        if not args.commit:
            adopted.append((slug, prior))
            continue
        if not args.yes:
            try:
                ans = input(f"adopt {slug}? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans != "y":
                skipped.append((slug, "declined"))
                continue
        cmd = [sys.executable, str(mesh_emit), "--no-nudge",
               "--kind", "lesson", "--subject", f"lesson/{slug}",
               # INDEX_CONTENT_CHARS, not [:1000]: make_event REFUSES lesson
               # content over 200 chars rather than truncating it, so a
               # 1000-char slice is a permanent failure for every description
               # over 200 — the same bug fixed in cmd_write on 2026-08-08 and
               # missed here, its sibling call site.
               "--content", desc[:INDEX_CONTENT_CHARS],
               "--hook", desc[:HOOK_MAX_CHARS],
               "--body", body,
               "--session", os.environ.get("CLAUDE_SESSION_ID", "adopt-backfill"),
               "--lineage", "operator-direct" if lineage == "craig-direct"
                            else "contains-untrusted"]
        if prior:
            cmd += ["--supersedes", ",".join(prior)]
        if re.search(r"^\s*type:\s*reference\s*$", body, re.M) and \
                "--pointer" in mesh_emit.read_text(encoding="utf-8"):
            # Same carve-out as `write`: a reference-type FILE being carried
            # into the log may name the fact it points at. Any other type with
            # a fact literal is refused by make_event, and that refusal is the
            # one-home rule finding a copy — the reason is printed, not hidden.
            cmd += ["--pointer"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            adopted.append((slug, prior))
            print(f"adopted {slug}")
            # Same producer rule as `write`: adoption carries an existing FILE
            # into the log, so the memory is not new to Craig — but it IS new to
            # the fold's ranking, and that is what spends residency. Keeping it
            # on-demand preserves the status quo ante exactly; a slug that was
            # already resident is left alone by default_on_demand.
            touched += default_on_demand(slug)
        else:
            skipped.append((slug, proc_error(r)))

    if touched and args.commit and not getattr(args, "no_git", False):
        class _A:  # git_commit() reads .slug/.description/.no_push off args
            slug = f"{len(touched)} slug(s)"
            description = "default new adoptions to on-demand (admission policy E)"
            no_push = getattr(args, "no_push", False)
        git_commit(sorted(set(touched), key=str), _A())

    print(f"\n{'adopted' if args.commit else 'would adopt'}: {len(adopted)}"
          f" | skipped: {len(skipped)}")
    for slug, why in skipped:
        print(f"  skip {slug}: {why}")
    if not args.commit:
        print("(dry run — pass --commit to apply)")
    return 0


def cmd_write(args):
    """Serialise the whole check-then-write against other agents on this host.

    The lock spans the doctrine budget read, the file write, the index/exclude
    updates AND the mesh emit — because the invariant is "what I measured is
    still true when I write", and any gap between those two lets a second agent
    slip a doctrine row in between. A dry run takes no lock: it writes nothing,
    and blocking previews behind a writer's lock would make `stage` hang on a
    busy store for no safety gain.
    """
    if not args.commit:
        return _cmd_write(args)
    fh = door_lock()
    try:
        return _cmd_write(args)
    finally:
        fh.close()          # releases the flock


def _cmd_write(args):
    if not SLUG_RE.match(args.slug):
        raise SystemExit(f"error: slug must be kebab-case (letters/digits/hyphens): {args.slug!r}")
    if args.type not in TYPES:
        raise SystemExit(f"error: --type must be one of {sorted(TYPES)}")
    if args.lineage not in LINEAGES:
        raise SystemExit(f"error: --lineage must be one of {sorted(LINEAGES)}")

    # B2 (continuous-verification audit, 2026-08-06; quick-fix round per Grok
    # review — memory-mesh/reviews/2026-08-06-grok-b2-provenance-review.md +
    # .../2026-08-06-grok-b2-quickfix-convergence.md): a second signal on a
    # craig-direct claim, independent of the self-declared tag itself. B1
    # (evals/memory_poison_probe.py) proved the lineage tag alone gates
    # nothing — a GhostWriter-style mistag sails straight to the servable
    # index because nothing checks the tag against anything. This checks it
    # against ONE thing the caller cannot assert honestly by just typing a
    # different flag: did THIS session's own tool-call record show it
    # touching an untrusted-content tool (a web fetch, a Gmail/Outlook/
    # Calendar/Drive read) before this write? See
    # memory-mesh/hooks/session_provenance.py for the full design and the
    # STAGED (not-yet-wired) hook that produces the evidence this reads.
    #
    # Round 1 (shipped 2026-08-06) refused a flagged write unless the caller
    # passed --provenance-override. Grok's review named that the load-bearing
    # hole (F1): the flag sits on the SAME process that wants the write, so
    # an agent about to mistag a memory will just as happily rubber-stamp its
    # own override — "recorded in git" is not a control against the process
    # making the commit. This round: there is no override. A flagged
    # craig-direct write is silently REASSIGNED to contains-untrusted and
    # routed through the same require_enforceable_quarantine() gate below
    # that an honestly-declared contains-untrusted write already pays. The
    # write always SUCCEEDS — Grok's F2 finding was that a hard refusal
    # (even an audited one) creates exactly the friction that trains an
    # operator or agent to route around a control entirely; downgrading
    # instead of blocking removes that pressure while still keeping the
    # memory OUT of the trusted index until Craig reviews and promotes it
    # (memory-mesh/sign.py --promote).
    #
    # Applies to craig-direct only: a contains-untrusted claim already pays
    # the require_enforceable_quarantine() cost below, and this signal exists
    # to catch an UNDER-classified claim, not to further gate an honestly
    # over-cautious one.
    args.provenance = None
    if args.lineage == "craig-direct":
        SP = _session_provenance()
        session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if SP is None:
            # Degrade exactly like doctrine_budget_state(): never block a
            # write over an unmeterable check (the hook is not wired on any
            # host yet, so this is the common case today) — but never let an
            # unchecked claim render as a VERIFIED one either
            # (no-data-must-not-render-as-positive-data). UNVERIFIED is
            # deliberately NOT downgraded: forcing every write to
            # contains-untrusted before Craig has even authorized wiring the
            # hook would quarantine ordinary memory-writing by accident — a
            # much bigger behavior change than this fix is scoped to make.
            args.provenance = "unverified"
            print("NOTE: session-provenance check unavailable (module not "
                  "importable) — writing craig-direct as UNVERIFIED, not "
                  "clean. This is an observation gap, not a block.",
                  file=sys.stderr)
        else:
            state, detail = SP.state_for_session(session_id)
            args.provenance = state
            if state == "flagged":
                args.lineage = "contains-untrusted"
                args.provenance = "flagged-downgraded"
                print(f"NOTE: craig-direct write DOWNGRADED to "
                      f"contains-untrusted — this session's own tool-call "
                      f"record shows it touched untrusted content before "
                      f"this write ({detail}). A craig-direct tag from a "
                      f"session that just read a Gmail thread or fetched a "
                      f"web page is exactly the GhostWriter mistag B1 "
                      f"measured getting served unscreened. The memory is "
                      f"NOT lost — it is quarantined; promote it explicitly "
                      f"if it turns out unrelated to what the session read "
                      f"(memory-mesh/sign.py --promote).", file=sys.stderr)
            elif state == "unverified":
                print(f"NOTE: session-provenance UNVERIFIED for this write "
                      f"({detail}) — writing craig-direct without this "
                      f"second signal, not claiming it as clean.",
                      file=sys.stderr)

    # B3 (2026-08-06, Grok-reviewed — memory-mesh/reviews/2026-08-06-grok-b3
    # -plan-review.md): the "quiet twin" of has_signed_promotion(), found
    # during B3's own design review. That check gates retag/adopt; a plain
    # `write` on an ALREADY-promoted slug was a second, ungated door to the
    # same TOCTOU — a clean session (passing the check above with no
    # trouble) could silently overwrite signed content with unreviewed
    # bytes, never touching retag at all. Only engages when there's a
    # signed promotion on record to protect: brand-new memories and updates
    # to never-promoted craig-direct files are unaffected — same
    # downgrade-not-refuse philosophy as the session-taint check above, so
    # the write still succeeds, just not as the trusted claim it asked for.
    if args.lineage == "craig-direct":
        target_preview = STORE / f"{args.slug}.md"
        if target_preview.exists():
            SM = _mesh_lib()
            if SM is not None:
                ok, _reason = has_signed_promotion(args.slug)
                if ok:
                    new_fingerprint = SM.content_fingerprint(render_file(args))
                    old_fingerprint = SM.content_fingerprint(target_preview.read_text())
                    if new_fingerprint != old_fingerprint:
                        args.lineage = "contains-untrusted"
                        args.provenance = "promotion-revoked"
                        print(f"NOTE: craig-direct write DOWNGRADED to "
                              f"contains-untrusted — '{args.slug}' has a "
                              f"signed operator promotion on record, but "
                              f"this write's content does not match what "
                              f"was signed. A signature covers specific "
                              f"bytes; changed bytes are unreviewed bytes, "
                              f"even under an already-trusted slug. "
                              f"Re-promote if this change is legitimate "
                              f"(memory-mesh/sign.py --promote).",
                              file=sys.stderr)

    if args.lineage == "contains-untrusted":
        require_enforceable_quarantine()

    # SPEC v4 A4: bound the hook by REWRITE at the door, never by truncation
    # downstream. The hook is the line every session reads; a machine-cut rule
    # can lose the qualifier that made it correct ("...only when X"). Checked
    # here so the human fixes it while the content is in front of them, rather
    # than having emit.py refuse after the file is already written.
    if len(args.hook) > HOOK_MAX_CHARS:
        raise SystemExit(
            f"error: --hook is {len(args.hook)} chars, over the "
            f"{HOOK_MAX_CHARS} limit. Rewrite it shorter — it is the line "
            f"every session reads, and truncating it would cut the rule, not "
            f"just the prose.\n  {args.hook!r}")
    hit = fact_shape(args.rule, args.why, args.how, args.description, args.hook)
    if hit and args.type != "reference":
        raise SystemExit(
            f"error: fact-shaped content ({hit[1]}: {hit[0]!r}) — memory stores "
            f"behavior and pointers, never a second copy of an infrastructure "
            f"fact (one home per fact, FLEET.md seam rule 1, 2026-07-27).\n"
            f"Put the fact in its home ({FACT_HOMES}), then write a --type "
            f"reference memory that POINTS there if a recall hook is needed.")

    # SPEC v4 door metering. A full doctrine tier NEVER drops the lesson: it
    # lands as `state` carrying doctrine-candidate, which the brief surfaces as
    # a promotion queue. Grok round 2 caught the alternative — a nonzero exit
    # on a full tier turns session-end capture into amnesia under exactly the
    # pressure the door creates.
    args.doctrine_candidate = False
    if getattr(args, "residency", None) == "doctrine":
        budget = doctrine_budget_state()
        if budget is None:
            # Fail toward the SAFE tier, not toward "no metering". An
            # unmeterable doctrine write is exactly the one that must not
            # silently claim always-on space — but refusing it outright would
            # let a mesh outage block a memory write, which the mesh was built
            # never to do. Degrading to state keeps the lesson AND the budget.
            # (Undeclared-residency writes never reach here, so migration is
            # unaffected.)
            args.residency = "state"
            args.doctrine_candidate = True
            print("NOTE: doctrine budget is unmeasurable (mesh unavailable or "
                  "no doctrine tier yet) — writing as STATE with "
                  "doctrine-candidate set, not as unmetered doctrine.",
                  file=sys.stderr)
        else:
            used, cap, demotable = budget
            need = len(index_line(args).encode()) + 1
            if used + need > cap:
                args.residency = "state"
                args.doctrine_candidate = True
                print(f"NOTE: doctrine tier is full ({used}+{need} B > {cap} B "
                      f"cap) — writing as STATE with doctrine-candidate set.\n"
                      f"      The memory is NOT lost; it is queued for "
                      f"promotion in the morning brief.\n"
                      f"      Smallest demotable doctrine rows, if you want "
                      f"room now:", file=sys.stderr)
                for b, subj in demotable[:3]:
                    print(f"        {b:4d} B  {subj}", file=sys.stderr)

    rendered = render_file(args)
    target = STORE / f"{args.slug}.md"

    print(f"--- {target} ---")
    print(rendered)

    index_target = INDEX if args.lineage == "craig-direct" else QUARANTINE
    line = index_line(args)

    if not args.commit:
        print(f"\n(dry run — would write {target} and update {index_target.name}; pass --commit to apply)")
        return

    target.write_text(rendered)
    print(f"wrote {target}")

    if index_is_generated():
        # Fold-owned projections: the mesh event (emitted below) carries this
        # write into the generated index within one fold cycle. Editing a
        # generated file here would just be overwritten — and reintroduce
        # dueling writers.
        #
        # 2026-07-30: this deference now covers the QUARANTINE list too, not
        # only the craig-direct index. It was `args.lineage == "craig-direct"
        # and index_is_generated()`, so an untrusted write still hand-maintained
        # the store's quarantine list — a list whose stated owner consolidate.py
        # NO LONGER EXISTS, so nothing ever removed an entry again. That is how
        # a memory promoted and served in the morning was still listed as
        # quarantined hours later. The fold writes both projections from one
        # verdict set (Craig's ruling: "if I promote it, that must be fact
        # everywhere"), so both are left alone here.
        touched = [target]
        print(f"note: {index_target.name} is fold-generated (.mesh-generated) — "
              "index update flows via the mesh event")
        touched += default_on_demand(args.slug)
    else:
        index_text = index_target.read_text() if index_target.exists() else "# QUARANTINE\n\nUnpromoted contains-untrusted memories.\n"
        if args.supersedes:
            index_text = remove_index_line(index_text, args.supersedes)
        if args.lineage == "craig-direct":
            if args.slug in on_demand_slugs():
                # Respect the standing two-tier choice: updating an on-demand memory
                # refreshes the FILE, never re-promotes it to the always-on index.
                # (Before this guard, `write` on an on-demand slug added a second
                # index entry that `demote` then refused to clean up.)
                print(f"note: '{args.slug}' is on-demand (_index-exclude.txt) — "
                      "file updated, always-on index left alone. "
                      "Remove it from _index-exclude.txt to promote.")
            else:
                index_text = insert_index_line(index_text, args.section or "Unsorted",
                                               line, args.slug)
        else:
            index_text = index_text.rstrip("\n") + f"\n{line}\n"
        index_target.write_text(index_text)
        print(f"updated {index_target}")
        touched = [target, index_target]

    if not args.no_git:
        git_commit(touched, args)

    # Mesh dual-write (cutover phase 7, 2026-07-27): every store write also
    # becomes a mesh event so parallel sessions and peer hosts see it within
    # one fold cycle, and contradictions PARK instead of silently coexisting.
    # Best-effort by design — the store write above already succeeded, and a
    # mesh outage must never block a memory write (it degrades to exactly the
    # pre-mesh world, loudly).
    mesh_emit = _mesh_emit_path()
    if mesh_emit is not None:
        # SPEC v4: the event carries the FACT — the approved hook (the served
        # index line) and the full body — so store files become projections
        # the fold can repair on any host, and a pin signs the whole fact
        # rather than a slogan. `--content` stays the description for
        # backwards compatibility with every pre-v4 event already in the log.
        cmd = [sys.executable, str(mesh_emit), "--no-nudge",
               "--kind", "lesson", "--subject", f"lesson/{args.slug}",
               "--content", args.description[:INDEX_CONTENT_CHARS],
               "--hook", args.hook,
               "--body", rendered,
               "--session", os.environ.get("CLAUDE_SESSION_ID", "memory-write"),
               "--lineage", "operator-direct" if args.lineage == "craig-direct"
                            else "contains-untrusted"]
        if getattr(args, "residency", None):
            cmd += ["--residency", args.residency]
        if getattr(args, "expires", None):
            cmd += ["--expires", args.expires]
        if args.type == "reference" and \
                "--pointer" in mesh_emit.read_text(encoding="utf-8"):
            # The reference carve-out this door granted at fact_shape() above,
            # carried to the funnel so make_event's body gate honours the same
            # decision — otherwise the file lands and the event is refused.
            # Skew-guarded on the EMITTER's text: a host whose memory-mesh
            # checkout predates --pointer would choke on the flag, and that
            # older emit does not gate the body anyway, so nothing is lost.
            cmd += ["--pointer"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            # emit.py prints "emitted <id> (kind subject) -> log". That id is
            # the ONLY handle `sign.py --promote` accepts, and this used to
            # swallow it — so a quarantined write ended with "event emitted"
            # and no way to act on it short of grepping the ndjson by hand.
            # 2026-08-11: Craig was handed `--promote <slug>` and it failed
            # with "no event found". A producer that does not tell you what it
            # produced is half-shipped.
            emitted = re.search(r"emitted\s+([0-9a-f]{8,})", r.stdout or "")
            event_id = emitted.group(1) if emitted else None
            print(f"mesh: event emitted{f' ({event_id})' if event_id else ''}")
            if args.lineage != "craig-direct" or args.provenance == \
                    "flagged-downgraded":
                # Quarantined: not served by ANY tier until the operator's key
                # says otherwise. Print the exact command rather than the shape
                # of it — surfacing the depth at the decision point is the
                # whole difference between a note and an action.
                if event_id:
                    print(f"QUARANTINED (lineage contains-untrusted) — not "
                          f"served until promoted. Craig runs:\n"
                          f"  python3 memory-mesh/sign.py "
                          f"--promote {event_id}")
                else:
                    print("QUARANTINED — not served until promoted, but the "
                          "event id could not be parsed from emit output; "
                          "find it in ~/memory-events/events/*.ndjson")
        else:
            print(f"mesh: EMIT FAILED (store write is safe; mesh will lag): "
                  f"{proc_error(r)}")
        # A supersede must also retire the OLD slug's lesson event, or the
        # generated index would keep serving the superseded rule forever.
        if args.supersedes and index_is_generated():
            r2 = subprocess.run(
                [sys.executable, str(mesh_emit), "--no-nudge",
                 "--kind", "retract", "--subject", f"lesson/{args.supersedes}",
                 "--content", f"superseded by {args.slug}",
                 "--session", os.environ.get("CLAUDE_SESSION_ID", "memory-write"),
                 "--supersedes-live-on", f"lesson/{args.supersedes}"],
                capture_output=True, text=True, timeout=30)
            print("mesh: retract emitted for superseded "
                  f"lesson/{args.supersedes}" if r2.returncode == 0 else
                  f"mesh: RETRACT FAILED for lesson/{args.supersedes} — the old "
                  f"rule may linger in the generated index: "
                  f"{proc_error(r2)}")


def _subject_in_log(subject):
    """Is this exact subject live in the event log? Typo guard for prefixed
    subjects, which have no store file to check existence against."""
    M = _mesh_lib()
    if M is None:
        return False
    events, _ = M.read_all_events()
    reg = M.load_registry()
    return any(e["subject"] == subject for e in M.fold_events(events, reg)["live"])


def cmd_demote(args):
    """Move memories from the always-loaded index to the on-demand tier.

    Demotion is the cheapest lever the store has when MEMORY.md approaches its
    ~24.4 KB load ceiling (see memory-reconcile/budget.py): the fact file is
    untouched and stays fully live for /recall and the typed graph — only its
    always-on index line goes away. Nothing is deleted, so this is reversible
    by deleting the slug from _index-exclude.txt.

    It lives here rather than in a standalone script because the memory-write
    guard (Story 029) blocks every other write path into the store, and it
    should: an out-of-band editor is exactly the hole the lineage gate exists
    to close. Demotion writes no memory CONTENT, so it needs no lineage.
    """
    missing, demoted = [], []
    gen = index_is_generated()
    exclude_text = EXCLUDE.read_text() if EXCLUDE.exists() else ""
    already = {l.strip() for l in exclude_text.splitlines()
               if l.strip() and not l.startswith("#")}
    index_text = "" if gen else INDEX.read_text()

    if getattr(args, "undo", False):
        keep, dropped = [], []
        for line in exclude_text.splitlines():
            s = line.split("#", 1)[0].strip()
            if s and s in args.slugs:
                dropped.append(s)
            else:
                keep.append(line)
        absent = [s for s in args.slugs if s not in dropped]
        if absent:
            raise SystemExit(
                "error: not on-demand, nothing to undo: " + ", ".join(absent)
                + "\n(refusing a no-op undo — a typo would silently do nothing)")
        print(f"restoring {len(dropped)} memories to always-on:")
        for s in dropped:
            print(f"  {s}")
        if not args.commit:
            print("\n(dry run — pass --commit to apply)")
            return
        EXCLUDE.write_text("\n".join(keep).rstrip("\n") + "\n")
        print(f"updated {EXCLUDE}")
        print("the next fold STAGES this as a residency delta — it is not live "
              "until Craig promotes it.")
        if not args.no_git:
            class _A:
                slug = ", ".join(dropped)
                description = "restored to always-on (demote --undo)"
                no_push = args.no_push
            git_commit([EXCLUDE], _A())
        return

    for slug in args.slugs:
        # A PREFIXED SUBJECT (`home/cc-claude-md`, `ssh-route/{{REDACTED}}`) is a
        # legitimate demotion target with no store file behind it: those rows
        # are fold-emitted pointer events, not written memories. Until
        # 2026-08-01 both this loop and mesh_lib.residency_partition keyed on
        # the bare lesson slug, so such a row could not be named here AND would
        # not have matched the exclude list if it had been — an always-on row
        # that structurally could not leave the tier. Found when Craig asked to
        # demote `home/*` and the sanctioned door had no handle on that side.
        prefixed = "/" in slug
        if prefixed:
            head, _, tail = slug.partition("/")
            if not (SLUG_RE.match(head) and SLUG_RE.match(tail)):
                raise SystemExit(
                    f"error: subject must be kebab-case[/kebab-case]: {slug!r}")
        elif not SLUG_RE.match(slug):
            raise SystemExit(f"error: slug must be kebab-case: {slug!r}")
        # Existence check only applies to written memories. A prefixed subject
        # is verified against the LOG instead, so a typo still cannot silently
        # do nothing.
        if prefixed:
            if not _subject_in_log(slug):
                missing.append(slug)
                continue
        elif not (STORE / f"{slug}.md").exists():
            missing.append(slug)
            continue
        if gen:
            # Fold-owned index: demotion is purely an exclude-manifest change;
            # the next fold moves the slug to the on-demand appendix.
            if slug in already:
                print(f"note: '{slug}' already on-demand — nothing to do")
            else:
                demoted.append(slug)
            continue
        before = index_text
        index_text = remove_index_line(index_text, slug)
        stripped = index_text != before
        if slug in already:
            # Already excluded, but a stray always-on line can still exist (a
            # `write` update used to re-add one). Clean it up rather than
            # short-circuiting — the old code skipped here and left the
            # duplicate stranded, unfixable through the sanctioned tool.
            if stripped:
                print(f"note: '{slug}' already on-demand — removed a stray "
                      "always-on index line")
                demoted.append(slug)
            else:
                print(f"note: '{slug}' already on-demand and index is clean "
                      "— nothing to do")
            continue
        if not stripped:
            print(f"note: '{slug}' had no always-on index line — adding to exclude anyway")
        demoted.append(slug)

    if missing:
        raise SystemExit("error: no such memory file(s): " + ", ".join(missing)
                         + "\n(refusing to demote a slug that doesn't exist — "
                           "a typo would silently do nothing)")
    if not demoted:
        print("nothing to demote")
        return

    # Only slugs not already listed get appended — a stray-line cleanup on an
    # already-excluded slug must not duplicate it in the exclude file.
    to_add = [s for s in demoted if s not in already]
    new_exclude = (exclude_text.rstrip("\n") + "\n" + "\n".join(to_add) + "\n"
                   if to_add else exclude_text)

    print(f"demoting {len(demoted)} memories to on-demand:")
    for s in demoted:
        print(f"  {s}")
    if gen:
        print("\n(MEMORY.md is fold-generated — exclude manifest only; the "
              "next fold moves these to the on-demand appendix)")
    else:
        print(f"\nMEMORY.md: {len(INDEX.read_text())} B -> {len(index_text)} B "
              f"({len(INDEX.read_text()) - len(index_text)} B recovered)")

    if not args.commit:
        print("\n(dry run — pass --commit to apply)")
        return

    if not gen:
        INDEX.write_text(index_text)
        print(f"updated {INDEX}")
    EXCLUDE.write_text(new_exclude)
    print(f"updated {EXCLUDE}")

    if not args.no_git:
        class _A:  # git_commit() reads .slug/.description/.no_push off args
            slug = f"{len(demoted)} memories"
            description = "demoted to on-demand to recover index headroom"
            no_push = args.no_push
        git_commit([EXCLUDE] if gen else [INDEX, EXCLUDE], _A())


def cmd_delete(args):
    """Delete memories whose fact now lives in its ONE home (2026-07-27 purge).

    The counterpart of the fact-shape gate in cmd_write: the gate stops NEW
    fact-copies at the door; delete retires the existing ones once their fact
    has a verified home. --home is REQUIRED and recorded in the commit — the
    tool refuses an unexplained deletion the same way the gate refuses an
    unexplained fact. Files stay recoverable in the store's git history.

    Lives here for the same reason demote does: the memory-write guard blocks
    every out-of-band mutation of the store, and deletion must not be the one
    unguarded door.
    """
    missing, victims = [], []
    for slug in args.slugs:
        if not SLUG_RE.match(slug):
            raise SystemExit(f"error: slug must be kebab-case: {slug!r}")
        if (STORE / f"{slug}.md").exists():
            victims.append(slug)
        else:
            missing.append(slug)
    if missing:
        raise SystemExit("error: no such memory file(s): " + ", ".join(missing)
                         + "\n(refusing — a typo would silently delete nothing "
                           "while reporting success)")
    if not victims:
        print("nothing to delete")
        return

    gen = index_is_generated()
    index_text = "" if gen else INDEX.read_text()
    exclude_text = EXCLUDE.read_text() if EXCLUDE.exists() else ""
    for slug in victims:
        if not gen:
            index_text = remove_index_line(index_text, slug)
        exclude_text = "\n".join(l for l in exclude_text.splitlines()
                                 if l.strip() != slug) + "\n"

    print(f"deleting {len(victims)} memories (home: {args.home}):")
    for s in victims:
        print(f"  {s}")
    if not args.commit:
        print("\n(dry run — pass --commit to apply)")
        return

    paths = [EXCLUDE] if gen else [INDEX, EXCLUDE]
    for slug in victims:
        fp = STORE / f"{slug}.md"
        fp.unlink()
        paths.append(fp)
        print(f"deleted {fp}")
    if not gen:
        INDEX.write_text(index_text)
        print(f"updated {INDEX}")
    EXCLUDE.write_text(exclude_text)
    print(f"updated {EXCLUDE}")

    # Fold-owned index: also retire any live lesson event for each deleted
    # slug, or the generated MEMORY.md would keep serving a deleted memory.
    if gen:
        mesh_emit = _mesh_emit_path()
        if mesh_emit is not None:
            for slug in victims:
                r = subprocess.run(
                    [sys.executable, str(mesh_emit), "--no-nudge",
                     "--kind", "retract", "--subject", f"lesson/{slug}",
                     "--content", f"memory deleted — fact lives in {args.home}",
                     "--session", os.environ.get("CLAUDE_SESSION_ID", "memory-write"),
                     "--supersedes-live-on", f"lesson/{slug}"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    print(f"mesh: RETRACT FAILED for lesson/{slug}: "
                          f"{proc_error(r)}")

    if not args.no_git:
        class _A:
            slug = f"{len(victims)} memories"
            description = f"deleted — fact lives in its one home: {args.home}"
            no_push = args.no_push
        git_commit(paths, _A())


def cmd_flip(args):
    """Cutover phase 7 (memory-mesh): flip THIS store's MEMORY.md to
    fold-generated, or revert. The marker (.mesh-generated) is what every
    gated consumer keys on — this writer, consolidate.py, reconcile.py, and
    the fold itself. Lives here because the memory-write guard rightly blocks
    every out-of-band mutation of the store, including this one: the flip is
    a store-level state change and deserves the same one-door audit trail.
    Writes no memory CONTENT, so it needs no lineage (same reasoning as
    demote/retag)."""
    gitignore = STORE / ".gitignore"

    if args.revert:
        if not MESH_MARKER.exists():
            print("nothing to do — store is not flipped")
            return
        if not args.commit:
            print("(dry run) would: remove .mesh-generated, un-ignore and "
                  "re-track MEMORY.md as-is, commit.\nTo restore the last "
                  "hand-built index afterwards: git -C %s log --diff-filter=D "
                  "-- MEMORY.md  (then git checkout <sha>~1 -- MEMORY.md)" % STORE)
            return
        MESH_MARKER.unlink()
        if gitignore.exists():
            gitignore.write_text("".join(
                l for l in gitignore.read_text().splitlines(keepends=True)
                if l.strip() != "MEMORY.md"))
        _git("add", "-f", "--", "MEMORY.md", ".gitignore")
        _git("rm", "--cached", "--ignore-unmatch", "-q", "--",
             MESH_MARKER.name)
        _flip_commit("memory: flip-generated --revert — MEMORY.md back to "
                     "hand-maintained (marker removed)", args)
        print("reverted — MEMORY.md is hand-maintained again (currently holds "
              "the last generated content; see git log --diff-filter=D to "
              "restore the pre-flip index)")
        return

    if MESH_MARKER.exists():
        print("nothing to do — store is already flipped (.mesh-generated)")
        return
    if not args.commit:
        print("(dry run) would: write .mesh-generated, add MEMORY.md to "
              ".gitignore, git rm --cached MEMORY.md (history keeps the last "
              "hand-built index), commit + push. Pass --commit to apply.")
        return
    MESH_MARKER.write_text(
        "MEMORY.md is GENERATED by the memory-mesh fold (cutover phase 7, "
        "2026-07-28).\nRevert: memory_write.py flip-generated --revert "
        "--commit\n")
    ig = gitignore.read_text() if gitignore.exists() else ""
    if "MEMORY.md" not in ig.split():
        gitignore.write_text(ig.rstrip("\n") + ("\n" if ig else "") + "MEMORY.md\n")
    _git("add", "--", MESH_MARKER.name, ".gitignore")
    rc, out = _git("rm", "-q", "--cached", "--", "MEMORY.md")
    if rc != 0:
        print(f"note: git rm --cached MEMORY.md: {out}")
    _flip_commit("memory: flip-generated — MEMORY.md is fold-generated "
                 "(memory-mesh cutover phase 7); untracked, last hand-built "
                 "index preserved in history", args)
    print("flipped — the next fold writes MEMORY.md; consolidate/reconcile/"
          "this writer key off the marker")


def _flip_commit(msg, args):
    """Commit the staged index (the flip stages a removal, which a path-scoped
    git_commit() would drop). Verify nothing unrelated is staged first."""
    _, staged = _git("diff", "--cached", "--name-only")
    unrelated = [f for f in staged.splitlines()
                 if f not in ("MEMORY.md", ".gitignore", MESH_MARKER.name)]
    if unrelated:
        raise SystemExit("error: unrelated staged changes in the store — "
                         "refusing to sweep them into the flip commit: "
                         + ", ".join(unrelated))
    rc, out = _git("-c", "user.name={{REDACTED}}",
                   "-c", "user.email={{REDACTED}}@gmail.com",
                   "commit", "-q", "-m", msg)
    if rc != 0:
        raise SystemExit(f"error: flip commit failed: {out}")
    _, sha = _git("rev-parse", "--short", "HEAD")
    print(f"committed {sha} — {msg}")
    if getattr(args, "no_push", False):
        print("note: --no-push — commit is local only")
        return
    rc, out = _git("push", "-q")
    print("pushed" if rc == 0 else
          f"⚠ committed locally but PUSH FAILED: {out}")


_FM_LINEAGE = re.compile(r"^lineage:[ \t]*.*$\n?", re.M)
_FM_NESTED_LINEAGE = re.compile(r"^[ \t]+lineage:[ \t]*.*$\n?", re.M)


def _split_frontmatter(text):
    """(fm_body, rest) for a `---\\n...\\n---\\n` note, or (None, text)."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    return text[4:end + 1], text[end + 1:]


def set_lineage(text, value):
    """Return `text` with a top-level `lineage: <value>` in its frontmatter.

    Rewrites ONLY the lineage key: body, description, metadata and every other
    field are byte-preserved. That is the whole point — a lineage backfill must
    never become an excuse to regenerate a memory's content, or the tag would
    certify text the tagger just rewrote.

    Also strips any INDENTED `lineage:` (the nested-lineage drift
    memory-reconcile/lineage.py lints for: nested tags parse as `unknown`, so a
    note can carry a confident-looking tag that counts for nothing).
    """
    fm, rest = _split_frontmatter(text)
    if fm is None:
        raise ValueError("no `---` frontmatter block — refusing to guess")
    fm = _FM_LINEAGE.sub("", fm)
    fm = _FM_NESTED_LINEAGE.sub("", fm)
    line = f"lineage: {value}\n"
    # Order mirrors render_frontmatter(): name, description, lineage, ...
    anchor = re.search(r"^description:[ \t]*.*$\n", fm, re.M) \
        or re.search(r"^name:[ \t]*.*$\n", fm, re.M)
    fm = fm[:anchor.end()] + line + fm[anchor.end():] if anchor else line + fm
    return "---\n" + fm + rest


def cmd_retag(args):
    """Set `lineage:` on EXISTING memories without touching their content.

    Story 029's backfill path. Pre-029 memories carry no tag, so
    lineage.trust_class() reads them as `unknown` — which means
    policy_gate.justification_ok() will not let them anchor a privileged
    action, and reconcile.py flags them every week. The only other writers are
    `write` (regenerates the whole file from --rule/--why/--how — it would
    clobber the body) and a hand edit (blocked by the memory-write guard, and
    rightly: an out-of-band editor is the hole the gate exists to close).

    Retag writes no memory CONTENT, so it needs no lineage of its own — same
    reasoning as `demote`. It does NOT move index lines: promoting a note to
    `contains-untrusted` pulls it out of MEMORY.md into QUARANTINE.md on the
    next consolidate.py run, which owns that routing.

    B2 quick-fix (2026-08-06, Grok-reviewed): promoting TO craig-direct now
    requires `has_signed_promotion(slug)` — a live, cryptographically
    verified operator signature on `lesson/<slug>` in the mesh event log
    (memory-mesh/sign.py --promote, gated on Craig's passphrase key). Session
    taint (the check `_cmd_write` uses) is the WRONG signal here: retag
    doesn't author content this session, it promotes something already
    written, possibly long ago in a different, tainted session — a clean
    session checking its OWN taint proves nothing about the memory's
    history. Grok's review demonstrated the exact laundering path this
    closes: write as contains-untrusted (pays the quarantine cost), wait for
    a later clean session, retag straight to craig-direct with no session
    check catching it. See has_signed_promotion()'s docstring for the one
    known residual (the signed event isn't yet bound to a content hash).
    """
    if args.lineage not in LINEAGES:
        raise SystemExit(f"error: --lineage must be one of {sorted(LINEAGES)}")
    if args.lineage == "contains-untrusted":
        require_enforceable_quarantine()

    # VERBAL PROMOTION (2026-08-12, Craig's ruling). The second promotion class.
    # A per-call ARGUMENT carrying his actual words, deliberately not an env var
    # and not a bare flag — the _lib/mail.py authorized=True pattern, so it
    # cannot be set once and forgotten, and so the thing recorded is WHAT he
    # approved rather than merely THAT something was approved.
    #
    # Say plainly what this is not: it is not a security control. An agent can
    # pass any string. The signature path remains the only agent-impossible one.
    # What this buys is an auditable record and a distinct stamp, which is the
    # trade Craig made knowingly (decisions/verbal-approval-promotes-untrusted-
    # memory-2026-08-12.md).
    approved = (getattr(args, "operator_approved", None) or "").strip()
    PROMOTION_KEY = PROMOTION_VERBAL = None
    if args.lineage == "craig-direct":
        PROMOTION_KEY, PROMOTION_VERBAL, MIN_APPROVAL_WORDS = _promotion_vocab()
    if approved and args.lineage != "craig-direct":
        raise SystemExit(
            "error: --operator-approved only applies when promoting TO "
            "craig-direct. Approving something INTO quarantine is not a "
            "promotion, and silently ignoring the flag would teach the caller "
            "it had done something it had not.")
    if approved and len(approved) < MIN_APPROVAL_WORDS:
        raise SystemExit(
            f"error: --operator-approved must quote Craig's actual words "
            f"(at least {MIN_APPROVAL_WORDS} characters; got {len(approved)}).\n"
            "  The quote IS the audit trail for a promotion with no signature "
            "behind it. A token value would serve an untrusted-lineage memory "
            "while recording nothing anyone could check.")

    missing, unsigned, changed, unchanged = [], [], [], []
    for slug in args.slugs:
        if not SLUG_RE.match(slug):
            raise SystemExit(f"error: slug must be kebab-case: {slug!r}")
        p = STORE / f"{slug}.md"
        if not p.exists():
            missing.append(slug)
            continue
        klass = None
        if args.lineage == "craig-direct":
            if approved:
                klass = PROMOTION_VERBAL
            else:
                ok, reason = has_signed_promotion(slug)
                if not ok:
                    unsigned.append((slug, reason))
                    continue
                klass = PROMOTION_KEY
        before = p.read_text()
        # A key-signed promotion outranks a verbal one. Downgrading is legal —
        # Craig may re-approve something verbally — but it must never be quiet,
        # because the file would afterwards claim WEAKER provenance than it once
        # had and nothing else would say so.
        if klass == PROMOTION_VERBAL and f"promotion: {PROMOTION_KEY}" in before:
            print(f"warning: {slug} was {PROMOTION_KEY}; a verbal approval is "
                  f"WEAKER provenance and will replace that stamp",
                  file=sys.stderr)
        try:
            after = set_lineage(before, args.lineage)
            if klass:
                after = set_promotion(after, klass, approved or None)
        except ValueError as e:
            raise SystemExit(f"error: {slug}: {e}")
        (unchanged if after == before else changed).append((slug, p, after))

    if missing:
        raise SystemExit("error: no such memory file(s): " + ", ".join(missing)
                         + "\n(refusing to retag a slug that doesn't exist — "
                           "a typo would silently do nothing)")
    if unsigned:
        lines = "\n".join(f"    {slug}: {reason}" for slug, reason in unsigned)
        raise SystemExit(
            "error: refusing to retag to craig-direct:\n" + lines + "\n"
            "  retag no longer decides trust itself; it only executes what a "
            "signed mesh event already declared. Promote first:\n"
            "    memory-mesh/sign.py --promote <event-id>\n"
            "  (or --subject/--content directly), which requires Craig's "
            "passphrase-gated key and will call this retag for you as its "
            "mechanical follow-through. A 'modified after promotion' reason "
            "means the file changed since it was last signed — re-promote "
            "the CURRENT content deliberately, don't assume the old "
            "signature still applies.")

    for slug, _, _ in unchanged:
        print(f"note: '{slug}' already lineage: {args.lineage} — skipping")
    if not changed:
        print("nothing to retag")
        return

    print(f"retagging {len(changed)} memories -> lineage: {args.lineage}")
    for slug, _, _ in changed:
        print(f"  {slug}")
    if not args.commit:
        print("\n(dry run — pass --commit to apply)")
        return

    paths = []
    for slug, p, after in changed:
        p.write_text(after)
        paths.append(p)
    print(f"wrote {len(paths)} files")

    if not args.no_git:
        class _A:
            slug = f"{len(changed)} memories"
            description = f"lineage backfill -> {args.lineage}"
            no_push = args.no_push
        git_commit(paths, _A())


def _promotion_vocab():
    """(PROMOTION_KEY, PROMOTION_VERBAL, MIN_APPROVAL_WORDS) from mesh_lib.

    Read from the one authority rather than copied here. memory_write.py and
    mesh_lib already carry one duplicated constant (INDEX_CONTENT_CHARS) with a
    drift WARNING as its only guard; a second copy of the promotion vocabulary
    would be the same bug with worse consequences, because a drifted class name
    would silently stop matching the fold's serve gate and the memory would just
    never be served. Degrades safe: no mesh, no promotion.
    """
    M = _mesh_lib()
    if M is None:
        raise SystemExit(
            "error: memory-mesh is not importable, so the promotion vocabulary "
            "cannot be read from its one authority and a promotion cannot be "
            "stamped. Refusing rather than guessing a class name that might not "
            "match the fold's serve gate.")
    return M.PROMOTION_KEY, M.PROMOTION_VERBAL, M.MIN_APPROVAL_WORDS


_FM_PROMOTION = re.compile(r"^promotion:[ \t]*.*$\n?", re.M)
_FM_APPROVED = re.compile(r"^approved:[ \t]*.*$\n?", re.M)


def set_promotion(text, klass, words=None):
    """Stamp HOW a memory was promoted, byte-preserving everything else.

    Two classes since 2026-08-12 (Craig: "there is key signed and verbally
    signed"). Both are stamped, not just the weak one: if only verbal
    promotions carried a marker, an UNSTAMPED file would be ambiguous between
    "key-signed" and "predates this field", and the audit question — how did
    this become trusted? — would have no answer for exactly the files where it
    matters. Absence now means "promoted before 2026-08-12", which is honest.

    `approved:` holds Craig's verbatim words on the verbal path. That quote is
    the entire audit trail for a promotion with no signature behind it, so it
    is stored next to the fact rather than only in the mesh event log.
    """
    fm, rest = _split_frontmatter(text)
    if fm is None:
        raise ValueError("no `---` frontmatter block — refusing to guess")
    fm = _FM_PROMOTION.sub("", fm)
    fm = _FM_APPROVED.sub("", fm)
    lines = f"promotion: {klass}\n"
    if words:
        # json.dumps gives correct YAML-compatible quoting/escaping for a
        # free-text quote that may contain colons, quotes or newlines.
        lines += f"approved: {json.dumps(words, ensure_ascii=False)}\n"
    anchor = (re.search(r"^lineage:[ \t]*.*$\n", fm, re.M)
              or re.search(r"^description:[ \t]*.*$\n", fm, re.M)
              or re.search(r"^name:[ \t]*.*$\n", fm, re.M))
    fm = fm[:anchor.end()] + lines + fm[anchor.end():] if anchor else lines + fm
    return "---\n" + fm + rest


_FM_CORRECTED = re.compile(r"^corrected:[ \t]*.*$\n?", re.M)


def set_corrected(text, stamp):
    """Return `text` with a top-level `corrected: <stamp>` in its frontmatter.

    A visible staleness marker: memory-prune's LLM pass is staleness-triggered,
    so a note that has been corrected once is exactly the kind of note worth
    re-reading later."""
    fm, rest = _split_frontmatter(text)
    if fm is None:
        raise ValueError("no `---` frontmatter block — refusing to guess")
    fm = _FM_CORRECTED.sub("", fm)
    line = f"corrected: {stamp}\n"
    anchor = (re.search(r"^lineage:[ \t]*.*$\n", fm, re.M)
              or re.search(r"^description:[ \t]*.*$\n", fm, re.M)
              or re.search(r"^name:[ \t]*.*$\n", fm, re.M))
    fm = fm[:anchor.end()] + line + fm[anchor.end():] if anchor else line + fm
    return "---\n" + fm + rest


def _reemit_corrected(slug):
    """Carry a just-corrected FILE back into the event log.

    Root cause of the four store/event divergences alarming on every fold as of
    2026-08-01 (`memory-mesh/audits/2026-08-01-rehome-and-divergence-audit.md`):
    `correct` wrote the store file and emitted nothing, so the file forked away
    from its event permanently and the fold alarmed forever after. Worse, the
    alarm's advice — `adopt <slug>` — refused precisely because an event body
    existed, and the only remaining path (signing the stale event) would have
    overwritten the correction with the text it corrected. Measured on
    `cc-backup`: the event still claimed a {{REDACTED}} cron job retired by the
    2026-07-26 systemd migration, so signing it would have restored a verified
    falsehood.

    A correction is a supersede, not a side channel. Emitting here keeps the
    log the one home and makes `correct` self-consistent by construction.
    """
    class _Adopt:
        slugs = [slug]
        yes = True
        commit = True
        reconcile = True
    try:
        cmd_adopt(_Adopt())
    except SystemExit as e:              # mesh not importable — file is written
        print(f"warning: correction to {slug} did NOT reach the event log "
              f"({e}). The fold will alarm on the divergence until it does; "
              f"resolve with: memory_write.py adopt {slug} --reconcile --commit",
              file=sys.stderr)


def cmd_correct(args):
    """Fix a WRONG FACT inside a memory without rewriting the memory.

    The gap this closes (found 2026-07-29 by memory-reconcile): every other way
    to fix one stale line was disproportionate or forbidden.
      * `write` regenerates the body from --rule/--why/--how, so correcting one
        path in a 127-line reference memory means re-authoring the document —
        and re-authoring to fix a typo is how the *rest* of a document silently
        drifts.
      * a hand edit is blocked by the memory-write guard, and rightly: an
        out-of-band editor is the hole the lineage gate exists to close.
    So the cheapest honest fix cost a full rewrite, nobody paid it, and five
    known-wrong facts sat in the always-on store for weeks. A store that can
    add knowledge and replace it wholesale, but cannot cheaply CORRECT it, will
    always drift toward wrong.

    Unlike `retag` and `demote`, this writes memory CONTENT, so it needs its
    own honest --lineage: whoever sourced the correction is making a claim.

    Fails closed, loudly, on anything ambiguous — an edit that silently hits
    the wrong occurrence is worse than the stale fact it replaces.
    """
    if args.lineage not in LINEAGES:
        raise SystemExit(f"error: --lineage must be one of {sorted(LINEAGES)}")
    if args.lineage == "contains-untrusted":
        require_enforceable_quarantine()
    if not SLUG_RE.match(args.slug):
        raise SystemExit(f"error: slug must be kebab-case: {args.slug!r}")
    if args.old == args.new:
        raise SystemExit("error: --old and --new are identical — nothing to correct")

    p = STORE / f"{args.slug}.md"
    if not p.exists():
        raise SystemExit(f"error: no such memory: {args.slug}\n"
                         "(refusing to correct a slug that doesn't exist — a "
                         "typo would silently do nothing)")
    before = p.read_text()
    fm, rest = _split_frontmatter(before)
    if fm is None:
        raise SystemExit(f"error: {args.slug}: no `---` frontmatter — refusing to guess")

    # The laundering guard runs BEFORE any write path, not inside one of them.
    # It first sat below the --in-description branch, which returns early — so
    # the description (the field that decides whether a memory surfaces at all)
    # was writable with contains-untrusted lineage while the body was not. A
    # control that only covers the path you thought of is not a control; found
    # by an adversarial review of this very change, 2026-07-29.
    note_lineage = re.search(r"^lineage:[ \t]*(\S+)", fm, re.M)
    note_lineage = note_lineage.group(1) if note_lineage else "unknown"
    if args.lineage == "contains-untrusted" and note_lineage != "contains-untrusted":
        raise SystemExit(
            f"error: refusing to apply a contains-untrusted correction to a "
            f"'{note_lineage}' memory.\nThat is the laundering path the gate "
            "exists to close. If the correction is real, `demote`/`retag` the "
            "note deliberately first, or write it as its own quarantined note.")

    # B2 quick-fix (2026-08-06, Grok-reviewed): correct writes memory CONTENT
    # with its own --lineage claim, same class of live assertion as `write` —
    # so it gets the same session-provenance check, not retag's signed-mesh
    # check (retag/adopt promote something already-written; correct is
    # authoring new body text right now, possibly from something this
    # session just read). Unlike `write`, correct can't silently redirect to
    # contains-untrusted: its whole charter is "never touch the lineage
    # frontmatter" (that's what the check above enforces the other
    # direction), so a flagged session's claim of craig-direct is refused
    # outright rather than downgraded.
    if args.lineage == "craig-direct":
        SP = _session_provenance()
        session_id = getattr(args, "session_id", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if SP is not None:
            state, detail = SP.state_for_session(session_id)
            if state == "flagged":
                raise SystemExit(
                    "error: refusing a craig-direct correction — this "
                    "session's own tool-call record shows it touched "
                    f"untrusted content before this write ({detail}).\n"
                    "  correct cannot quarantine in place (it never touches "
                    "the lineage frontmatter). If the correction is real:\n"
                    "   - write it as a fresh --lineage contains-untrusted "
                    "note and promote later, or\n"
                    "   - apply the correction from a session that hasn't "
                    "touched untrusted content this session.")

    # --in-description scopes the same exact-substring replace to the
    # `description:` line. It is a separate flag rather than a widening of the
    # body search because description feeds RECALL RELEVANCE: it decides
    # whether a memory surfaces at all, so editing it should be a deliberate
    # act, never something a body correction does as a side effect.
    if args.in_description:
        dm = re.search(r"^description:[ \t]*(.*)$", fm, re.M)
        if not dm:
            raise SystemExit(f"error: {args.slug}: no `description:` line")
        cur = dm.group(1)
        if cur.count(args.old) != 1:
            raise SystemExit(
                f"error: {args.slug}: --old appears {cur.count(args.old)} times "
                "in description (need exactly 1)")
        new_fm = fm[:dm.start(1)] + cur.replace(args.old, args.new) + fm[dm.end(1):]
        after = set_corrected("---\n" + new_fm + rest, args.stamp)
        print(f"correcting {args.slug} DESCRIPTION (lineage: {args.lineage})")
        print(f"  - {cur}\n  + {cur.replace(args.old, args.new)}")
        if not args.commit:
            print("\n(dry run — pass --commit to apply)")
            return
        p.write_text(after)
        print(f"wrote {p}  ({len(after)} B)")
        _reemit_corrected(args.slug)
        if not args.no_git:
            class _A:
                slug = args.slug
                description = args.reason or "corrected the description"
                no_push = args.no_push
            git_commit([p], _A())
        return

    # Body only. Correcting frontmatter through a text-replace would let a
    # "correction" rewrite name/description/lineage — use write/retag for those.
    nl = rest.find("\n")
    body_start = nl + 1 if nl != -1 else len(rest)
    head, body = rest[:body_start], rest[body_start:]
    if args.old in fm:
        raise SystemExit(f"error: {args.slug}: --old matches FRONTMATTER, not the "
                         "body. Use `write` (description) or `retag` (lineage).")

    hits = body.count(args.old)
    if hits == 0:
        raise SystemExit(f"error: {args.slug}: --old not found in the body.\n"
                         "The memory may already have been corrected, or the "
                         "text differs (check whitespace/line wrapping).")
    if hits > 1:
        raise SystemExit(f"error: {args.slug}: --old appears {hits} times — "
                         "ambiguous.\nLengthen --old until it is unique; a "
                         "correction that hits the wrong occurrence is worse "
                         "than the stale fact.")

    after_body = body.replace(args.old, args.new)
    after = set_corrected("---\n" + fm + head + after_body, args.stamp)

    print(f"correcting {args.slug}  (lineage: {args.lineage}, {len(before)} B)")
    print(f"  - {args.old}")
    print(f"  + {args.new}")
    if args.reason:
        print(f"  reason: {args.reason}")
    if not args.commit:
        print("\n(dry run — pass --commit to apply)")
        return

    p.write_text(after)
    print(f"wrote {p}  ({len(after)} B)")
    _reemit_corrected(args.slug)
    if not args.no_git:
        class _A:
            slug = args.slug
            description = args.reason or "corrected a stale fact"
            no_push = args.no_push
        git_commit([p], _A())


def selftest():
    """Deterministic, isolated, bounded — proves the B2 session-provenance
    check at the ACTUAL enforced chokepoint (_cmd_write), not a preview
    helper. Called both via the door-locked production entry point
    (cmd_write) and DIRECTLY (_cmd_write) to prove there is no wrapper-only
    enforcement a caller could route around.

    SAFETY: every write lands in a throwaway shadow store + a throwaway,
    git-init'd shadow mesh event log (same MESH_ROOT-diversion mechanism
    evals/memory_poison_probe.py and memory-mesh/drill.py already use, and
    the same sandbox guard: a fresh subprocess must show
    mesh_lib.harness_store() resolving to None under the diverted MESH_ROOT
    BEFORE anything is planted, or this refuses to proceed). Nothing here
    can reach Craig's real store or ~/memory-events. Verified again at the
    end: the real store is globbed for the selftest's self-labeling
    zzselftest-b2-* slugs.
    """
    import shutil
    import subprocess
    import tempfile

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'ok' if cond else 'FAIL'}: {name}")

    global STORE, INDEX, QUARANTINE, EXCLUDE, MESH_MARKER
    real_store = STORE

    tmp = Path(tempfile.mkdtemp(prefix="memory-write-selftest-"))
    shadow_store = tmp / "store"
    shadow_store.mkdir()
    (shadow_store / "MEMORY.md").write_text("# MEMORY\n\n## Unsorted\n")
    shadow_mesh = tmp / "mesh_root"
    (shadow_mesh / "events").mkdir(parents=True)
    git_env = os.environ.copy()
    subprocess.run(["git", "init", "-q", str(shadow_mesh)], check=True, env=git_env)
    subprocess.run(["git", "-C", str(shadow_mesh), "config", "user.email",
                     "{{OPERATOR_EMAIL}}"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(shadow_mesh), "config", "user.name",
                     "selftest"], check=True, env=git_env)
    (shadow_mesh / "events" / ".keep").write_text("")
    (shadow_mesh / ".gitignore").write_text("views/\nstate/\nview.version\n")
    subprocess.run(["git", "-C", str(shadow_mesh), "add", "-A"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(shadow_mesh), "commit", "-q", "-m",
                     "shadow mesh seed"], check=True, env=git_env)
    prov_log = tmp / "provenance.jsonl"
    # Set once, up front, for the whole selftest (not toggled per-scenario):
    # the B2 quick-fix routes a flagged write through the SAME
    # require_enforceable_quarantine() gate an honest contains-untrusted
    # write already pays, and that gate raises unless the store looks
    # mesh-adopted. Every scenario below needs that to be true.

    saved_env = {k: os.environ.get(k) for k in
                 ("MESH_ROOT", "MESH_HOST", "SESSION_PROVENANCE_LOG",
                  "CLAUDE_CODE_SESSION_ID")}
    os.environ["MESH_ROOT"] = str(shadow_mesh)
    os.environ["MESH_HOST"] = "memory-write-selftest"
    os.environ["SESSION_PROVENANCE_LOG"] = str(prov_log)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def base_args(**over):
        ns = argparse.Namespace(
            slug="zzselftest-b2-provenance", type="user",
            description="selftest artifact (evals-style ZZ marker, never a "
                        "real memory) — exercises the B2 session-provenance "
                        "check",
            lineage="craig-direct", rule="selftest artifact — not real",
            why=None, how=None, hook="selftest artifact",
            section="Unsorted", residency=None, expires=None,
            supersedes=None, contradicts=None, session_id=None,
            commit=True, no_git=True, no_push=True)
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    try:
        # Safety FIRST: prove the sandbox guard engages under the diverted
        # MESH_ROOT, in a fresh subprocess, before planting anything.
        code = ("import sys; sys.path.insert(0, %r); import mesh_lib as M; "
                 "print(repr(M.harness_store()))"
                 % str(_mesh_code_dir()))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=os.environ.copy(), timeout=30)
        sandboxed = r.stdout.strip() == "None"
        check("sandbox guard engaged (mesh_lib.harness_store() -> None "
              "under diverted MESH_ROOT) — safety proven BEFORE any write",
              sandboxed)
        if not sandboxed:
            raise RuntimeError("SAFETY ABORT: refusing to plant anything — "
                                f"harness_store() returned {r.stdout!r}")

        STORE = shadow_store
        INDEX = shadow_store / "MEMORY.md"
        QUARANTINE = shadow_store / "QUARANTINE.md"
        EXCLUDE = shadow_store / "_index-exclude.txt"
        MESH_MARKER = shadow_store / ".mesh-generated"
        assert str(STORE).startswith(str(tmp)), "STORE patch did not take"
        assert not str(STORE).startswith(str(real_store)), \
            "SAFETY ABORT: shadow STORE resolves under the real store"
        MESH_MARKER.write_text("selftest marker\n")

        # 1. No session id at all -> unverified, write proceeds. (This is
        #    also the FIRST craig-direct write, so it's what lazily imports
        #    session_provenance and binds its LOG to our shadow prov_log —
        #    every later scenario relies on that having already happened.)
        a = base_args(session_id=None)
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("no session id -> write proceeds", p.exists())
        check("no session id -> stamped provenance: unverified",
              p.exists() and "provenance: unverified" in p.read_text())

        SP = sys.modules.get("session_provenance")
        check("session_provenance module was actually imported by _cmd_write "
              "(not skipped)", SP is not None)

        # 1b. Fact-shape gate: the door and the funnel share ONE discriminator
        #     (mesh_lib.FACT_SHAPES, 2026-09-16). A project-type memory with an
        #     IP in its rule is refused at the door BEFORE any file lands; a
        #     reference-type one is admitted at the door AND the --pointer
        #     carve-out reaches make_event, so the event lands in the mesh.
        #     The half-state this closes is "file written, event refused".
        a = base_args(slug="zzselftest-b2-factcopy", type="project",
                      rule="the Envoy is at {{HOST_IP}}, verified",
                      session_id=None)
        try:
            _cmd_write(a)
            refused = False
        except SystemExit:
            refused = True
        check("fact-shaped rule in a project memory is REFUSED at the door", refused)
        check("…and no file landed for it",
              not (shadow_store / f"{a.slug}.md").exists())
        a = base_args(slug="zzselftest-b2-factref", type="reference",
                      rule="the Envoy is at {{HOST_IP}} — see FLEET.md",
                      session_id=None)
        _cmd_write(a)
        check("reference memory with a fact is admitted at the door",
              (shadow_store / f"{a.slug}.md").exists())
        logged = "".join(p.read_text(encoding="utf-8")
                         for p in (shadow_mesh / "events").glob("*.ndjson"))
        check("…and its event REACHED the mesh (--pointer carried the carve-out "
              "through make_event's body gate — no file-without-event half-state)",
              f"lesson/{a.slug}" in logged and "{{HOST_IP}}" in logged)

        # 2. SessionStart witnessed, no untrusted touch -> clean.
        SP.record("SessionStart", {"session_id": "sess-clean"})
        a = base_args(slug="zzselftest-b2-clean", session_id="sess-clean")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("clean session -> write proceeds without override", p.exists())
        check("clean session -> stamped provenance: clean",
              p.exists() and "provenance: clean" in p.read_text())

        # 3. Untrusted touch this session -> DOWNGRADED, not refused. Round 1
        #    (2026-08-06) refused unless the caller passed
        #    --provenance-override; Grok's review named that flag itself as
        #    the hole (F1 — the same process that mistags can rubber-stamp
        #    its own override). This round: the write always SUCCEEDS —
        #    called via _cmd_write DIRECTLY (bypassing the door_lock wrapper
        #    cmd_write()), the no-bypass proof: the check is IN the
        #    sanctioned entry point, not a wrapper a direct call skips.
        SP.record("SessionStart", {"session_id": "sess-flagged"})
        SP.record("PreToolUse", {"session_id": "sess-flagged",
                                  "tool_name": "WebFetch", "tool_input": {}})
        a = base_args(slug="zzselftest-b2-flagged", session_id="sess-flagged")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        body = p.read_text() if p.exists() else ""
        check("flagged session, _cmd_write called DIRECTLY -> write STILL "
              "SUCCEEDS (no refusal, no override needed — there is none)",
              p.exists())
        check("flagged write is REASSIGNED to lineage: contains-untrusted",
              "lineage: contains-untrusted" in body)
        check("flagged write is stamped provenance: flagged-downgraded",
              "provenance: flagged-downgraded" in body)
        check("no provenance_override line exists anywhere (flag removed, "
              "nothing to record)", "provenance_override" not in body)

        # 3b. Same flagged session via the PRODUCTION entry point
        #     (cmd_write(), which takes the door lock) -> also downgrades.
        a2 = base_args(slug="zzselftest-b2-flagged2", session_id="sess-flagged")
        cmd_write(a2)
        p2 = shadow_store / f"{a2.slug}.md"
        check("flagged session downgrades via the production cmd_write() "
              "wrapper too (enforcement isn't direct-call-only either)",
              p2.exists() and "lineage: contains-untrusted" in p2.read_text())

        # 4. The CLI flag itself is gone, not just unused — invoke the REAL
        #    CLI (a subprocess, not an in-process call) with
        #    --provenance-override and confirm argparse itself rejects it.
        #    Proves the removal at the actual command surface, not just that
        #    nothing in this module happens to call it anymore.
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "write",
             "--slug", "zzselftest-b2-cliflag", "--type", "user",
             "--description", "d", "--rule", "r", "--hook", "h",
             "--provenance-override", "should not parse"],
            capture_output=True, text=True, timeout=30)
        check("--provenance-override is REJECTED by the real CLI (argparse "
              "unrecognized-argument error, not silently accepted)",
              r.returncode != 0 and "unrecognized arguments" in r.stderr
              and "--provenance-override" in r.stderr)

        # 5. contains-untrusted lineage, explicitly declared: UNTOUCHED by
        #    this check even in a flagged session — it already pays
        #    require_enforceable_quarantine() the ordinary way.
        a = base_args(slug="zzselftest-b2-untrusted",
                       lineage="contains-untrusted", session_id="sess-flagged")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("contains-untrusted lineage: no provenance: line at all "
              "(check is craig-direct-scoped, by design)",
              p.exists() and "provenance:" not in p.read_text())

        # 6. Corrupt provenance log -> unverified (never "clean"), write
        #    still proceeds (never blocks on an unmeterable/broken check).
        prov_log.write_text("not valid json at all\n")
        a = base_args(slug="zzselftest-b2-corrupt", session_id="sess-clean")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("corrupt provenance log -> stamped unverified, not clean",
              p.exists() and "provenance: unverified" in p.read_text())
        prov_log.write_text("")

        # 7. Instrument unavailable (simulated import failure) -> degrades
        #    to unverified; never a silent pass rendered as clean
        #    (no-data-must-not-render-as-positive-data).
        real_lookup = globals()["_session_provenance"]
        globals()["_session_provenance"] = lambda: None
        try:
            a = base_args(slug="zzselftest-b2-noinstrument",
                           session_id="sess-clean")
            _cmd_write(a)
            p = shadow_store / f"{a.slug}.md"
            check("instrument unavailable -> degrades to unverified, "
                  "write still proceeds (never blocks on an unmeterable "
                  "check)",
                  p.exists() and "provenance: unverified" in p.read_text())
        finally:
            globals()["_session_provenance"] = real_lookup

        # 8. retag: the signed-mesh gate closes the laundering path Grok's
        #    review found in round 1 (dirty session -> quarantined, wait for
        #    a later CLEAN session, retag straight to craig-direct with no
        #    session check catching it — session state is the wrong signal
        #    for a tool that promotes already-written content).
        a = base_args(slug="zzselftest-b2-retagtarget",
                       lineage="contains-untrusted", session_id="sess-clean")
        _cmd_write(a)
        target_file = shadow_store / "zzselftest-b2-retagtarget.md"
        check("retag target seeded as contains-untrusted", target_file.exists())

        hsp_ok, hsp_reason = has_signed_promotion("zzselftest-b2-retagtarget")
        check("has_signed_promotion() against a REAL, empty shadow mesh "
              "(no monkeypatch) -> (False, reason), refuse by default",
              hsp_ok is False and "never promoted" in hsp_reason)

        retag_args = argparse.Namespace(
            slugs=["zzselftest-b2-retagtarget"], lineage="craig-direct",
            commit=True, no_git=True, no_push=True)
        refused = False
        try:
            cmd_retag(retag_args)
        except SystemExit:
            refused = True
        check("retag to craig-direct with NO signed promotion -> refused",
              refused)
        check("refused retag left the file's lineage UNCHANGED",
              "lineage: contains-untrusted" in target_file.read_text())

        real_hsp = globals()["has_signed_promotion"]
        globals()["has_signed_promotion"] = (
            lambda slug: (True, "mock: signed") if slug == "zzselftest-b2-retagtarget"
            else (False, "mock: not this slug"))
        try:
            cmd_retag(retag_args)
            check("retag SUCCEEDS once has_signed_promotion() confirms a "
                  "signed event (mesh_lib.fold_events' own _signed "
                  "re-verification, not this module's to redo)",
                  "lineage: craig-direct" in target_file.read_text())
        finally:
            globals()["has_signed_promotion"] = real_hsp

        # 8a-verbal. THE SECOND PROMOTION CLASS (2026-08-12, Craig's ruling:
        # "there is key signed and verbally signed"). Every promise the new
        # path makes gets an assertion, including the ones that are
        # inconvenient — that a verbal promotion is REFUSED when it quotes
        # nothing, and that it is stamped WEAKER rather than equal.
        target_file.write_text(set_lineage(target_file.read_text(),
                                           "contains-untrusted"))
        va = argparse.Namespace(
            slugs=["zzselftest-b2-retagtarget"], lineage="craig-direct",
            commit=True, no_git=True, no_push=True,
            operator_approved="build it. There is key signed and verbally signed")
        cmd_retag(va)
        after = target_file.read_text()
        check("verbal approval promotes to craig-direct with NO signed event "
              "(the whole point of the ruling)",
              "lineage: craig-direct" in after)
        check("a verbal promotion is STAMPED verbally-signed, so it can never "
              "be mistaken for a key-signed one",
              "promotion: verbally-signed" in after)
        check("Craig's verbatim words are recorded in the file — the only "
              "audit trail a signature-less promotion has",
              "verbally signed" in after and after.count("approved:") == 1)

        for bad, why in (("ok", "too short to quote anything"),
                         ("   ", "whitespace only")):
            target_file.write_text(set_lineage(target_file.read_text(),
                                               "contains-untrusted"))
            refused = False
            try:
                cmd_retag(argparse.Namespace(
                    slugs=["zzselftest-b2-retagtarget"], lineage="craig-direct",
                    commit=True, no_git=True, no_push=True,
                    operator_approved=bad))
            except SystemExit:
                refused = True
            check(f"a token --operator-approved ({why}) is REFUSED, not "
                  f"silently accepted", refused)
            check("the refused verbal retag left lineage UNCHANGED",
                  "lineage: contains-untrusted" in target_file.read_text())

        refused = False
        try:
            cmd_retag(argparse.Namespace(
                slugs=["zzselftest-b2-retagtarget"],
                lineage="contains-untrusted", commit=True, no_git=True,
                no_push=True,
                operator_approved="approving this into quarantine is meaningless"))
        except SystemExit:
            refused = True
        check("--operator-approved with --lineage contains-untrusted is "
              "REFUSED rather than ignored — a silently-dropped flag teaches "
              "the caller it did something it did not", refused)

        globals()["has_signed_promotion"] = (
            lambda slug: (True, "mock: signed"))
        try:
            target_file.write_text(set_lineage(target_file.read_text(),
                                               "contains-untrusted"))
            cmd_retag(argparse.Namespace(
                slugs=["zzselftest-b2-retagtarget"], lineage="craig-direct",
                commit=True, no_git=True, no_push=True,
                operator_approved=None))
            check("a KEY-signed promotion is stamped too, so an unstamped file "
                  "is unambiguously 'promoted before 2026-08-12' rather than "
                  "'key-signed, probably'",
                  "promotion: key-signed" in target_file.read_text())
        finally:
            globals()["has_signed_promotion"] = real_hsp

        # 8b. B3: has_signed_promotion() itself detects a post-promotion
        #     content change, not just retag's plumbing around it. Simulate
        #     a signed event carrying a body_sha256 that does NOT match the
        #     current file (content changed after signing) by monkeypatching
        #     mesh_lib.fold_events() through the real function — cheapest
        #     way to exercise the real comparison logic without needing an
        #     actual interactive signature.
        real_mesh_lib = globals()["_mesh_lib"]
        import types
        fake_signed_stale = {
            "subject": "lesson/zzselftest-b3-stale", "_signed": True,
            "body_sha256": "0" * 64,  # will not match any real content
        }
        fake_signed_legacy = {
            "subject": "lesson/zzselftest-b3-legacy", "_signed": True,
            # no body_sha256 key at all -> grandfathered
        }

        class _FakeMeshLib:
            def read_all_events(self):
                return [], 0

            def load_registry(self):
                return {}

            def fold_events(self, events, registry):
                return {"live": [fake_signed_stale, fake_signed_legacy]}

            content_fingerprint = staticmethod(real_mesh_lib().content_fingerprint)

        (shadow_store / "zzselftest-b3-stale.md").write_text(
            "---\nname: zzselftest-b3-stale\ndescription: d\n"
            "lineage: craig-direct\nmetadata:\n  node_type: memory\n"
            "  type: user\n---\n\nchanged after promotion\n")
        (shadow_store / "zzselftest-b3-legacy.md").write_text(
            "---\nname: zzselftest-b3-legacy\ndescription: d\n"
            "lineage: craig-direct\nmetadata:\n  node_type: memory\n"
            "  type: user\n---\n\nlegacy content\n")

        globals()["_mesh_lib"] = lambda: _FakeMeshLib()
        try:
            ok, reason = has_signed_promotion("zzselftest-b3-stale")
            check("B3: signed event with a body_sha256 that does NOT match "
                  "the current file -> refused, distinct 'modified after "
                  "promotion' reason (the actual TOCTOU catch)",
                  ok is False and "modified after promotion" in reason)

            ok, reason = has_signed_promotion("zzselftest-b3-legacy")
            check("B3: signed event with NO body_sha256 at all (pre-B3, "
                  "legacy) -> grandfathered, still trusted",
                  ok is True and "legacy" in reason)
        finally:
            globals()["_mesh_lib"] = real_mesh_lib

        # 8c. B3's real invariance property, unmocked: content_fingerprint()
        #     is identical across a lineage change, different across a body
        #     change — proven directly, not by inference from retag's
        #     behavior alone.
        M_real = real_mesh_lib()
        text_a = ("---\nname: x\ndescription: d\nlineage: craig-direct\n"
                  "metadata:\n  node_type: memory\n  type: user\n---\n\nbody\n")
        text_b_same_content_diff_lineage = text_a.replace(
            "lineage: craig-direct", "lineage: contains-untrusted")
        text_c_diff_body = text_a.replace("body\n", "DIFFERENT body\n")
        check("content_fingerprint(): identical body, different lineage "
              "line -> SAME fingerprint (invariant under retag's one "
              "sanctioned mutation)",
              M_real.content_fingerprint(text_a) ==
              M_real.content_fingerprint(text_b_same_content_diff_lineage))
        check("content_fingerprint(): different body -> DIFFERENT "
              "fingerprint (sensitive to the thing that actually matters)",
              M_real.content_fingerprint(text_a) !=
              M_real.content_fingerprint(text_c_diff_body))

        # 8d. The write-path twin (Delta 2): overwriting an ALREADY-promoted
        #     craig-direct memory with different content, from a CLEAN
        #     session, auto-demotes instead of silently keeping the trusted
        #     stamp — closes the door B3's retag-only design would have
        #     left wide open.
        promoted_slug = "zzselftest-b3-writepath"
        (shadow_store / f"{promoted_slug}.md").write_text(
            "---\nname: %s\ndescription: d\nlineage: craig-direct\n"
            "metadata:\n  node_type: memory\n  type: user\n---\n\n"
            "original signed content\n" % promoted_slug)
        fake_signed_writepath = {
            "subject": f"lesson/{promoted_slug}", "_signed": True,
            "body_sha256": M_real.content_fingerprint(
                (shadow_store / f"{promoted_slug}.md").read_text()),
        }

        class _FakeMeshLibWritepath:
            def read_all_events(self):
                return [], 0

            def load_registry(self):
                return {}

            def fold_events(self, events, registry):
                return {"live": [fake_signed_writepath]}

            content_fingerprint = staticmethod(real_mesh_lib().content_fingerprint)

        globals()["_mesh_lib"] = lambda: _FakeMeshLibWritepath()
        try:
            wa = base_args(slug=promoted_slug, session_id="sess-clean",
                            rule="DIFFERENT unreviewed content")
            _cmd_write(wa)
            body = (shadow_store / f"{promoted_slug}.md").read_text()
            check("write-path twin: overwriting a signed-promoted memory "
                  "with DIFFERENT content, clean session, DOWNGRADES to "
                  "contains-untrusted (not silently kept craig-direct)",
                  "lineage: contains-untrusted" in body)
            check("write-path twin: stamped provenance: promotion-revoked",
                  "provenance: promotion-revoked" in body)
        finally:
            globals()["_mesh_lib"] = real_mesh_lib

        # Same slug, SAME content as what's on disk now (post-revocation) —
        # a same-content write must NOT trip the check (nothing to protect
        # once the fake signed event's hash no longer applies here; this
        # also proves the check doesn't false-positive on a no-op write).
        wa2 = base_args(slug="zzselftest-b2-clean", session_id="sess-clean")
        _cmd_write(wa2)
        check("write-path twin: an ordinary craig-direct write with no "
              "prior signed promotion on record is UNAFFECTED",
              "provenance: promotion-revoked" not in
              (shadow_store / "zzselftest-b2-clean.md").read_text())

        # 9. correct: a live content claim (like write), so it gets the
        #    session-taint check, not the signed-mesh one — and since
        #    correct's charter is "never touch lineage frontmatter" it
        #    refuses outright rather than downgrading.
        # Re-seed sess-flagged: test 6 (corrupt-log) truncated prov_log to
        # empty as part of ITS OWN cleanup, which wiped every session's
        # recorded rows, sess-flagged included — without this, sess-flagged
        # would read as unverified here, not flagged, and the refusal below
        # would pass for the wrong reason (or not exercise the real check).
        SP.record("SessionStart", {"session_id": "sess-flagged"})
        SP.record("PreToolUse", {"session_id": "sess-flagged",
                                  "tool_name": "WebFetch", "tool_input": {}})
        correct_args = argparse.Namespace(
            slug="zzselftest-b2-clean", old="not real",
            new="not real (corrected in a flagged session — should refuse)",
            lineage="craig-direct", in_description=False,
            reason="selftest", stamp="2026-08-06", commit=True,
            no_git=True, no_push=True, session_id="sess-flagged")
        before_body = (shadow_store / "zzselftest-b2-clean.md").read_text()
        refused = False
        try:
            cmd_correct(correct_args)
        except SystemExit:
            refused = True
        check("correct with --lineage craig-direct from a FLAGGED session "
              "-> refused", refused)
        check("refused correction left the body UNCHANGED",
              (shadow_store / "zzselftest-b2-clean.md").read_text() == before_body)

        correct_args.session_id = "sess-clean"
        cmd_correct(correct_args)
        check("correct with --lineage craig-direct from a CLEAN session "
              "-> succeeds",
              "corrected in a flagged session" in
              (shadow_store / "zzselftest-b2-clean.md").read_text())

        # 10. adopt: an UNTAGGED file (no lineage: key at all) used to
        #     default silently to craig-direct — closed the same way as
        #     retag, by requiring a signed promotion before trusting it.
        import contextlib
        import io
        untagged = shadow_store / "zzselftest-b2-untagged.md"
        untagged.write_text(
            "---\nname: zzselftest-b2-untagged\n"
            "description: selftest untagged legacy file\n"
            "metadata:\n  node_type: memory\n  type: user\n---\n\n"
            "legacy body, no lineage tag at all\n")
        adopt_args = argparse.Namespace(
            slugs=["zzselftest-b2-untagged"], yes=True, commit=False,
            reconcile=False, no_git=True, no_push=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _cmd_adopt(adopt_args)
        check("adopt refuses to silently default an UNTAGGED file to "
              "craig-direct without a signed promotion",
              "untagged file, never promoted" in buf.getvalue())

        # (b3) REGRESSION, 2026-08-09: adopt sliced --content to [:1000] while
        # make_event REFUSES lesson content over INDEX_CONTENT_CHARS (200) —
        # it never truncates. So every description longer than 200 chars was a
        # permanent emit failure on this path. cmd_write was fixed on
        # 2026-08-08; this sibling call site was missed, which is why the check
        # asserts the BOUND rather than one known-bad length.
        long_desc = "L" * 260
        longf = shadow_store / "zzselftest-b2-longdesc.md"
        longf.write_text(
            f"---\nname: zzselftest-b2-longdesc\n"
            f"description: {long_desc}\n"
            "lineage: craig-direct\nprovenance: clean\n"
            "metadata:\n  node_type: memory\n  type: user\n---\n\n"
            "body long enough to adopt\n")
        seen = []

        class _FakeProc:
            returncode, stdout, stderr = 0, "", ""

        real_run = subprocess.run
        subprocess.run = lambda cmd, *a, **k: (seen.append(cmd), _FakeProc())[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                # commit=True on purpose: a dry run returns before the emit is
                # ever built, so commit=False would assert the bound against
                # an empty command list. subprocess.run is intercepted and
                # no_git suppresses the commit, so nothing escapes the shadow.
                _cmd_adopt(argparse.Namespace(
                    slugs=["zzselftest-b2-longdesc"], yes=True, commit=True,
                    reconcile=False, no_git=True, no_push=True))
        finally:
            subprocess.run = real_run

        emits = [c for c in seen if "--content" in c]
        check("adopt reached the mesh emit for a long-description file",
              bool(emits))
        over = [c[c.index("--content") + 1] for c in emits
                if len(c[c.index("--content") + 1]) > INDEX_CONTENT_CHARS]
        check(f"adopt bounds --content to INDEX_CONTENT_CHARS "
              f"({INDEX_CONTENT_CHARS}) so make_event cannot refuse it",
              not over)
        # The hook has its own, tighter door; assert it too rather than
        # trusting that one bound implies the other.
        hooks_over = [c[c.index("--hook") + 1] for c in emits
                      if "--hook" in c
                      and len(c[c.index("--hook") + 1]) > HOOK_MAX_CHARS]
        check(f"adopt bounds --hook to HOOK_MAX_CHARS ({HOOK_MAX_CHARS})",
              not hooks_over)

        # (b4) proc_error must surface the EXCEPTION line of a traceback, not
        # the header — the clip that turned an actionable refusal into a bare
        # "skipped" for the operator.
        class _Tb:
            returncode, stdout = 1, ""
            stderr = ('Traceback (most recent call last):\n'
                      '  File "emit.py", line 148, in main\n'
                      '    ev, line = M.make_event(\n'
                      'ValueError: make_event refused lesson/x: content is '
                      '224 chars; the renderer cuts at 200\n')
        check("proc_error surfaces the exception line, not the traceback head",
              proc_error(_Tb()).startswith("ValueError: make_event refused")
              and "content is 224 chars" in proc_error(_Tb()))

        class _Plain:
            returncode, stdout, stderr = 1, "", "emit: line one\n  and line two"
        check("proc_error keeps a multi-line hand-written error whole",
              "line two" in proc_error(_Plain()))
    finally:
        STORE = real_store
        INDEX = real_store / "MEMORY.md"
        QUARANTINE = real_store / "QUARANTINE.md"
        EXCLUDE = real_store / "_index-exclude.txt"
        MESH_MARKER = real_store / ".mesh-generated"
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
        check("shadow tempdir removed", not tmp.exists())
        real_hits = list(real_store.glob("zzselftest-b2-*"))
        check("real memory store has ZERO hits for the selftest markers",
              not real_hits)

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true",
                     help="run the deterministic offline selftest and exit "
                          "(shadow store + shadow mesh; never touches the "
                          "real store)")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("retag", help="set lineage: on existing memories (Story 029 backfill)")
    r.add_argument("slugs", nargs="+")
    r.add_argument("--lineage", required=True, help=" | ".join(sorted(LINEAGES)))
    r.add_argument("--operator-approved", metavar="WORDS",
                   help="promote to craig-direct on Craig's VERBAL approval "
                        "instead of a signed mesh event (2026-08-12). Value must "
                        "be his verbatim words; they are recorded in the file. "
                        "Weaker than a signature and stamped as such.")
    r.add_argument("--commit", action="store_true",
                   help="apply (default is dry-run). Also git-commits + pushes.")
    r.add_argument("--no-git", action="store_true")
    r.add_argument("--no-push", action="store_true")
    r.set_defaults(func=cmd_retag)

    c = sub.add_parser("correct",
                       help="fix a wrong fact in a memory's body, byte-preserving the rest")
    c.add_argument("--slug", required=True)
    c.add_argument("--old", required=True,
                   help="exact text to replace; must appear EXACTLY ONCE in the body")
    c.add_argument("--new", required=True, help="what it should say")
    c.add_argument("--lineage", required=True,
                   help="honest source of the CORRECTION: " + " | ".join(sorted(LINEAGES)))
    c.add_argument("--in-description", action="store_true",
                   help="scope the replace to the frontmatter `description:` "
                        "line (recall relevance) instead of the body")
    c.add_argument("--reason", help="why (goes in the commit message)")
    c.add_argument("--stamp", default=datetime.date.today().isoformat(),
                   help="date for the `corrected:` frontmatter marker")
    c.add_argument("--commit", action="store_true",
                   help="apply (default is dry-run). Also git-commits + pushes.")
    c.add_argument("--no-git", action="store_true")
    c.add_argument("--no-push", action="store_true")
    c.set_defaults(func=cmd_correct)

    d = sub.add_parser("demote", help="move memories to the on-demand tier (index headroom)")
    d.add_argument("slugs", nargs="+")
    d.add_argument("--undo", action="store_true",
                   help="REVERSE a demotion: drop these from the exclude "
                        "manifest so the next fold restores their always-on "
                        "row. Demotion has always been documented as "
                        "'reversible by deleting the slug from "
                        "_index-exclude.txt' — but that file is behind the "
                        "memory-write guard, so until 2026-08-01 there was no "
                        "sanctioned path back and the door was one-way in "
                        "practice. Still a residency delta: the fold stages "
                        "it and Craig promotes.")
    d.add_argument("--commit", action="store_true",
                   help="apply (default is dry-run). Also git-commits + pushes.")
    d.add_argument("--no-git", action="store_true")
    d.add_argument("--no-push", action="store_true")
    d.set_defaults(func=cmd_demote)

    ad = sub.add_parser("adopt",
                        help="SPEC v4: carry a store file's body into the event "
                             "log (the fact's one home), superseding any "
                             "pre-existing event for the same memory")
    ad.add_argument("slugs", nargs="+")
    ad.add_argument("--yes", action="store_true",
                    help="skip the per-item confirmation. Intended for a "
                         "reviewed batch file, never for interactive bulk use.")
    ad.add_argument("--commit", action="store_true",
                    help="apply (default is dry-run). Also git-commits.")
    ad.add_argument("--reconcile", action="store_true",
                    help="FILE WINS on a store/event divergence: supersede an "
                         "event that already carries a body with the file's "
                         "current text. Without this, adopt refuses such a "
                         "slug — which left the fold's own advice "
                         "('adopt <slug> (file wins)') a dead end, and the "
                         "only other path (signing the stale event) silently "
                         "destroys the correction. Prints the diff first.")
    ad.set_defaults(func=cmd_adopt)

    x = sub.add_parser("delete", help="delete memories whose fact now lives in its one home")
    x.add_argument("slugs", nargs="+")
    x.add_argument("--home", required=True,
                   help="where the fact now lives (recorded in the commit) — required")
    x.add_argument("--commit", action="store_true",
                   help="apply (default is dry-run). Also git-commits + pushes.")
    x.add_argument("--no-git", action="store_true")
    x.add_argument("--no-push", action="store_true")
    x.set_defaults(func=cmd_delete)

    f = sub.add_parser("flip-generated",
                       help="cutover phase 7: MEMORY.md becomes fold-generated "
                            "(--revert to go back)")
    f.add_argument("--revert", action="store_true")
    f.add_argument("--commit", action="store_true",
                   help="apply (default is dry-run). Also git-commits + pushes.")
    f.add_argument("--no-push", action="store_true")
    f.set_defaults(func=cmd_flip)

    w = sub.add_parser("write", help="write or update a memory file + its MEMORY.md/QUARANTINE.md line")
    w.add_argument("--slug", required=True)
    w.add_argument("--type", required=True)
    w.add_argument("--description", required=True)
    w.add_argument("--lineage", default="craig-direct")
    w.add_argument("--rule", required=True, help="the lesson/fact body text")
    w.add_argument("--why")
    w.add_argument("--how")
    w.add_argument("--hook", required=True, help="short MEMORY.md index hook text")
    w.add_argument("--section", help="MEMORY.md section header to file under (default: Unsorted)")
    # SPEC v4. Deliberately NOT defaulted: residency is Craig's declaration at
    # the /improve gate, and a default would quietly re-invent the derived
    # residency this design exists to replace. Undeclared renders exactly as
    # v3 did, so omitting it is safe during migration.
    w.add_argument("--residency", choices=["doctrine", "state", "pinned"],
                   help="SPEC v4 tier (Craig declares it). doctrine/pinned "
                        "need an operator signature to hold across the mesh.")
    w.add_argument("--expires", metavar="YYYY-MM-DD",
                   help="state rows only: render-hide after this date")
    w.add_argument("--supersedes")
    w.add_argument("--contradicts")
    w.add_argument("--session-id")
    # B2 quick-fix (2026-08-06, Grok-reviewed): --provenance-override REMOVED.
    # Round 1 shipped it as an audited escape hatch; Grok's adversarial review
    # (memory-mesh/reviews/2026-08-06-grok-b2-provenance-review.md, finding
    # F1) called it correctly: a flag the SAME agent that mistagged the write
    # can also pass is not a control, it's a soft gate. "Recorded in git" is
    # not a check against the process making the commit. There is no
    # replacement flag — see _cmd_write: a flagged session now downgrades the
    # write to contains-untrusted instead of refusing it, so there is nothing
    # to override.
    # NAMING TRAP, kept deliberately: --commit has always meant "actually write
    # the file, as opposed to dry-run" — it never meant "git commit". That read
    # the obvious wrong way and cost 10 days / 64 uncommitted memories. The flag
    # keeps its name (every skill doc and habit uses it) but now does BOTH, which
    # is what everyone already believed it did.
    w.add_argument("--commit", action="store_true",
                   help="apply the write (default is dry-run/preview). Also git-commits "
                        "+ pushes the touched files unless --no-git/--no-push.")
    w.add_argument("--no-git", action="store_true",
                   help="write the files but do not git-commit them")
    w.add_argument("--no-push", action="store_true",
                   help="git-commit but do not push (commit stays on this disk)")
    w.set_defaults(func=cmd_write)

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.cmd:
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
