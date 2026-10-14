import json
import os
import re

from agres import config, db, memory, status
from tests.helpers import TempHome


class StatusTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def render(self, **kw):
        return status.render(self.s, color=False, **kw)

    def test_sections_and_live_counts(self):
        memory.add_item(self.s, "pin", "Zero brick tolerance", priority=1)
        memory.add_item(self.s, "pin", "Board services list")
        old = memory.add_handoff(self.s, "first")
        memory.add_handoff(self.s, "Next: stage 2")
        memory.add_item(self.s, "decision", "Use MQTT")
        memory.add_item(self.s, "decision", "Blue dot", status="rejected")
        out = self.render()
        for head in ("memory", "next session", "attention", "activity", "code", "store"):
            self.assertRegex(out, rf"(?m)^{head}\b", head)
        self.assertIn("2 pins (1 critical)", out)
        self.assertIn("1 rejected", out)
        self.assertNotIn("2 handoffs", out)  # superseded ones don't count
        self.assertIn(f"handoff H{old + 1}", out)
        self.assertNotIn("\x1b[", out)

    def test_next_session_shows_pack_size_and_reach(self):
        for i in range(120):
            memory.add_item(self.s, "pin", f"fact {i} " + "word " * 25)
        line = next(l for l in self.render().splitlines() if l.startswith("next session"))
        m = re.search(r"([\d,]+) / ([\d,]+) chars", line)
        self.assertTrue(m, line)
        self.assertLessEqual(int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))
        self.assertRegex(line, r"\d+ of 120 items reachable")

    def test_attention_flags_actionable_problems(self):
        t = memory.add_item(self.s, "task", "Ancient refactor")
        self.s.x("UPDATE items SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (t,))
        memory.add_item(self.s, "pin", "some fact")
        with open(os.path.join(self.s.dir, "hook.log"), "w") as fh:
            ts = status.now_iso()
            fh.write(f"--- {ts} prompt\nTraceback ...\nRuntimeError: disk on fire\n")
        out = self.render()
        att = out[out.index("attention"):out.index("activity")]
        self.assertIn("no critical pins", att)
        self.assertIn("1 stale task", att)
        self.assertIn(f"T{t}", att)
        self.assertIn("1 hook error", att)
        self.assertIn("disk on fire", att)
        self.assertIn("agres", att)  # every warning says what to run

    def test_all_good(self):
        memory.add_item(self.s, "pin", "rule", priority=1)
        memory.touch_session(self.s, "s1", agent="claude-code", prompt=True)
        memory.add_turn(self.s, "s1", "user", "hello")
        out = self.render()
        self.assertIn("nothing needs attention", out)

    def test_hooks_capturing_vs_silent(self):
        memory.touch_session(self.s, "s1", agent="claude-code", prompt=True)
        out = self.render()
        self.assertIn("prompts seen but none captured", out)
        memory.add_turn(self.s, "s1", "user", "hi")
        self.assertNotIn("none captured", self.render())

    def test_off_and_v0_strays(self):
        config.set_off(self.root, True)
        stray = os.path.join(self.root, "packages", "x", ".agres")
        os.makedirs(stray)
        open(os.path.join(stray, "agres.db"), "w").close()
        out = self.render()
        self.assertIn("OFF", out.splitlines()[0])
        self.assertIn("agres on", out)

    def test_json(self):
        memory.add_item(self.s, "task", "x")
        d = json.loads(status.render(self.s, as_json=True))
        for k in ("project", "memory", "next_session", "attention", "activity", "code", "store", "hooks"):
            self.assertIn(k, d)
        self.assertEqual(d["memory"]["tasks_open"], 1)

    def test_color_only_when_asked(self):
        memory.add_item(self.s, "pin", "x")
        self.assertIn("\x1b[", status.render(self.s, color=True))
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(status.use_color(force=None))
        finally:
            os.environ.pop("NO_COLOR")
