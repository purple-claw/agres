"""`agres dash`: full-screen memory dashboard (layout, panels, width safety, json, cli)."""
import json
import os
import re
import subprocess
import sys

from agres import dash, db, fold, memory
from tests.helpers import TempHome

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PANELS = ["MEMORY", "NEXT SESSION", "ORIGAMI", "ACTIVITY", "SESSIONS", "OPEN WORK", "CONSTRAINTS",
          "HOOKS", "CODE", "PRIVACY", "ATTENTION"]


def seed(s):
    memory.add_item(s, "pin", "Zero brick tolerance on the dev board.", priority=1)
    memory.add_item(s, "pin", "Board services: mosquitto, hermes.")
    memory.add_item(s, "decision", "Use MQTT on 1884.")
    memory.add_item(s, "decision", "Blue dot in icon.", status="rejected", why="ring only")
    memory.add_item(s, "task", "Secure boot runbook stage 2")
    memory.add_item(s, "error", "KNX tunnel drops after 10 minutes")
    memory.add_handoff(s, "Done: stage 1. Next: rollback test.")
    memory.touch_session(s, "sess-aaaa1111", agent="claude-code", prompt=True)
    t = memory.add_turn(s, "sess-aaaa1111", "user", "please fix the tunnel")
    fold.add_fold(s, "tool", "Bash stdout: make", "x", body="y\n" * 3000, meta={"lines": 3000, "chars": 6000})
    fold.add_fold(s, "span", "compaction", "Goal: knx. Done: keepalive.", meta={"turns": [t, t]})


class DashTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def render(self, width, color=False, data=None):
        return dash.render(data or dash.collect(self.s), width=width, color=color)

    def test_all_panels_present_and_lines_fit_every_width(self):
        seed(self.s)
        for width in (60, 80, 100, 140, 200):
            out = self.render(width)
            for p in PANELS:
                self.assertIn(p, out, (width, p))
            for line in out.splitlines():
                self.assertLessEqual(len(line), width, (width, line))

    def test_two_columns_when_wide(self):
        seed(self.s)
        narrow = self.render(80).splitlines()
        wide = self.render(160).splitlines()
        self.assertLess(len(wide), len(narrow))

    def test_content(self):
        seed(self.s)
        out = self.render(140)
        self.assertIn("P1", out)                  # critical pin listed
        self.assertIn("D4", out)                  # rejected decision listed
        self.assertIn("T5", out)                  # open task
        self.assertIn("F2", out)                  # span fold
        self.assertIn("sess-aaa", out)            # session row
        self.assertRegex(out, r"[▁▂▃▄▅▆▇█]")      # sparkline
        self.assertRegex(out, r"[█░]{5,}")        # bar

    def test_color_only_adds_escapes(self):
        seed(self.s)
        data = dash.collect(self.s)
        plain = self.render(120, data=data)
        colored = self.render(120, color=True, data=data)
        self.assertIn("\x1b[", colored)
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_empty_store(self):
        out = self.render(100)
        self.assertIn("MEMORY", out)
        self.assertIn("nothing recorded yet", out)

    def test_collect_is_json_serialisable(self):
        seed(self.s)
        data = dash.collect(self.s)
        for key in ("pack_sections", "daily", "sessions", "folds", "open_work", "constraints", "rejected", "langs"):
            self.assertIn(key, data)
        self.assertEqual(len(data["daily"]["prompts"]), dash.DAYS)
        json.dumps(data)

    def test_cli(self):
        seed(self.s)
        self.s.close()
        env = dict(os.environ, PYTHONPATH=REPO, HOME=self.home, COLUMNS="120")
        p = subprocess.run([sys.executable, "-m", "agres", "dash"], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ORIGAMI", p.stdout)
        p = subprocess.run([sys.executable, "-m", "agres", "dash", "--json"], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("pack_sections", json.loads(p.stdout))
        self.s = db.Store(self.root)
