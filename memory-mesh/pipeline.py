#!/usr/bin/env python3
"""pipeline.py — mechanical stages of the improve/capture memory pipeline.

Story D2 (harness-portability audit): the harvest -> filter -> classify ->
dedup -> lineage-gate pipeline lived entirely in SKILL.md prose. This CLI
exposes the MECHANICAL stages so any harness (or Craig by hand) can drive the
judgment steps around it. The split, explicitly:

  mechanical (this tool)            judgment (the model / Craig — SKILL.md prose)
  --------------------------------  ---------------------------------------------
  harvest: scan given files for     deciding a lesson is real, durable and
    correction/preference signal      general enough to keep; distilling wording
  dedup: check a slug + keywords    choosing update-in-place vs supersede vs
    against the live memory store     contradicts when a belief changed
  stage: structural filter (slug,   classifying type; setting lineage HONESTLY
    type, lineage present, why/how    (does the lesson trace to untrusted
    contract, secret scan) + emit     content?); Craig's show-then-write approval
    the exact memory_write.py call

SECURITY INVARIANT (Story 029, OWASP ASI06): this tool NEVER writes into the
memory store. memory_write.py remains the one sanctioned writer with the
lineage gate. `stage` prints the memory_write.py invocation as a PROPOSAL —
it does not execute it. All subcommands are read-only against the store.

Usage:
    /usr/bin/python3 pipeline.py harvest <file>... [--topic X]
    /usr/bin/python3 pipeline.py dedup --slug foo-bar [--keywords "a b c"]
    /usr/bin/python3 pipeline.py stage --slug foo-bar --type feedback \\
        --description "..." --lineage craig-direct|contains-untrusted \\
        --rule "..." [--why "..." --how "..."] --hook "..." --section "..." \\
        --not-implied-by "what PRINCIPLES.md does not force here" \\
        [--supersedes s] [--contradicts s] [--session-id id]
    /usr/bin/python3 pipeline.py sections
    /usr/bin/python3 pipeline.py --selftest

All output is structured JSON on stdout. Stdlib only; /usr/bin/python3.
"""
import argparse
import json
import re
import shlex
import sys
from pathlib import Path

# _lib lives at the workspace root. Since 2026-09-18 (SEED-080) this file's
# canonical home is memory-mesh/ in git, so the workspace is simply its
# parent — true for the fleet's ~/{{REDACTED}} and for any seed recipient's
# chosen root alike. The older candidates stay, last, for a checkout that
# still runs from the vault's skills-core or from cc-skills.
_HERE = Path(__file__).resolve().parent
for _root in (_HERE.parent, _HERE.parents[2], Path.home() / "{{REDACTED}}"):
    if (_root / "_lib" / "frontmatter.py").exists():
        sys.path.insert(0, str(_root))
        break
from _lib import frontmatter  # noqa: E402

# The store is DERIVED, never typed: the literal here was one host's truth
# shipped to every host (SEED-080 M2, the one-store property). mesh_lib owns
# the derivation; without it, fall back to the same rule applied locally.
def _store():
    try:
        sys.path.insert(0, str(_HERE))
        import mesh_lib
        return mesh_lib.store_dir()
    except Exception:
        return (Path.home() / ".claude" / "projects"
                / str(_HERE.parent).replace("/", "-") / "memory")


STORE = _store()
MEMORY_WRITE = Path(__file__).resolve().parent / "memory_write.py"

TYPES = {"feedback", "user", "project", "reference"}
LINEAGES = {"craig-direct", "contains-untrusted"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# ---- bounds (every loop and output is capped) -------------------------------
MAX_FILES = 25            # harvest: files scanned per run
MAX_FILE_BYTES = 5_000_000  # harvest/dedup: per-file read cap
MAX_CANDIDATES = 100      # harvest: candidate lines emitted
MAX_MATCHES = 20          # dedup: overlap matches emitted
MAX_KEYWORDS = 8          # dedup: keywords considered
SNIPPET_LEN = 240         # harvest: emitted snippet length

# ---- harvest signal patterns (heuristic pre-filter; judgment stays upstream) -
SIGNALS = [
    ("capture-signal", re.compile(
        r"/improve\b|/capture\b|remember (?:this|that)\b|bake (?:that|this) in"
        r"|make (?:that|this) stick|don.?t do that again", re.I)),
    ("correction", re.compile(
        r"\bactually\b|\bno[,;] (?:do|use|not)\b|not like that|\bredo\b"
        r"|\bwrong\b|\binstead\b", re.I)),
    ("preference", re.compile(
        r"\balways\b|\bnever\b|going forward|from now on|\bprefer\b", re.I)),
]

# ---- secret scan (structural filter fails CLOSED on suspicion) --------------
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(password|passwd|api[_-]?key|secret|token)\b\s*[:=]\s*\S{6,}",
               re.I),
]

STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "not",
             "are", "was", "has", "have", "when", "into", "over", "its",
             "use", "you", "but", "all", "one", "per", "via", "any"}


def _read_capped(path):
    """Read at most MAX_FILE_BYTES of a file as text; never raises on codec."""
    with open(path, "rb") as f:
        return f.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")


# ================================================================== harvest ==
def harvest(paths, topic=None):
    """Scan the given transcript/source files for candidate-lesson lines.

    Mechanical only: pattern-matched signal lines with provenance. Whether a
    candidate is a REAL durable lesson, and its wording, is model judgment.
    """
    candidates, errors, truncated = [], [], False
    for p in paths[:MAX_FILES]:
        path = Path(p)
        if not path.is_file():
            errors.append(f"not a file: {p}")
            continue
        text = _read_capped(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(candidates) >= MAX_CANDIDATES:
                truncated = True
                break
            stripped = line.strip()
            if not stripped:
                continue
            if topic and topic.lower() not in stripped.lower():
                continue
            for kind, rx in SIGNALS:
                if rx.search(stripped):
                    candidates.append({
                        "file": str(path),
                        "line": lineno,
                        "kind": kind,
                        "snippet": stripped[:SNIPPET_LEN],
                    })
                    break
        if truncated:
            break
    if len(paths) > MAX_FILES:
        truncated = True
        errors.append(f"only first {MAX_FILES} of {len(paths)} files scanned")
    return {
        "cmd": "harvest",
        "candidates": candidates,
        "count": len(candidates),
        "truncated": truncated,
        "errors": errors,
        "note": "heuristic pre-filter — judging which candidates are durable "
                "lessons (and their wording) is the model's job, not this tool's",
    }


# ==================================================================== dedup ==
def _frontmatter_fields(text):
    """Best-effort name/description from a memory note's frontmatter.

    Thin wrapper over _lib.frontmatter.parse (2026-08-08 — this used to be a
    third near-duplicate of session_brief.parse_brief / agy-bundle's
    _frontmatter; see that module's docstring). Keeps this function's
    original (name, desc) tuple return shape so its one call site (dedup)
    doesn't change.
    """
    meta, _ = frontmatter.parse(text)
    return meta.get("name", "").strip(), meta.get("description", "").strip('"')


def _tiers(store):
    """Map slug -> index tier from the store's index artifacts (read-only).

    Bug fixed 2026-08-08: this used to look for a `(slug.md)` markdown-link
    pattern in MEMORY.md's always-on bullets. The mesh-fold rendering moved
    to `- [type/slug] text (date)` at some point and this regex was never
    updated, so it matched zero always-on entries against every real store —
    every slug silently fell through to "on-demand" or unknown. The
    selftest's own MEMORY.md fixture below still used the old link format,
    so it kept passing while production was broken the whole time (see
    memory [[test-asserting-source-shape-defends-the-bug]]). Found building
    cc-skills/agy-bundle/build.py, which needed real tier data and got zero
    always-on hits against the live 104-entry store.
    """
    tiers = {}
    index = store / "MEMORY.md"
    quarantine = store / "QUARANTINE.md"
    exclude = store / "_index-exclude.txt"
    if index.exists():
        always_on_text = _read_capped(index).split("## On-demand memories", 1)[0]
        for m in re.finditer(r"^- \[(?:[a-z0-9_]+/)?([a-z0-9-]+)\]", always_on_text, re.M):
            tiers[m.group(1)] = "always-on"
    if quarantine.exists():
        qt = _read_capped(quarantine)
        for m in re.finditer(r"^- \*\*([a-z0-9-]+)\*\*", qt, re.M):
            tiers[m.group(1)] = "quarantine"
        for m in re.finditer(r"\(([a-z0-9-]+)\.md\)", qt):
            tiers[m.group(1)] = "quarantine"
    if exclude.exists():
        for line in _read_capped(exclude).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tiers[line] = "on-demand"
    return tiers


def _keywords_from(raw):
    words = re.split(r"[^a-z0-9]+", (raw or "").lower())
    seen, out = set(), []
    for w in words:
        if len(w) >= 3 and w not in STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def dedup(slug=None, keywords_raw=None, store=STORE):
    """Check a proposed slug + keywords against the live memory store.

    Read-only. Reports whether the slug already exists, its index tier, and
    which existing memories overlap the keywords — the update-vs-supersede-vs-
    new decision stays with the model/Craig.
    """
    keywords = _keywords_from(keywords_raw)
    tiers = _tiers(store)
    slug_exists = bool(slug) and (store / f"{slug}.md").exists()

    matches = []
    files = sorted(store.glob("*.md")) if store.is_dir() else []
    for path in files:  # bounded: one pass over the store, capped emission
        s = path.stem
        if s in ("MEMORY", "QUARANTINE"):
            continue
        text = _read_capped(path)
        name, desc = _frontmatter_fields(text)
        low = text.lower()
        hit = [k for k in keywords if k in low]
        if not hit and s != slug:
            continue
        where = []
        for k in hit:
            if k in name.lower():
                where.append("name")
            elif k in desc.lower():
                where.append("description")
            else:
                where.append("body")
        matches.append({
            "slug": s,
            "tier": tiers.get(s, "unindexed"),
            "description": desc[:SNIPPET_LEN],
            "matched_keywords": hit,
            "matched_in": sorted(set(where)),
            "score": len(hit),
        })
    matches.sort(key=lambda m: (-m["score"], m["slug"]))
    truncated = len(matches) > MAX_MATCHES
    matches = matches[:MAX_MATCHES]

    if slug_exists:
        verdict = "slug-exists"
    elif any(m["score"] >= 2 for m in matches):
        verdict = "overlap-candidates"
    elif matches:
        verdict = "weak-overlap"
    else:
        verdict = "clear"
    return {
        "cmd": "dedup",
        "slug": slug,
        "slug_exists": slug_exists,
        "slug_tier": tiers.get(slug, "unindexed") if slug_exists else None,
        "keywords": keywords,
        "matches": matches,
        "truncated": truncated,
        "verdict": verdict,
        "note": "read-only. update-in-place vs supersede vs contradicts vs new "
                "is a judgment call — see SKILL.md step 4",
    }


# ================================================================= sections ==
def sections(store=STORE):
    """List MEMORY.md section headers (so --section targets a real one)."""
    index = store / "MEMORY.md"
    heads = []
    if index.exists():
        heads = re.findall(r"^## +(.+)$", _read_capped(index), re.M)
    return {"cmd": "sections", "sections": heads[:100], "count": len(heads)}


# ==================================================================== stage ==
_NULL_ANSWERS = {"n/a", "na", "none", "nothing", "-", "unknown", "tbd",
                 "it is not", "not implied", "no"}


def _structural_problems(a):
    """The structural filter — every check here is mechanical, none is policy
    judgment. Lineage HONESTY (does the lesson trace to untrusted content?)
    cannot be checked here; only its presence and validity can."""
    problems, warnings = [], []
    if not a.get("slug") or not SLUG_RE.match(a["slug"]):
        problems.append(f"slug must be kebab-case: {a.get('slug')!r}")
    if a.get("type") not in TYPES:
        problems.append(f"--type must be one of {sorted(TYPES)}")
    if a.get("lineage") not in LINEAGES:
        problems.append(
            f"--lineage is REQUIRED, one of {sorted(LINEAGES)} — the Story 029 "
            "belief-poisoning gate; set it honestly (see SKILL.md Lineage)")
    if not (a.get("description") or "").strip():
        problems.append("--description is required (recall relevance)")
    if not (a.get("rule") or "").strip():
        problems.append("--rule is required")
    if not (a.get("hook") or "").strip():
        problems.append("--hook is required (index line text)")
    if a.get("type") in ("feedback", "project"):
        if not a.get("why") or not a.get("how"):
            problems.append(f"type '{a.get('type')}' requires --why and --how")
    elif a.get("type") in ("user", "reference") and (a.get("why") or a.get("how")):
        problems.append(f"type '{a.get('type')}' is a single paragraph — no --why/--how")
    # Earn-the-write gate (2026-08-01). Auto-memory is byte-budgeted and
    # always-on, so a rule that PRINCIPLES.md already forces costs context in
    # every future session and freezes the principle besides. The judgment is
    # the model's; what is mechanical — and therefore lives here — is that the
    # judgment was MADE and recorded. This REFUSES rather than warns, because a
    # limit that only narrates is not a limit: the standing instruction against
    # principle-restatement already existed in CLAUDE.md and two restatements
    # were staged anyway (2026-08-01), which is what a prose-only gate is worth.
    # The bar is deliberately "not FORCED by a principle for this case", not
    # "does not resemble one" — a measured gotcha that illustrates a principle
    # without being implied by it is exactly what auto-memory is for.
    nib = (a.get("not_implied_by") or "").strip()
    if not nib:
        problems.append(
            "--not-implied-by is REQUIRED: name what this adds that "
            "PRINCIPLES.md does not already FORCE for this case (a literal, a "
            "ban, a host fact, a measured gotcha, a taste call) — or drop the "
            "item. Resembling a principle is fine; being implied by one is not")
    elif nib.lower() in _NULL_ANSWERS or len(nib) < 20:
        problems.append(
            f"--not-implied-by is a non-answer ({nib!r}) — either state the "
            "residual concretely or drop the item")
    for s in ("supersedes", "contradicts"):
        v = a.get(s)
        if v and not SLUG_RE.match(v):
            problems.append(f"--{s} must be a kebab-case slug: {v!r}")
    # secret scan — fail closed; never store credential material in memory
    for field in ("rule", "why", "how", "description", "hook"):
        v = a.get(field) or ""
        for rx in SECRET_RES:
            if rx.search(v):
                problems.append(
                    f"--{field} looks like it contains a secret "
                    f"(pattern {rx.pattern[:40]!r}) — secrets live in ~/.key/, "
                    "never in memory; rephrase without the value")
                break
    return problems, warnings


def stage(a, store=STORE):
    """Structural filter + emit the exact memory_write.py call as a PROPOSAL.

    Executes NOTHING and writes NOTHING. memory_write.py stays the one
    sanctioned writer; its own dry-run (no --commit) is still the preview
    step, and Craig's approval still gates --commit.
    """
    problems, warnings = _structural_problems(a)

    ded = None
    if a.get("slug") and SLUG_RE.match(a["slug"]):
        ded = dedup(a["slug"], a.get("description"), store=store)
        if ded["slug_exists"]:
            warnings.append(
                f"slug '{a['slug']}' already exists (tier: {ded['slug_tier']}) — "
                "a write is an in-place UPDATE; confirm that is intended "
                "(vs supersede under a new slug)")
        elif ded["verdict"] == "overlap-candidates":
            top = ", ".join(m["slug"] for m in ded["matches"][:3])
            warnings.append(f"possible overlap with existing memories: {top}")
    if a.get("section"):
        secs = sections(store)["sections"]
        if secs and not any(a["section"] in s for s in secs):
            warnings.append(
                f"section '{a['section']}' not found in MEMORY.md — "
                "memory_write.py would file under Unsorted (loudly)")

    result = {
        "cmd": "stage",
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "dedup": {k: ded[k] for k in ("slug_exists", "slug_tier", "verdict")} if ded else None,
    }
    if problems:
        return result

    argv = ["python3", str(MEMORY_WRITE), "write",
            "--slug", a["slug"], "--type", a["type"],
            "--description", a["description"], "--lineage", a["lineage"],
            "--rule", a["rule"]]
    if a.get("why"):
        argv += ["--why", a["why"]]
    if a.get("how"):
        argv += ["--how", a["how"]]
    argv += ["--hook", a["hook"]]
    if a.get("section"):
        argv += ["--section", a["section"]]
    for opt in ("supersedes", "contradicts"):
        if a.get(opt):
            argv += [f"--{opt}", a[opt]]
    if a.get("session_id"):
        argv += ["--session-id", a["session_id"]]

    result.update({
        "preview_command": shlex.join(argv),
        "commit_command": shlex.join(argv + ["--commit"]),
        "note": "PROPOSAL only — nothing was executed or written. Show Craig "
                "the drafted memory; on approval run preview_command (dry run) "
                "then commit_command. memory_write.py is the only writer.",
    })
    return result


# ================================================================= selftest ==
def _selftest():
    import tempfile
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # --- harvest ---
        t = d / "transcript.txt"
        t.write_text("hello world\n"
                     "Actually, always use tabs for this repo\n"
                     "no relevant signal here at all\n"
                     "please bake that in going forward\n"
                     "the solar inverter clamp torque is 12Nm\n")
        h = harvest([str(t)])
        ok(h["count"] >= 2, "harvest-finds-signal-lines")
        ok(any(c["kind"] == "capture-signal" for c in h["candidates"]),
           "harvest-detects-capture-signal")
        ok(all("snippet" in c and "line" in c for c in h["candidates"]),
           "harvest-carries-provenance")
        ht = harvest([str(t)], topic="tabs")
        ok(ht["count"] == 1 and "tabs" in ht["candidates"][0]["snippet"],
           "harvest-topic-filter")
        big = d / "big.txt"
        big.write_text("always do X\n" * (MAX_CANDIDATES + 50))
        hb = harvest([str(big)])
        ok(hb["count"] == MAX_CANDIDATES and hb["truncated"],
           "harvest-bounded-output")
        hm = harvest([str(d / "ghost.txt")])
        ok(hm["errors"] and hm["count"] == 0, "harvest-missing-file-loud")

        # --- dedup against a fake store ---
        store = d / "store"
        store.mkdir()
        (store / "solar-tilt-lesson.md").write_text(
            "---\nname: solar-tilt-lesson\ndescription: \"panel tilt angle "
            "seasonal adjustment lesson\"\nlineage: craig-direct\n---\n\nBody about solar tilt.\n")
        (store / "quarantined-note.md").write_text(
            "---\nname: quarantined-note\ndescription: \"untrusted thing\"\n"
            "lineage: contains-untrusted\n---\n\nBody.\n")
        (store / "ondemand-note.md").write_text(
            "---\nname: ondemand-note\ndescription: \"rarely needed\"\n---\n\nBody.\n")
        (store / "MEMORY.md").write_text(
            "# Index\n\n"
            "- [lesson/solar-tilt-lesson] tilt hook (08-08)\n"
            "\n## On-demand memories\n\nondemand-note\n")
        (store / "QUARANTINE.md").write_text(
            "# Q\n\n- **quarantined-note** — untrusted thing\n")
        (store / "_index-exclude.txt").write_text("# comment\nondemand-note\n")

        ds = dedup("solar-tilt-lesson", "irrelevant words", store=store)
        ok(ds["slug_exists"] and ds["verdict"] == "slug-exists"
           and ds["slug_tier"] == "always-on", "dedup-slug-exists-always-on")
        dk = dedup("new-solar-idea", "solar panel tilt", store=store)
        ok(dk["verdict"] == "overlap-candidates"
           and dk["matches"][0]["slug"] == "solar-tilt-lesson"
           and dk["matches"][0]["score"] >= 2, "dedup-keyword-overlap")
        dq = dedup("quarantined-note", "untrusted", store=store)
        ok(dq["slug_tier"] == "quarantine", "dedup-quarantine-tier")
        do_ = dedup("ondemand-note", None, store=store)
        ok(do_["slug_tier"] == "on-demand", "dedup-on-demand-tier")
        dc = dedup("fresh-slug", "zzqx wvvk", store=store)
        ok(dc["verdict"] == "clear" and not dc["slug_exists"], "dedup-clear")

        secs = sections(store)
        # Current mesh-fold MEMORY.md carries no "## " category headers
        # before the always-on bullets (see _tiers fix above, same drift) —
        # "On-demand memories" is the only one that exists in the real file.
        # A prior fixture asserted a category header format ("🛠 Working
        # Practices & Harness Lessons") that no longer matches production;
        # `--section` staging targets are effectively always "Unsorted" now.
        ok(secs["sections"] == ["On-demand memories"], "sections-listed")

        # --- stage ---
        base = dict(slug="new-lesson", type="feedback",
                    description="a new lesson", lineage="craig-direct",
                    rule="Do it this way.", why="Because.", how="Like so.",
                    hook="new lesson hook",
                    not_implied_by="a measured host literal no principle forces",
                    section="Working Practices & Harness Lessons")
        st = stage(dict(base), store=store)
        ok(st["ok"] and "--commit" not in st["preview_command"]
           and st["commit_command"].endswith("--commit"),
           "stage-emits-preview-and-commit-commands")
        ok("memory_write.py" in st["preview_command"], "stage-targets-memory-write")
        ok(not (store / "new-lesson.md").exists(), "stage-writes-nothing")

        st2 = stage(dict(base, why=None, how=None), store=store)
        ok(not st2["ok"] and any("--why" in p for p in st2["problems"]),
           "stage-feedback-needs-why-how")
        st3 = stage(dict(base, lineage=None), store=store)
        ok(not st3["ok"] and any("lineage" in p for p in st3["problems"]),
           "stage-lineage-required")
        st4 = stage(dict(base, lineage="trusted-honest"), store=store)
        ok(not st4["ok"], "stage-lineage-must-be-valid")
        st5 = stage(dict(base, slug="Bad_Slug"), store=store)
        ok(not st5["ok"], "stage-slug-kebab-enforced")
        st6 = stage(dict(base, rule="the password = hunter2secret ok"), store=store)
        ok(not st6["ok"] and any("secret" in p for p in st6["problems"]),
           "stage-secret-scan-fails-closed")
        st7 = stage(dict(base, lineage="contains-untrusted"), store=store)
        ok(st7["ok"] and "contains-untrusted" in st7["preview_command"],
           "stage-untrusted-lineage-flows-to-command")
        st8 = stage(dict(base, slug="solar-tilt-lesson"), store=store)
        ok(st8["ok"] and any("already exists" in w for w in st8["warnings"]),
           "stage-warns-on-existing-slug-update")
        st_nib = stage(dict(base, not_implied_by=None), store=store)
        ok(not st_nib["ok"]
           and any("not-implied-by" in p for p in st_nib["problems"]),
           "stage-earn-the-write-required")
        st_nib2 = stage(dict(base, not_implied_by="n/a"), store=store)
        ok(not st_nib2["ok"]
           and any("non-answer" in p for p in st_nib2["problems"]),
           "stage-earn-the-write-refuses-null-answer")
        st9 = stage(dict(base, section="No Such Section"), store=store)
        ok(st9["ok"] and any("not found" in w for w in st9["warnings"]),
           "stage-warns-unknown-section")
        st10 = stage(dict(base, type="user", why=None, how=None), store=store)
        ok(st10["ok"] and "--why" not in st10["preview_command"],
           "stage-user-type-single-paragraph")

    n = 21
    if fails:
        print("pipeline selftest: FAIL %d/%d -> %s" % (len(fails), n, ", ".join(fails)))
        return 1
    print("pipeline selftest: PASS %d/%d" % (n, n))
    return 0


# ===================================================================== main ==
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        return _selftest()

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="scan files for candidate-lesson signal lines")
    h.add_argument("paths", nargs="+")
    h.add_argument("--topic", help="only lines containing this substring")

    dd = sub.add_parser("dedup", help="check a slug/keywords against the memory store (read-only)")
    dd.add_argument("--slug")
    dd.add_argument("--keywords", help="free text; tokenized, stopwords dropped, "
                                       f"max {MAX_KEYWORDS} keywords")

    sub.add_parser("sections", help="list MEMORY.md section headers")

    st = sub.add_parser("stage", help="structural filter + emit the memory_write.py "
                                      "call as a PROPOSAL (never executed)")
    st.add_argument("--slug")
    st.add_argument("--type")
    st.add_argument("--description")
    st.add_argument("--lineage", help=" | ".join(sorted(LINEAGES)) +
                    " — REQUIRED, no default: set it honestly (Story 029)")
    st.add_argument("--rule")
    st.add_argument("--why")
    st.add_argument("--how")
    st.add_argument("--hook")
    st.add_argument("--section")
    st.add_argument("--supersedes")
    st.add_argument("--contradicts")
    st.add_argument("--not-implied-by", dest="not_implied_by",
                    help="REQUIRED: what this adds that PRINCIPLES.md does "
                         "not already force for this case")
    st.add_argument("--session-id", dest="session_id")

    a = ap.parse_args(argv)
    if a.cmd == "harvest":
        out = harvest(a.paths, topic=a.topic)
    elif a.cmd == "dedup":
        if not a.slug and not a.keywords:
            ap.error("dedup needs --slug and/or --keywords")
        out = dedup(a.slug, a.keywords)
    elif a.cmd == "sections":
        out = sections()
    else:
        out = stage(vars(a))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
