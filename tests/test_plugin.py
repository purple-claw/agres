"""The repo is a Claude Code plugin + its own marketplace (spec R1–R3)."""
import json
import os
import unittest

from agres import __version__, config, install

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_CMD = '"${CLAUDE_PLUGIN_ROOT}/bin/agres"'


def load(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return json.load(fh)


class PluginLayoutTest(unittest.TestCase):
    def test_manifest(self):
        m = load(".claude-plugin", "plugin.json")
        self.assertEqual(m["name"], "agres")
        self.assertEqual(m["version"], __version__)
        for key in config.PLUGIN_OPTIONS:
            self.assertIn(key, m["userConfig"], key)
            opt = m["userConfig"][key]
            self.assertEqual(opt["default"], config.DEFAULTS[key], key)
        self.assertEqual(m["userConfig"]["capture"]["options"], ["all", "prompts", "none"])

    def test_marketplace_points_at_repo_root(self):
        mk = load(".claude-plugin", "marketplace.json")
        entry = [p for p in mk["plugins"] if p["name"] == "agres"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["source"], "./")

    def test_hooks_json_matches_installer(self):
        self.assertEqual(load("hooks", "hooks.json")["hooks"], install.hook_entries(PLUGIN_CMD))

    def test_fold_hook_is_sync_and_others_that_write_late_are_async(self):
        hooks = load("hooks", "hooks.json")["hooks"]
        by_cmd = {h["command"].split(" hook ")[1]: h for gs in hooks.values() for g in gs for h in g["hooks"]}
        self.assertNotIn("async", by_cmd["fold"])
        self.assertNotIn("async", by_cmd["session-start"])
        for ev in ("stop", "edit", "agent-fold", "post-compact"):
            self.assertTrue(by_cmd[ev].get("async"), ev)

    def test_bin_holds_only_the_cli(self):
        self.assertEqual(sorted(os.listdir(os.path.join(REPO, "bin"))), ["agres"])
        self.assertTrue(os.access(os.path.join(REPO, "bin", "agres"), os.X_OK))

    def test_skill_is_where_the_manifest_says(self):
        m = load(".claude-plugin", "plugin.json")
        root = m.get("skills", "./skills/")
        root = root[0] if isinstance(root, list) else root
        self.assertTrue(os.path.isfile(os.path.join(REPO, root, "agres", "SKILL.md")))
