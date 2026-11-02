"""Status shows capture policy, retention, redaction limits (audit #5/#6, R9), origami and plugin wiring."""
import json
import os

from agres import config, db, fold, status
from tests.helpers import TempHome


class PrivacyStatusTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def render(self):
        return status.render(self.s, color=False)

    def test_privacy_row_defaults(self):
        out = self.render()
        self.assertIn("capture all", out)
        self.assertIn("turns kept forever", out)
        self.assertIn("redaction is best effort", out)
        self.assertIn("agres forget M<id>", out)

    def test_privacy_row_with_retention_and_patterns(self):
        config.save_project(self.root, {"capture": "prompts", "turn_retention_days": 30,
                                        "redact_patterns": ["ACME-\\d+"]})
        out = self.render()
        self.assertIn("capture prompts", out)
        self.assertIn("turns kept 30d", out)
        self.assertIn("+1 custom pattern", out)

    def test_origami_row(self):
        fold.add_fold(self.s, "tool", "make", "x", body="y" * 50_000, meta={"chars": 50_000})
        fold.add_fold(self.s, "span", "compaction", "Goal: z")
        out = self.render()
        self.assertIn("origami", out)
        self.assertIn("fold on", out)
        self.assertIn("2 folds (1 tool, 1 span)", out)
        data = json.loads(status.render(self.s, as_json=True))
        self.assertEqual(data["origami"]["folds"], 2)

    def test_plugin_enabled_counts_as_wired(self):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as fh:
            json.dump({"enabledPlugins": {"agres@agres": True}}, fh)
        self.assertIn("✓ wired (plugin)", self.render())
