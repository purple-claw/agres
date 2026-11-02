"""Regressions from the opencode free-model QA runs (turns + hooks sandboxes)."""
import io
import json
import os
import subprocess
import sys
from unittest import mock

from agres import config, db, hooks, memory, paths, util
from tests.helpers import TempHome
from tests.test_hooks import HookTest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Cli(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("w", "p")

    def agres(self, *args, cwd=None):
        env = dict(os.environ, PYTHONPATH=REPO, HOME=self.home)
        env.pop("AGRES_ROOT", None)
        return subprocess.run([sys.executable, "-m", "agres", *args], cwd=cwd or self.root, env=env,
                              capture_output=True, text=True, timeout=60)

    def items(self):
        with db.Store(self.root) as s:
            return [tuple(r) for r in s.q("SELECT id, kind, status, text FROM items ORDER BY id")]


class IdPrefixTest(Cli):
    def setUp(self):
        super().setUp()
        self.agres("decide", "keep the decision")
        self.agres("pin", "keep the pin")
        self.agres("task", "keep the task")

    def test_forget_with_wrong_prefix_is_refused(self):
        p = self.agres("forget", "T1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no item T1", p.stderr)
        self.assertEqual(self.items()[0], (1, "decision", "accepted", "keep the decision"))

    def test_drop_and_done_with_wrong_prefix_are_refused(self):
        for verb, ref in (("drop", "P3"), ("done", "E3")):
            p = self.agres(verb, ref)
            self.assertEqual(p.returncode, 1, ref)
        self.assertEqual(self.items()[2][2], "open")

    def test_bare_numbers_still_work(self):
        self.assertEqual(self.agres("drop", "2").returncode, 0)
        self.assertEqual(self.items()[1][2], "retired")

    def test_forget_keeps_a_closed_status(self):
        self.agres("done", "T3")
        self.agres("forget", "T3")
        self.assertEqual(self.items()[2][2:], ("done", "[forgotten]"))

    def test_forget_dry_run_dedupes(self):
        with db.Store(self.root) as s:
            t = memory.add_turn(s, "s", "user", "hello there")
        out = self.agres("forget", f"M{t}", f"M{t}", "--dry-run").stdout
        self.assertIn("would delete 1 turn", out)


class CliOddsTest(Cli):
    def test_init_honours_dash_c(self):
        target = self.git_repo("w", "other")
        self.agres("init", "-C", target)
        self.assertTrue(os.path.isfile(paths.db_path(target)))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".agres")))

    def test_project_config_and_off_write_gitignore(self):
        self.agres("config", "guard", "off", "--project")
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agres", ".gitignore")))
        other = self.git_repo("w", "q")
        self.agres("off", cwd=other)
        self.assertTrue(os.path.isfile(os.path.join(other, ".agres", ".gitignore")))

    def test_unknown_config_key_is_an_error(self):
        p = self.agres("config", "nope")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown setting", p.stderr)

    def test_search_kind_plurals_and_unknown(self):
        with db.Store(self.root, create=True) as s:
            memory.add_turn(s, "s", "user", "the mosquitto broker restarts")
        self.assertIn("M1", self.agres("search", "mosquitto", "--kind", "turns").stdout)
        p = self.agres("search", "mosquitto", "--kind", "bogus")
        self.assertEqual(p.returncode, 1)


class RedactionTest(TempHome):
    def tearDown(self):
        util.set_extra_patterns([])
        super().tearDown()

    def test_string_redact_patterns_in_file_is_one_pattern(self):
        root = self.git_repo("w", "r")
        os.makedirs(os.path.join(root, ".agres"))
        with open(os.path.join(root, ".agres", "config.json"), "w") as fh:
            json.dump({"redact_patterns": "abc"}, fh)
        config.load(root)
        self.assertEqual(util.redact("abc and banana"), "[redacted] and banana")

    def test_nested_private_spans_do_not_leak(self):
        out = util.redact("a <private>one <private>two</private> LEAKME</private> b")
        self.assertNotIn("LEAKME", out)
        self.assertEqual(out, "a [private] b")
        self.assertEqual(util.redact("x <private>open to end"), "x [private]")


class RootGuardTest(TempHome):
    def test_agres_root_cannot_be_home_or_slash(self):
        for bad in (self.home, "/"):
            with mock.patch.dict(os.environ, {"AGRES_ROOT": bad}):
                self.assertIsNone(paths.find_root(self.home))


class HookTypesTest(HookTest):
    def test_wrong_types_are_ignored_not_logged_as_errors(self):
        for event, payload in (("prompt", {"prompt": ["hello"]}), ("edit", {"tool_name": "Edit", "tool_input": 42}),
                               ("post-compact", {"compact_summary": 42}), ("stop", {"last_assistant_message": 7}),
                               ("fold", {"tool_name": "Bash", "tool_input": "x", "tool_response": {"stdout": 5}}),
                               ("agent-fold", {"tool_name": "Agent", "tool_input": [], "tool_response": 3})):
            self.run_hook("session-start", source="startup")
            self.assertEqual(self.run_hook(event, **payload), "", event)
        log = os.path.join(self.root, ".agres", "hook.log")
        self.assertFalse(os.path.exists(log) and open(log).read().strip(), open(log).read() if os.path.exists(log) else "")
