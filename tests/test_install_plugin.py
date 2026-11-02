"""Plugin migration, every skill root, double-wiring detection (audit #1, spec R4, R5)."""
import json
import os

from agres import install
from tests.helpers import TempHome

ORIGAMI_V05 = """---
name: agres
description: "Agres Origami Memory: makes a limited context window behave like a much larger one (fold/unfold)."
default_enabled: true
---
# old
"""


class PluginInstallTest(TempHome):
    def setUp(self):
        super().setUp()
        h = self.home
        self.settings = os.path.join(h, ".claude", "settings.json")
        for d in (".claude/skills", ".codex/skills", ".agent-skills", ".agents/skills", ".local/bin"):
            os.makedirs(os.path.join(h, d), exist_ok=True)
        # today's machine: 1.0 settings hooks + links, plus a stale v0.5 copy in ~/.agents/skills
        os.symlink(install.SKILL_SRC, os.path.join(h, ".codex", "skills", "agres"))
        os.symlink(install.SKILL_SRC, os.path.join(h, ".agent-skills", "agres"))
        os.symlink(install.SKILL_SRC, os.path.join(h, ".claude", "skills", "agres"))
        self.write(h, ".agents/skills/agres/SKILL.md", ORIGAMI_V05)
        os.symlink(install.SHIM_SRC, os.path.join(h, ".local", "bin", "agres"))
        settings = install.with_agres_hooks({"model": "opus", "hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "~/.claude/hooks/mine.sh"}]}]}}, "/x/.local/bin/agres")
        with open(self.settings, "w") as fh:
            json.dump(settings, fh)

    def read_settings(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def enable_plugin(self):
        s = self.read_settings()
        s["enabledPlugins"] = {"agres@agres": True}
        with open(self.settings, "w") as fh:
            json.dump(s, fh)

    # ------------------------------------------------------------- scan
    def test_scan_reports_every_root_with_version(self):
        rows = {r["path"]: r for r in install.scan_skill_roots()}
        stale = rows[os.path.join(self.home, ".agents", "skills", "agres")]
        self.assertEqual(stale["state"], "copy")
        self.assertIn("v0", stale["version"])
        self.assertEqual(rows[os.path.join(self.home, ".codex", "skills", "agres")]["state"], "ours")

    def test_plain_install_now_covers_agents_skills(self):
        descs = [d for _, d, _ in install.build_plan().steps]
        self.assertTrue(any(".agents/skills/agres" in d for d in descs), descs)

    # ----------------------------------------------------------- plugin
    def test_plugin_plan(self):
        plan = install.build_plan(plugin=True)
        kinds = {(k, os.path.relpath(d["path"], self.home)) for k, _, d in plan.steps}
        self.assertIn(("settings", ".claude/settings.json"), kinds)
        self.assertIn(("unlink", ".claude/skills/agres"), kinds)
        self.assertIn(("link", ".agents/skills/agres"), kinds)
        self.assertNotIn(("link", ".codex/skills/agres"), kinds)  # already ours
        self.assertFalse(any(p == ".local/bin/agres" for _, p in kinds))  # shim stays for the terminal

    def test_plugin_apply_and_uninstall(self):
        install.apply(install.build_plan(plugin=True))
        s = self.read_settings()
        cmds = [h["command"] for gs in s.get("hooks", {}).values() for g in gs for h in g["hooks"]]
        self.assertFalse(any("agres hook" in c for c in cmds), cmds)
        self.assertIn("~/.claude/hooks/mine.sh", cmds)
        self.assertEqual(s["model"], "opus")
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".claude", "skills", "agres")))
        agents = os.path.join(self.home, ".agents", "skills", "agres")
        self.assertEqual(os.path.realpath(agents), os.path.realpath(install.SKILL_SRC))
        archived = [f for r, _, fs in os.walk(os.path.join(self.home, ".agres-archive")) for f in fs]
        self.assertIn("SKILL.md", archived)  # the v0.5 copy was moved, not deleted
        install.uninstall()
        s = self.read_settings()
        cmds = [h["command"] for gs in s.get("hooks", {}).values() for g in gs for h in g["hooks"]]
        self.assertTrue(any("agres hook session-start" in c for c in cmds))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "agres")))
        with open(os.path.join(agents, "SKILL.md")) as fh:
            self.assertIn("Origami", fh.read())

    # ----------------------------------------------------------- doctor
    def test_doctor_flags_stale_copy_and_double_wiring(self):
        self.enable_plugin()
        msgs = {m: ok for ok, m in install.doctor()}
        bad = [m for m, ok in msgs.items() if not ok]
        self.assertTrue(any(".agents/skills/agres" in m and "v0" in m for m in bad), bad)
        self.assertTrue(any("double" in m and "settings.json" in m for m in bad), bad)
        self.assertTrue(any("double" in m and ".claude/skills/agres" in m for m in bad), bad)

    def test_doctor_clean_after_plugin_install(self):
        install.apply(install.build_plan(plugin=True))
        self.enable_plugin()
        bad = [m for ok, m in install.doctor() if not ok and "PATH" not in m]
        self.assertEqual(bad, [])
        self.assertTrue(any("plugin agres@agres enabled" in m for ok, m in install.doctor() if ok))
