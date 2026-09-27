#!/usr/bin/env python3
"""learn_harvest / learn_card: only quarantined writes, only through the door;
promotion only through sign.py; filters run before any model sees a line."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GOOD = "Run the backup before any seed update; the rollback point is not a backup."


class _CP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LEARN_STATE": self.tmp.name}, clear=False)
        self.env.start()
        for k in ("LEARN_SOURCES", "LEARN_DENY_FILE", "LEARN_NOTIFY_CMD", "LEARN_CARD_DIR",
                  "LEARN_ROLES", "LEARN_JUDGE"):
            os.environ.pop(k, None)
        import learn_harvest
        import learn_card
        self.h = importlib.reload(learn_harvest)
        self.c = importlib.reload(learn_card)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class TestFilter(Base):
    def test_only_the_operators_clean_long_lines_survive(self):
        kept = self.h.code_filter([
            "user\tok",
            "grok\t" + GOOD + " (the model's own reply)",
            "user\tsk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF is the key for it",
            "user\tthe verification code for the bank login is 123456, keep it handy",
            "user\t" + GOOD,
        ])
        self.assertEqual(kept, [GOOD])

    def test_plain_lines_are_the_operator(self):
        self.assertEqual(self.h.code_filter([GOOD]), [GOOD])

    def test_seen_and_rejected_never_return(self):
        self.assertEqual(self.h.code_filter([GOOD]), [GOOD])
        self.assertEqual(self.h.code_filter([GOOD]), [])
        other = GOOD + " Also: check the receipt."
        self.h.remember_reject(other, "known")
        self.assertEqual(self.h.code_filter([other]), [])

    def test_operator_deny_file_applies(self):
        Path(self.tmp.name, "deny-patterns.txt").write_text("# clients\nacme\\s+corp\n")
        self.assertEqual(self.h.code_filter(["user\tThe Acme  Corp rollout needs " + GOOD]), [])

    def test_a_broken_deny_pattern_denies_everything(self):
        Path(self.tmp.name, "deny-patterns.txt").write_text("(unclosed\n")
        self.assertEqual(self.h.code_filter([GOOD]), [])


class TestWrite(Base):
    def test_every_write_is_untrusted_and_goes_through_the_door(self):
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return _CP(0, "wrote x\nmesh: event emitted (0123456789abcdef)\nQUARANTINED")

        with mock.patch.object(self.h.subprocess, "run", side_effect=fake_run):
            eid = self.h.write_untrusted({"hook": "h" * 10, "body": "b", "slug": "harvest-x"})
        self.assertEqual(eid, "0123456789abcdef")
        cmd = seen[0]
        self.assertEqual(Path(cmd[1]).name, "memory_write.py")
        self.assertEqual(cmd[cmd.index("--lineage") + 1], "contains-untrusted")
        self.assertNotIn("emit.py", " ".join(cmd))

    def test_a_door_refusal_is_reported_not_swallowed(self):
        with mock.patch.object(self.h.subprocess, "run",
                               return_value=_CP(1, "", "error: refusing a `contains-untrusted` memory")):
            with self.assertRaises(RuntimeError) as e:
                self.h.write_untrusted({"hook": "h", "body": "b", "slug": "harvest-x"})
        self.assertIn("refusing", str(e.exception))

    def test_no_code_path_writes_a_trusted_lineage(self):
        for name in ("learn_harvest.py", "learn_card.py"):
            src = (HERE / name).read_text()
            for lit in ('"craig-direct"', '"operator-direct"', "'craig-direct'", "'operator-direct'"):
                self.assertNotIn(lit, src, "%s carries %s" % (name, lit))


class TestNightly(Base):
    def test_card_carries_event_ids_and_the_inbox_empties(self):
        src = Path(self.tmp.name, "thread.jsonl")
        src.write_text("user\t" + GOOD + "\n")
        os.environ["LEARN_SOURCES"] = str(src)
        cands = [{"hook": "Back up before a seed update.", "body": GOOD, "slug": "harvest-backup"}]
        with mock.patch.object(self.h, "judge", return_value=cands), \
             mock.patch.object(self.h.subprocess, "run",
                               return_value=_CP(0, "mesh: event emitted (aaaabbbbccccdddd)")):
            self.assertEqual(self.h.nightly(send=False), 0)
        card = json.loads(self.h.CARD.read_text())
        self.assertEqual(card["items"][0]["id"], "aaaabbbbccccdddd")
        self.assertEqual(self.h.INBOX.read_text(), "")

    def test_judge_candidates_are_bounded_and_scrubbed(self):
        raw = json.dumps({"candidates": [
            {"hook": "one", "body": "b1", "slug": "a/b"},
            {"hook": "token", "body": "use sk-abcdefghijklmnopqrstuvwxyz0123", "slug": "s"},
            {"hook": "two", "body": "b2", "slug": ""},
            {"hook": "three", "body": "b3", "slug": "c"},
            {"hook": "four", "body": "b4", "slug": "d"},
        ]})
        with mock.patch.object(self.h, "_judge_call", return_value=raw):
            out = self.h.judge([GOOD])
        self.assertEqual([c["hook"] for c in out], ["one", "two"])
        self.assertTrue(all(c["slug"].startswith("harvest-") for c in out))

    def test_a_failed_judge_is_a_quiet_night(self):
        with mock.patch.object(self.h, "_judge_call", side_effect=RuntimeError("down")):
            self.assertEqual(self.h.judge([GOOD]), [])


class TestCollect(Base):
    def test_sources_are_read_incrementally(self):
        src = Path(self.tmp.name, "t.jsonl")
        os.environ["LEARN_SOURCES"] = str(src)
        src.write_text("a\nb\n")
        self.assertEqual(self.h.collect(), 2)
        self.assertEqual(self.h.collect(), 0)
        src.write_text("a\nb\nc\n")
        self.assertEqual(self.h.collect(), 1)
        src.write_text("z\n")  # rotated
        self.assertEqual(self.h.collect(), 1)


class TestCard(Base):
    def _card(self, items):
        self.h._write_json(self.h.CARD, {"date": "2026-09-27", "items": items})

    def test_accept_is_the_signed_promotion_and_nothing_else(self):
        self._card([{"hook": "h", "body": "b", "slug": "harvest-x", "id": "feedfacefeedface"}])
        with mock.patch.object(self.c.subprocess, "run", return_value=_CP(0)) as run:
            self.assertEqual(self.c.main(["accept", "1"]), 0)
        cmd = run.call_args[0][0]
        self.assertEqual(Path(cmd[1]).name, "sign.py")
        self.assertEqual(cmd[2:], ["--promote", "feedfacefeedface"])

    def test_accept_without_an_event_id_refuses(self):
        self._card([{"hook": "h", "body": "b", "slug": "harvest-x"}])
        with mock.patch.object(self.c.subprocess, "run") as run:
            self.assertEqual(self.c.main(["accept", "1"]), 2)
        run.assert_not_called()

    def test_reject_fingerprints_the_item(self):
        self._card([{"hook": "a hook", "body": GOOD, "slug": "harvest-x", "id": "1234567890abcdef"}])
        self.assertEqual(self.c.main(["reject", "1", "--reason", "known"]), 0)
        self.assertEqual(self.h.code_filter([GOOD]), [])


if __name__ == "__main__":
    unittest.main()
