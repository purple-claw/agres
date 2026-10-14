import io
import json
import os
import sqlite3
import time
from unittest import mock

from agres import config, db, hooks, memory, paths
from tests.helpers import TempHome


class HookTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.sub = self.mkdir("work", "okas", "packages", "pulsar")

    def run_hook(self, event, **payload):
        payload.setdefault("session_id", "sess-1")
        payload.setdefault("cwd", self.sub)
        out = io.StringIO()
        rc = hooks.main(event, io.StringIO(json.dumps(payload)), out)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def context(self, raw):
        self.assertTrue(raw, "expected hook output")
        data = json.loads(raw)
        return data["hookSpecificOutput"]["additionalContext"]

    def store(self):
        return db.Store(self.root)

    def turns(self, role=None):
        with self.store() as s:
            sql = "SELECT role, text FROM turns" + (" WHERE role=?" if role else "") + " ORDER BY id"
            return [tuple(r) for r in s.q(sql, (role,) if role else ())]


class SessionStartTest(HookTest):
    def test_injects_pack_and_creates_store_at_repo_root(self):
        ctx = self.context(self.run_hook("session-start", source="startup"))
        self.assertTrue(ctx.startswith("# Agres memory — okas"))
        self.assertLess(len(ctx), 10_000)
        self.assertTrue(os.path.isfile(paths.db_path(self.root)))
        self.assertFalse(os.path.exists(os.path.join(self.sub, ".agres")))
        with self.store() as s:
            self.assertEqual(s.one("SELECT agent FROM sessions WHERE id='sess-1'")[0], "claude-code")

    def test_claude_event_names_and_auto_dispatch(self):
        ctx = self.context(self.run_hook("auto", hook_event_name="SessionStart", source="compact"))
        self.assertIn("Agres memory", ctx)
        self.assertIn("Agres memory", self.context(self.run_hook("SessionStart", source="resume")))

    def test_home_and_root_are_silent_and_create_nothing(self):
        for cwd in (self.home, "/", self.mkdir("Downloads")):
            self.assertEqual(self.run_hook("session-start", cwd=cwd, source="startup"), "")
        found = [d for d, _, _ in os.walk(self.home) if d.endswith(".agres")]
        self.assertEqual(found, [])

    def test_auto_init_off_means_no_store(self):
        config.save_global({"auto_init": False})
        self.assertEqual(self.run_hook("session-start", source="startup"), "")
        self.assertFalse(os.path.exists(paths.db_path(self.root)))

    def test_v0_store_gets_migrate_notice_and_is_untouched(self):
        os.makedirs(paths.store_dir(self.root))
        conn = sqlite3.connect(paths.db_path(self.root))
        conn.execute("CREATE TABLE folds(id TEXT)")
        conn.commit()
        conn.close()
        before = os.path.getmtime(paths.db_path(self.root))
        ctx = self.context(self.run_hook("session-start", source="startup"))
        self.assertIn("agres migrate", ctx)
        self.assertEqual(self.run_hook("prompt", prompt="hello"), "")
        self.assertEqual(os.path.getmtime(paths.db_path(self.root)), before)

    def test_shows_existing_memory(self):
        with db.Store(self.root, create=True) as s:
            pid = memory.add_item(s, "pin", "Zero brick tolerance on the dev board", priority=1)
        self.assertIn(f"P{pid} Zero brick", self.context(self.run_hook("session-start", source="startup")))

    def test_latency_with_realistic_store(self):
        with db.Store(self.root, create=True) as s:
            for i in range(80):
                memory.add_item(s, "pin", f"Pinned fact {i} about service {i} and port {9000 + i}")
            for i in range(60):
                memory.add_item(s, "decision", f"Decision {i} for module {i}", status="rejected" if i % 20 == 0 else None)
            for i in range(30):
                memory.add_item(s, "task", f"Task {i}")
        t0 = time.perf_counter()
        for _ in range(5):
            self.run_hook("session-start", source="startup")
        self.assertLess((time.perf_counter() - t0) / 5, 0.3)


class CaptureTest(HookTest):
    def setUp(self):
        super().setUp()
        self.run_hook("session-start", source="startup")

    def test_prompt_and_reply_captured_once(self):
        self.assertEqual(self.run_hook("prompt", prompt="fix the KNX tunnel", prompt_id="p1"), "")
        self.run_hook("prompt", prompt="fix the KNX tunnel", prompt_id="p1")
        for _ in range(3):  # Stop re-fires when another Stop hook blocks
            self.assertEqual(self.run_hook("stop", last_assistant_message="Fixed: keepalive 30s.",
                                           stop_hook_active=True), "")
        self.assertEqual(self.turns(), [("user", "fix the KNX tunnel"), ("assistant", "Fixed: keepalive 30s.")])
        with self.store() as s:
            self.assertEqual(s.one("SELECT prompts FROM sessions WHERE id='sess-1'")[0], 1)

    def test_harness_prompts_are_not_human_turns(self):
        for p in ("<task-notification>\n<task-id>x</task-id>", "<system-reminder>bg</system-reminder>",
                  "  <agent-message from='a'>report</agent-message>", "[SYSTEM NOTIFICATION - NOT USER INPUT]"):
            self.run_hook("prompt", prompt=p)
        self.assertEqual(self.turns(), [])

    def test_private_and_secrets_never_stored(self):
        self.run_hook("prompt", prompt="deploy with <private>pw=hunter2</private> and key AKIAABCDEFGHIJKLMNOP")
        (role, text), = self.turns()
        self.assertNotIn("hunter2", text)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", text)

    def test_capture_prompts_only_setting(self):
        config.save_global({"capture": "prompts"})
        self.run_hook("prompt", prompt="only me")
        self.run_hook("stop", last_assistant_message="not me")
        self.assertEqual(self.turns(), [("user", "only me")])


class OnOffTest(HookTest):
    def test_off_and_on_phrases(self):
        self.run_hook("session-start", source="startup")
        ctx = self.context(self.run_hook("prompt", prompt="Stop using Agres for now"))
        self.assertIn("off", ctx.lower())
        self.assertTrue(config.is_off(self.root))
        self.run_hook("prompt", prompt="this should not be stored")
        self.run_hook("stop", last_assistant_message="nor this")
        self.assertEqual(self.turns(), [])
        off_ctx = self.context(self.run_hook("session-start", source="startup"))
        self.assertIn("off", off_ctx.lower())
        self.assertNotIn("## ", off_ctx)
        on_ctx = self.context(self.run_hook("prompt", prompt="agres on"))
        self.assertIn("on", on_ctx.lower())
        self.assertFalse(config.is_off(self.root))

    def test_phrase_inside_long_prompt_does_not_toggle(self):
        self.run_hook("session-start", source="startup")
        long = "Please document in the README that users can type 'agres off' to pause capture, " * 3
        self.run_hook("prompt", prompt=long)
        self.assertFalse(config.is_off(self.root))


class GuardTest(HookTest):
    def setUp(self):
        super().setUp()
        with db.Store(self.root, create=True) as s:
            self.rej = memory.add_item(s, "decision", "Burn eFuses on a sacrificial board first", status="rejected",
                                       why="only one board; zero brick tolerance")
            self.crit = memory.add_item(s, "pin", "Never flash unsigned images to the field units", priority=1)
            memory.add_item(s, "decision", "Burn-in tests run nightly")  # accepted: never guarded

    def test_rejected_idea_is_flagged_once_per_session(self):
        ctx = self.context(self.run_hook("prompt", prompt="let's burn the eFuses on a sacrificial board to save time"))
        self.assertIn(f"D{self.rej}", ctx)
        self.assertIn("rejected", ctx)
        self.assertLessEqual(len(ctx), 600)
        self.assertEqual(self.run_hook("prompt", prompt="ok burn the eFuses on a sacrificial board now"), "")
        self.assertIn(f"D{self.rej}", self.context(self.run_hook(
            "prompt", session_id="sess-2", prompt="burn eFuses on the sacrificial board")))

    def test_unrelated_prompt_is_quiet(self):
        self.assertEqual(self.run_hook("prompt", prompt="rename the splash screen widget"), "")

    def test_guard_can_be_disabled(self):
        config.save_global({"guard": False})
        self.assertEqual(self.run_hook("prompt", prompt="burn the eFuses on a sacrificial board"), "")


class LifecycleTest(HookTest):
    def setUp(self):
        super().setUp()
        self.run_hook("session-start", source="startup")

    def test_post_compact_summary_becomes_handoff(self):
        self.run_hook("post-compact", trigger="auto", compact_summary="Primary request: runbook stage 2.")
        with self.store() as s:
            h = s.one("SELECT text, source, status FROM items WHERE kind='handoff'")
        self.assertEqual(tuple(h), ("Primary request: runbook stage 2.", "compact", "active"))

    def test_session_end_writes_auto_handoff_when_none(self):
        self.run_hook("prompt", prompt="wire the splash to the auth flow")
        with mock.patch.object(hooks, "_touch_files", return_value=1):
            self.run_hook("edit", tool_name="Edit", tool_input={"file_path": os.path.join(self.root, "app/lib/splash.dart")})
        self.run_hook("session-end", reason="prompt_input_exit")
        with self.store() as s:
            h = s.one("SELECT text, source FROM items WHERE kind='handoff'")
            sess = s.one("SELECT ended_at, end_reason FROM sessions WHERE id='sess-1'")
        self.assertEqual(h["source"], "auto")
        self.assertIn("wire the splash", h["text"])
        self.assertIn("app/lib/splash.dart", h["text"])
        self.assertEqual(sess["end_reason"], "prompt_input_exit")

    def test_session_end_keeps_written_handoff(self):
        with self.store() as s:
            memory.add_handoff(s, "Agent-written state", session_id="sess-1")
        self.run_hook("session-end", reason="other")
        with self.store() as s:
            self.assertEqual(s.one("SELECT COUNT(*) FROM items WHERE kind='handoff'")[0], 1)

    def test_empty_session_writes_nothing(self):
        self.run_hook("session-end", reason="clear")
        with self.store() as s:
            self.assertEqual(s.one("SELECT COUNT(*) FROM items")[0], 0)

    def test_edit_and_bash_touch_files(self):
        with mock.patch.object(hooks, "_touch_files", return_value=1) as touch:
            self.run_hook("edit", tool_name="Write", tool_input={"file_path": os.path.join(self.root, "a.py")})
            self.run_hook("edit", tool_name="NotebookEdit", tool_input={"notebook_path": os.path.join(self.root, "n.ipynb")})
            self.run_hook("edit", tool_name="Bash", tool_input={"command": "sed -i s/a/b/ b.py"},
                          tool_response={"bashEditDiff": [{"filePath": os.path.join(self.root, "b.py")}]})
            self.run_hook("edit", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": "x"})
        touched = [c.args[1] for c in touch.call_args_list]
        self.assertEqual(touched, [[os.path.join(self.root, "a.py")], [os.path.join(self.root, "n.ipynb")],
                                   [os.path.join(self.root, "b.py")]])


class FailOpenTest(HookTest):
    def test_garbage_stdin(self):
        out = io.StringIO()
        self.assertEqual(hooks.main("prompt", io.StringIO("{not json"), out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_unknown_event(self):
        self.assertEqual(self.run_hook("no-such-event"), "")

    def test_handler_crash_is_logged_not_raised(self):
        self.run_hook("session-start", source="startup")
        with mock.patch.object(memory, "add_turn", side_effect=RuntimeError("disk on fire")):
            self.assertEqual(self.run_hook("prompt", prompt="hello"), "")
        with open(os.path.join(paths.store_dir(self.root), "hook.log")) as fh:
            self.assertIn("disk on fire", fh.read())


class ReviewFixesHookTest(HookTest):
    def setUp(self):
        super().setUp()
        self.run_hook("session-start", source="startup")

    def test_phrases_inside_questions_do_not_toggle(self):
        for p in ("how do I disable agres hooks for one command?", "is the agres offline mode ok",
                  "because agres crashed yesterday", "agres only stores prompts, right?",
                  "can you use agres search to find the MQTT decision"):
            self.run_hook("prompt", prompt=p)
            self.assertFalse(config.is_off(self.root), p)
        self.assertIn("off", self.context(self.run_hook("prompt", prompt="agres off please")).lower())
        self.assertTrue(config.is_off(self.root))
        self.assertIn("on", self.context(self.run_hook("prompt", prompt="ok, enable agres again")).lower())
        self.assertFalse(config.is_off(self.root))

    def test_identical_replies_in_different_turns_are_kept(self):
        self.run_hook("prompt", prompt="first", prompt_id="p1")
        self.run_hook("stop", last_assistant_message="Done.")
        self.run_hook("stop", last_assistant_message="Done.")  # re-fire, same turn
        self.run_hook("prompt", prompt="second", prompt_id="p2")
        self.run_hook("stop", last_assistant_message="Done.")
        self.assertEqual([t for t in self.turns("assistant")], [("assistant", "Done."), ("assistant", "Done.")])

    def test_session_end_while_off_writes_nothing(self):
        self.run_hook("prompt", prompt="some work here")
        config.set_off(self.root, True)
        self.run_hook("session-end", reason="other")
        with self.store() as s:
            self.assertIsNone(s.one("SELECT ended_at FROM sessions WHERE id='sess-1'")["ended_at"])
            self.assertEqual(s.one("SELECT COUNT(*) FROM items")[0], 0)
