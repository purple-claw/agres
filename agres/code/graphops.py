"""Code-graph tables and node/edge/search operations, mixed into agres.db.Store.

Design notes (from the 1.0 engine)
----------------------------------
* Code entities are nodes; relations are edges. Edges are *bi-temporal-lite*
  (after Zep/Graphiti): never deleted, `valid_to` is set when a fact stops being
  true, so history survives.
* A partial unique index keeps at most one *active* edge per (src, dst, kind),
  which makes diff-based syncing safe and idempotent.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zlib
from typing import Any, Iterable, Sequence

from ..util import now_iso, search_text, split_identifier

CODE_SCHEMA = """
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


class GraphOps:
    """Needs q/one/x/many and a `fts` flag from the host Store."""

    fts: bool

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
            self.x(f"DELETE FROM fts_code WHERE rowid IN ({ph})", chunk)

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
        self.x("DELETE FROM fts_code WHERE rowid=?", (nid,))
        self.x("INSERT INTO fts_code(rowid, body) VALUES(?,?)", (nid, search_text(*parts)))

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
                    "SELECT s.rowid AS id, bm25(fts_code) AS s FROM fts_code s JOIN nodes n ON n.id=s.rowid "
                    "WHERE fts_code MATCH ? AND n.deleted_at IS NULL" + kind_sql +
                    " ORDER BY bm25(fts_code) LIMIT ?",
                    [match, *kind_args, limit],
                )
                return [(r["id"], -float(r["s"])) for r in rows]
            except sqlite3.OperationalError:
                pass
        rows = self.q("SELECT s.rowid AS id, s.body AS t FROM fts_code s JOIN nodes n ON n.id=s.rowid "
                      "WHERE n.deleted_at IS NULL" + kind_sql, kind_args)
        for r in rows:
            low = r["t"].lower()
            hit = sum(1 for t in toks if t in low)
            if hit:
                out.append((r["id"], float(hit)))
        out.sort(key=lambda p: -p[1])
        return out[:limit]
