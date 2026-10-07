#!/usr/bin/env python3
"""memory_write.py — deterministic writer for the auto-memory store.

Backs the `improve` and `capture` skills: formats frontmatter, maintains the
MEMORY.md index (add/update/remove), and records supersedes (new file gets
`supersedes:`, old index line dropped, old file kept as history).

Store: ~/.claude/projects/<workspace-key>/memory/ (MEMORY.md = index).
`contains-untrusted` memories are written but never served: the memory-mesh
fold holds them out of the index and /recall. Promotion needs a signed mesh
event (memory-mesh/sign.py --promote). Without the mesh, untrusted writes are
refused (see require_enforceable_quarantine()).

Usage:
    # New or updated memory (same slug = in-place update)
    python3 memory_write.py write --slug foo-bar --type feedback \\
        --description "one-line description for recall relevance" \\
        --lineage operator-direct \\
        --rule "The lesson, stated as a concrete rule." \\
        --why "Why it matters." --how "Exactly what to do next time." \\
        --hook "short index hook" --section "Working Practices & Harness Lessons"

    # A belief changed: write the replacement, drop old-slug's index line,
    # keep old-slug's file on disk as history.
    python3 memory_write.py write --slug new-slug --type feedback \\
        --supersedes old-slug --rule "..." --why "..." --how "..." \\
        --hook "..." --section "..." --description "..."

    # user / reference memories: single paragraph, no --why/--how
    python3 memory_write.py write --slug likes-x --type user \\
        --description "..." --rule "Single paragraph body." \\
        --hook "..." --section "..."

Maintenance subcommands (index/frontmatter only, no --lineage of their own):

    # free always-on index headroom (fact file stays live for /recall)
    python3 memory_write.py demote slug-a slug-b --commit

    # set lineage: on EXISTING notes, body byte-preserved
    python3 memory_write.py retag slug-a slug-b --lineage operator-direct --commit

Prints the rendered file + MEMORY.md diff. Without --commit it is a dry run.
Stdlib only.
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
# Order: $MESH_CODE_DIR, this file's directory, <root>/memory-mesh walking up
# from cwd, then the legacy ~/{{REDACTED}} path.
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


def _emit_tier(slugs, tier):
    """Replicate an index-tier change as `tier` events so every host's fold agrees.

    The local change already happened; a failure here is loud, never fatal."""
    d = _mesh_code_dir()
    tool = d / "tier.py" if d else None
    if not (tool and tool.is_file() and slugs):
        if slugs:
            print("mesh: tier.py not found — this tier change stays LOCAL to this "
                  "host until someone runs tier.py", file=sys.stderr)
        return
    r = subprocess.run([sys.executable, str(tool), f"--{tier}", *slugs, "--commit"],
                       capture_output=True, text=True, timeout=120)
    tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
    print(f"mesh: tier {tier}: {tail[0]}" if r.returncode == 0 else
          f"mesh: TIER EMIT FAILED (local change stands): {proc_error(r)}")


def _mesh_emit_path():
    """The emitter, or None — and None is said out loud by every caller."""
    d = _mesh_code_dir()
    return (d / "emit.py") if d else None


# The harness keys the store by the workspace path with / → -.
def _store():
    """The store the harness serves for this workspace.

    $MEMORY_WRITE_STORE, else mesh_lib.store_dir(), else the first cwd ancestor
    whose store carries .mesh-generated, else the ~/{{REDACTED}} store."""
    override = os.environ.get("MEMORY_WRITE_STORE")
    if override:
        return Path(override).expanduser()
    # Ask mesh_lib so both halves of the door agree on the store.
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
# When this marker exists MEMORY.md is generated by the mesh fold and must not
# be edited here; the exclude manifest and QUARANTINE.md stay this writer's.
MESH_MARKER = STORE / ".mesh-generated"
# Mirrors mesh_lib.HOOK_MAX_CHARS (checked in _mesh_lib(); copied so the door
# works with the mesh absent).
HOOK_MAX_CHARS = 140
# Mirrors mesh_lib.INDEX_CONTENT_CHARS: make_event refuses longer lesson content.
INDEX_CONTENT_CHARS = 200
# Bytes of the generated index that are not rows (header, counts, stub).
HARNESS_HEADER_RESERVE = 600


def index_is_generated():
    return MESH_MARKER.exists()


def proc_error(r, limit=400):
    """The most informative line of a failed subprocess.

    For a traceback that is the last line (the exception message); otherwise
    the output as-is, truncated to `limit`.
    """
    text = (r.stderr or r.stdout or "").strip()
    if not text:
        return f"no output (exit {r.returncode})"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1 and lines[0].lstrip().startswith("Traceback"):
        return lines[-1].strip()[:limit]
    return text[:limit]


def require_enforceable_quarantine():
    """Refuse a contains-untrusted write on a host without the mesh fold.

    The fold (opted in via MESH_MARKER) is quarantine's only enforcer. No
    --force on purpose: the alternative is to write a document instead.
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
        "  A operator-direct memory is unaffected; only the untrusted class is "
        "refused.")


TYPES = {"feedback", "user", "project", "reference"}
LINEAGES = {"operator-direct", "contains-untrusted"}
# Legacy spelling of operator-direct; accepted on input and on read.
LEGACY_LINEAGES = {"craig-direct": "operator-direct"}


def canon_lineage(value):
    """The current spelling of a lineage value; legacy aliases map forward."""
    return LEGACY_LINEAGES.get(value, value)
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def on_demand_slugs():
    """Slugs kept out of the always-on index (an update must not promote them)."""
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
    """Default a NEW memory to on-demand (admission policy E).

    Returns the paths touched. Never demotes a slug that is already resident.
    """
    if slug in resident_slugs():
        print(f"note: '{slug}' is already always-on — left resident. "
              f"Demoting a live rule is the owner's call, not a write's side effect.")
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
    # Session-provenance verdict: clean | unverified | flagged-downgraded.
    if getattr(args, "provenance", None):
        lines.append(f"provenance: {args.provenance}")
    if args.supersedes:
        lines.append(f"supersedes: [{args.supersedes}]")
    if args.contradicts:
        lines.append(f"contradicts: [{args.contradicts}]")
    # Mirror of the event's residency for readers; the event is the authority.
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
        # No matching section: file under Unsorted (created if missing).
        print(f"warning: section '{section}' not found in MEMORY.md — filing under Unsorted", file=sys.stderr)
        m = find_section(text, "Unsorted")
        if not m:
            return text.rstrip("\n") + f"\n\n## Unsorted (auto-added by memory_write.py)\n{line}\n"
    # Insert at the end of the section's body.
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
    """Commit only the files this invocation touched, then push.

    Scoped to `paths` so unrelated in-flight edits are not swept in. Never
    fatal: the write already succeeded, so failures are reported loudly.
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


# --- Fact-shape gate ("one home per fact") -----------------------------------
# Infrastructure facts (hosts, endpoints, reachability) live in their owning
# doc or code; memory points at them. Mirrors mesh_lib.FACT_SHAPES so the door
# works with the mesh absent; _mesh_lib() warns if they diverge. `0.0.0.0` is
# excluded as the all-sources CIDR idiom, not a host.
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
    """Import session_provenance lazily, or None.

    A missing import degrades the operator-direct check to UNVERIFIED, never clean."""
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
    """Import mesh_lib lazily, or None; warns if mirrored constants drifted.

    The mesh is optional: features needing it degrade rather than refuse a write.
    """
    try:
        d = _mesh_code_dir()
        if d is None:
            return None
        sys.path.insert(0, str(d))
        import mesh_lib
    except Exception:
        return None
    # Mirrored limits must match, or the emitter refuses after the file landed.
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
    if [rx.pattern for rx, _ in FACT_SHAPES] != \
            [rx.pattern for rx, _ in getattr(mesh_lib, "FACT_SHAPES", [])]:
        print("warning: FACT_SHAPES disagrees — memory_write's fact-shape gate "
              "and mesh_lib.make_event's will refuse different writes; the "
              "door could admit a body the funnel then refuses AFTER the file "
              "landed (mesh_lib older than 2026-09-16, or the lists drifted)",
              file=sys.stderr)
    return mesh_lib


def has_signed_promotion(slug):
    """(ok, reason): does `lesson/<slug>` carry a verified operator signature
    covering its current content?

    The only path to promote a memory to operator-direct via `retag`. Relies on
    fold_events() re-verifying signatures (`_signed`) and compares the file's
    content_fingerprint() to the signed `body_sha256`; events without
    `body_sha256` are accepted as legacy. Any failure returns (False, reason).
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

    Advisory flock on a dedicated lock file, not the store directory.
    """
    import fcntl
    lock_path = STORE / ".door.lock"
    lock_path.touch(exist_ok=True)
    fh = open(lock_path, "r+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def doctrine_budget_state():
    """(used_bytes, cap_bytes, demotable_rows) for the doctrine tier, or None.

    None when the mesh is absent or no memory has declared doctrine residency.
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
    # Doctrine cap = delivered size minus bytes pins actually use (not the pin cap).
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
    """Carry existing store files into the event log.

    Emits a body-carrying event per file and supersedes any prior events for
    the same memory. Shows each body and confirms per item unless --yes.
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
        # An untagged file needs a signed promotion to be adopted as
        # operator-direct; an explicit tag was already checked when it was set.
        if raw_lineage is None:
            ok, reason = has_signed_promotion(slug)
            if not ok:
                skipped.append((slug, f"untagged file, {reason} — "
                                      "defaulting to operator-direct is "
                                      "refused; retag it explicitly after "
                                      "`sign.py --promote`, or leave it "
                                      "contains-untrusted"))
                continue
        lineage = canon_lineage(raw_lineage) or "operator-direct"
        desc = _frontmatter_value(body, "description") or slug
        # Residency is NOT read from frontmatter (a hand-editable mirror);
        # adopted memories arrive undeclared.
        prior = M.unsuperseded_ids(f"lesson/{slug}", events)
        already = [e for e in events
                   if e["subject"] == f"lesson/{slug}" and e.get("body")]
        if already and not getattr(args, "reconcile", False):
            skipped.append((slug, "already carries a body in the log "
                                  "(divergence? use --reconcile)"))
            continue
        if len(body.encode()) > M.MAX_EVENT_BYTES - 1024:
            # Refuse, never truncate: a partial body would overwrite the file.
            skipped.append((slug, f"body {len(body.encode())}B too large — "
                                  f"split it or shorten before adopting"))
            continue

        print(f"\n=== {slug}  ({len(body.encode())} B, lineage={lineage})")
        print(f"    supersedes {len(prior)} prior event(s): "
              f"{', '.join(prior) if prior else 'none'}")
        if already:
            # Show the diff against the body being replaced.
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
               # make_event refuses content over INDEX_CONTENT_CHARS.
               "--content", desc[:INDEX_CONTENT_CHARS],
               "--hook", desc[:HOOK_MAX_CHARS],
               "--body", body,
               "--session", os.environ.get("CLAUDE_SESSION_ID", "adopt-backfill"),
               "--lineage", lineage if lineage == "operator-direct"
                            else "contains-untrusted"]
        if prior:
            cmd += ["--supersedes", ",".join(prior)]
        if re.search(r"^\s*type:\s*reference\s*$", body, re.M) and \
                "--pointer" in mesh_emit.read_text(encoding="utf-8"):
            # A reference-type memory may name the fact it points at.
            cmd += ["--pointer"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            adopted.append((slug, prior))
            print(f"adopted {slug}")
            # New to the fold's ranking, so default to on-demand like `write`.
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
    """Run _cmd_write under the door lock (budget read through mesh emit).

    A dry run takes no lock.
    """
    if not args.commit:
        return _cmd_write(args)
    fh = door_lock()
    try:
        return _cmd_write(args)
    finally:
        fh.close()          # releases the flock


def _write_store_file(path, text):
    """Write a store file without following a symlink (refused loudly)."""
    path = Path(path)
    if path.is_symlink():
        raise SystemExit(f"error: {path} is a symlink — refusing to write "
                         "through it (a store file must be a regular file)")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError as exc:
        raise SystemExit(f"error: cannot write {path} ({exc}) — refusing")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _cmd_write(args):
    if not SLUG_RE.match(args.slug):
        raise SystemExit(f"error: slug must be kebab-case (letters/digits/hyphens): {args.slug!r}")
    if args.type not in TYPES:
        raise SystemExit(f"error: --type must be one of {sorted(TYPES)}")
    args.lineage = canon_lineage(args.lineage)
    if args.lineage not in LINEAGES:
        raise SystemExit(f"error: --lineage must be one of {sorted(LINEAGES)}")

    # Session-provenance check on operator-direct claims: if this session touched
    # untrusted content (web fetch, mail/calendar/drive read) before the write,
    # downgrade to contains-untrusted. No override flag; the write still succeeds.
    args.provenance = None
    if args.lineage == "operator-direct":
        SP = _session_provenance()
        session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if SP is None:
            # Unavailable check: record UNVERIFIED (not clean), but do not downgrade.
            args.provenance = "unverified"
            print("NOTE: session-provenance check unavailable (module not "
                  "importable) — writing operator-direct as UNVERIFIED, not "
                  "clean. This is an observation gap, not a block.",
                  file=sys.stderr)
        else:
            state, detail = SP.state_for_session(session_id)
            args.provenance = state
            if state == "flagged":
                args.lineage = "contains-untrusted"
                args.provenance = "flagged-downgraded"
                print(f"NOTE: operator-direct write DOWNGRADED to "
                      f"contains-untrusted — this session's own tool-call "
                      f"record shows it touched untrusted content before "
                      f"this write ({detail}). A operator-direct tag from a "
                      f"session that just read a Gmail thread or fetched a "
                      f"web page is exactly the GhostWriter mistag B1 "
                      f"measured getting served unscreened. The memory is "
                      f"NOT lost — it is quarantined; promote it explicitly "
                      f"if it turns out unrelated to what the session read "
                      f"(memory-mesh/sign.py --promote).", file=sys.stderr)
            elif state == "unverified":
                print(f"NOTE: session-provenance UNVERIFIED for this write "
                      f"({detail}) — writing operator-direct without this "
                      f"second signal, not claiming it as clean.",
                      file=sys.stderr)

    # Changing the content of a signed-promoted slug downgrades it to
    # contains-untrusted until re-promoted.
    if args.lineage == "operator-direct":
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
                        print(f"NOTE: operator-direct write DOWNGRADED to "
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

    # Over-long hooks are refused (to be rewritten), never truncated.
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

    # Doctrine metering: a full or unmeasurable tier writes as `state` with
    # doctrine-candidate set; the lesson is never dropped.
    args.doctrine_candidate = False
    if getattr(args, "residency", None) == "doctrine":
        budget = doctrine_budget_state()
        if budget is None:
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

    index_target = INDEX if args.lineage == "operator-direct" else QUARANTINE
    line = index_line(args)

    if not args.commit:
        print(f"\n(dry run — would write {target} and update {index_target.name}; pass --commit to apply)")
        return

    _write_store_file(target, rendered)
    print(f"wrote {target}")

    if index_is_generated():
        # The fold generates both the index and the quarantine list from the
        # mesh event emitted below; neither is edited here.
        touched = [target]
        print(f"note: {index_target.name} is fold-generated (.mesh-generated) — "
              "index update flows via the mesh event")
        touched += default_on_demand(args.slug)
    else:
        index_text = index_target.read_text() if index_target.exists() else "# QUARANTINE\n\nUnpromoted contains-untrusted memories.\n"
        if args.supersedes:
            index_text = remove_index_line(index_text, args.supersedes)
        if args.lineage == "operator-direct":
            if args.slug in on_demand_slugs():
                # Updating an on-demand memory never re-adds it to the index.
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

    # Mesh dual-write: best-effort; the store write above already succeeded.
    mesh_emit = _mesh_emit_path()
    if mesh_emit is not None:
        # The event carries the hook and full body; --content stays the
        # description for compatibility with older events.
        cmd = [sys.executable, str(mesh_emit), "--no-nudge",
               "--kind", "lesson", "--subject", f"lesson/{args.slug}",
               "--content", args.description[:INDEX_CONTENT_CHARS],
               "--hook", args.hook,
               "--body", rendered,
               "--session", os.environ.get("CLAUDE_SESSION_ID", "memory-write"),
               "--lineage", args.lineage]
        if getattr(args, "residency", None):
            cmd += ["--residency", args.residency]
        if getattr(args, "expires", None):
            cmd += ["--expires", args.expires]
        if args.type == "reference" and \
                "--pointer" in mesh_emit.read_text(encoding="utf-8"):
            # Pass the reference carve-out on, if this emit.py supports it.
            cmd += ["--pointer"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            # The event id is what `sign.py --promote` needs.
            emitted = re.search(r"emitted\s+([0-9a-f]{8,})", r.stdout or "")
            event_id = emitted.group(1) if emitted else None
            print(f"mesh: event emitted{f' ({event_id})' if event_id else ''}")
            if args.slug in on_demand_slugs():
                _emit_tier([args.slug], "ondemand")
            if args.lineage != "operator-direct" or args.provenance == \
                    "flagged-downgraded":
                # Quarantined: print the exact promote command.
                if event_id:
                    print(f"QUARANTINED (lineage contains-untrusted) — not "
                          f"served until promoted. The owner runs:\n"
                          f"  python3 memory-mesh/sign.py "
                          f"--promote {event_id}")
                else:
                    print("QUARANTINED — not served until promoted, but the "
                          "event id could not be parsed from emit output; "
                          "find it in ~/memory-events/events/*.ndjson")
        else:
            print(f"mesh: EMIT FAILED (store write is safe; mesh will lag): "
                  f"{proc_error(r)}")
        # Retract the superseded slug's event (never this call's own slug).
        if args.supersedes and args.supersedes != args.slug and index_is_generated():
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

    The fact file stays live for /recall; reversible with --undo.
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
        _emit_tier(dropped, "always")
        print("the next fold STAGES this as a residency delta — it is not live "
              "until the owner promotes it.")
        if not args.no_git:
            class _A:
                slug = ", ".join(dropped)
                description = "restored to always-on (demote --undo)"
                no_push = args.no_push
            git_commit([EXCLUDE], _A())
        return

    for slug in args.slugs:
        # A prefixed subject (`home/x`) is a fold-emitted pointer row with no
        # store file; it is verified against the log instead.
        prefixed = "/" in slug
        if prefixed:
            head, _, tail = slug.partition("/")
            if not (SLUG_RE.match(head) and SLUG_RE.match(tail)):
                raise SystemExit(
                    f"error: subject must be kebab-case[/kebab-case]: {slug!r}")
        elif not SLUG_RE.match(slug):
            raise SystemExit(f"error: slug must be kebab-case: {slug!r}")
        if prefixed:
            if not _subject_in_log(slug):
                missing.append(slug)
                continue
        elif not (STORE / f"{slug}.md").exists():
            missing.append(slug)
            continue
        if gen:
            # Fold-owned index: only the exclude manifest changes.
            if slug in already:
                print(f"note: '{slug}' already on-demand — nothing to do")
            else:
                demoted.append(slug)
            continue
        before = index_text
        index_text = remove_index_line(index_text, slug)
        stripped = index_text != before
        if slug in already:
            # Already excluded: still remove any stray always-on line.
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

    # Append only slugs not already listed.
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
    _emit_tier(demoted, "ondemand")

    if not args.no_git:
        class _A:  # git_commit() reads .slug/.description/.no_push off args
            slug = f"{len(demoted)} memories"
            description = "demoted to on-demand to recover index headroom"
            no_push = args.no_push
        git_commit([EXCLUDE] if gen else [INDEX, EXCLUDE], _A())


def cmd_delete(args):
    """Delete memories whose fact now lives in its own home.

    --home is required and recorded in the commit; files stay recoverable in
    the store's git history.
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

    # Fold-owned index: retract each deleted slug's lesson event.
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
    """Flip this store's MEMORY.md to fold-generated (.mesh-generated), or revert."""
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
    """Commit the staged flip; refuses if unrelated changes are staged."""
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

    Everything else is byte-preserved; any indented (nested) `lineage:` is removed.
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
    """Set `lineage:` on existing memories without touching their content.

    Does not move index lines. Retagging to operator-direct requires either a
    signed promotion (has_signed_promotion) or --operator-approved words.
    """
    args.lineage = canon_lineage(args.lineage)
    if args.lineage not in LINEAGES:
        raise SystemExit(f"error: --lineage must be one of {sorted(LINEAGES)}")
    if args.lineage == "contains-untrusted":
        require_enforceable_quarantine()

    # Verbal promotion: a per-call quote of the owner's approval. An audit
    # record, not a security control; the signature path remains the strong one.
    approved = (getattr(args, "operator_approved", None) or "").strip()
    PROMOTION_KEY = PROMOTION_VERBAL = None
    if args.lineage == "operator-direct":
        PROMOTION_KEY, PROMOTION_VERBAL, MIN_APPROVAL_WORDS = _promotion_vocab()
    if approved and args.lineage != "operator-direct":
        raise SystemExit(
            "error: --operator-approved only applies when promoting TO "
            "operator-direct. Approving something INTO quarantine is not a "
            "promotion, and silently ignoring the flag would teach the caller "
            "it had done something it had not.")
    if approved and len(approved) < MIN_APPROVAL_WORDS:
        raise SystemExit(
            f"error: --operator-approved must quote the owner's actual words "
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
        if args.lineage == "operator-direct":
            if approved:
                klass = PROMOTION_VERBAL
            else:
                ok, reason = has_signed_promotion(slug)
                if not ok:
                    unsigned.append((slug, reason))
                    continue
                klass = PROMOTION_KEY
        before = p.read_text()
        # Replacing a key-signed stamp with a verbal one is allowed but warned.
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
            "error: refusing to retag to operator-direct:\n" + lines + "\n"
            "  retag no longer decides trust itself; it only executes what a "
            "signed mesh event already declared. Promote first:\n"
            "    memory-mesh/sign.py --promote <event-id>\n"
            "  (or --subject/--content directly), which requires the owner's "
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
        _write_store_file(p, after)
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

    Not copied locally; without the mesh no promotion is possible.
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
    """Stamp how a memory was promoted (`promotion:`), byte-preserving the rest.

    On the verbal path `approved:` stores the verbatim approval words.
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
    """Return `text` with a top-level `corrected: <stamp>` in its frontmatter."""
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
    """Carry a just-corrected file back into the event log (adopt --reconcile)."""
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
    """Replace one exact substring in a memory's body (or description).

    Writes content, so it takes its own --lineage. Fails closed on any
    ambiguity (zero or multiple matches, frontmatter hits).
    """
    args.lineage = canon_lineage(args.lineage)
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

    # Laundering guard: must run before every write path, including --in-description.
    note_lineage = re.search(r"^lineage:[ \t]*(\S+)", fm, re.M)
    note_lineage = note_lineage.group(1) if note_lineage else "unknown"
    if args.lineage == "contains-untrusted" and note_lineage != "contains-untrusted":
        raise SystemExit(
            f"error: refusing to apply a contains-untrusted correction to a "
            f"'{note_lineage}' memory.\nThat is the laundering path the gate "
            "exists to close. If the correction is real, `demote`/`retag` the "
            "note deliberately first, or write it as its own quarantined note.")

    # Session-provenance check as in `write`, but refused rather than
    # downgraded: correct never changes the note's lineage.
    if args.lineage == "operator-direct":
        SP = _session_provenance()
        session_id = getattr(args, "session_id", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if SP is not None:
            state, detail = SP.state_for_session(session_id)
            if state == "flagged":
                raise SystemExit(
                    "error: refusing a operator-direct correction — this "
                    "session's own tool-call record shows it touched "
                    f"untrusted content before this write ({detail}).\n"
                    "  correct cannot quarantine in place (it never touches "
                    "the lineage frontmatter). If the correction is real:\n"
                    "   - write it as a fresh --lineage contains-untrusted "
                    "note and promote later, or\n"
                    "   - apply the correction from a session that hasn't "
                    "touched untrusted content this session.")

    # --in-description: apply the replace to the `description:` line only.
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
        _write_store_file(p, after)
        print(f"wrote {p}  ({len(after)} B)")
        _reemit_corrected(args.slug)
        if not args.no_git:
            class _A:
                slug = args.slug
                description = args.reason or "corrected the description"
                no_push = args.no_push
            git_commit([p], _A())
        return

    # Body only; frontmatter changes go through write/retag.
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

    _write_store_file(p, after)
    print(f"wrote {p}  ({len(after)} B)")
    _reemit_corrected(args.slug)
    if not args.no_git:
        class _A:
            slug = args.slug
            description = args.reason or "corrected a stale fact"
            no_push = args.no_push
        git_commit([p], _A())


def selftest():
    """Isolated selftest of the write/retag/correct/adopt gates.

    Runs against a shadow store and shadow mesh log (MESH_ROOT diverted);
    aborts before writing unless mesh_lib.harness_store() resolves to None.
    Afterwards verifies the real store has no zzselftest-b2-* files.
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
            lineage="operator-direct", rule="selftest artifact — not real",
            why=None, how=None, hook="selftest artifact",
            section="Unsorted", residency=None, expires=None,
            supersedes=None, contradicts=None, session_id=None,
            commit=True, no_git=True, no_push=True)
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    try:
        # Prove the sandbox guard engages before planting anything.
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

        # 1. No session id -> unverified. Also the first import of
        #    session_provenance, binding its log to prov_log.
        a = base_args(session_id=None)
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("no session id -> write proceeds", p.exists())
        check("no session id -> stamped provenance: unverified",
              p.exists() and "provenance: unverified" in p.read_text())

        SP = sys.modules.get("session_provenance")
        check("session_provenance module was actually imported by _cmd_write "
              "(not skipped)", SP is not None)

        # 1b. Fact-shape gate: project memory with an IP is refused before any
        #     file lands; a reference memory is admitted and its event lands.
        a = base_args(slug="zzselftest-b2-factcopy", type="project",
                      rule="the Envoy is at 192.0.2.158, verified",
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
                      rule="the Envoy is at 192.0.2.158 — see FLEET.md",
                      session_id=None)
        _cmd_write(a)
        check("reference memory with a fact is admitted at the door",
              (shadow_store / f"{a.slug}.md").exists())
        logged = "".join(p.read_text(encoding="utf-8")
                         for p in (shadow_mesh / "events").glob("*.ndjson"))
        check("…and its event REACHED the mesh (--pointer carried the carve-out "
              "through make_event's body gate — no file-without-event half-state)",
              f"lesson/{a.slug}" in logged and "192.0.2.158" in logged)

        # 2. SessionStart witnessed, no untrusted touch -> clean.
        SP.record("SessionStart", {"session_id": "sess-clean"})
        a = base_args(slug="zzselftest-b2-clean", session_id="sess-clean")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("clean session -> write proceeds without override", p.exists())
        check("clean session -> stamped provenance: clean",
              p.exists() and "provenance: clean" in p.read_text())

        # 3. Untrusted touch this session -> downgraded, not refused. Called
        #    via _cmd_write directly to prove the check is not wrapper-only.
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

        # 4. The real CLI rejects --provenance-override.
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

        # 5. Declared contains-untrusted is not stamped with provenance.
        a = base_args(slug="zzselftest-b2-untrusted",
                       lineage="contains-untrusted", session_id="sess-flagged")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("contains-untrusted lineage: no provenance: line at all "
              "(check is operator-direct-scoped, by design)",
              p.exists() and "provenance:" not in p.read_text())

        # 6. Corrupt provenance log -> unverified, write proceeds.
        prov_log.write_text("not valid json at all\n")
        a = base_args(slug="zzselftest-b2-corrupt", session_id="sess-clean")
        _cmd_write(a)
        p = shadow_store / f"{a.slug}.md"
        check("corrupt provenance log -> stamped unverified, not clean",
              p.exists() and "provenance: unverified" in p.read_text())
        prov_log.write_text("")

        # 7. Instrument unavailable -> unverified, never clean.
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

        # 8. retag to operator-direct requires a signed promotion.
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
            slugs=["zzselftest-b2-retagtarget"], lineage="operator-direct",
            commit=True, no_git=True, no_push=True)
        refused = False
        try:
            cmd_retag(retag_args)
        except SystemExit:
            refused = True
        check("retag to operator-direct with NO signed promotion -> refused",
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
                  "lineage: operator-direct" in target_file.read_text())
        finally:
            globals()["has_signed_promotion"] = real_hsp

        # 8a. Verbal promotion: stamped distinctly; token quotes refused.
        target_file.write_text(set_lineage(target_file.read_text(),
                                           "contains-untrusted"))
        va = argparse.Namespace(
            slugs=["zzselftest-b2-retagtarget"], lineage="operator-direct",
            commit=True, no_git=True, no_push=True,
            operator_approved="build it. There is key signed and verbally signed")
        cmd_retag(va)
        after = target_file.read_text()
        check("verbal approval promotes to operator-direct with NO signed event "
              "(the whole point of the ruling)",
              "lineage: operator-direct" in after)
        check("a verbal promotion is STAMPED verbally-signed, so it can never "
              "be mistaken for a key-signed one",
              "promotion: verbally-signed" in after)
        check("The owner's verbatim words are recorded in the file — the only "
              "audit trail a signature-less promotion has",
              "verbally signed" in after and after.count("approved:") == 1)

        for bad, why in (("ok", "too short to quote anything"),
                         ("   ", "whitespace only")):
            target_file.write_text(set_lineage(target_file.read_text(),
                                               "contains-untrusted"))
            refused = False
            try:
                cmd_retag(argparse.Namespace(
                    slugs=["zzselftest-b2-retagtarget"], lineage="operator-direct",
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
                slugs=["zzselftest-b2-retagtarget"], lineage="operator-direct",
                commit=True, no_git=True, no_push=True,
                operator_approved=None))
            check("a KEY-signed promotion is stamped too, so an unstamped file "
                  "is unambiguously 'promoted before 2026-08-12' rather than "
                  "'key-signed, probably'",
                  "promotion: key-signed" in target_file.read_text())
        finally:
            globals()["has_signed_promotion"] = real_hsp

        # 8b. has_signed_promotion() detects content changed after signing
        #     (fake fold with a mismatched body_sha256).
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
            "lineage: operator-direct\nmetadata:\n  node_type: memory\n"
            "  type: user\n---\n\nchanged after promotion\n")
        (shadow_store / "zzselftest-b3-legacy.md").write_text(
            "---\nname: zzselftest-b3-legacy\ndescription: d\n"
            "lineage: operator-direct\nmetadata:\n  node_type: memory\n"
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

        # 8c. content_fingerprint() ignores lineage, tracks the body.
        M_real = real_mesh_lib()
        text_a = ("---\nname: x\ndescription: d\nlineage: operator-direct\n"
                  "metadata:\n  node_type: memory\n  type: user\n---\n\nbody\n")
        text_b_same_content_diff_lineage = text_a.replace(
            "lineage: operator-direct", "lineage: contains-untrusted")
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

        # 8d. Overwriting a signed-promoted memory with new content downgrades it.
        promoted_slug = "zzselftest-b3-writepath"
        (shadow_store / f"{promoted_slug}.md").write_text(
            "---\nname: %s\ndescription: d\nlineage: operator-direct\n"
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
                  "contains-untrusted (not silently kept operator-direct)",
                  "lineage: contains-untrusted" in body)
            check("write-path twin: stamped provenance: promotion-revoked",
                  "provenance: promotion-revoked" in body)
        finally:
            globals()["_mesh_lib"] = real_mesh_lib

        # A write with no prior signed promotion is unaffected.
        wa2 = base_args(slug="zzselftest-b2-clean", session_id="sess-clean")
        _cmd_write(wa2)
        check("write-path twin: an ordinary operator-direct write with no "
              "prior signed promotion on record is UNAFFECTED",
              "provenance: promotion-revoked" not in
              (shadow_store / "zzselftest-b2-clean.md").read_text())

        # 8e. A symlinked store file is refused; its target is untouched.
        outside = tmp / "outside-target.txt"
        outside.write_text("ORIGINAL\n")
        link = shadow_store / "zzselftest-g4-symlink.md"
        os.symlink(outside, link)
        refused = False
        try:
            _cmd_write(base_args(slug="zzselftest-g4-symlink",
                                 session_id="sess-clean"))
        except SystemExit:
            refused = True
        check("G4: write to a slug that is a SYMLINK is refused", refused)
        check("G4: the symlink's outside target is UNCHANGED",
              outside.read_text() == "ORIGINAL\n")
        check("G4: the link is still a link (not silently replaced)",
              link.is_symlink())

        # 9. correct from a flagged session is refused. Re-seed sess-flagged:
        #    test 6 truncated prov_log.
        SP.record("SessionStart", {"session_id": "sess-flagged"})
        SP.record("PreToolUse", {"session_id": "sess-flagged",
                                  "tool_name": "WebFetch", "tool_input": {}})
        correct_args = argparse.Namespace(
            slug="zzselftest-b2-clean", old="not real",
            new="not real (corrected in a flagged session — should refuse)",
            lineage="operator-direct", in_description=False,
            reason="selftest", stamp="2026-08-06", commit=True,
            no_git=True, no_push=True, session_id="sess-flagged")
        before_body = (shadow_store / "zzselftest-b2-clean.md").read_text()
        refused = False
        try:
            cmd_correct(correct_args)
        except SystemExit:
            refused = True
        check("correct with --lineage operator-direct from a FLAGGED session "
              "-> refused", refused)
        check("refused correction left the body UNCHANGED",
              (shadow_store / "zzselftest-b2-clean.md").read_text() == before_body)

        correct_args.session_id = "sess-clean"
        cmd_correct(correct_args)
        check("correct with --lineage operator-direct from a CLEAN session "
              "-> succeeds",
              "corrected in a flagged session" in
              (shadow_store / "zzselftest-b2-clean.md").read_text())

        # 10. adopt of an untagged file requires a signed promotion.
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
              "operator-direct without a signed promotion",
              "untagged file, never promoted" in buf.getvalue())

        # adopt bounds --content to INDEX_CONTENT_CHARS.
        long_desc = "L" * 260
        longf = shadow_store / "zzselftest-b2-longdesc.md"
        longf.write_text(
            f"---\nname: zzselftest-b2-longdesc\n"
            f"description: {long_desc}\n"
            "lineage: operator-direct\nprovenance: clean\n"
            "metadata:\n  node_type: memory\n  type: user\n---\n\n"
            "body long enough to adopt\n")
        seen = []

        class _FakeProc:
            returncode, stdout, stderr = 0, "", ""

        real_run = subprocess.run
        subprocess.run = lambda cmd, *a, **k: (seen.append(cmd), _FakeProc())[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                # commit=True so the emit is built; subprocess.run is stubbed.
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
        # The hook has its own bound.
        hooks_over = [c[c.index("--hook") + 1] for c in emits
                      if "--hook" in c
                      and len(c[c.index("--hook") + 1]) > HOOK_MAX_CHARS]
        check(f"adopt bounds --hook to HOOK_MAX_CHARS ({HOOK_MAX_CHARS})",
              not hooks_over)

        # proc_error surfaces a traceback's exception line.
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
                   help="promote to operator-direct on the owner's VERBAL approval "
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
                        "it and the owner promotes.")
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
    w.add_argument("--lineage", default="operator-direct")
    w.add_argument("--rule", required=True, help="the lesson/fact body text")
    w.add_argument("--why")
    w.add_argument("--how")
    w.add_argument("--hook", required=True, help="short MEMORY.md index hook text")
    w.add_argument("--section", help="MEMORY.md section header to file under (default: Unsorted)")
    # No default: residency is an explicit declaration; undeclared is safe.
    w.add_argument("--residency", choices=["doctrine", "state", "pinned"],
                   help="SPEC v4 tier (the owner declares it). doctrine/pinned "
                        "need an operator signature to hold across the mesh.")
    w.add_argument("--expires", metavar="YYYY-MM-DD",
                   help="state rows only: render-hide after this date")
    w.add_argument("--supersedes")
    w.add_argument("--contradicts")
    w.add_argument("--session-id")
    # There is intentionally no --provenance-override (selftest asserts this).
    # --commit both writes the files and git-commits them.
    w.add_argument("--commit", action="store_true",
                   help="apply the write (default is dry-run/preview). Also git-commits "
                        "+ pushes the touched files unless --no-git/--no-push.")
    w.add_argument("--no-git", action="store_true",
                   help="write the files but do not git-commit them")
    w.add_argument("--no-push", action="store_true",
                   help="git-commit but do not push (commit stays on this disk)")
    w.set_defaults(func=cmd_write)

    args = ap.parse_args()
    if getattr(args, "lineage", None):
        args.lineage = canon_lineage(args.lineage)
    if args.selftest:
        return selftest()
    if not args.cmd:
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
