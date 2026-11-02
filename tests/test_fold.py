"""Origami: fold store (schema 101), tool folds in the live window, exact unfold (spec R14–R18)."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import time

from agres import config, db, fold, memory
from tests.helpers import TempHome
from tests.test_hooks import HookTest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def big_output(n=3000):
    lines = [f"line {i:05d} compiling module_{i}" for i in range(1, n + 1)]
    lines[1500] = "src/knx.c:212: error: 'tunnel' undeclared"
    lines[2200] = "Traceback (most recent call last):"
    return "\n".join(lines)


class SchemaTest(TempHome):
    def test_new_store_is_101_with_folds(self):
        root = self.git_repo("w", "a")
        with db.Store(root, create=True) as s:
            self.assertEqual(s.one("PRAGMA user_version")[0], 101)
            self.assertTrue(s.one("SELECT 1 FROM sqlite_master WHERE name='folds'"))

    def test_100_store_upgrades_in_place_and_keeps_data(self):
        root = self.git_repo("w", "b")
        with db.Store(root, create=True) as s:
            memory.add_item(s, "pin", "keep me through the upgrade")
            s.x("DROP TABLE folds")
            s.x("DROP TABLE fts_folds")
            s.x("PRAGMA user_version=100")
        with db.Store(root) as s:
            self.assertEqual(s.one("PRAGMA user_version")[0], 101)
            self.assertEqual(s.one("SELECT text FROM items")[0], "keep me through the upgrade")
            fid = fold.add_fold(s, "manual", "t", "summary words", body="exact body")
            self.assertEqual(fold.get(s, fid)["body"], "exact body")


class FoldCoreTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("w", "p")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_preview_keeps_head_tail_and_salient_lines_verbatim(self):
        text = big_output()
        prev = fold.preview(text, head=40, tail=20)
        self.assertIn("line 00001 compiling module_1", prev)
        self.assertIn("line 00040 compiling", prev)
        self.assertNotIn("line 00041 compiling", prev)
        self.assertIn("line 03000 compiling", prev)
        self.assertIn("src/knx.c:212: error: 'tunnel' undeclared", prev)
        self.assertIn("Traceback (most recent call last):", prev)
        self.assertLess(len(prev), len(text) // 10)

    def test_unfold_is_exact_and_bounded(self):
        text = big_output()
        fid = fold.add_fold(self.s, "tool", "seq", "x", body=text)
        self.assertEqual(fold.unfold(self.s, fid, all_=True), text)
        part = fold.unfold(self.s, fid)
        self.assertLessEqual(len(part), fold.UNFOLD_CHARS + 200)
        self.assertIn("showing lines 1–", part)

    def test_unfold_lines_and_grep(self):
        fid = fold.add_fold(self.s, "tool", "seq", "x", body=big_output())
        out = fold.unfold(self.s, fid, lines=(10, 12))
        self.assertIn("line 00010", out)
        self.assertIn("line 00012", out)
        self.assertNotIn("line 00013", out)
        g = fold.unfold(self.s, fid, grep="error", context=1)
        self.assertIn("1501: src/knx.c:212: error", g)
        self.assertIn("1500: line 01500", g)
        self.assertNotIn("line 00010", g)

    def test_fold_body_is_redacted(self):
        fid = fold.add_fold(self.s, "tool", "env", "x", body="token ghp_" + "a" * 36 + " end")
        self.assertIn("[redacted]", fold.get(self.s, fid)["body"])

    def test_span_fold_unfolds_exact_turns(self):
        a = memory.add_turn(self.s, "s1", "user", "first exact prompt")
        b = memory.add_turn(self.s, "s1", "assistant", "first exact reply")
        fid = fold.add_fold(self.s, "span", "compaction", "Goal: x", meta={"turns": [a, b]})
        out = fold.unfold(self.s, fid, all_=True)
        self.assertIn("first exact prompt", out)
        self.assertIn("first exact reply", out)
        self.assertIn(f"M{a}", out)

    def test_delete_raw_fold(self):
        fid = fold.add_fold(self.s, "tool", "seq", "x", body="abc")
        fold.delete(self.s, fid)
        self.assertIsNone(fold.get(self.s, fid))


class ToolFoldHookTest(HookTest):
    def bash(self, stdout, command="make all", stderr="", **cfg):
        if cfg:
            config.save_project(self.root, cfg)
        resp = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False,
                "noOutputExpected": False}
        return self.run_hook("fold", hook_event_name="PostToolUse", tool_name="Bash",
                             tool_input={"command": command}, tool_response=resp)

    def test_small_output_passes_through(self):
        self.assertEqual(self.bash("ok\n"), "")

    def test_big_output_is_replaced_with_preview_and_stub(self):
        text = big_output()
        raw = self.bash(text)
        out = json.loads(raw)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        new = out["updatedToolOutput"]
        self.assertEqual(set(new), {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"})
        self.assertIn("[agres fold F1:", new["stdout"])
        self.assertIn("agres unfold F1", new["stdout"])
        self.assertIn("src/knx.c:212: error", new["stdout"])
        self.assertLess(len(new["stdout"]), 6000)
        with self.store() as s:
            self.assertEqual(fold.get(s, 1)["body"], text)
            self.assertEqual(json.loads(fold.get(s, 1)["meta"])["command"], "make all")

    def test_agres_output_is_never_refolded(self):
        for cmd in ("agres unfold F1 --all", "cd x && agres show F2", "/home/u/.local/bin/agres search q"):
            self.assertEqual(self.bash(big_output(), command=cmd), "", cmd)

    def test_fold_off_and_threshold(self):
        self.assertEqual(self.bash(big_output(), fold=False), "")
        self.assertEqual(self.bash("x" * 5000, fold=True, fold_chars=20000), "")
        self.assertTrue(self.bash("y\n" * 3000, fold_chars=1000))

    def test_interrupted_image_and_other_tools_pass(self):
        resp = {"stdout": big_output(), "stderr": "", "interrupted": True}
        self.assertEqual(self.run_hook("fold", tool_name="Bash", tool_input={"command": "x"},
                                       tool_response=resp), "")
        self.assertEqual(self.run_hook("fold", tool_name="Read", tool_input={"file_path": "/a"},
                                       tool_response={"file": {"content": big_output()}}), "")

    def test_off_project_is_untouched(self):
        config.set_off(self.root, True)
        self.assertEqual(self.bash(big_output()), "")

    def test_fold_hook_latency_on_1mb(self):
        text = ("x" * 99 + "\n") * 10_000
        self.bash("warm")
        t = time.perf_counter()
        self.assertTrue(self.bash(text))
        self.assertLess(time.perf_counter() - t, 0.5)


class FoldCliTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("w", "c")
        with db.Store(self.root, create=True) as s:
            self.fid = fold.add_fold(s, "tool", "make all", "x", body=big_output())
            self.t1 = memory.add_turn(s, "s1", "user", "please refactor the knx tunnel")
            self.t2 = memory.add_turn(s, "s1", "assistant", "refactored, tests pass")

    def agres(self, *args, ok=True, stdin=None):
        env = dict(os.environ, PYTHONPATH=REPO, HOME=self.home)
        p = subprocess.run([sys.executable, "-m", "agres", *args], cwd=self.root, env=env, input=stdin,
                           capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, f"{args}\n{p.stdout}\n{p.stderr}")
        return p

    def test_unfold_cli(self):
        self.assertIn("line 00011", self.agres("unfold", f"F{self.fid}", "--lines", "11-11").stdout)
        self.assertIn("error", self.agres("unfold", f"F{self.fid}", "--grep", "undeclared").stdout)
        self.assertIn("line 00001", self.agres("show", f"F{self.fid}").stdout)

    def test_manual_fold_over_turns_and_stdin(self):
        out = self.agres("fold", "Goal: knx refactor. Done: tunnel split.", "--turns",
                         f"M{self.t1}-M{self.t2}").stdout
        self.assertTrue(out.startswith("F2"), out)
        self.assertIn("refactored, tests pass", self.agres("unfold", "F2", "--all").stdout)
        out = self.agres("fold", "--title", "notes", "summary of notes", "--stdin", stdin="raw body text\n").stdout
        self.assertTrue(out.startswith("F3"), out)
        self.assertIn("raw body text", self.agres("unfold", "F3").stdout)

    def test_forget_fold_and_bad_ids(self):
        self.agres("forget", f"F{self.fid}")
        self.assertIn("no fold", self.agres("unfold", f"F{self.fid}", ok=False).stderr)
        p = self.agres("done", "F2", ok=False)
        self.assertNotIn("Traceback", p.stderr)

    def test_search_finds_folds(self):
        out = self.agres("search", "undeclared", "--no-code").stdout
        self.assertIn(f"F{self.fid}", out)


class SpanAndAgentFoldHookTest(HookTest):
    def test_compaction_folds_the_span_once(self):
        self.run_hook("prompt", prompt="refactor the knx tunnel keepalive")
        self.run_hook("stop", last_assistant_message="done: keepalive is 30s now")
        self.run_hook("post-compact", trigger="auto", compact_summary="Goal: knx keepalive. Done: 30s.")
        self.run_hook("prompt", prompt="now write the rollback test")
        self.run_hook("post-compact", trigger="manual", compact_summary="Goal: rollback test.")
        with self.store() as s:
            folds = s.q("SELECT * FROM folds WHERE kind='span' ORDER BY id")
            self.assertEqual(len(folds), 2)
            first = fold.unfold(s, folds[0]["id"], all_=True)
            self.assertIn("refactor the knx tunnel keepalive", first)
            self.assertIn("keepalive is 30s now", first)
            self.assertNotIn("rollback test", first)
            self.assertIn("rollback test", fold.unfold(s, folds[1]["id"], all_=True))

    def test_subagent_result_becomes_agent_fold(self):
        self.run_hook("session-start", source="startup")
        self.run_hook("agent-fold", tool_name="Agent",
                      tool_input={"description": "Find MQTT auth code", "prompt": "Where is MQTT auth?"},
                      tool_response={"content": [{"type": "text", "text": "Auth lives in admin-api/auth.py:40"}]})
        with self.store() as s:
            f = s.one("SELECT * FROM folds WHERE kind='agent'")
            self.assertIn("Find MQTT auth code", f["title"])
            self.assertIn("admin-api/auth.py:40", fold.unfold(s, f["id"], all_=True))
            self.assertIn("Where is MQTT auth?", f["body"])


class PersistedOutputTest(HookTest):
    """Claude Code keeps outputs over ~30k chars in a file (`persistedOutputPath`) and hands hooks only
    the first 30,000 chars. The fold must hold the full file so unfold stays exact (probe, 2.1.316)."""

    def test_full_persisted_output_is_folded(self):
        full = big_output(6000) + "\nLAST LINE marker"
        path = self.write(self.home, "tool-results/abc.txt", full)
        resp = {"stdout": full[:30000], "stderr": "", "interrupted": False, "isImage": False,
                "noOutputExpected": False, "persistedOutputPath": path, "persistedOutputSize": len(full)}
        raw = self.run_hook("fold", tool_name="Bash", tool_input={"command": "make"}, tool_response=resp)
        new = json.loads(raw)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("LAST LINE marker", new["stdout"])
        self.assertEqual(new["persistedOutputPath"], path)
        with self.store() as s:
            self.assertEqual(fold.unfold(s, 1, all_=True), full)

    def test_unreadable_persisted_path_falls_back_to_given_text(self):
        resp = {"stdout": big_output(3000), "stderr": "", "persistedOutputPath": "/nonexistent/x.txt"}
        raw = self.run_hook("fold", tool_name="Bash", tool_input={"command": "make"}, tool_response=resp)
        self.assertIn("agres fold F1", raw)
