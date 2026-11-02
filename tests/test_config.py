"""Settings: plugin options from env, retention, extra redaction (audit #4, #6; spec R3, R8, R9)."""
import os
from unittest import mock

from agres import config, db, memory, util
from tests.helpers import TempHome
from tests.test_hooks import HookTest

OLD = "2020-01-01T00:00:00Z"


class PluginOptionTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "p")

    def test_plugin_option_env_overrides_global_file_but_not_project(self):
        config.save_global({"capture": "none", "budget_chars": 5000})
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_CAPTURE": "prompts",
                                          "CLAUDE_PLUGIN_OPTION_TURN_RETENTION_DAYS": "30",
                                          "CLAUDE_PLUGIN_OPTION_FOLD": "false"}):
            cfg = config.load(self.root)
            self.assertEqual(cfg["capture"], "prompts")
            self.assertEqual(cfg["turn_retention_days"], 30)
            self.assertIs(cfg["fold"], False)
            self.assertEqual(cfg["budget_chars"], 5000)
            config.save_project(self.root, {"capture": "all"})
            self.assertEqual(config.load(self.root)["capture"], "all")

    def test_bad_or_empty_plugin_option_is_ignored(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_CAPTURE": "everything",
                                          "CLAUDE_PLUGIN_OPTION_BUDGET_CHARS": "",
                                          "CLAUDE_PLUGIN_OPTION_TURN_RETENTION_DAYS": "lots"}):
            cfg = config.load(self.root)
        self.assertEqual(cfg["capture"], "all")
        self.assertEqual(cfg["budget_chars"], config.DEFAULTS["budget_chars"])
        self.assertEqual(cfg["turn_retention_days"], 0)

    def test_new_defaults(self):
        cfg = config.load(self.root)
        self.assertEqual(cfg["turn_retention_days"], 0)
        self.assertEqual(cfg["redact_patterns"], [])
        self.assertIs(cfg["fold"], True)
        self.assertEqual(cfg["fold_chars"], 12000)

    def test_coerce_new_keys(self):
        self.assertEqual(config.coerce("turn_retention_days", "14"), 14)
        self.assertEqual(config.coerce("redact_patterns", r"ACME-\d{6}, x[0-9]+"), [r"ACME-\d{6}", "x[0-9]+"])
        with self.assertRaises(ValueError):
            config.coerce("redact_patterns", "([unclosed")
        with self.assertRaises(ValueError):
            config.coerce("turn_retention_days", "-3")


class ExtraRedactionTest(TempHome):
    def tearDown(self):
        util.set_extra_patterns([])
        super().tearDown()

    def test_extra_patterns_apply_after_builtins(self):
        bad = util.set_extra_patterns([r"ACME-\d{6}", "([bad"])
        self.assertEqual(bad, ["([bad"])
        self.assertEqual(util.redact("ticket ACME-123456 ok"), "ticket [redacted] ok")

    def test_loading_config_installs_patterns(self):
        root = self.git_repo("work", "q")
        config.save_project(root, {"redact_patterns": [r"ACME-\d{6}"]})
        config.load(root)
        self.assertIn("[redacted]", util.redact("ACME-654321"))


class RetentionHookTest(HookTest):
    def seed(self):
        with db.Store(self.root, create=True) as s:
            memory.add_turn(s, "old", "user", "ancient prompt", created_at=OLD)
            memory.add_turn(s, "sess-1", "user", "fresh prompt")
            memory.add_item(s, "decision", "Old decision stays forever", created_at=OLD)

    def test_retention_off_by_default_keeps_everything(self):
        self.seed()
        self.run_hook("session-end", reason="exit")
        self.assertEqual(len(self.turns()), 2)

    def test_retention_prunes_old_turns_once_a_day_and_never_items(self):
        self.seed()
        config.save_project(self.root, {"turn_retention_days": 30})
        self.run_hook("session-end", reason="exit")
        self.assertEqual([t for _, t in self.turns()], ["fresh prompt"])
        with self.store() as s:
            self.assertEqual(s.one("SELECT COUNT(*) FROM items WHERE kind='decision'")[0], 1)
            self.assertTrue(s.get_meta("last_retention"))
            memory.add_turn(s, "old2", "user", "another ancient one", created_at=OLD)
        self.run_hook("session-end", reason="exit")  # same day: no second prune
        self.assertIn("another ancient one", [t for _, t in self.turns()])
