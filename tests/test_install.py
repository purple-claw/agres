import json
import os
from unittest import mock

from agres import install
from tests.helpers import TempHome

OLD_CLAUDE_MD = """# Global defaults
- Daily driver: Sonnet.

# Agres — project memory injector + watchdog (default ON)
Two jobs, entire project long: INJECT ... WATCHDOG (capture turns verbatim, `touch` on edit).
- Stop only on: "stop using Agres".

# Other section
keep me
"""


class InstallTest(TempHome):
    """A fake $HOME shaped like the real one before 1.0."""

    def setUp(self):
        super().setUp()
        h = self.home
        self.settings = os.path.join(h, ".claude", "settings.json")
        os.makedirs(os.path.join(h, ".claude", "hooks"))
        os.makedirs(os.path.join(h, ".claude", "commands"))
        os.makedirs(os.path.join(h, ".claude", "skills"))
        os.makedirs(os.path.join(h, ".codex", "skills", "agres"))
        os.makedirs(os.path.join(h, ".agent-skills", "agres", "legacy"))
        os.makedirs(os.path.join(h, ".local", "bin"))
        self.write(h, ".agent-skills/agres/SKILL.md", "old skill")
        self.write(h, ".codex/skills/agres/SKILL.md", "old codex copy")
        os.symlink(os.path.join(h, ".agent-skills", "agres"), os.path.join(h, ".claude", "skills", "agres"))
        self.write(h, ".local/bin/agres", "#!/bin/sh\nexec python3 old_runtime.py \"$@\"\n")
        for name in install.LEGACY_HOOKS:
            self.write(h, f".claude/hooks/{name}", "#!/bin/bash\n")
        self.write(h, ".claude/commands/agres.md", "Use the Agres Origami fold protocol")
        self.write(h, ".claude/CLAUDE.md", OLD_CLAUDE_MD)
        hooks = {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "~/.claude/hooks/format-after-edit.sh"}]},
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "~/.claude/hooks/agres-touch.sh"}]},
            ],
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/session-start.sh"}]},
                {"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/agres-session.sh"}]},
            ],
            "PreCompact": [
                {"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/backup-transcript.sh"}]},
                {"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/agres-checkpoint.sh pre-compact"}]},
            ],
            "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/agres-checkpoint.sh"}]}],
        }
        with open(self.settings, "w") as fh:
            json.dump({"model": "sonnet", "hooks": hooks}, fh)

    def read_settings(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def commands(self, settings):
        return [h["command"] for gs in settings["hooks"].values() for g in gs for h in g["hooks"]]

    def test_dry_run_changes_nothing(self):
        before = self.read_settings()
        plan = install.build_plan()
        kinds = sorted({k for k, _, _ in plan.steps})
        self.assertEqual(kinds, ["archive", "link", "settings", "write"])
        self.assertEqual(self.read_settings(), before)
        self.assertFalse(os.path.islink(os.path.join(self.home, ".local", "bin", "agres")))

    def test_install_rewires_everything_and_keeps_other_hooks(self):
        install.apply(install.build_plan())
        s = self.read_settings()
        cmds = self.commands(s)
        self.assertEqual(s["model"], "sonnet")
        for keep in ("format-after-edit.sh", "session-start.sh", "backup-transcript.sh"):
            self.assertTrue(any(keep in c for c in cmds), keep)
        self.assertFalse(any(x in c for c in cmds for x in install.LEGACY_HOOKS))
        shim = os.path.join(self.home, ".local", "bin", "agres")
        for event in ("session-start", "prompt", "stop", "edit", "post-compact", "session-end"):
            self.assertEqual(cmds.count(f"{shim} hook {event}"), 1, event)
        stop = s["hooks"]["Stop"][0]["hooks"][0]
        self.assertTrue(stop["async"])
        self.assertEqual(os.path.realpath(shim), os.path.realpath(install.SHIM_SRC))
        for link in (".agent-skills/agres", ".claude/skills/agres", ".codex/skills/agres"):
            self.assertEqual(os.path.realpath(os.path.join(self.home, link)), os.path.realpath(install.SKILL_SRC), link)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "commands", "agres.md")))
        with open(os.path.join(self.home, ".claude", "CLAUDE.md")) as fh:
            md = fh.read()
        self.assertIn("never run `agres capture`", md)
        self.assertNotIn("WATCHDOG", md)
        self.assertIn("# Global defaults", md)
        self.assertIn("# Other section\nkeep me", md)
        archived = os.listdir(os.path.join(self.home, ".agres-archive"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(install.build_plan().steps, [])  # idempotent

    def test_uninstall_restores(self):
        original = self.read_settings()
        install.apply(install.build_plan())
        install.uninstall()
        self.assertEqual(self.read_settings(), original)
        with open(os.path.join(self.home, ".local", "bin", "agres")) as fh:
            self.assertIn("old_runtime.py", fh.read())
        with open(os.path.join(self.home, ".agent-skills", "agres", "SKILL.md")) as fh:
            self.assertEqual(fh.read(), "old skill")
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "agres")))
        with open(os.path.join(self.home, ".codex", "skills", "agres", "SKILL.md")) as fh:
            self.assertEqual(fh.read(), "old codex copy")
        self.assertTrue(os.path.exists(os.path.join(self.home, ".claude", "commands", "agres.md")))

    def test_doctor_after_install(self):
        install.apply(install.build_plan())
        bindir = os.path.join(self.home, ".local", "bin")
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
            results = install.doctor(cwd=self.mkdir("loose"))
        failed = [m for ok, m in results if not ok]
        self.assertEqual(failed, [])

    def test_doctor_flags_legacy_wiring(self):
        failed = [m for ok, m in install.doctor(cwd=self.mkdir("loose")) if not ok]
        self.assertTrue(any("legacy" in m for m in failed), failed)
        self.assertTrue(any("hook SessionStart" in m for m in failed), failed)

    def test_claude_md_block_appended_when_missing(self):
        self.assertIn("# Agres — project memory", install.replace_claude_md_block("# Mine\nx\n"))

    def test_skill_frontmatter_is_spec_valid(self):
        ok, msg = install._check_skill(os.path.join(install.SKILL_SRC, "SKILL.md"))
        self.assertTrue(ok, msg)


class InstallSafetyTest(InstallTest):
    def test_uninstall_keeps_changes_made_after_install(self):
        install.apply(install.build_plan())
        s = self.read_settings()
        s["permissions"] = {"allow": ["Bash(npm test)"]}
        s["hooks"].setdefault("Notification", []).append({"hooks": [{"type": "command", "command": "notify.sh"}]})
        with open(self.settings, "w") as fh:
            json.dump(s, fh)
        md = os.path.join(self.home, ".claude", "CLAUDE.md")
        with open(md, "a") as fh:
            fh.write("\n# Added later\nnew rule\n")
        install.uninstall()
        after = self.read_settings()
        self.assertEqual(after["permissions"], {"allow": ["Bash(npm test)"]})
        cmds = self.commands(after)
        self.assertIn("notify.sh", cmds)
        self.assertFalse(any(" hook " in c and "agres" in c for c in cmds))
        self.assertTrue(any("agres-touch.sh" in c for c in cmds))  # legacy wiring back
        with open(md) as fh:
            text = fh.read()
        self.assertIn("# Added later\nnew rule", text)
        self.assertIn("WATCHDOG", text)
        self.assertNotIn("never run `agres capture`", text)

    def test_settings_mode_and_symlink_preserved(self):
        real = os.path.join(self.home, "dotfiles", "settings.json")
        os.makedirs(os.path.dirname(real))
        os.replace(self.settings, real)
        os.symlink(real, self.settings)
        os.chmod(real, 0o600)
        install.apply(install.build_plan())
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        install.uninstall()
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)

    def test_invalid_settings_refused(self):
        with open(self.settings, "w") as fh:
            fh.write("{ not json")
        with self.assertRaises(ValueError):
            install.build_plan()

    def test_half_done_install_can_be_undone(self):
        plan = install.build_plan()
        real_write = install._atomic_write
        with mock.patch.object(install, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install.apply(plan)
        install._atomic_write = real_write
        install.uninstall()
        with open(os.path.join(self.home, ".local", "bin", "agres")) as fh:
            self.assertIn("old_runtime.py", fh.read())
