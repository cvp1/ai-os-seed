#!/usr/bin/env python3
"""import_pack — read-only inspection and integrity verification of a cc-pack.

    import_pack.py --pack <path> --inspect
    import_pack.py --pack <path> --verify [--target <ROOT>]

Stdlib only, standalone — this file ships inside cc-seed/dist/ and runs on
install targets that do NOT have the cc-pack workspace repo. It carries its
own manifest-reading and SHA256SUMS-checking logic rather than importing
cc-pack/pack_lib.py; that is a DECLARED DUPLICATE, the same split as
session_brief.py's parse_brief vs _lib/frontmatter.py (see that file's
docstring). cc-pack/selftest.py runs shared hostile fixtures against both
this file and pack_lib.py so a drift between the two is caught rather than
discovered later.

<path> may be a pack DIRECTORY or a .tar file. --inspect never extracts a
tar to disk (Principle 17: approving a pack has to be possible from what
--inspect shows, without unpacking it first) — it reads pack.json and
SHA256SUMS straight out of the tar's member table.

Both verbs here are read-only — an agent may run them freely. The write path
(P3, 2026-08-08) is a human running:
    install.py --target <ROOT> --approve import-pack --from-pack <pack-dir>
install.py shells out to THIS file's --verify before moving any byte, then
applies every part to an out-of-repo delivery root
($XDG_STATE_HOME/cc-pack/<slug of ROOT>/) — never into --target's own git
tree. See install.py's own module docstring and cc-pack/README.md Status for
what P3 does and does not cover (a DIRECTORY pack only; a .tar pack must be
extracted first).

VERIFY'S INTEGRITY CHECK IS SEPARATE FROM SIGNATURE VERIFICATION (2026-08-09:
signing landed — pack.json.sig, ssh-keygen -Y, namespace "cc-pack"). --verify
prints a "signature: <state>" line (one of unsigned/invalid/unknown-signer/
verification-error/verified) when --allowed-signers is given, but its EXIT
CODE stays governed by integrity alone, unchanged from before — this file
CLASSIFIES a signature, it does not enforce a policy on it. install.py's
caller is what enforces "require verified for replica by default, unsigned
only via --allow-unsigned" by parsing that line. See pack_lib.py's own
POSTURE comment for the full why. NO PROBING for --allowed-signers here,
ever — this file ships to hosts without the cc-pack workspace repo, so a
fuzzy default location would repeat the exact silent-fork failure class
mesh_lib.py's own signer-registry probe comment describes.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PACK_SCHEMA = 1
AUDIENCES = ("replica", "shareable")
KINDS = ("system", "memory", "vault")
MANIFEST_NAME = "pack.json"
SIG_NAME = "pack.json.sig"
SUMS_NAME = "SHA256SUMS"
_RESERVED_TOP = {MANIFEST_NAME, SIG_NAME, SUMS_NAME}

# Unattended-verify DoS bounds — same values as pack_lib.py, kept in sync by
# hand (declared duplicate).
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SUMS_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024

# Layer 3 of the audience gate (declared duplicate) — this file has no
# handler registry to consult (it ships without cc-pack/parts/), so the
# type -> allowed-audiences mapping is a STATIC TABLE that must be updated
# by hand every time cc-pack/parts/<type>.py ships a new handler or changes
# its AUDIENCES. cc-pack/selftest.py's cross-fixture check is what catches
# a forgotten update — treat a failure there as this table being stale, not
# as a false alarm.
PART_AUDIENCES = {
    "briefs": frozenset({"replica"}),
    "doctrine": frozenset({"replica", "shareable"}),
    "skills": frozenset({"replica", "shareable"}),
    "memory-digest": frozenset({"replica", "shareable"}),
    "mesh-events": frozenset({"replica"}),
    "secret-handles": frozenset({"replica", "shareable"}),
    "secrets-encrypted": frozenset({"replica"}),
    "vault": frozenset({"replica"}),
    "usage-ledgers": frozenset({"replica"}),
}
PART_SCHEMAS = {
    "briefs": 1,
    "doctrine": 1,
    "skills": 1,
    "memory-digest": 1,
    "mesh-events": 1,
    "secret-handles": 1,
    "secrets-encrypted": 1,
    "vault": 1,
    "usage-ledgers": 1,
}

# \A/\Z, not ^/$ — see pack_lib.py's identical fix, same reasoning
# (declared duplicate, 2026-08-08 P3 review finding).
_SAFE_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_safe_relpath(rel):
    """Declared duplicate of pack_lib.is_safe_relpath — see that function's
    docstring for why this check must run BEFORE any path join, never after
    (Path.__truediv__ silently discards the left side on an absolute right
    side; confirmed live 2026-08-08, the tri-model review's top finding)."""
    if not rel or not isinstance(rel, str):
        return False
    if "\x00" in rel or "\\" in rel:
        return False
    if any(ord(c) < 0x20 for c in rel):
        return False
    if rel.startswith("/"):
        return False
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def is_safe_component(name):
    return isinstance(name, str) and bool(_SAFE_COMPONENT_RE.match(name))


def _safe_display(s, maxlen=200):
    """Sanitize an untrusted manifest string before it reaches a terminal.
    --inspect IS the approval surface (Principle 17) — a hostile tag,
    filename, or origin value containing control/escape characters could
    otherwise hide lines, forge a fake 'VERIFIED', or emit terminal escape
    sequences (2026-08-08 review). Anything with a control character prints
    as its repr() instead of raw; everything is length-capped."""
    s = str(s)
    if len(s) > maxlen:
        s = s[:maxlen] + "…[truncated]"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in s):
        return repr(s)
    return s


class PackSource:
    """Uniform read access over a directory pack or a tar pack, without ever
    extracting a tar member to disk for inspection purposes."""

    def __init__(self, path):
        self.path = Path(path)
        self.is_tar = self.path.is_file()
        self._tf = tarfile.open(self.path, "r") if self.is_tar else None
        if self.is_tar:
            names = self._tf.getnames()
            if len(names) != len(set(names)):
                self.close()
                raise SystemExit(f"{path}: duplicate member names in the tar — refusing")
            # Every real member lives under a single top-level pack-id dir
            # (build_pack.py's tarfile.add(final_dir, arcname=pack_id)).
            roots = {n.split("/", 1)[0] for n in names if n}
            if len(roots) != 1:
                self.close()
                raise SystemExit(
                    f"{path}: expected exactly one top-level dir in the tar, "
                    f"found {sorted(roots)}")
            self.root = roots.pop()
            for m in self._tf.getmembers():
                if not (m.isfile() or m.isdir()):
                    self.close()
                    raise SystemExit(
                        f"{path}: member {m.name!r} is not a regular file or "
                        f"directory (type={m.type!r}) — refusing symlinks/"
                        f"hardlinks/devices/fifos in an untrusted pack")
        else:
            self.root = None
            if not (self.path / MANIFEST_NAME).exists():
                raise SystemExit(f"{path}: no {MANIFEST_NAME} — not a pack directory")

    def close(self):
        if self._tf is not None:
            self._tf.close()

    def _member(self, rel):
        return f"{self.root}/{rel}" if self.is_tar else rel

    def exists(self, rel):
        if not is_safe_relpath(rel):
            return False
        if self.is_tar:
            try:
                self._tf.getmember(self._member(rel))
                return True
            except KeyError:
                return False
        return (self.path / rel).exists()

    def read_bytes(self, rel, max_bytes=MAX_MANIFEST_BYTES):
        if not is_safe_relpath(rel):
            raise SystemExit(f"{rel}: unsafe path, refusing to read")
        if self.is_tar:
            m = self._tf.getmember(self._member(rel))
            if m.size > max_bytes:
                raise SystemExit(f"{rel}: {m.size} bytes exceeds the {max_bytes}-byte cap")
            f = self._tf.extractfile(m)
            if f is None:
                raise SystemExit(f"{rel}: not a regular file in the tar")
            return f.read()
        p = self.path / rel
        size = p.stat().st_size
        if size > max_bytes:
            raise SystemExit(f"{rel}: {size} bytes exceeds the {max_bytes}-byte cap")
        return p.read_bytes()

    def sha256(self, rel, chunk=1 << 20, max_bytes=MAX_MEMBER_BYTES):
        if not is_safe_relpath(rel):
            return None
        h = hashlib.sha256()
        if self.is_tar:
            try:
                m = self._tf.getmember(self._member(rel))
            except KeyError:
                return None
            if not m.isfile():
                return None
            if m.size > max_bytes:
                raise SystemExit(f"{rel}: {m.size} bytes exceeds the {max_bytes}-byte member cap")
            f = self._tf.extractfile(m)
            if f is None:
                return None
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
            return h.hexdigest()
        p = self.path / rel
        if not p.exists() or not p.is_file():
            return None
        size = p.stat().st_size
        if size > max_bytes:
            raise SystemExit(f"{rel}: {size} bytes exceeds the {max_bytes}-byte member cap")
        with open(p, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def total_size(self):
        if self.is_tar:
            return sum(m.size for m in self._tf.getmembers() if m.isfile())
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())

    def list_files(self):
        """Every physical file present, as pack-relative posix paths, minus
        the three reserved top-level names. The other half of the bijection
        verify_pack needs — files present but never declared in SHA256SUMS
        were invisible to the original version (2026-08-08 review)."""
        if self.is_tar:
            out = set()
            prefix = self.root + "/"
            for m in self._tf.getmembers():
                if not m.isfile():
                    continue
                if not m.name.startswith(prefix):
                    continue
                rel = m.name[len(prefix):]
                if rel in _RESERVED_TOP:
                    continue
                out.add(rel)
            return out
        out = set()
        for p in self.path.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.path).as_posix()
            if rel in _RESERVED_TOP:
                continue
            out.add(rel)
        return out


def read_manifest(src):
    try:
        raw = src.read_bytes(MANIFEST_NAME)
    except SystemExit:
        raise
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{MANIFEST_NAME} did not parse: {e}")
    if not isinstance(manifest, dict):
        raise SystemExit(f"{MANIFEST_NAME} top level is {type(manifest).__name__}, expected an object")
    return manifest


def _expected_member_set(manifest, problems):
    """Declared duplicate of pack_lib._expected_member_set."""
    expected = set()
    parts = manifest.get("parts")
    if not isinstance(parts, list):
        problems.append(f"manifest 'parts' is {type(parts).__name__}, expected a list")
        return expected
    seen_ids = set()
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            problems.append(f"parts[{i}] is {type(part).__name__}, expected an object")
            continue
        pid, files = part.get("id"), part.get("files")
        if not is_safe_component(pid):
            problems.append(f"parts[{i}].id {pid!r} is not a safe path component")
            continue
        if pid in seen_ids:
            problems.append(f"duplicate part id {pid!r}")
        seen_ids.add(pid)
        if not isinstance(files, list):
            problems.append(f"parts[{i}] ({pid}).files is {type(files).__name__}, expected a list")
            continue
        for f in files:
            if not is_safe_relpath(f):
                problems.append(f"parts[{i}] ({pid}) has an unsafe file entry {f!r}")
                continue
            expected.add(f"parts/{pid}/{f}")
    return expected


def verify_pack(src):
    """Same checks as cc-pack/pack_lib.py's verify_pack — declared duplicate,
    see module docstring. Returns (ok, problems); reports every mismatch
    found, not just the first."""
    problems = []
    if not src.exists(MANIFEST_NAME):
        return False, [f"no {MANIFEST_NAME}"]
    try:
        manifest = read_manifest(src)
    except SystemExit as e:
        return False, [str(e)]

    if manifest.get("pack_schema") != PACK_SCHEMA:
        problems.append(
            f"pack_schema {manifest.get('pack_schema')!r} — this importer supports {PACK_SCHEMA}")
    audience = manifest.get("audience")
    if audience not in AUDIENCES:
        problems.append(f"unknown audience {audience!r}")
    if manifest.get("kind") not in KINDS:
        problems.append(f"unknown kind {manifest.get('kind')!r}")
    if manifest.get("requires") not in (None, []):
        problems.append(f"'requires' must be empty in v1, got {manifest.get('requires')!r}")
    if audience == "shareable" and "scrub" not in manifest:
        problems.append("audience=shareable but no 'scrub' report present")
    if audience == "replica" and "scrub" in manifest:
        problems.append("audience=replica but a 'scrub' report is present (shareable-only field)")

    if not src.exists(SUMS_NAME):
        problems.append(f"no {SUMS_NAME}")
        return False, problems
    try:
        sums_bytes = src.read_bytes(SUMS_NAME, max_bytes=MAX_SUMS_BYTES)
    except SystemExit as e:
        problems.append(str(e))
        return False, problems
    actual_sums_hash = hashlib.sha256(sums_bytes).hexdigest()
    claimed_sums_hash = manifest.get("sha256sums_sha256")
    if actual_sums_hash != claimed_sums_hash:
        problems.append(
            f"sha256sums_sha256 mismatch: manifest says {claimed_sums_hash}, "
            f"{SUMS_NAME} hashes to {actual_sums_hash}")

    declared = {}
    for line in sums_bytes.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        h, _, rel = line.partition("  ")
        if not rel:
            problems.append(f"malformed {SUMS_NAME} line: {line!r}")
            continue
        if not is_safe_relpath(rel):
            problems.append(f"{SUMS_NAME} declares an unsafe path, refusing to read it: {rel!r}")
            continue
        declared[rel] = h

    for rel, expected in declared.items():
        try:
            actual = src.sha256(rel)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if actual is None:
            problems.append(f"declared file missing or not a regular file: {rel}")
        elif actual != expected:
            problems.append(f"hash mismatch: {rel} (expected {expected}, got {actual})")

    physical = src.list_files()
    expected_from_manifest = _expected_member_set(manifest, problems)
    declared_set = set(declared)
    extra_in_sums = sorted(declared_set - expected_from_manifest)
    missing_from_sums = sorted(expected_from_manifest - declared_set)
    extra_on_disk = sorted(physical - declared_set)
    if extra_in_sums:
        problems.append(
            f"{len(extra_in_sums)} file(s) in {SUMS_NAME} but not claimed by any manifest "
            f"part (possible smuggled content): " + ", ".join(extra_in_sums[:5])
            + (" …" if len(extra_in_sums) > 5 else ""))
    if missing_from_sums:
        problems.append(
            f"{len(missing_from_sums)} manifest-declared file(s) not in {SUMS_NAME}: "
            + ", ".join(missing_from_sums[:5])
            + (" …" if len(missing_from_sums) > 5 else ""))
    if extra_on_disk:
        problems.append(
            f"{len(extra_on_disk)} physical file(s) present but not in {SUMS_NAME} "
            f"(possible smuggled content): " + ", ".join(extra_on_disk[:5])
            + (" …" if len(extra_on_disk) > 5 else ""))

    if audience in AUDIENCES:
        for part in manifest.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            ptype, pschema = part.get("type"), part.get("schema")
            if ptype not in PART_AUDIENCES:
                problems.append(f"unknown part type {ptype!r} — refusing (no --skip-unknown-parts in P1)")
                continue
            if audience not in PART_AUDIENCES[ptype]:
                problems.append(
                    f"part type {ptype!r} is not permitted for audience {audience!r} "
                    f"(audience gate layer 3)")
                continue
            if not isinstance(pschema, int) or pschema > PART_SCHEMAS.get(ptype, 0):
                problems.append(
                    f"part {ptype!r} claims schema {pschema!r}, this importer's static "
                    f"table supports up to {PART_SCHEMAS.get(ptype, 0)} — refusing, no escape")

    return (len(problems) == 0), problems


# --- signing verify (P6c, 2026-08-09) — DECLARED DUPLICATE of
# pack_lib.verify_pack_sig, adapted to PackSource so it works uniformly for
# a directory OR a tar pack (pack_lib's own version only handles a real
# on-disk directory, since it's the build-side tool). Kept in sync BY HAND;
# cc-pack/selftest.py's cross-fixture tests run the same hostile fixtures
# against both this copy and pack_lib.py's and fail loud on drift — same
# split as verify_pack() above.
#
# NO PROBING HERE, EVER (2026-08-09 review, Grok #5/GPT #4 CRITICAL): unlike
# pack_lib.py's build-side default_allowed_signers_probe() (a dev-checkout
# convenience), THIS file ships to install targets that never have the
# cc-pack workspace repo — it must receive --allowed-signers explicitly and
# refuse to guess a location. Guessing here is exactly the failure mode
# that already forked mesh fold state fleet-wide once (mesh_lib.py's own
# "a host that can't find this file treats every signature as unverified"
# comment) — a fuzzy fallback baked into a file with no fixed install
# location would make that worse, not safer.
SIG_NAMESPACE = "cc-pack"
SIG_STATES = ("unsigned", "invalid", "unknown-signer", "verification-error", "verified")


def _ssh_keygen_path():
    """Declared duplicate of pack_lib._ssh_keygen_path — same FIDO/sk- key
    override cc-handoff/sign_task.py uses on macOS Homebrew installs."""
    for candidate in ("/opt/homebrew/bin/ssh-keygen",):
        if Path(candidate).exists():
            return candidate
    return shutil.which("ssh-keygen") or "ssh-keygen"


def verify_pack_sig(src, allowed_signers):
    """Returns (state, principal_or_None, detail_or_None), state one of
    SIG_STATES. CLASSIFIES ONLY — install.py's caller enforces policy (see
    pack_lib.py's POSTURE comment for the full rationale, unchanged here).

    Same ordered algorithm as pack_lib.verify_pack_sig, live-verified
    against this host's actual ssh-keygen -Y behavior: check existence of
    the signature and the registry EXPLICITLY and separately before ever
    asking ssh-keygen anything (a naive find-principals-first approach
    cannot distinguish 'unregistered key' from 'missing registry' — both
    produced byte-identical output in testing), then check-novalidate
    (crypto validity, independent of trust) before find-principals/verify
    (trust)."""
    if not src.exists(SIG_NAME):
        return "unsigned", None, "no pack.json.sig present"
    if allowed_signers is None:
        return "verification-error", None, "no --allowed-signers path given"
    allowed = Path(allowed_signers)
    if not allowed.exists():
        return "verification-error", None, f"allowed_signers registry not found: {allowed}"
    if not src.exists(MANIFEST_NAME):
        return "verification-error", None, f"no {MANIFEST_NAME} in this pack"

    try:
        data = src.read_bytes(MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES)
        sig_bytes = src.read_bytes(SIG_NAME, max_bytes=MAX_MANIFEST_BYTES)
    except SystemExit as e:
        return "verification-error", None, str(e)

    keygen = _ssh_keygen_path()
    sig_path = None
    try:
        fd, sig_path_str = tempfile.mkstemp(suffix=".sig")
        sig_path = Path(sig_path_str)
        with os.fdopen(fd, "wb") as f:
            f.write(sig_bytes)

        r = subprocess.run(
            [keygen, "-Y", "check-novalidate", "-n", SIG_NAMESPACE, "-s", str(sig_path)],
            input=data, capture_output=True, timeout=20)
        if r.returncode != 0:
            return "invalid", None, (r.stdout + r.stderr).decode(errors="replace").strip()[:300]

        r = subprocess.run(
            [keygen, "-Y", "find-principals", "-f", str(allowed), "-s", str(sig_path)],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown-signer", None, "signature key is not present in allowed_signers"
        # find-principals can return MULTIPLE lines when the same key is
        # registered under more than one principal/options entry — declared
        # duplicate of pack_lib.py's identical fix (2026-08-09
        # post-implementation review, all three models independently: taking
        # only the first line could reject an otherwise-authorized key whose
        # first-listed principal carries a narrower namespace restriction
        # than a later-listed one for the same key). Try each candidate in
        # order; accept the first that verifies.
        candidates = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
        last_detail = None
        for principal in candidates:
            r = subprocess.run(
                [keygen, "-Y", "verify", "-f", str(allowed), "-I", principal,
                 "-n", SIG_NAMESPACE, "-s", str(sig_path)],
                input=data, capture_output=True, timeout=20)
            if r.returncode == 0:
                return "verified", principal, None
            last_detail = (r.stdout + r.stderr).decode(errors="replace").strip()[:300]
        return "invalid", candidates[0] if candidates else None, last_detail
    except subprocess.TimeoutExpired:
        return "verification-error", None, "ssh-keygen timed out"
    except OSError as e:
        return "verification-error", None, f"ssh-keygen invocation failed: {e}"
    finally:
        if sig_path is not None:
            try:
                sig_path.unlink()
            except OSError:
                pass


def cmd_inspect(args):
    src = PackSource(args.pack)
    try:
        manifest = read_manifest(src)
        print(f"id:        {_safe_display(manifest.get('id', '?'))}")
        print(f"kind:      {_safe_display(manifest.get('kind', '?'))}")
        print(f"audience:  {_safe_display(manifest.get('audience', '?'))}")
        print(f"created:   {_safe_display(manifest.get('created', '?'))}")
        print(f"builder:   {_safe_display(manifest.get('builder', '?'))}")
        tags = manifest.get("tags", [])
        tags_str = ", ".join(_safe_display(t) for t in tags) if isinstance(tags, list) else "(malformed)"
        print(f"tags:      {tags_str or '(none)'}")
        print(f"schema:    {_safe_display(manifest.get('pack_schema', '?'))}")
        sig_state, sig_principal, sig_detail = verify_pack_sig(src, args.allowed_signers)
        principal_note = f", principal={_safe_display(sig_principal)}" if sig_principal else ""
        print(f"signature: {sig_state}{principal_note}")
        if sig_state == "verification-error" and args.allowed_signers is None:
            print("           (pass --allowed-signers <path> to verify beyond presence)")
        elif sig_detail and sig_state not in ("unsigned", "verified"):
            print(f"           {_safe_display(sig_detail, maxlen=300)}")
        print(f"scrub:     {'yes' if 'scrub' in manifest else 'no'}")
        if manifest.get("origin"):
            print(f"origin:    {_safe_display(manifest['origin'])}")
        print(f"size:      {src.total_size()} bytes")
        print("NOTE: --inspect shows manifest claims; it does not verify hashes. "
              "Run --verify before trusting this pack's content.")
        print("parts:")
        parts = manifest.get("parts", [])
        if not isinstance(parts, list):
            print(f"  (malformed: 'parts' is {type(parts).__name__})")
            return 0
        for part in parts:
            if not isinstance(part, dict):
                print(f"  (malformed part entry: {type(part).__name__})")
                continue
            files = part.get("files", [])
            if not isinstance(files, list):
                files = []
            print(f"  - {_safe_display(part.get('type'))} (id={_safe_display(part.get('id'))}, "
                  f"schema={_safe_display(part.get('schema'))}, dest={_safe_display(part.get('dest'))!r}, "
                  f"{len(files)} file(s))")
            for f in files[:args.max_files]:
                print(f"      {_safe_display(f)}")
            if len(files) > args.max_files:
                print(f"      … {len(files) - args.max_files} more")
        return 0
    finally:
        src.close()


def cmd_verify(args):
    src = PackSource(args.pack)
    try:
        ok, problems = verify_pack(src)
        if ok:
            print("VERIFIED — pack_schema known, SHA256SUMS self-consistent, "
                  "every declared file present and hash-matching, no "
                  "undeclared/extra files, audience gate holds. See the "
                  "'signature:' line below for whether pack.json's own "
                  "metadata is also authenticated.")
        else:
            print("FAILED:")
            for p in problems:
                print(f"  - {_safe_display(p, maxlen=500)}")
        # Signature CLASSIFICATION only — this does not change the exit
        # code, which stays governed by integrity (verify_pack) alone, same
        # as it always has. install.py's caller is what enforces a policy
        # (require verified for replica by default, etc.) by parsing this
        # exact "signature: <state>" line; classify here, enforce there.
        sig_state, sig_principal, sig_detail = verify_pack_sig(src, args.allowed_signers)
        principal_note = f" principal={_safe_display(sig_principal)}" if sig_principal else ""
        print(f"signature: {sig_state}{principal_note}")
        if sig_detail and sig_state not in ("unsigned", "verified"):
            print(f"  {_safe_display(sig_detail, maxlen=300)}")
        if args.target:
            print(f"\n--target {_safe_display(args.target)}: informational only here — this "
                  f"tool never writes. Run install.py --approve import-pack --from-pack "
                  f"<pack-dir> --target {_safe_display(args.target)} to actually apply it.")
        return 0 if ok else 1
    finally:
        src.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True, help="pack directory or .tar file")
    ap.add_argument("--target", default=None,
                     help="informational only — this tool never writes; see install.py --approve import-pack")
    ap.add_argument("--max-files", type=int, default=10,
                     help="--inspect: cap listed filenames per part (default 10)")
    ap.add_argument("--allowed-signers", default=None,
                     help="signer registry path for signature verification — "
                          "REQUIRED to get anything beyond presence/'unsigned'; "
                          "this tool never probes a default location (it ships "
                          "to hosts without the cc-pack workspace repo)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inspect", action="store_true")
    g.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    return cmd_inspect(args) if args.inspect else cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
