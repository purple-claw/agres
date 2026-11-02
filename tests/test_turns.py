"""Captured-turn control: gc preview, exact-turn and per-session forget (audit #2, #3)."""
import os
import subprocess
import sys

from agres import db, memory
from tests.helpers import TempHome

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = "2020-01-01T00:00:00Z"


class TurnControlTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "p")
        with db.Store(self.root, create=True) as s:
            self.old = memory.add_turn(s, "s1", "user", "old prompt about mqtt", created_at=OLD)
            self.a = memory.add_turn(s, "s1", "user", "leaked thing in s1")
            self.b = memory.add_turn(s, "s2", "user", "keep me in s2 please")
            self.c = memory.add_turn(s, "s2", "assistant", "reply in s2 here")

    def agres(self, *args, ok=True):
        env = dict(os.environ, PYTHONPATH=REPO, HOME=self.home)
        env.pop("AGRES_SESSION", None)
        p = subprocess.run([sys.executable, "-m", "agres", *args], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, f"{args}\n{p.stdout}\n{p.stderr}")
        return p

    def turn_ids(self):
        with db.Store(self.root) as s:
            return [r[0] for r in s.q("SELECT id FROM turns ORDER BY id")]

    def fts_hits(self, word):
        with db.Store(self.root) as s:
            return memory.search_turn_ids(s, word)

    # -------------------------------------------------------------- gc
    def test_gc_dry_run_deletes_nothing(self):
        out = self.agres("gc", "--turns-older-than", "30", "--dry-run").stdout
        self.assertIn("would remove 1 turn", out)
        self.assertIn("cutoff", out)
        self.assertEqual(len(self.turn_ids()), 4)

    def test_gc_really_prunes(self):
        self.agres("gc", "--turns-older-than", "30")
        self.assertNotIn(self.old, self.turn_ids())

    def test_gc_dry_run_is_documented(self):
        self.assertIn("--dry-run", self.agres("gc", "--help").stdout)

    # ---------------------------------------------------------- forget
    def test_forget_turn_by_id_removes_row_and_search_entry(self):
        self.assertEqual(self.fts_hits("leaked"), [self.a])
        out = self.agres("forget", f"M{self.a}").stdout
        self.assertIn(f"M{self.a}", out)
        self.assertNotIn(self.a, self.turn_ids())
        self.assertEqual(self.fts_hits("leaked"), [])
        self.assertIn("no turn", self.agres("show", f"M{self.a}", ok=False).stderr)

    def test_forget_several_turns(self):
        self.agres("forget", f"M{self.a}", f"M{self.b}")
        self.assertEqual(self.turn_ids(), [self.old, self.c])

    def test_forget_session(self):
        out = self.agres("forget", "--session", "s2").stdout
        self.assertIn("2 turn", out)
        self.assertEqual(self.turn_ids(), [self.old, self.a])

    def test_forget_dry_run(self):
        out = self.agres("forget", "--session", "s2", "--dry-run").stdout
        self.assertIn("would", out)
        self.assertEqual(len(self.turn_ids()), 4)

    def test_forget_unknown_turn_fails_cleanly(self):
        p = self.agres("forget", "M999", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_forget_item_still_tombstones(self):
        self.agres("note", "temp token abc123secretvalue")
        self.agres("forget", "N1")
        self.assertIn("[forgotten]", self.agres("show", "N1").stdout)
