import os
import sqlite3

from agres import db, paths
from tests.helpers import TempHome


class StoreTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "p")

    def test_create_makes_schema_and_private_gitignore(self):
        with db.Store(self.root, create=True) as s:
            self.assertEqual(s.one("PRAGMA user_version")[0], db.SCHEMA_VERSION)
            tables = {r[0] for r in s.q("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("meta", "sessions", "items", "turns", "nodes", "edges", "files"):
                self.assertIn(t, tables)
            self.assertNotIn("memory", tables)
        with open(os.path.join(self.root, ".agres", ".gitignore")) as fh:
            self.assertEqual(fh.read().strip(), "*")

    def test_open_missing_store_without_create(self):
        with self.assertRaises(db.NoStore):
            db.Store(self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".agres")))

    def test_v0_store_is_refused(self):
        os.makedirs(os.path.join(self.root, ".agres"))
        conn = sqlite3.connect(paths.db_path(self.root))
        conn.execute("CREATE TABLE sessions(id TEXT, goal TEXT)")
        conn.execute("CREATE TABLE folds(id TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(db.V0Store):
            db.Store(self.root)
        with self.assertRaises(db.V0Store):
            db.Store(self.root, create=True)

    def test_newer_schema_is_refused(self):
        os.makedirs(os.path.join(self.root, ".agres"))
        conn = sqlite3.connect(paths.db_path(self.root))
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(db.NewerSchema):
            db.Store(self.root)

    def test_reopen_is_idempotent(self):
        db.Store(self.root, create=True).close()
        with db.Store(self.root) as s:
            s.set_meta("k", "v")
        with db.Store(self.root) as s:
            self.assertEqual(s.get_meta("k"), "v")

    def test_tx_rolls_back(self):
        with db.Store(self.root, create=True) as s:
            with self.assertRaises(RuntimeError):
                with s.tx():
                    s.set_meta("k", "v")
                    raise RuntimeError("boom")
            self.assertIsNone(s.get_meta("k"))

    def test_fts_fallback_tables(self):
        with db.Store(self.root, create=True, fts=False) as s:
            self.assertFalse(s.fts)
            s.x("INSERT INTO fts_items(rowid, body) VALUES(1, 'hello')")
            self.assertEqual(s.one("SELECT body FROM fts_items WHERE rowid=1")[0], "hello")
