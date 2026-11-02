"""Regressions from the 1.1 fresh-eyes review (F-numbers refer to that review)."""
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

from agres import config, db, fold, hooks, install, memory, pack
from agres.code import engine
from tests.helpers import TempHome
from tests.test_hooks import HookTest
from tests.test_index import IndexCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = "2020-01-01T00:00:00Z"


def cli(home, cwd, *args, stdin=None):
    env = dict(os.environ, PYTHONPATH=REPO, HOME=home)
    return subprocess.run([sys.executable, "-m", "agres", *args], cwd=cwd, env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


class GcAndForgetTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("w", "p")
        with db.Store(self.root, create=True) as s:
            memory.add_turn(s, "s1", "user", "fresh turn")
            fold.add_fold(s, "tool", "t", "x", body="tool output of s1", session_id="s1")
            fold.add_fold(s, "agent", "a", "y", body="agent result of s1", session_id="s1")
            fold.add_fold(s, "tool", "t", "x", body="tool output of s2", session_id="s2")

    def test_f2_gc_rejects_non_positive_days(self):
        for n in ("-5", "0"):
            p = cli(self.home, self.root, "gc", "--turns-older-than", n)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        with db.Store(self.root) as s:
            self.assertEqual(s.one("SELECT COUNT(*) FROM turns")[0], 1)

    def test_f9_forget_session_also_drops_its_raw_folds(self):
        out = cli(self.home, self.root, "forget", "--session", "s1").stdout
        self.assertIn("2 fold", out)
        with db.Store(self.root) as s:
            self.assertEqual([r[0] for r in s.q("SELECT session_id FROM folds")], ["s2"])

    def test_f19_unknown_session_fails(self):
        p = cli(self.home, self.root, "forget", "--session", "nope")
        self.assertEqual(p.returncode, 1)
        self.assertIn("nothing", p.stderr)

    def test_f25_fold_summary_text_starting_with_keep_is_recorded(self):
        with db.Store(self.root) as s:
            t = memory.add_turn(s, "s1", "user", "another turn")
        p = cli(self.home, self.root, "fold", "--keep going, see notes", "--turns", f"M{t}-M{t}")
        self.assertTrue(p.stdout.startswith("F"), p.stdout + p.stderr)


class InstallGuardTest(TempHome):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w") as fh:
            json.dump(install.with_agres_hooks({}, "agres"), fh)

    def test_f1_install_plugin_refuses_without_enabled_plugin(self):
        p = cli(self.home, self.home, "install", "--plugin", "--yes")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("not enabled", p.stdout + p.stderr)
        with open(self.settings) as fh:
            self.assertIn("agres hook session-start", fh.read())

    def test_f15_uninstall_warns_when_plugin_still_enabled(self):
        with open(self.settings) as fh:
            s = json.load(fh)
        s["enabledPlugins"] = {"agres@agres": True}
        with open(self.settings, "w") as fh:
            json.dump(s, fh)
        cli(self.home, self.home, "install", "--plugin", "--yes")
        out = cli(self.home, self.home, "uninstall").stdout
        self.assertIn("plugin agres@agres is still enabled", out)


class RetentionRobustnessTest(HookTest):
    def test_f4_future_last_retention_does_not_block_forever(self):
        with db.Store(self.root, create=True) as s:
            memory.add_turn(s, "old", "user", "ancient", created_at=OLD)
            s.set_meta("last_retention", "2099-01-01T00:00:00Z")
        config.save_project(self.root, {"turn_retention_days": 30})
        self.run_hook("session-end", reason="exit")
        self.assertEqual(self.turns(), [])

    def test_f5_retention_failure_still_closes_session_and_writes_handoff(self):
        self.run_hook("prompt", prompt="do the important migration work now")
        config.save_project(self.root, {"turn_retention_days": 30})
        with mock.patch.object(memory, "delete_turns", side_effect=RuntimeError("locked")):
            self.run_hook("session-end", reason="exit")
        with self.store() as s:
            self.assertTrue(s.one("SELECT ended_at FROM sessions WHERE id='sess-1'")[0])
            self.assertTrue(s.one("SELECT 1 FROM items WHERE kind='handoff' AND source='auto'"))

    def test_f5_huge_retention_value_is_clamped(self):
        self.assertEqual(config.coerce("turn_retention_days", str(10 ** 9)), 36500)


class SpanResumeTest(HookTest):
    def test_f11_resumed_session_does_not_refold_old_turns(self):
        self.run_hook("prompt", prompt="first piece of work on the tunnel")
        self.run_hook("post-compact", compact_summary="Goal: first.")
        self.run_hook("session-end", reason="exit")
        self.run_hook("prompt", prompt="second piece of work after resume")
        self.run_hook("post-compact", compact_summary="Goal: second.")
        with self.store() as s:
            spans = [json.loads(r[0])["turns"] for r in s.q("SELECT meta FROM folds WHERE kind='span' ORDER BY id")]
        self.assertEqual(len(spans), 2)
        self.assertLess(spans[0][1], spans[1][0])


class FoldPreviewTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("w", "f")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_f6_long_lines_are_bounded(self):
        for text in ("x" * 500_000, "\n".join(["y" * 5000] * 60), "\n".join(["error " + "z" * 3000] * 200)):
            self.assertLessEqual(len(fold.preview(text)), fold.PREVIEW_CHARS + 200)

    def test_f7_unfold_shows_a_cut_long_line(self):
        fid = fold.add_fold(self.s, "tool", "t", "x", body="q" * 20_000 + "\nsecond")
        out = fold.unfold(self.s, fid)
        self.assertIn("qqqq", out)
        self.assertIn("line cut", out)

    def test_f8_line_numbers_follow_newlines_only(self):
        fid = fold.add_fold(self.s, "tool", "t", "x", body="a\r\nb\x0cstill b\r\nc\n")
        self.assertEqual(fold.unfold(self.s, fid, lines=(2, 2)), "b\x0cstill b\r")
        self.assertEqual(fold.unfold(self.s, fid, all_=True), "a\r\nb\x0cstill b\r\nc\n")

    def test_f13_grep_is_literal_and_fast(self):
        fid = fold.add_fold(self.s, "tool", "t", "x", body="a" * 40 + "!\nerror|FAILED here\nFAILED x\n")
        t = time.perf_counter()
        self.assertIn("no line matches", fold.unfold(self.s, fid, grep="(a+)+$"))
        self.assertLess(time.perf_counter() - t, 1)
        self.assertIn("2: error|FAILED here", fold.unfold(self.s, fid, grep="error|FAILED"))
        both = fold.unfold(self.s, fid, grep=["error", "FAILED"])
        self.assertIn("3: FAILED x", both)
        self.assertIn("3: FAILED x", fold.unfold(self.s, fid, regex="^FAILED"))

    def test_f20_partly_pruned_span_says_so(self):
        a = memory.add_turn(self.s, "s", "user", "one")
        b = memory.add_turn(self.s, "s", "user", "two")
        fid = fold.add_fold(self.s, "span", "c", "sum", meta={"turns": [a, b], "session": "s"})
        memory.delete_turns(self.s, [a])
        self.assertIn("1 of 2 turns still stored", fold.unfold(self.s, fid, all_=True))

    def test_r19_agent_fold_is_tagged_in_pack(self):
        memory.add_item(self.s, "pin", "a pin")
        fold.add_fold(self.s, "agent", "subagent: research", "Web says do X", body="b")
        self.assertIn("[subagent]", pack.build(self.s, git=False))


class AgresCommandDetectionTest(HookTest):
    def bash(self, cmd):
        resp = {"stdout": ("line\n" * 4000), "stderr": ""}
        return self.run_hook("fold", tool_name="Bash", tool_input={"command": cmd}, tool_response=resp)

    def test_f12_paths_and_words_named_agres_still_fold(self):
        for cmd in ("grep agres file.txt", "cd ~/Dev/agres && make test", "ls /x/agres"):
            self.assertTrue(self.bash(cmd), cmd)

    def test_f12_agres_in_command_position_never_folds(self):
        for cmd in ('bash -c "agres unfold F1"', "x=`agres show F1`", "$(agres unfold F1)", "agres unfold F1",
                    "cd x && agres show F2", "/home/u/.local/bin/agres search q", "python3 -m agres unfold F1",
                    "~/Dev/agres/bin/agres unfold F3 --all"):
            self.assertEqual(self.bash(cmd), "", cmd)


class FastPathTest(TempHome):
    def run_main(self, stdin, env=None):
        e = dict(os.environ, PYTHONPATH=REPO, HOME=self.home, **(env or {}))
        data = stdin if isinstance(stdin, bytes) else stdin.encode()
        p = subprocess.run([sys.executable, "-m", "agres", "hook", "fold"], input=data, env=e,
                           capture_output=True, timeout=30)
        p.stdout, p.stderr = p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
        return p

    def test_f24_garbage_stdin_exits_zero(self):
        for raw in ("[" * 100_000, b"\xff\xfe not utf8", '{"tool_response": 5}'):
            p = self.run_main(raw)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "")

    def test_f14_fast_path_uses_configured_threshold(self):
        from agres import __main__ as m  # noqa: F401  (import must not run the CLI)
        self.assertEqual(m.fast_threshold({"CLAUDE_PLUGIN_OPTION_FOLD_CHARS": "20000"}, None), 20000)
        self.assertEqual(m.fast_threshold({"CLAUDE_PLUGIN_OPTION_FOLD": "false"}, None), None)
        self.assertEqual(m.fast_threshold({}, None), 12000)


class MonorepoReconcileTest(IndexCase):
    def test_f3_reconcile_works_when_store_root_is_a_subdir(self):
        repo = self.project("mono", git=True)
        pkg = os.path.join(repo, "pkg")
        self.put(repo, {"pkg/a.py": "def alpha():\n    return 1\n", "other/z.py": "def z():\n    pass\n"})
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "init")
        s = self.store(pkg)
        engine.sync(s, use_git=False)
        p = os.path.join(pkg, "a.py")
        self.write(pkg, "a.py", "def alpha_two():\n    return 2\n")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        self.write(repo, "other/z.py", "def z2():\n    pass\n")  # outside the store root: ignored
        self.assertEqual(engine.reconcile(s), 1)
