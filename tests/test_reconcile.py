"""Index freshness: shell edits without bashEditDiff are reconciled (audit #4 medium, R11)."""
import io
import json
import os
import time

from agres import hooks
from agres.code import engine
from tests.test_index import IndexCase


class ReconcileTest(IndexCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("rec", git=True)
        self.put(self.root, {"a.py": "def alpha():\n    return 1\n", "b.py": "def beta():\n    return 2\n"})
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-qm", "init")
        self.s = self.store(self.root)
        engine.sync(self.s)

    def symbols(self):
        return {r["name"] for r in self.s.q("SELECT name FROM nodes WHERE deleted_at IS NULL AND kind='function'")}

    def bump(self, rel, text):
        self.write(self.root, rel, text)
        p = os.path.join(self.root, rel)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    def test_reconcile_picks_up_modified_new_and_deleted_files(self):
        self.bump("a.py", "def alpha_renamed():\n    return 1\n")
        self.bump("c.py", "def gamma():\n    return 3\n")
        os.remove(os.path.join(self.root, "b.py"))
        n = engine.reconcile(self.s)
        self.assertEqual(n, 3)
        names = self.symbols()
        self.assertIn("alpha_renamed", names)
        self.assertIn("gamma", names)
        self.assertNotIn("beta", names)
        self.assertTrue(self.s.get_meta("last_reconcile"))

    def test_unchanged_dirty_files_are_not_reparsed_twice(self):
        self.bump("a.py", "def alpha2():\n    return 1\n")
        self.assertEqual(engine.reconcile(self.s), 1)
        self.assertEqual(engine.reconcile(self.s), 0)

    def test_no_index_means_no_work(self):
        other = self.project("empty", git=True)
        s = self.store(other)
        self.put(other, {"x.py": "def x():\n    pass\n"})
        self.assertEqual(engine.reconcile(s), 0)
        self.assertFalse(s.one("SELECT 1 FROM files LIMIT 1"))

    def test_bash_hook_without_bash_edit_diff_reconciles(self):
        self.bump("a.py", "def from_sed():\n    return 1\n")
        payload = {"session_id": "s", "cwd": self.root, "tool_name": "Bash",
                   "tool_input": {"command": "sed -i s/alpha/from_sed/ a.py"},
                   "tool_response": {"stdout": "", "stderr": ""}}
        self.assertEqual(hooks.main("edit", io.StringIO(json.dumps(payload)), io.StringIO()), 0)
        self.assertIn("from_sed", self.symbols())
