"""One SQLite store per project: <root>/.agres/agres.db.

Memory (sessions, items, turns) and the code graph share the file. Full-text
search tables are maintained in Python (not triggers) so identifier sub-words
can be indexed ("getUserToken" also matches "user token"); without FTS5 they are
plain tables searched with LIKE.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

from . import paths
from .code.graphops import CODE_SCHEMA, GraphOps
from .util import now_iso

SCHEMA_VERSION = 100  # v0.x stores have user_version 0; the unreleased 1.0 engine used 1

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  agent TEXT, title TEXT, cwd TEXT,
  started_at TEXT, last_seen_at TEXT, ended_at TEXT, end_reason TEXT,
  prompts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_seen ON sessions(last_seen_at);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('pin','decision','task','error','note','handoff')),
  status TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  why TEXT,
  files TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  superseded_by INTEGER REFERENCES items(id),
  session_id TEXT,
  source TEXT NOT NULL,
  origin TEXT,
  close_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS items_kind_status ON items(kind, status);
CREATE INDEX IF NOT EXISTS items_updated ON items(updated_at);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
  text TEXT NOT NULL,
  hash TEXT NOT NULL,
  prompt_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, role, hash)
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
"""

FTS_TABLES = ("fts_items", "fts_turns", "fts_code")
V0_MARKERS = {"folds", "window_items", "compaction_artifacts", "context_packs"}


class StoreError(Exception):
    pass


class NoStore(StoreError):
    pass


class V0Store(StoreError):
    pass


class NewerSchema(StoreError):
    pass


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


class Store(GraphOps):
    def __init__(self, root: str, create: bool = False, busy_ms: int = 5000, fts: Optional[bool] = None):
        self.root = os.path.realpath(root)
        self.dir = paths.store_dir(self.root)
        self.path = paths.db_path(self.root)
        if not os.path.isfile(self.path) and not create:
            raise NoStore(f"no agres store at {self.root}")
        os.makedirs(self.dir, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=busy_ms / 1000, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._tx_depth = 0
        try:
            self.db.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
            self._check_version(create, fts)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            self.db.close()
            raise
        self.fts = self.get_meta("fts") == "1"
        self._ensure_gitignore()

    # ------------------------------------------------------------- version
    def _check_version(self, create: bool, fts: Optional[bool]) -> None:
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise NewerSchema(f"{self.path} has schema {ver}; this agres understands {SCHEMA_VERSION}. Upgrade agres.")
        if ver == SCHEMA_VERSION:
            return
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if ver != 0 or tables & V0_MARKERS or "sessions" in tables:
            raise V0Store(f"{self.path} is a v0 agres store; run `agres migrate`")
        if tables and not create:
            raise NoStore(f"{self.path} is not an agres store")
        self._create_schema(fts)

    def _create_schema(self, fts: Optional[bool]) -> None:
        with self.tx():
            for stmt in _split_sql(MEMORY_SCHEMA) + _split_sql(CODE_SCHEMA):
                self.db.execute(stmt)
            use_fts = fts if fts is not None else _fts5_available(self.db)
            for t in FTS_TABLES:
                if use_fts:
                    self.db.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {t} USING fts5(body, "
                        "tokenize='unicode61 remove_diacritics 2')")
                else:
                    self.db.execute(f"CREATE TABLE IF NOT EXISTS {t}(rowid INTEGER PRIMARY KEY, body TEXT)")
            self.set_meta("fts", "1" if use_fts else "0")
            self.set_meta("created_at", now_iso())
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _ensure_gitignore(self) -> None:
        gi = os.path.join(self.dir, ".gitignore")
        if not os.path.exists(gi):
            try:
                with open(gi, "w", encoding="utf-8") as fh:
                    fh.write("*\n")
            except OSError:
                pass

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def tx(self) -> Iterator[None]:
        """Re-entrant write transaction (IMMEDIATE to fail fast on lock contention)."""
        if self._tx_depth == 0:
            self.db.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.db.execute("ROLLBACK")
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.db.execute("COMMIT")

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    def many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.db.executemany(sql, rows)

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def open_project(start: Optional[str] = None, create: bool = False, busy_ms: int = 5000) -> Store:
    """Store for the project containing `start` (default cwd). Raises NoStore if there is none."""
    root = paths.find_root(start)
    if root is None:
        raise NoStore("not inside an agres project (no .agres/ or git checkout above here); run `agres init`")
    return Store(root, create=create, busy_ms=busy_ms)
