#!/usr/bin/env python3
"""Offline tests for bridge.py and mcp_server.py. Every case can fail.

Runs against temp directories only: the guard is pointed at a fake store via
MEMORY_WRITE_GUARD_STORE, sources at temp folders, installs at temp files.
    /usr/bin/python3 memory-mesh/test_bridge.py
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="bridge-test-"))
os.environ["MESH_BRIDGE_CONF_DIR"] = str(TMP / "conf")
os.environ["MESH_BRIDGE_TERMS"] = str(TMP / "terms.txt")
os.environ["LEARN_DENY_FILE"] = str(TMP / "no-such-deny.txt")
(TMP / "terms.txt").write_text("\\bAcmeBank\\b\n")
STORE = TMP / "store"
STORE.mkdir()
os.environ["MEMORY_WRITE_GUARD_STORE"] = str(STORE)
os.environ["MESH_BRIDGE_CLAUDE_PROJECTS"] = str(TMP / "claude-projects")
sys.path.insert(0, str(HERE))
import bridge as B  # noqa: E402
import mcp_server as S  # noqa: E402


def cfg_with(**sources):
    base = json.loads((HERE / "bridge.json").read_text())
    base["sources"] = sources
    return base


class Guard(unittest.TestCase):
    def deny(self, ev):
        ok, why = B.guard(json.dumps(ev))
        self.assertFalse(ok, ev)
        self.assertIn("memory-write-guard", why)

    def allow(self, ev):
        ok, why = B.guard(json.dumps(ev))
        self.assertTrue(ok, why)

    def test_grok_file_write_into_store(self):
        self.deny({"toolName": "search_replace", "cwd": "/tmp",
                   "toolInput": {"file_path": str(STORE / "MEMORY.md")}})

    def test_grok_write_elsewhere(self):
        self.allow({"toolName": "write", "cwd": "/tmp", "toolInput": {"file_path": "/tmp/x.md"}})

    def test_grok_shell_redirect(self):
        self.deny({"toolName": "run_terminal_command", "cwd": "/tmp",
                   "toolInput": {"command": f"echo hi > {STORE}/foo.md"}})

    def test_shell_read_allowed(self):
        self.allow({"toolName": "run_terminal_command", "cwd": "/tmp",
                    "toolInput": {"command": f"cat {STORE}/MEMORY.md"}})

    def test_opencode_relative_path(self):
        self.deny({"tool": "edit", "cwd": str(TMP), "args": {"filePath": "store/x.md"}})

    def test_codex_apply_patch(self):
        self.deny({"tool_name": "apply_patch", "cwd": "/tmp", "tool_input": {
            "input": f"*** Begin Patch\n*** Add File: {STORE}/evil.md\n+x\n*** End Patch"}})

    def test_codex_exec_list_form(self):
        self.deny({"tool_name": "exec_command", "cwd": "/tmp",
                   "tool_input": {"command": ["bash", "-lc", f"rm {STORE}/MEMORY.md"]}})

    def test_garbage_fails_open(self):
        self.assertEqual(B.guard("not json"), (True, ""))

    def test_garbage_naming_store_fails_closed(self):
        self.assertFalse(B.guard("not json " + str(STORE))[0])

    def test_codex_cmd_key(self):
        self.deny({"tool_name": "exec_command", "cwd": "/tmp",
                   "tool_input": {"cmd": f"rm {STORE}/MEMORY.md"}})

    def test_patch_move_to(self):
        self.deny({"tool_name": "apply_patch", "cwd": "/tmp", "tool_input": {
            "input": f"*** Begin Patch\n*** Update File: /tmp/a.md\n*** Move to: {STORE}/b.md\n*** End Patch"}})

    def test_file_key_and_nested_edits(self):
        self.deny({"toolName": "edit", "cwd": "/tmp", "toolInput": {"file": str(STORE / "c.md")}})
        self.deny({"tool_name": "MultiEdit", "cwd": "/tmp", "tool_input": {
            "edits": [{"file_path": str(STORE / "d.md"), "old_string": "a", "new_string": "b"}]}})

    def test_unknown_tool_naming_store_refused(self):
        ok, why = B.guard(json.dumps({"tool_name": "mystery_mutator", "cwd": "/tmp",
                                      "tool_input": {"blob": f"put it in {STORE}/e.md"}}))
        self.assertFalse(ok)

    def test_read_tool_naming_store_allowed(self):
        self.allow({"tool_name": "read_file", "cwd": "/tmp",
                    "tool_input": {"target": str(STORE / "MEMORY.md")}})
        self.allow({"toolName": "grep", "cwd": "/tmp",
                    "toolInput": {"pattern": "x", "path": str(STORE)}})

    def test_policy_relative_and_inline(self):
        conf = Path(os.environ["MESH_BRIDGE_CONF_DIR"])
        conf.mkdir(parents=True, exist_ok=True)
        bash = lambda c, cwd="/tmp": B.guard(json.dumps({"tool_name": "Bash", "cwd": cwd,
                                                         "tool_input": {"command": c}}))[0]
        self.assertFalse(bash("rm bridge.json", cwd=str(conf)))               # relative path
        self.assertFalse(bash("python3 -c \"open('%s/bridge.json','w')\"" % conf))
        self.assertFalse(bash("python3 - <<'EOF'\nimport os; os.remove('%s/bridge.json')\nEOF" % conf))
        self.assertTrue(bash("git commit -m 'note: %s/bridge.json -> tidy'" % conf))  # message is data
        self.assertTrue(bash("cat %s/bridge.json" % conf))
        self.assertTrue(bash("ls /tmp > /tmp/listing.txt"))                  # nothing protected named

    def test_policy_round3_shapes(self):
        conf = Path(os.environ["MESH_BRIDGE_CONF_DIR"])
        conf.mkdir(parents=True, exist_ok=True)
        bash = lambda c, cwd="/tmp": B.guard(json.dumps({"tool_name": "Bash", "cwd": cwd,
                                                         "tool_input": {"command": c}}))[0]
        self.assertFalse(bash("cd %s && rm bridge.json" % conf))                # cd tracking
        self.assertFalse(bash("git checkout HEAD -- %s/bridge.json" % conf))    # not a redirect
        self.assertFalse(bash("python3 -c \"from pathlib import Path; Path('bridge.json').unlink()\"",
                              cwd=str(conf)))                                   # quoted relative
        self.assertTrue(bash("cat <<'EOF'\npython bridge.json\nEOF", cwd=str(conf)))  # heredoc to cat
        self.assertTrue(bash("git log --oneline -- %s/bridge.json" % conf))
        self.assertTrue(bash("grep -n lane %s/bridge.json | head" % conf))

    def test_timeout_on_bare_store_name_fails_closed(self):
        import subprocess as sp
        orig = B.subprocess.run
        B.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("g", 5))
        try:
            ok, _ = B.guard(json.dumps({"tool_name": "Bash", "cwd": "/tmp",
                                        "tool_input": {"command": "rm MEMORY.md"}}))
        finally:
            B.subprocess.run = orig
        self.assertFalse(ok)

    def test_claude_reinstall_updates_guard_matcher(self):
        user = TMP / "user2.json"
        user.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/usr/bin/python3 /x/bridge.py guard --harness claude --policy-only"}]}]}},
            indent=2) + "\n")
        doc = json.loads(B._claude_settings_plan({"path": str(user)})[0][2])
        guards = [e for e in doc["hooks"]["PreToolUse"]
                  if any("bridge.py guard" in h["command"] for h in e["hooks"])]
        self.assertEqual(len(guards), 1)
        self.assertIn("mcp__", guards[0]["matcher"])

    def test_code_is_policy_for_other_harnesses_only(self):
        ev = json.dumps({"toolName": "write", "cwd": "/tmp",
                         "toolInput": {"file_path": str(HERE / "bridge.py")}})
        self.assertFalse(B.guard(ev)[0])                    # grok/opencode: code is policy
        self.assertTrue(B.guard(ev, policy_only=True)[0])   # claude develops it

    def test_policy_files_guarded(self):
        conf = Path(os.environ["MESH_BRIDGE_CONF_DIR"])
        ok, why = B.guard(json.dumps({"toolName": "write", "cwd": "/tmp",
                                      "toolInput": {"file_path": str(conf / "bridge.json")}}))
        self.assertFalse(ok)
        self.assertIn("policy", why)
        ok, _ = B.guard(json.dumps({"tool_name": "Bash", "cwd": "/tmp", "tool_input": {
            "command": f"echo x >> {conf}/client-terms.txt"}}), policy_only=True)
        self.assertFalse(ok)
        ok, _ = B.guard(json.dumps({"tool_name": "Bash", "cwd": "/tmp", "tool_input": {
            "command": f"cat {conf}/client-terms.txt"}}), policy_only=True)
        self.assertTrue(ok)

    def test_guard_timeout_fails_closed(self):
        import subprocess as sp
        orig = B.subprocess.run
        def boom(*a, **k):
            raise sp.TimeoutExpired("guard", 15)
        B.subprocess.run = boom
        try:
            ok, _ = B.guard(json.dumps({"toolName": "write", "cwd": "/tmp",
                                        "toolInput": {"file_path": str(STORE / "x.md")}}))
            self.assertFalse(ok)
            ok, _ = B.guard(json.dumps({"toolName": "write", "cwd": "/tmp",
                                        "toolInput": {"file_path": "/tmp/fine.md"}}))
            self.assertTrue(ok)
        finally:
            B.subprocess.run = orig


class Classify(unittest.TestCase):
    def test_terms(self):
        self.assertEqual(B.classify("work for AcmeBank"), "employer-client")
        self.assertEqual(B.classify("likes horses"), "personal")

    def test_explicit_can_only_tighten(self):
        self.assertEqual(B.classify("---\ndata_class: employer-client\n---\nhorses"),
                         "employer-client")
        self.assertEqual(B.classify("data_class: personal\nthe AcmeBank deal"),
                         "employer-client")                         # a forged label loses

    def test_emitted_pieces_classified_together(self):
        self.assertEqual(B.classify_all("- [x] AcmeBank staffing", "horses only"),
                         "employer-client")                         # term only in the index line

    def test_terms_file_required(self):
        old = os.environ["MESH_BRIDGE_TERMS"]
        deny = TMP / "deny.txt"
        deny.write_text(r"\bOtherCo\b" + "\n")
        os.environ["LEARN_DENY_FILE"] = str(deny)
        try:
            os.environ["MESH_BRIDGE_TERMS"] = str(TMP / "missing.txt")
            self.assertEqual(B.classify("horses"), "unknown")       # deny list can't stand in
            (TMP / "empty.txt").write_text("# nothing\n")
            os.environ["MESH_BRIDGE_TERMS"] = str(TMP / "empty.txt")
            self.assertEqual(B.classify("horses"), "unknown")       # empty file is not usable
            self.assertEqual(B.classify("OtherCo"), "employer-client")
        finally:
            os.environ["MESH_BRIDGE_TERMS"] = old
            os.environ["LEARN_DENY_FILE"] = str(TMP / "no-such-deny.txt")

    def test_invalid_regex_makes_terms_unusable(self):
        old = os.environ["MESH_BRIDGE_TERMS"]
        bad = TMP / "bad.txt"
        bad.write_text("\\bAcmeBank\\b\n(unclosed\n")
        os.environ["MESH_BRIDGE_TERMS"] = str(bad)
        try:
            self.assertEqual(B.classify("horses"), "unknown")
            self.assertEqual(B.classify("AcmeBank"), "employer-client")
        finally:
            os.environ["MESH_BRIDGE_TERMS"] = old

    def test_terms_reload_on_change(self):
        p = Path(os.environ["MESH_BRIDGE_TERMS"])
        self.assertEqual(B.classify("NewClientX"), "personal")
        st = p.stat()
        p.write_text(p.read_text() + "\\bNewClientX\\b\n")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))           # timestamp preserved
        self.assertEqual(B.classify("NewClientX"), "employer-client")

    def test_lane_policy(self):
        cfg = cfg_with()
        self.assertIn("employer-client", B.lane_allow(cfg, "claude"))
        self.assertNotIn("employer-client", B.lane_allow(cfg, "codex"))
        self.assertEqual(B.lane_allow(cfg, "never-heard-of-it"), {"personal"})


class Sources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = TMP / "md"
        d.mkdir()
        (d / "horses.md").write_text("---\ndescription: the horses eat hay at dawn\n---\nhay hay\n")
        (d / "bank.md").write_text("---\ndescription: AcmeBank staffing hay plan\n---\nhay\n")
        prog = TMP / "src.py"
        prog.write_text(
            "import json,sys\nq=json.load(sys.stdin)\n"
            "print(json.dumps({'hits':[{'id':'w1','desc':'weather hay forecast','score':3}]}"
            " if q['op']=='search' else {'text':'full weather note'}))\n")
        cls.cfg = cfg_with(
            md={"type": "markdown_dir", "enabled": True, "path": str(d), "glob": "*.md"},
            ext={"type": "command", "enabled": True, "weight": 0.5,
                 "argv": [sys.executable, str(prog)]},
            off={"type": "markdown_dir", "enabled": False, "path": str(d)})

    def test_merge_and_filter(self):
        ids = [h["id"] for h in B.recall("hay", "codex", 5, cfg=self.cfg)]
        self.assertIn("md:horses.md", ids)
        self.assertIn("ext:w1", ids)
        self.assertNotIn("md:bank.md", ids)          # employer-client withheld
        ids = [h["id"] for h in B.recall("hay", "claude", 5, cfg=self.cfg)]
        self.assertIn("md:bank.md", ids)

    def test_get_and_escape(self):
        self.assertEqual(B.get("ext:w1", "codex", cfg=self.cfg)["text"], "full weather note")
        self.assertIsNone(B.get("md:../../etc/passwd", "codex", cfg=self.cfg))
        self.assertIsNone(B.get("md:bank.md", "codex", cfg=self.cfg))   # no existence oracle

    def test_command_self_label_cannot_loosen(self):
        prog = TMP / "liar.py"
        prog.write_text("import json,sys\nprint(json.dumps({'hits':[{'id':'x','desc':'AcmeBank hay',"
                        "'score':1,'data_class':'personal'}]}))\n")
        cfg = cfg_with(liar={"type": "command", "enabled": True, "argv": [sys.executable, str(prog)]})
        self.assertEqual(B.recall("hay", "codex", 5, cfg=cfg), [])

    def test_broken_command_source_fails_open(self):
        cfg = cfg_with(bad={"type": "command", "enabled": True, "argv": ["/no/such/prog"]})
        self.assertEqual(B.recall("hay", "codex", 5, cfg=cfg), [])

    def test_render_bounded(self):
        text = B.render_lane("codex", cfg=self.cfg)
        self.assertLessEqual(len(text), B.lane_chars(self.cfg, "codex"))
        self.assertIn("memory_recall", text)

    def test_lane_budgets(self):
        cfg = cfg_with()
        self.assertEqual(B.lane_chars(cfg, "codex"), B.DEFAULT_LANE_CHARS)
        self.assertEqual(B.lane_chars(cfg, "local"), 2500)
        self.assertEqual(B.lane_chars(cfg, "claude"), 9500)

    def test_row_cannot_carry_markers(self):
        self.assertNotIn(B.END, B._clean_row("- [x] evil " + B.END + " tail"))


class Splice(unittest.TestCase):
    def test_roundtrip_preserves_human_text(self):
        human = "# mine\nkeep me\n"
        once = B.splice(human, "block one")
        twice = B.splice(once, "block two")
        self.assertIn("keep me", twice)
        self.assertNotIn("block one", twice)
        self.assertEqual(twice.count(B.BEGIN), 1)
        self.assertEqual(B.unsplice(twice), human)

    def test_malformed_markers_raise(self):
        with self.assertRaises(B.MarkerError):
            B.splice(B.END + "\nhuman\n" + B.BEGIN + "\nold rows\n", "new")
        with self.assertRaises(B.MarkerError):
            B.splice(B.BEGIN + "\nold\n", "new")

    def test_render_all_fails_closed_on_damaged_markers(self):
        f = TMP / "damaged.md"
        f.write_text(B.BEGIN + "\n- [stale-row] withheld now\nhuman text\n")
        B.RENDER_DIR.mkdir(parents=True, exist_ok=True)
        (B.RENDER_DIR / "codex.md").write_text("- [stale-row] withheld now\n")   # last cycle's render
        cfg = cfg_with()
        cfg["targets"] = {"t": {"type": "static_file", "lane": "codex", "path": str(f)}}
        res = B.render_all(cfg=cfg, quiet=True)
        self.assertNotIn("stale-row", f.read_text())
        self.assertIn("human text", f.read_text())                 # the human's line survives
        self.assertTrue(any("damaged" in a or "malformed" in a for a in res["alarms"]))

    def test_render_failure_serves_stub(self):
        f = TMP / "stub.md"
        cfg = cfg_with(bad={"type": "mesh", "enabled": True})
        cfg["targets"] = {"t": {"type": "static_file", "lane": "codex", "path": str(f)}}
        orig = B.render_lane
        B.render_lane = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            res = B.render_all(cfg=cfg, quiet=True)
        finally:
            B.render_lane = orig
        self.assertIn("unavailable", f.read_text())
        self.assertTrue(any("stub served" in a for a in res["alarms"]))

    def test_damaged_markers_keep_human_text(self):
        gen = {"- [stale-row] withheld now", "# Craig's shared memory (memory-mesh)"}
        old = B.BEGIN + "\n# Craig's shared memory (memory-mesh)\n- [stale-row] withheld now\nmy own note\n"
        out = B.salvage_human(old, gen)
        self.assertEqual(out, "my own note\n")
        older = "- [ancient-row] from three renders ago\nmine\n"
        self.assertEqual(B.salvage_human(older, set(), {"ancient-row"}), "mine\n")

    def test_config_failure_withdraws_known_blocks(self):
        f = TMP / "known.md"
        f.write_text(B.splice("human\n", "- [old-row] stale"))
        B.STATE_DIR.mkdir(parents=True, exist_ok=True)
        B._known_targets_path().write_text(json.dumps({"t": {"path": str(f), "lane": "codex"}}))
        orig = B.load_config
        B.load_config = lambda *a, **k: (_ for _ in ()).throw(ValueError("corrupt"))
        try:
            res = B.render_all(quiet=True)
        finally:
            B.load_config = orig
        self.assertNotIn("old-row", f.read_text())
        self.assertIn("human", f.read_text())
        self.assertTrue(any("withdrawn" in a for a in res["alarms"]))

    def test_config_failure_stubs_context_files(self):
        B.RENDER_DIR.mkdir(parents=True, exist_ok=True)
        ctx = B.RENDER_DIR / "default.md"
        ctx.write_text("- [stale-client] AcmeBank row\n")
        orig = B.load_config
        B.load_config = lambda *a, **k: (_ for _ in ()).throw(ValueError("corrupt"))
        try:
            B.render_all(quiet=True)
        finally:
            B.load_config = orig
        self.assertNotIn("stale-client", ctx.read_text())
        self.assertIn("unavailable", ctx.read_text())

    def test_empty_file(self):
        self.assertEqual(B.unsplice(B.splice("", "x")), "")


class Install(unittest.TestCase):
    def test_toml_roundtrip(self):
        p = TMP / "codex.toml"
        p.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "y"\n')
        cfg = cfg_with()
        t = {"type": "mcp_toml", "lane": "codex", "path": str(p)}
        (path, old, new), = B.plan_target("t", t, cfg)
        self.assertIn("[mcp_servers.memory]", new)
        self.assertIn('"--lane", "codex"', new)
        p.write_text(new)
        self.assertEqual(B.plan_target("t", t, cfg), [])            # idempotent
        (path, old, undone), = B.plan_target("t", t, cfg, undo=True)
        self.assertNotIn("mcp_servers.memory", undone)
        self.assertIn("[mcp_servers.other]", undone)

    def test_toml_refuses_foreign_memory_server(self):
        p = TMP / "foreign.toml"
        p.write_text('[mcp_servers.memory]\ncommand = "someone-else"\n')
        self.assertEqual(B.plan_target("t", {"type": "mcp_toml", "lane": "codex",
                                             "path": str(p)}, cfg_with()), [])

    def test_claude_settings_moves_recall_hook(self):
        user, proj = TMP / "user.json", TMP / "proj.json"
        user.write_text(json.dumps({"hooks": {"PreToolUse": []}}, indent=2) + "\n")
        cmd = "/venv/python3 /x/memory-mesh/retrieve.py"
        proj.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": cmd}]}],
            "Stop": []}}, indent=2) + "\n")
        plans = B._claude_settings_plan({"path": str(user), "project_settings": [str(proj)]})
        new_user = json.loads(plans[0][2])
        new_proj = json.loads(plans[1][2])
        ups = new_user["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(ups, cmd)                                  # verbatim move
        self.assertNotIn("UserPromptSubmit", new_proj["hooks"])
        self.assertIn("Stop", new_proj["hooks"])
        ss = new_user["hooks"]["SessionStart"][0]
        self.assertIn("bridge.py hook claude-session", ss["hooks"][0]["command"])


class Hooks(unittest.TestCase):
    def test_session_silent_in_generated_folder(self):
        key = B._claude_key(TMP / "gen")
        marker = TMP / "claude-projects" / key / "memory" / ".mesh-generated"
        (TMP / "gen").mkdir()
        marker.parent.mkdir(parents=True)
        marker.write_text("")
        self.assertEqual(B.hook_claude_session(json.dumps({"cwd": str(TMP / "gen")})), "")

    def test_session_injects_elsewhere(self):
        (TMP / "elsewhere").mkdir()
        B.RENDER_DIR.mkdir(parents=True, exist_ok=True)
        (B.RENDER_DIR / "claude.md").write_text("# Craig's shared memory (memory-mesh)\nfixture\n")
        out = json.loads(B.hook_claude_session(json.dumps({"cwd": str(TMP / "elsewhere")})))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Craig's shared memory", ctx)
        self.assertLessEqual(len(ctx), B.MAX_CONTEXT_CHARS)


class Propose(unittest.TestCase):
    def setUp(self):
        os.environ["MESH_BRIDGE_DRY_PROPOSE"] = "1"

    def tearDown(self):
        os.environ.pop("MESH_BRIDGE_DRY_PROPOSE", None)

    def test_derived_slug_and_untrusted(self):
        out = B.propose("Grok!", "short hook", "a durable lesson")
        self.assertTrue(out["ok"])
        self.assertTrue(out["slug"].startswith("proposed-grok-"))
        self.assertIn("contains-untrusted", out["dry_run"])

    def test_cap_holds_under_concurrency(self):
        import threading
        results = []
        def go(i):
            results.append(B.propose("race", "hook %d" % i, "lesson number %d" % i)["ok"])
        ths = [threading.Thread(target=go, args=(i,)) for i in range(25)]
        os.environ.pop("MESH_BRIDGE_DRY_PROPOSE", None)
        orig = B.subprocess.run
        B.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        try:
            [t.start() for t in ths]
            [t.join() for t in ths]
        finally:
            B.subprocess.run = orig
            os.environ["MESH_BRIDGE_DRY_PROPOSE"] = "1"
        self.assertEqual(sum(results), B.PROPOSALS_PER_LANE_PER_DAY)

    def test_refusals(self):
        self.assertFalse(B.propose("x", "", "r")["ok"])
        self.assertFalse(B.propose("x", "h" * 141, "r")["ok"])
        self.assertFalse(B.propose("x", "h", "key sk-abcdefghijklmnopqrstu")["ok"])
        self.assertFalse(B.propose("x", "h", "the AcmeBank account")["ok"])


class Mcp(unittest.TestCase):
    def test_protocol(self):
        init = S.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05"}}, "codex")
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        names = {t["name"] for t in S.handle({"id": 2, "method": "tools/list"}, "x")["result"]["tools"]}
        self.assertEqual(names, {"memory_recall", "memory_get", "memory_index", "memory_propose"})
        self.assertIsNone(S.handle({"method": "notifications/initialized"}, "x"))
        self.assertEqual(S.handle({"id": 3, "method": "tools/call",
                                   "params": {"name": "nope"}}, "x")["error"]["code"], -32602)


class McpCliExe(unittest.TestCase):
    """A harness CLI that is not at its configured path must be found on PATH
    (mise shims on the Omarchy laptops), and one that is absent everywhere must
    read as "no harness" — never "ok" (false green, 2026-10-31)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_path = os.environ.get("PATH", "")

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def _target(self, exe):
        return {"type": "mcp_cli", "lane": "claude",
                "probe": [exe, "mcp", "get", "memory"],
                "add": [exe, "mcp", "add", "memory"], "remove": [exe, "mcp", "remove", "memory"]}

    def test_found_on_path_when_configured_path_missing(self):
        shim = self.tmp / "fakeclaude"
        shim.write_text("#!/bin/sh\nexit 1\n")
        shim.chmod(0o755)
        os.environ["PATH"] = str(self.tmp)
        t = self._target(str(self.tmp / "nowhere" / "fakeclaude"))
        self.assertEqual(B._mcp_cli_exe(t, {}), str(shim))
        plans = B.plan_target("x", t, {})
        self.assertEqual(plans[0][1][0], str(shim))  # add runs the resolved shim

    def test_absent_everywhere_is_no_harness_not_ok(self):
        os.environ["PATH"] = str(self.tmp)
        cfg = {"targets": {"x": self._target(str(self.tmp / "nowhere" / "absentcli"))}}
        self.assertEqual(B.status(cfg)[0][3], "no harness")

    def test_missing_harness_script_is_no_harness(self):
        cfg = {"targets": {"x": {"type": "mcp_cli", "lane": "corral",
                                 "probe": [sys.executable, str(self.tmp / "mcp.py"), "test", "memory"],
                                 "add": [sys.executable], "remove": [sys.executable]}}}
        self.assertEqual(B.status(cfg)[0][3], "no harness")


class PortablePaths(unittest.TestCase):
    """bridge.json ships in the seed: no host's layout may be baked into it.
    {corral_mcp} resolves per install method, {workspace} per host, and a host
    that evidently HAS the thing but cannot resolve it reads ERROR, not
    "no harness" (cc-handoff …3af6, 2026-11-21). Sandbox HOME throughout."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="bridge-home-"))
        self.saved = {k: os.environ.get(k) for k in ("HOME", "PATH", "AIOS_WORKSPACE")}
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.home / "empty-bin")   # no corral-light on PATH
        os.environ.pop("AIOS_WORKSPACE", None)
        self.saved_cands = getattr(B, "CORRAL_MCP_CANDIDATES", ())  # absent on old master
        self.cfg = cfg_with()
        self.cfg["python"] = sys.executable
        self.log = self.home / "argv.log"

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        B.CORRAL_MCP_CANDIDATES = self.saved_cands
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    # ── helpers ──
    def _stub(self, rel):
        """A Corral mcp.py that records its argv; `test` fails (not wired yet)."""
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("import sys, json\n"
                     f"open({str(self.log)!r}, 'a').write(json.dumps(sys.argv) + '\\n')\n"
                     "sys.exit(1 if sys.argv[1] == 'test' else 0)\n")
        return p

    def _corral(self):
        return self.cfg["targets"]["mcp-corral"]

    def _status(self, name):
        return dict((r[0], r[3]) for r in B.status(self.cfg))[name]

    def _assert_wires(self, mcp):
        (kind, argv), = B.plan_target("mcp-corral", self._corral(), self.cfg)
        self.assertEqual((kind, argv[1], argv[2]), ("cmd", str(mcp), "add"))
        rows = B.install(apply=True, only=["mcp-corral"], cfg=self.cfg)
        self.assertEqual(rows[0][1], "ran", rows)
        calls = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(calls[-1][0], str(mcp))           # the resolved script really ran
        self.assertEqual(calls[-1][1:3], ["add", "memory"])

    # ── {corral_mcp} ──
    def test_linux_install_sh_layout(self):
        self._assert_wires(self._stub("tools/corral-light/mcp.py"))

    def test_dev_checkout_layout(self):
        self._assert_wires(self._stub("corral-light/mcp.py"))

    def test_homebrew_layout(self):
        mcp = self._stub("brew/opt/corral-light/libexec/mcp.py")
        B.CORRAL_MCP_CANDIDATES = self.saved_cands[:2] + (str(mcp),)
        self._assert_wires(mcp)

    def test_shipped_candidates_cover_every_install_method(self):
        c = self.saved_cands
        self.assertIn("~/tools/corral-light/mcp.py", c)
        self.assertIn("~/corral-light/mcp.py", c)
        for prefix in ("/opt/homebrew", "/usr/local", "{{HOME}}/.linuxbrew"):
            self.assertIn(f"{prefix}/opt/corral-light/libexec/mcp.py", c)

    def test_override_wins(self):
        self._stub("corral-light/mcp.py")                    # a candidate also exists
        mcp = self._stub("elsewhere/mcp.py")
        self.cfg["corral_mcp"] = str(mcp)
        self._assert_wires(mcp)

    def test_corral_absent_is_no_harness(self):
        self.assertEqual(self._status("mcp-corral"), "no harness")
        self.assertEqual(B.plan_target("mcp-corral", self._corral(), self.cfg), [])

    def test_consult_only_config_dir_is_no_harness(self):
        # {{REDACTED}}: ~/.config/corral-light holds codex-home + a consult
        # session and there is no Corral. Must not become an ERROR.
        d = self.home / ".config/corral-light/codex-home"
        d.mkdir(parents=True)
        (d.parent / "consult-session.json").write_text("{}")
        self.assertEqual(self._status("mcp-corral"), "no harness")

    def test_installed_but_mcp_py_unresolved_is_error(self):
        d = self.home / ".config/corral-light"
        d.mkdir(parents=True)
        (d / "mcp.json").write_text("{}")
        self.assertTrue(self._status("mcp-corral").startswith("ERROR"))
        rows = B.install(apply=False, only=["mcp-corral"], cfg=self.cfg)
        self.assertEqual(rows[0][1], "ERROR", rows)

    def test_corral_on_path_but_mcp_py_unresolved_is_error(self):
        b = self.home / "empty-bin"
        b.mkdir()
        (b / "corral-light").write_text("#!/bin/sh\n")
        (b / "corral-light").chmod(0o755)
        self.assertTrue(self._status("mcp-corral").startswith("ERROR"))

    def test_override_pointing_nowhere_is_error(self):
        self._stub("corral-light/mcp.py")                    # must NOT be used silently
        self.cfg["corral_mcp"] = str(self.home / "gone/mcp.py")
        self.assertTrue(self._status("mcp-corral").startswith("ERROR"))

    # ── {workspace} ──
    def _hooks(self, ws_dir=None):
        user = self.home / ".claude/settings.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text("{}\n")
        if ws_dir is not None:
            proj = ws_dir / ".claude/settings.json"
            proj.parent.mkdir(parents=True, exist_ok=True)
            proj.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "/v/python3 /x/memory-mesh/retrieve.py"}]}]}}))
            return proj
        return None

    def _planned(self):
        return [p for p, _o, _n in B.plan_target("hooks-claude", self.cfg["targets"]["hooks-claude"], self.cfg)]

    def test_explicit_workspace_present(self):
        proj = self._hooks(self.home / "ai-os")
        self.cfg["workspace"] = "~/ai-os"
        self.assertIn(proj, self._planned())

    def test_env_workspace_present(self):
        proj = self._hooks(self.home / "ws-env")
        os.environ["AIOS_WORKSPACE"] = str(self.home / "ws-env")
        self.assertIn(proj, self._planned())

    def test_default_workspace(self):
        proj = self._hooks(self.home / "aios")
        self.assertIn(proj, self._planned())

    def test_explicit_workspace_missing_is_error(self):
        self._hooks()
        self.cfg["workspace"] = "~/ai-os"                     # set, but not on disk
        with self.assertRaises(FileNotFoundError):
            self._planned()
        rows = B.install(apply=False, only=["hooks-claude"], cfg=self.cfg)
        self.assertEqual(rows[0][1], "ERROR", rows)

    def test_default_workspace_absent_skips(self):
        self._hooks()
        self.assertEqual(self._planned(), [self.home / ".claude/settings.json"])

    def test_policy_paths_guard_the_resolved_settings_file(self):
        self.cfg["workspace"] = str(self.home / "ai-os")
        paths = B.policy_paths(self.cfg, include_code=False)
        self.assertIn(str(self.home / "ai-os/.claude/settings.json"), paths)
        self.assertFalse([p for p in paths if "{" in p], paths)

    def test_shipped_bridge_json_has_no_host_layout(self):
        raw = (HERE / "bridge.json").read_text()
        self.assertNotIn("~/ai-os/", raw)
        self.assertNotIn("~/corral-light/", raw)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)


class MarkdownExcludeTests(unittest.TestCase):
    """A markdown source never reads or serves excluded, hidden or opted-out notes."""

    def _src(self, root, exclude=()):
        import bridge as B
        return B.MarkdownDirSource("vault", {"type": "markdown_dir", "path": str(root),
                                             "glob": "**/*.md", "exclude": list(exclude)})

    def _tree(self, d):
        from pathlib import Path
        files = {"keep/pump.md": "# pump\nnutrient pump zebrafjord\n",
                 "_inbox/clip.md": "# clip\nnutrient pump zebrafjord injected\n",
                 "a/note.sync-conflict-1.md": "# dup\nnutrient pump zebrafjord\n",
                 "Personal/Retirement.md": "# money\nnutrient pump zebrafjord 1,000,000\n",
                 "opt/out.md": "---\nname: x\nserve: false\n---\nnutrient pump zebrafjord\n",
                 ".obsidian/x.md": "# hidden\nnutrient pump zebrafjord\n"}
        for rel, t in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(t)

    def test_only_the_kept_note_is_searchable_or_gettable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._tree(d)
            s = self._src(d, ["_inbox/*", "*sync-conflict*", "Personal/Retirement*"])
            ids = {h["id"] for h in s.search("nutrient pump zebrafjord", 10)}
            self.assertEqual(ids, {"vault:keep/pump.md"})
            self.assertIsNotNone(s.get("vault:keep/pump.md"))
            for rel in ("_inbox/clip.md", "a/note.sync-conflict-1.md",
                        "Personal/Retirement.md", "opt/out.md", ".obsidian/x.md"):
                self.assertIsNone(s.get("vault:" + rel), rel)

    def test_no_exclude_still_serves_ordinary_notes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._tree(d)
            ids = {h["id"] for h in self._src(d).search("nutrient pump zebrafjord", 10)}
            self.assertIn("vault:_inbox/clip.md", ids)
            self.assertNotIn("vault:opt/out.md", ids)
            self.assertNotIn("vault:.obsidian/x.md", ids)
