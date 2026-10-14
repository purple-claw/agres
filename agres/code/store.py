"""SQLite storage for the project brain (one DB per repository: <root>/.agres/brain.db).

Design notes
------------
* Everything is a node or an edge. Code entities, packages, and memory items
  (decisions, tasks, errors, ...) live in one id space so one graph can
  connect "decision D is about class C which is called by file F".
* Edges are *bi-temporal-lite* (after Zep/Graphiti): they are never deleted.
  When a fact stops being true its ``valid_to`` is set, so history survives and
  "what did the architecture look like at time T" stays answerable.
* A partial unique index guarantees at most one *active* edge per
  (src, dst, kind), which makes diff-based syncing safe and idempotent.
* WAL + busy_timeout make concurrent CLI calls (agent + hooks) safe.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from ..util import now_iso, search_text, split_identifier

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  qualname TEXT,
  path TEXT,
  line INTEGER,
  end_line INTEGER,
  lang TEXT,
  sig TEXT,
  doc TEXT,
  hash TEXT,
  attrs TEXT,
  rank REAL DEFAULT 0,
  community INTEGER,
  created_at TEXT,
  updated_at TEXT,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name);
CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY,
  src INTEGER NOT NULL,
  dst INTEGER NOT NULL,
  kind TEXT NOT NULL,
  weight REAL DEFAULT 1.0,
  attrs TEXT,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  recorded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS edges_active ON edges(src, dst, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_src ON edges(src) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_kind ON edges(kind) WHERE valid_to IS NULL;
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,
  node_id INTEGER,
  hash TEXT,
  size INTEGER,
  mtime_ns INTEGER,
  lang TEXT,
  loc INTEGER,
  facts BLOB,
  parse_error TEXT,
  is_test INTEGER DEFAULT 0,
  is_entry INTEGER DEFAULT 0,
  scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS memory(
  id INTEGER PRIMARY KEY,
  node_id INTEGER,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT,
  meta TEXT,
  supersedes INTEGER,
  created_at TEXT,
  updated_at TEXT,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS memory_kind ON memory(kind, status);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY,
  ts TEXT, head TEXT, branch TEXT, dirty INTEGER,
  files INTEGER, symbols INTEGER, edges INTEGER, loc INTEGER,
  added INTEGER, modified INTEGER, deleted INTEGER
);
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY,
  scan_id INTEGER, ts TEXT, kind TEXT, path TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS changes_scan ON changes(scan_id);
CREATE TABLE IF NOT EXISTS commits(
  sha TEXT PRIMARY KEY, ts TEXT, author TEXT, subject TEXT, nfiles INTEGER
);
CREATE TABLE IF NOT EXISTS commit_files(
  sha TEXT, path TEXT, added INTEGER, deleted INTEGER,
  PRIMARY KEY(sha, path)
);
CREATE INDEX IF NOT EXISTS commit_files_path ON commit_files(path);
"""


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def pack(obj: Any) -> bytes:
    return zlib.compress(dumps(obj).encode("utf-8"), 6)


def unpack(blob: bytes | None) -> Any:
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Store:
    def __init__(self, root: str, db_path: str | None = None):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, ".agres")
        os.makedirs(self.dir, exist_ok=True)
        self.path = db_path or os.path.join(self.dir, "brain.db")
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self._tx_depth = 0
        self.fts = False
        self._migrate()
        self._init_search()
        self._ensure_gitignore()

    # ------------------------------------------------------------- plumbing
    def _migrate(self) -> None:
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"brain.db schema v{ver} is newer than this agres (v{SCHEMA_VERSION}); upgrade agres."
            )
        if ver < SCHEMA_VERSION:
            with self.tx():
                for stmt in _split_sql(SCHEMA):
                    self.db.execute(stmt)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.set_meta("created_at", self.get_meta("created_at") or now_iso())

    def _init_search(self) -> None:
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5("
                "text, tokenize='unicode61 remove_diacritics 2')"
            )
            self.fts = True
        except sqlite3.OperationalError:
            self.db.execute("CREATE TABLE IF NOT EXISTS search(rowid INTEGER PRIMARY KEY, text TEXT)")
            self.fts = False

    def _ensure_gitignore(self) -> None:
        gi = os.path.join(self.dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("# Agres: the DB and generated artifacts are local; MEMORY.md may be committed.\n")
                fh.write("brain.db\nbrain.db-wal\nbrain.db-shm\ngraph.html\ngraph.*.json\nBRIEF.md\n*.tmp\n")

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

    def one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    def many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.db.executemany(sql, rows)

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ----------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))

    # ---------------------------------------------------------------- nodes
    def upsert_node(self, key: str, kind: str, name: str, **f: Any) -> int:
        """Insert or update a node identified by ``key``; resurrects soft-deleted nodes."""
        ts = now_iso()
        attrs = f.pop("attrs", None)
        attrs_json = dumps(attrs) if attrs is not None else None
        row = self.one("SELECT id FROM nodes WHERE key=?", (key,))
        cols = ["qualname", "path", "line", "end_line", "lang", "sig", "doc", "hash"]
        vals = [f.get(c) for c in cols]
        if row:
            nid = row["id"]
            self.x(
                "UPDATE nodes SET kind=?, name=?, qualname=?, path=?, line=?, end_line=?, lang=?, sig=?,"
                " doc=?, hash=?, attrs=COALESCE(?, attrs), updated_at=?, deleted_at=NULL WHERE id=?",
                [kind, name, *vals, attrs_json, ts, nid],
            )
        else:
            cur = self.x(
                "INSERT INTO nodes(key,kind,name,qualname,path,line,end_line,lang,sig,doc,hash,attrs,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [key, kind, name, *vals, attrs_json, ts, ts],
            )
            nid = cur.lastrowid
        return int(nid)

    def node(self, nid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM nodes WHERE id=?", (nid,))

    def node_by_key(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM nodes WHERE key=?", (key,))

    def soft_delete_nodes(self, ids: Iterable[int]) -> None:
        ts = now_iso()
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            ph = ",".join("?" * len(chunk))
            self.x(f"UPDATE nodes SET deleted_at=?, updated_at=? WHERE id IN ({ph}) AND deleted_at IS NULL",
                   [ts, ts, *chunk])
            self.x(f"UPDATE edges SET valid_to=? WHERE valid_to IS NULL AND (src IN ({ph}) OR dst IN ({ph}))",
                   [ts, *chunk, *chunk])
            self.x(f"DELETE FROM search WHERE rowid IN ({ph})", chunk)

    # ---------------------------------------------------------------- edges
    def add_edge(self, src: int, dst: int, kind: str, weight: float = 1.0, attrs: Any = None) -> bool:
        """Create an active edge; if one exists just refresh weight/attrs. Returns True if new."""
        ts = now_iso()
        row = self.one("SELECT id, weight FROM edges WHERE src=? AND dst=? AND kind=? AND valid_to IS NULL",
                       (src, dst, kind))
        a = dumps(attrs) if attrs is not None else None
        if row:
            if abs((row["weight"] or 0) - weight) > 1e-9 or a is not None:
                self.x("UPDATE edges SET weight=?, attrs=COALESCE(?, attrs) WHERE id=?", (weight, a, row["id"]))
            return False
        self.x("INSERT INTO edges(src,dst,kind,weight,attrs,valid_from,recorded_at) VALUES(?,?,?,?,?,?,?)",
               (src, dst, kind, weight, a, ts, ts))
        return True

    def close_edge(self, edge_id: int) -> None:
        self.x("UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL", (now_iso(), edge_id))

    # --------------------------------------------------------------- search
    def index_text(self, nid: int, *parts: str | None) -> None:
        self.x("DELETE FROM search WHERE rowid=?", (nid,))
        self.x("INSERT INTO search(rowid, text) VALUES(?,?)", (nid, search_text(*parts)))

    def search_ids(self, query: str, limit: int = 20, kinds: Sequence[str] | None = None) -> list[tuple[int, float]]:
        """Return [(node_id, score)] best-first. Score is positive, larger is better."""
        toks: list[str] = []
        for t in re.findall(r"[A-Za-z0-9_]+", query):
            toks.append(t.lower())
            sub = split_identifier(t)
            if len(sub) > 1:
                toks.extend(sub)
        toks = list(dict.fromkeys(t for t in toks if len(t) > 1 or t.isdigit()))
        if not toks:
            return []
        out: list[tuple[int, float]] = []
        kind_sql, kind_args = "", []
        if kinds:
            kind_sql = " AND n.kind IN (%s)" % ",".join("?" * len(kinds))
            kind_args = list(kinds)
        if self.fts:
            match = " OR ".join('"%s"*' % t.replace('"', "") for t in toks)
            try:
                rows = self.q(
                    "SELECT s.rowid AS id, bm25(search) AS s FROM search s JOIN nodes n ON n.id=s.rowid "
                    "WHERE search MATCH ? AND n.deleted_at IS NULL" + kind_sql +
                    " ORDER BY bm25(search) LIMIT ?",
                    [match, *kind_args, limit],
                )
                return [(r["id"], -float(r["s"])) for r in rows]
            except sqlite3.OperationalError:
                pass
        # LIKE fallback (no FTS5): score = number of distinct tokens matched
        rows = self.q("SELECT s.rowid AS id, s.text AS t FROM search s JOIN nodes n ON n.id=s.rowid "
                      "WHERE n.deleted_at IS NULL" + kind_sql, kind_args)
        for r in rows:
            low = r["t"].lower()
            hit = sum(1 for t in toks if t in low)
            if hit:
                out.append((r["id"], float(hit)))
        out.sort(key=lambda p: -p[1])
        return out[:limit]


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
