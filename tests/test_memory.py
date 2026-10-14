import os
import time

from agres import db, memory
from tests.helpers import TempHome


class MemoryTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "p")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    # ---------------------------------------------------------------- ids
    def test_ids_round_trip(self):
        self.assertEqual(memory.fmt_id("decision", 12), "D12")
        self.assertEqual(memory.fmt_id("turn", 3), "M3")
        self.assertEqual(memory.parse_id("d12"), ("decision", 12))
        self.assertEqual(memory.parse_id("M3"), ("turn", 3))
        self.assertEqual(memory.parse_id("7"), (None, 7))
        with self.assertRaises(ValueError):
            memory.parse_id("X9")

    def test_title_is_first_sentence(self):
        self.assertEqual(memory.derive_title("Use MQTT on :1884. Because LAN auth."), "Use MQTT on :1884.")
        long = "word " * 40
        t = memory.derive_title(long)
        self.assertLessEqual(len(t), 80)
        self.assertTrue(t.endswith("…"))
        self.assertEqual(memory.derive_title("line one\nline two"), "line one")

    # -------------------------------------------------------------- items
    def test_add_defaults(self):
        iid = memory.add_item(self.s, "decision", "Keep applicationId com.okas.okas_smarthome.")
        it = memory.get_item(self.s, iid)
        self.assertEqual((it["kind"], it["status"], it["source"]), ("decision", "accepted", "agent"))
        self.assertEqual(memory.add_item(self.s, "task", "Ship it") - iid, 1)
        self.assertEqual(memory.get_item(self.s, iid + 1)["status"], "open")
        self.assertEqual(memory.get_item(self.s, memory.add_item(self.s, "pin", "x"))["status"], "active")

    def test_reject_is_a_decision_with_rejected_status(self):
        iid = memory.add_item(self.s, "decision", "Blue centre dot in icon", status="rejected")
        self.assertEqual(memory.get_item(self.s, iid)["status"], "rejected")

    def test_invalid_kind_and_status(self):
        with self.assertRaises(ValueError):
            memory.add_item(self.s, "memo", "x")
        with self.assertRaises(ValueError):
            memory.add_item(self.s, "task", "x", status="accepted")
        with self.assertRaises(ValueError):
            memory.add_item(self.s, "note", "   ")

    def test_replaces_supersedes_old_item(self):
        old = memory.add_item(self.s, "decision", "Use HTTP polling for loads")
        new = memory.add_item(self.s, "decision", "Use MQTT for loads", replaces=old)
        o = memory.get_item(self.s, old)
        self.assertEqual((o["status"], o["superseded_by"]), ("superseded", new))
        with self.assertRaises(ValueError):
            memory.add_item(self.s, "decision", "again", replaces=9999)

    def test_done_and_drop_transitions(self):
        t = memory.add_item(self.s, "task", "Write runbook")
        e = memory.add_item(self.s, "error", "KNX tunnel drops")
        p = memory.add_item(self.s, "pin", "No sacrificial board")
        d = memory.add_item(self.s, "decision", "Use xknx")
        self.assertEqual(memory.close_item(self.s, t, "done", note="verified"), ("open", "done"))
        self.assertEqual(memory.close_item(self.s, e, "done"), ("open", "fixed"))
        self.assertEqual(memory.close_item(self.s, p, "drop"), ("active", "retired"))
        self.assertEqual(memory.close_item(self.s, d, "drop"), ("accepted", "retracted"))
        it = memory.get_item(self.s, t)
        self.assertEqual(it["close_note"], "verified")
        self.assertIsNotNone(it["closed_at"])
        with self.assertRaises(ValueError):
            memory.close_item(self.s, p, "done")  # pins are never "done"
        with self.assertRaises(ValueError):
            memory.close_item(self.s, t, "done")  # already closed

    def test_forget_redacts_and_unindexes(self):
        iid = memory.add_item(self.s, "note", "token is hunter2-secret")
        memory.forget(self.s, iid)
        it = memory.get_item(self.s, iid)
        self.assertNotIn("hunter2", it["text"] + it["title"])
        self.assertEqual(memory.similar(self.s, "hunter2-secret token"), [])

    def test_similar_finds_near_duplicates_only(self):
        a = memory.add_item(self.s, "decision", "App talks to the board over MQTT on port 1884 with LAN auth")
        memory.add_item(self.s, "decision", "Splash screen shows only before sign-in")
        hits = memory.similar(self.s, "Use MQTT on board port 1884 for app traffic")
        self.assertEqual([h["id"] for h in hits], [a])
        self.assertEqual(memory.similar(self.s, "Completely unrelated gardening advice"), [])
        memory.close_item(self.s, a, "drop")
        self.assertEqual(memory.similar(self.s, "Use MQTT on board port 1884 for app traffic"), [])

    def test_redaction_on_write(self):
        iid = memory.add_item(self.s, "note", "key AKIAABCDEFGHIJKLMNOP and <private>my pin 1234</private> ok")
        txt = memory.get_item(self.s, iid)["text"]
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", txt)
        self.assertNotIn("1234", txt)
        self.assertIn("ok", txt)

    # ------------------------------------------------------------- turns
    def test_turn_dedupe_per_session_and_role(self):
        a = memory.add_turn(self.s, "s1", "user", "hello there")
        self.assertIsNotNone(a)
        self.assertIsNone(memory.add_turn(self.s, "s1", "user", "hello there"))
        self.assertIsNotNone(memory.add_turn(self.s, "s1", "assistant", "hello there"))
        self.assertIsNotNone(memory.add_turn(self.s, "s2", "user", "hello there"))
        self.assertIsNone(memory.add_turn(self.s, "s1", "user", "   "))

    def test_turn_cap_and_private(self):
        big = "x" * (memory.MAX_TURN_CHARS + 500)
        tid = memory.add_turn(self.s, "s1", "assistant", big)
        txt = self.s.one("SELECT text FROM turns WHERE id=?", (tid,))[0]
        self.assertLessEqual(len(txt), memory.MAX_TURN_CHARS + 40)
        self.assertIn("[truncated", txt)
        tid = memory.add_turn(self.s, "s1", "user", "deploy <private>pw=abc</private> now")
        self.assertNotIn("pw=abc", self.s.one("SELECT text FROM turns WHERE id=?", (tid,))[0])

    # ----------------------------------------------------------- sessions
    def test_session_upsert_and_end(self):
        memory.touch_session(self.s, "s1", agent="claude-code", cwd=self.root)
        memory.touch_session(self.s, "s1", prompt=True)
        row = self.s.one("SELECT * FROM sessions WHERE id='s1'")
        self.assertEqual((row["agent"], row["prompts"]), ("claude-code", 1))
        memory.end_session(self.s, "s1", "prompt_input_exit")
        row = self.s.one("SELECT * FROM sessions WHERE id='s1'")
        self.assertEqual(row["end_reason"], "prompt_input_exit")
        self.assertIsNotNone(row["ended_at"])

    # --------------------------------------------- imports keep their history
    def test_add_item_explicit_timestamps_and_close_note(self):
        iid = memory.add_item(self.s, "task", "Ship the thing", status="done",
                              created_at="2024-01-02T03:04:05Z", updated_at="2024-02-03T04:05:06Z",
                              close_note="imported")
        it = memory.get_item(self.s, iid)
        self.assertEqual((it["created_at"], it["updated_at"], it["closed_at"]),
                         ("2024-01-02T03:04:05Z", "2024-02-03T04:05:06Z", "2024-02-03T04:05:06Z"))
        self.assertEqual(it["close_note"], "imported")
        iid = memory.add_item(self.s, "task", "Other", status="dropped",
                              created_at="2024-01-02T03:04:05Z", closed_at="2024-03-04T00:00:00Z")
        it = memory.get_item(self.s, iid)
        self.assertEqual((it["updated_at"], it["closed_at"]), ("2024-01-02T03:04:05Z", "2024-03-04T00:00:00Z"))
        live = memory.get_item(self.s, memory.add_item(self.s, "pin", "Live", created_at="2024-01-02T03:04:05Z"))
        self.assertIsNone(live["closed_at"])

    def test_add_item_defaults_unchanged_without_new_kwargs(self):
        iid = memory.add_item(self.s, "task", "Plain")
        it = memory.get_item(self.s, iid)
        self.assertEqual(it["created_at"], it["updated_at"])
        self.assertIsNone(it["closed_at"])
        self.assertIsNone(it["close_note"])

    def test_add_session_inserts_once_with_given_times(self):
        self.assertTrue(memory.add_session(
            self.s, "old1", agent="opencode", title="Fix the thing", cwd="/x", started_at="2024-01-01T00:00:00Z",
            last_seen_at="2024-01-01T01:00:00Z", ended_at="2024-01-01T01:00:00Z", end_reason="migrated",
            prompts=4))
        row = self.s.one("SELECT * FROM sessions WHERE id='old1'")
        self.assertEqual((row["agent"], row["title"], row["cwd"], row["prompts"]), ("opencode", "Fix the thing", "/x", 4))
        self.assertEqual((row["started_at"], row["ended_at"], row["end_reason"]),
                         ("2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "migrated"))
        self.assertFalse(memory.add_session(self.s, "old1", title="changed"))
        self.assertEqual(self.s.one("SELECT title FROM sessions WHERE id='old1'")[0], "Fix the thing")

    def test_resolve_session_precedence(self):
        self.assertIsNone(memory.resolve_session(self.s))
        memory.touch_session(self.s, "recent")
        self.assertEqual(memory.resolve_session(self.s), "recent")
        os.environ["AGRES_SESSION"] = "from-env"
        self.assertEqual(memory.resolve_session(self.s), "from-env")
        self.assertEqual(memory.resolve_session(self.s, "explicit"), "explicit")
        os.environ.pop("AGRES_SESSION")
        self.s.x("UPDATE sessions SET last_seen_at='2000-01-01T00:00:00Z'")
        self.assertIsNone(memory.resolve_session(self.s))


class MemoryNoFtsTest(MemoryTest):
    """Same behaviour on SQLite builds without FTS5 (LIKE/term-overlap fallback)."""

    def setUp(self):
        TempHome.setUp(self)
        self.root = self.git_repo("work", "p")
        self.s = db.Store(self.root, create=True, fts=False)
