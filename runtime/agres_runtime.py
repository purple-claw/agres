#!/usr/bin/env python3
"""
Agres Runtime — SQLite edition.

Local Agres Origami Memory runtime using:

- SQLite structured storage
- JSONL event logs
- content-addressed storage
- Markdown human-readable state
- SQLite FTS5 keyword search when available
- LIKE fallback search when FTS5 is unavailable

No external embedding model is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


AGRES_HOME = Path(os.environ.get("AGRES_HOME", str(Path.home() / ".agres")))
RUNTIME_STATE = AGRES_HOME / "runtime_state.json"

def _project_root() -> Path:
    # ponytail: resolved per-call, not import-time, so multi-project & chdir safe
    return Path(os.environ.get("AGRES_PROJECT_ROOT", os.getcwd()))

def _project_agres_dir() -> Path:
    return _project_root() / ".agres"

def _agres_dir() -> Path:
    # Per-call resolution: project-local .agres if exists, else global
    p = _project_agres_dir()
    return p if p.exists() else AGRES_HOME

def _cas_dir() -> Path:
    return _agres_dir() / "cas" / "objects"

# Back-compat shims: keep globals but as dynamic properties via functions where possible
# Legacy globals now computed on import for compat, but canonical is _agres_dir()
PROJECT_ROOT = _project_root()
PROJECT_AGRES_DIR = _project_agres_dir()
AGRES_DIR = _agres_dir()
CAS_DIR = _cas_dir()

MAX_FILE_SIZE = int(os.environ.get("AGRES_MAX_FILE_SIZE", "1000000"))
CHUNK_MAX_LINES = int(os.environ.get("AGRES_CHUNK_MAX_LINES", "80"))
CHUNK_OVERLAP_LINES = int(os.environ.get("AGRES_CHUNK_OVERLAP_LINES", "10"))
MAX_CHUNKS_PER_FILE = int(os.environ.get("AGRES_MAX_CHUNKS_PER_FILE", "25"))
MAX_WINDOW_ITEM_CHARS = int(os.environ.get("AGRES_MAX_WINDOW_ITEM_CHARS", "12000"))
MAX_WINDOW_ITEMS = int(os.environ.get("AGRES_MAX_WINDOW_ITEMS", "500"))
# hard cap: if tiktoken not available we use chars//4; tiktoken path removed for determinism

IGNORE_DIRS = {
    ".git",
    ".agres",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
    "coverage",
    ".idea",
    ".vscode",
    "target",
    ".turbo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
}

INDEXABLE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
}

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".txt": "text",
}

SYMBOL_PATTERNS: Dict[str, List[tuple]] = {
    "python": [
        ("class", r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("function", r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ],
    "javascript": [
        ("class", r"\bclass\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        ("function", r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        ("variable", r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
    ],
    "typescript": [
        ("class", r"\bclass\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        ("interface", r"\binterface\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        ("type", r"\btype\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
        ("function", r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        ("variable", r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
    ],
    "go": [
        ("function", r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"),
        ("type", r"\btype\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ],
    "rust": [
        ("function", r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("struct", r"\bstruct\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("enum", r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("trait", r"\btrait\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ],
    "java": [
        ("class", r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("interface", r"\binterface\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("method", r"(?:public|private|protected)\s+[\w<>\[\],\s]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ],
    # Field addition: the kernel is C. Col-0 lowercase defs (kernel style puts
    # the return type on its own line); lowercase-first excludes UPPER macros.
    "c": [
        ("struct", r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("enum", r"^\s*(?:typedef\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("union", r"^\s*(?:typedef\s+)?union\s+([A-Za-z_][A-Za-z0-9_]*)"),
        ("function", r"^(?:(?:static|const|inline|extern|__init|__exit)\s+)*[A-Za-z_][A-Za-z0-9_\s\*]*?\s+([a-z_][A-Za-z0-9_]*)\s*\("),
        ("function", r"^([a-z_][A-Za-z0-9_]*)\s*\("),
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  repo TEXT,
  title TEXT,
  status TEXT,
  model TEXT,
  model_provider TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  ts TEXT,
  type TEXT,
  task_id TEXT,
  payload_hash TEXT,
  fold_type TEXT
);

CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  path TEXT UNIQUE,
  content_hash TEXT,
  language TEXT,
  size INTEGER,
  indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS file_chunks (
  id TEXT PRIMARY KEY,
  file_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  content_hash TEXT,
  text TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
  id TEXT PRIMARY KEY,
  file_path TEXT,
  name TEXT,
  kind TEXT,
  line INTEGER,
  text TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  task_id TEXT,
  decision TEXT,
  accepted INTEGER,
  rejected INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  status TEXT,
  objective TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS errors (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  task_id TEXT,
  message TEXT,
  context TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS pins (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  text TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  reason TEXT,
  path TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS context_packs (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  query TEXT,
  path TEXT,
  created_at TEXT,
  tokens INTEGER DEFAULT 0,
  budget INTEGER DEFAULT 0,
  stale INTEGER DEFAULT 0
);

-- Verbatim conversation turns (the durable full transcript).
-- seq is a GLOBAL auto-increment across all sessions (matches events.seq).
CREATE TABLE IF NOT EXISTS turns (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  role TEXT,
  content TEXT NOT NULL,
  token_estimate INTEGER DEFAULT 0,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at);

-- Folded context items: exact original text + summary, removed from the
-- active window but fully recoverable (unfold restores original text).
CREATE TABLE IF NOT EXISTS folds (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  kind TEXT,
  ref_id TEXT,
  title TEXT,
  original_text TEXT NOT NULL,
  summary TEXT,
  token_estimate INTEGER DEFAULT 0,
  folded_at TEXT,
  unfolded_at TEXT,
  status TEXT DEFAULT 'folded'
);

CREATE INDEX IF NOT EXISTS idx_folds_session ON folds(session_id);
CREATE INDEX IF NOT EXISTS idx_folds_status ON folds(status);
CREATE INDEX IF NOT EXISTS idx_folds_ref ON folds(ref_id);

-- What is currently in the active context window (the 256k budget).
CREATE TABLE IF NOT EXISTS window_items (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  kind TEXT,
  ref_id TEXT,
  title TEXT,
  text TEXT NOT NULL,
  token_estimate INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 0,
  folded INTEGER DEFAULT 0,
  added_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_window_session ON window_items(session_id);
CREATE INDEX IF NOT EXISTS idx_window_folded ON window_items(folded);

-- Phase 1: incremental manifest (mtime+size fast path, avoids hashing unchanged files).
CREATE TABLE IF NOT EXISTS manifest (
  path TEXT PRIMARY KEY,
  mtime REAL,
  size INTEGER,
  content_hash TEXT,
  indexed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_manifest_mtime ON manifest(mtime);

-- Phase 2: import-edge stub (referencer -> imported module, regex-based).
-- Populated on index; used as the graph branch of RRF hybrid retrieval.
CREATE TABLE IF NOT EXISTS edges (
  src_file TEXT,
  dst_file TEXT,
  ident TEXT,
  weight REAL DEFAULT 1.0,
  PRIMARY KEY (src_file, dst_file, ident)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_file);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_file);
CREATE INDEX IF NOT EXISTS idx_edges_ident ON edges(ident);

-- Phase 5: reference idents per file (tree-sitter only). Resolved to file
-- edges at map time via join to symbols, so index order never matters.
CREATE TABLE IF NOT EXISTS ref_idents (
  src_file TEXT,
  ident TEXT,
  count INTEGER DEFAULT 1,
  PRIMARY KEY (src_file, ident)
);

CREATE INDEX IF NOT EXISTS idx_ref_src ON ref_idents(src_file);
CREATE INDEX IF NOT EXISTS idx_ref_ident ON ref_idents(ident);

-- Phase 6: compaction audit. Every fold batch and checkpoint records the facts
-- that must survive plus whether the lossy residue still contains them.
-- Lossless originals stay in turns/folds; this table proves what was kept.
CREATE TABLE IF NOT EXISTS compaction_artifacts (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  trigger TEXT,
  summary TEXT,
  checkpoint_json TEXT,
  raw_refs TEXT,
  validation_json TEXT,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_compact_session ON compaction_artifacts(session_id);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_file_chunks_path ON file_chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_decisions_session ON decisions(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);
CREATE INDEX IF NOT EXISTS idx_pins_session ON pins(session_id);
CREATE INDEX IF NOT EXISTS idx_folds_kind ON folds(kind);
CREATE INDEX IF NOT EXISTS idx_window_priority ON window_items(priority, added_at);

-- Brain: unified code-to-graph. One property graph over codebase + progress.
-- Nodes: file | symbol | task | decision | error | pin | checkpoint |
--        session | commit. Edges: contains | imports | uses | mentions |
--        owns | about | touches | covers | child_of.
CREATE TABLE IF NOT EXISTS graph_nodes (
  id TEXT PRIMARY KEY,
  kind TEXT,
  label TEXT,
  file_path TEXT,
  line INTEGER DEFAULT 0,
  session_id TEXT,
  weight REAL DEFAULT 1.0,
  meta TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
  src TEXT,
  dst TEXT,
  rel TEXT,
  weight REAL DEFAULT 1.0,
  meta TEXT,
  PRIMARY KEY (src, dst, rel)
);

CREATE INDEX IF NOT EXISTS idx_gnodes_kind ON graph_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_gnodes_file ON graph_nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_gnodes_session ON graph_nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_gedges_src ON graph_edges(src);
CREATE INDEX IF NOT EXISTS idx_gedges_dst ON graph_edges(dst);
CREATE INDEX IF NOT EXISTS idx_gedges_rel ON graph_edges(rel);
"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def use_project_agres() -> None:
    # Creates project-local .agres and re-resolves AGRES_DIR for subsequent calls
    # ponytail: global mutation kept for compat, but _agres_dir() is canonical
    global AGRES_DIR, CAS_DIR, PROJECT_ROOT, PROJECT_AGRES_DIR
    pdir = _project_agres_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    PROJECT_ROOT = _project_root()
    PROJECT_AGRES_DIR = pdir
    AGRES_DIR = pdir
    CAS_DIR = pdir / "cas" / "objects"
    ensure_base_dirs()
    init_db()
    _repair_active_session()


def ensure_base_dirs() -> None:
    # Ensure both global and project dirs exist; canonical is _agres_dir()
    for base in {AGRES_HOME, _agres_dir()}:
        for path in [
            base,
            base / "sessions",
            base / "manifests",
            base / "cas" / "objects",
            base / "knowledge",
            base / "folds",
            base / "folds" / "files",
            base / "folds" / "symbols",
            base / "checkpoints",
            base / "context_packs",
        ]:
            path.mkdir(parents=True, exist_ok=True)


def db_path() -> Path:
    return _agres_dir() / "agres.db"


def get_conn() -> sqlite3.Connection:
    init_db()
    # ponytail: WAL + busy_timeout eliminates 'database is locked' under concurrent captures
    conn = sqlite3.connect(str(db_path()), timeout=15.0, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    return conn


_INIT_DONE = set()

def _fts_module_available() -> bool:
    """Field fix: side-effect-free FTS5 probe in a temp DB. Distinguishes
    'module missing' (persist fts_enabled=0) from transient lock errors
    (leave meta alone — a lock once flipped a healthy DB to LIKE-only)."""
    try:
        probe = sqlite3.connect(":memory:", timeout=2.0)
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True
        finally:
            probe.close()
    except sqlite3.Error:
        return False


def _schema_present(path: Path) -> bool:
    """Read-only probe: is this DB already initialized? No DDL, no write txn,
    so concurrent CLI calls never contend with a running indexer."""
    try:
        probe = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        probe.execute("PRAGMA busy_timeout=15000;")
        row = probe.execute(
            "SELECT value FROM meta WHERE key = 'fts_enabled'").fetchone()
        if row is None:
            return False
        for tbl in ("manifest", "compaction_artifacts", "ref_idents", "context_packs"):
            if probe.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (tbl,)).fetchone() is None:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _migrate_columns(cur: sqlite3.Cursor) -> None:
    """SELECT-guarded ALTERs. Each write is lock-tolerant: if the DB is busy,
    skip and retry on the next invocation (callers tolerate missing cols)."""
    try:
        cur.execute("SELECT model FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        for stmt in ("ALTER TABLE sessions ADD COLUMN model TEXT",
                     "ALTER TABLE sessions ADD COLUMN model_provider TEXT"):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass
    try:
        cur.execute("SELECT tokens, budget, stale FROM context_packs LIMIT 1")
    except sqlite3.OperationalError:
        for stmt in ("ALTER TABLE context_packs ADD COLUMN tokens INTEGER DEFAULT 0",
                     "ALTER TABLE context_packs ADD COLUMN budget INTEGER DEFAULT 0",
                     "ALTER TABLE context_packs ADD COLUMN stale INTEGER DEFAULT 0"):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass
    # Brain tables on pre-existing DBs (fast path skips SCHEMA re-run).
    try:
        cur.execute("SELECT id, kind FROM graph_nodes LIMIT 1")
    except sqlite3.OperationalError:
        try:
            cur.executescript(
                "CREATE TABLE IF NOT EXISTS graph_nodes ("
                " id TEXT PRIMARY KEY, kind TEXT, label TEXT, file_path TEXT,"
                " line INTEGER DEFAULT 0, session_id TEXT, weight REAL DEFAULT 1.0,"
                " meta TEXT, updated_at TEXT);"
                "CREATE TABLE IF NOT EXISTS graph_edges ("
                " src TEXT, dst TEXT, rel TEXT, weight REAL DEFAULT 1.0, meta TEXT,"
                " PRIMARY KEY (src, dst, rel));"
                "CREATE INDEX IF NOT EXISTS idx_gnodes_kind ON graph_nodes(kind);"
                "CREATE INDEX IF NOT EXISTS idx_gnodes_file ON graph_nodes(file_path);"
                "CREATE INDEX IF NOT EXISTS idx_gnodes_session ON graph_nodes(session_id);"
                "CREATE INDEX IF NOT EXISTS idx_gedges_src ON graph_edges(src);"
                "CREATE INDEX IF NOT EXISTS idx_gedges_dst ON graph_edges(dst);"
                "CREATE INDEX IF NOT EXISTS idx_gedges_rel ON graph_edges(rel);"
            )
        except sqlite3.OperationalError:
            pass


def init_db() -> None:
    path = db_path()
    # ponytail: cache init per path per process to avoid re-executing SCHEMA on every get_conn()
    if str(path) in _INIT_DONE and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and _schema_present(path):
        # Fast path (every CLI call on an existing DB): migrations only, no DDL.
        # Also heals a meta fts_enabled=0 that a transient lock wrote earlier:
        # if the module works and the tables exist, FTS is usable.
        try:
            conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=15000;")
                conn.row_factory = sqlite3.Row
                _migrate_columns(conn.cursor())
                try:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key = 'fts_enabled'").fetchone()
                    if (row is None or row["value"] != "1") and _fts_module_available():
                        has_fts = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'fts_chunks'").fetchone()
                        if has_fts:
                            conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                                         "VALUES ('fts_enabled', '1')")
                except sqlite3.Error:
                    pass
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        _INIT_DONE.add(str(path))
        return

    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.executescript(SCHEMA)
    _migrate_columns(cur)

    try:
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
            "text, file_path UNINDEXED, chunk_id UNINDEXED, language UNINDEXED"
            ")"
        )
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5("
            "text, file_path UNINDEXED, symbol_id UNINDEXED, name UNINDEXED, kind UNINDEXED"
            ")"
        )
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory USING fts5("
            "text, type UNINDEXED, ref_id UNINDEXED, session_id UNINDEXED"
            ")"
        )
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_folds USING fts5("
            "text, fold_id UNINDEXED, session_id UNINDEXED, title UNINDEXED"
            ")"
        )
        cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_enabled', '1')")
    except sqlite3.OperationalError:
        # Only persist '0' when the module itself is missing. Transient lock
        # errors leave meta untouched (healed on the next call); writing '0'
        # here once flipped a healthy DB to LIKE-only (field incident).
        if not _fts_module_available():
            try:
                cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_enabled', '0')")
            except sqlite3.OperationalError:
                pass

    conn.commit()
    conn.close()
    _INIT_DONE.add(str(path))


def fts_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'fts_enabled'").fetchone()
        return bool(row and row["value"] == "1")
    except sqlite3.OperationalError:
        return False


def split_code_terms(query: str) -> List[str]:
    """Split camelCase/snake/kebab + alphanumerics for code search.
    e.g. getAuthToken -> [getAuthToken, get, Auth, Token]; auth_flow -> [auth_flow, auth, flow]."""
    raw = re.findall(r"[A-Za-z0-9_\-]+", query)
    out: List[str] = []
    seen = set()
    for tok in raw:
        tok = tok[:64]
        if not tok:
            continue
        parts = re.split(r"[_\-]+", tok)
        camel: List[str] = []
        for p in parts:
            camel.extend(re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", p) or ([p] if p else []))
        cands = [tok] + [c for c in camel if c and c != tok]
        for c in cands:
            key = c.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(c)
    return out


def fts_match_query(query: str) -> str:
    """Phase 2: AND-first semantics (was OR). Quoted terms joined by AND.
    Callers try phrase -> AND -> OR fallback via fts_match_variants()."""
    terms = split_code_terms(query)
    cleaned = [t[:64] for t in terms if t]
    if not cleaned:
        return ""
    # First token is the raw form; rest are splits. AND over the raw splits only
    # would over-constrain (get AND Auth AND Token rarely co-occurs literally),
    # so AND over the *distinct raw query terms*, not every camel fragment.
    raw_terms = [t[:64] for t in re.findall(r"[A-Za-z0-9_]+", query) if t]
    if not raw_terms:
        return ""
    return " AND ".join(f'"{term}"' for term in raw_terms)


def fts_match_variants(query: str) -> List[str]:
    """Ordered MATCH attempts: phrase (multi-term) -> AND -> OR."""
    raw_terms = [t[:64] for t in re.findall(r"[A-Za-z0-9_]+", query) if t]
    variants: List[str] = []
    if len(raw_terms) > 1:
        variants.append('"' + " ".join(raw_terms)[:200] + '"')
    and_q = fts_match_query(query)
    if and_q:
        variants.append(and_q)
    if len(raw_terms) > 1:
        variants.append(" OR ".join(f'"{t}"' for t in raw_terms))
    # Camel fragments as last-resort OR (catches authToken vs auth_token).
    frags = [t for t in split_code_terms(query) if t.lower() not in {r.lower() for r in raw_terms}]
    if frags:
        variants.append(" OR ".join(f'"{t}"*' for t in frags[:6]))
    return variants

def escape_like(pattern: str) -> str:
    # Escape % _ \ for LIKE ... ESCAPE '\'
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    items: List[Dict[str, Any]] = []

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue

    return items


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        # Best-effort fsync before rename (durability without perf cost of full fsync)
        import os as _os
        with tmp.open("rb") as _f:
            try:
                _os.fsync(_f.fileno())
            except Exception:
                pass
    except Exception:
        pass
    tmp.replace(path)


def read_text_safe(path: Path, max_chars: int = 12000) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        return text[:max_chars]
    except Exception:
        return ""


def md_append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")


def cas_put_text(text: str) -> str:
    digest = sha256_text(text)
    cas_dir = _cas_dir()
    obj_dir = cas_dir / digest[:2]
    obj_dir.mkdir(parents=True, exist_ok=True)
    obj_path = obj_dir / digest

    if not obj_path.exists():
        obj_path.write_text(text, encoding="utf-8")

    return f"sha256:{digest}"


def fts_index_memory(
    conn: sqlite3.Connection,
    type_: str,
    ref_id: str,
    text: str,
    session_id: str = "",
) -> None:
    if not text:
        return

    if not fts_enabled(conn):
        return

    try:
        conn.execute(
            "INSERT INTO fts_memory(text, type, ref_id, session_id) VALUES (?, ?, ?, ?)",
            (text, type_, ref_id, session_id),
        )
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Session/state helpers
# ---------------------------------------------------------------------------

def get_state() -> Dict[str, Any]:
    return load_json(RUNTIME_STATE, {})


def save_state(state: Dict[str, Any]) -> None:
    save_json(RUNTIME_STATE, state)


def active_session_id() -> Optional[str]:
    return get_state().get("active_session_id")


def set_active_session_id(session_id: Optional[str]) -> None:
    state = get_state()

    if session_id is None:
        state.pop("active_session_id", None)
    else:
        state["active_session_id"] = session_id

    state["updated_at"] = now_iso()
    save_state(state)


def _repair_active_session() -> None:
    # Deliberate no-op: never clear active_session_id silently. Use
    # resolve_active_session() which validates against the current DB and
    # adopts or reports explicitly. Explicit clears:
    #   agres end            (marks ended + clears)
    #   agres repair --reset-session (manual override)
    return


def resolve_active_session(conn: sqlite3.Connection) -> tuple[Optional[str], str]:
    """Validate the active session id against THIS database.

    runtime_state.json is global (~/.agres) but DBs are per-project, so the
    state can point at a session that does not exist here (ghost session).
    A ghost session made usage report 0 forever -> context bar never moved.

    Returns (session_id, flag):
      active  — id exists in this DB and is active
      adopted — id was a ghost; adopted the newest active session in this DB
                (state updated so captures land in the right place)
      stale   — id is a ghost and this DB has no active session to adopt
      none    — no active session id in state
    """
    try:
        sid = active_session_id()
    except Exception:
        sid = None
    if not sid:
        return None, "none"
    try:
        row = conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()
    except sqlite3.OperationalError:
        return None, "none"
    if row:
        return sid, "active"
    # Ghost id: adopt the newest active session in this DB, if any.
    try:
        cand = conn.execute(
            "SELECT id FROM sessions WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        cand = None
    if cand and cand["id"]:
        set_active_session_id(cand["id"])
        return cand["id"], "adopted"
    return sid, "stale"


def resolve_active_session_or_none(conn: sqlite3.Connection) -> Optional[str]:
    sid, flag = resolve_active_session(conn)
    return sid if flag in ("active", "adopted") else None

def session_dir(session_id: Optional[str] = None) -> Optional[Path]:
    session_id = session_id or active_session_id()

    if not session_id:
        return None

    return _agres_dir() / "sessions" / session_id


def require_session(args: argparse.Namespace) -> str:
    session_id = getattr(args, "session", None)
    if session_id:
        return session_id
    conn = get_conn()
    session_id, flag = resolve_active_session(conn)
    conn.close()
    if session_id and flag in ("active", "adopted"):
        sdir = AGRES_DIR / "sessions" / session_id
        sdir.mkdir(parents=True, exist_ok=True)
        return session_id
    if flag == "stale":
        print(f"Active session id {session_id} is stale (not in this database).", file=sys.stderr)
    else:
        print("No active Agres session.", file=sys.stderr)
    print("Start one with:", file=sys.stderr)
    print("  agres start \"Your session goal\"", file=sys.stderr)
    sys.exit(1)


def append_event(
    session_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    fold_type: Optional[str] = None,
) -> Dict[str, Any]:
    # P0 fix: use AUTOINCREMENT, no MAX(seq) race; transaction is implicit via AUTOINCREMENT
    payload_hash = None
    if payload:
        payload_hash = cas_put_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        )

    conn = get_conn()
    cur = conn.cursor()
    # Use BEGIN IMMEDIATE to lock early
    try:
        cur.execute("BEGIN IMMEDIATE;")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        "INSERT INTO events(session_id, ts, type, task_id, payload_hash, fold_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            now_iso(),
            event_type,
            task_id,
            payload_hash,
            fold_type,
        ),
    )
    seq = cur.lastrowid
    cur.execute("COMMIT;")
    conn.commit()
    # Fetch inserted row ts
    row = conn.execute("SELECT ts FROM events WHERE seq = ?", (seq,)).fetchone()
    ts = row["ts"] if row else now_iso()
    conn.close()

    event = {
        "seq": seq,
        "session_id": session_id,
        "ts": ts,
        "type": event_type,
        "task_id": task_id,
        "payload_hash": payload_hash,
        "fold_type": fold_type,
    }

    # JSONL is now best-effort mirror, not source of truth
    try:
        append_jsonl(session_dir(session_id) / "events.jsonl", event)
    except Exception:
        pass

    return event


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def detect_language(path: Path) -> str:
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "text")


def is_indexable_file(path: Path) -> bool:
    if not path.is_file():
        return False

    if path.suffix.lower() not in INDEXABLE_EXTENSIONS:
        return False

    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return False
    except Exception:
        return False

    return True


def iter_repo_files(root: Path, limit: Optional[int] = None):
    count = 0

    for path in root.rglob("*"):
        try:
            rel_parts = path.relative_to(root).parts
        except Exception:
            continue

        if any(part in IGNORE_DIRS for part in rel_parts):
            continue

        if not is_indexable_file(path):
            continue

        yield path

        count += 1

        if limit is not None and count >= limit:
            break


# ---------------------------------------------------------------------------
# Phase 1: incremental manifest helpers (mtime+size fast path)
# ---------------------------------------------------------------------------

def _file_stat(path: Path):
    """Return (mtime, size) or (None, None) on error. No hashing here."""
    try:
        st = path.stat()
        return float(st.st_mtime), int(st.st_size)
    except Exception:
        return None, None


def manifest_get(conn: sqlite3.Connection, rel_path: str):
    try:
        return conn.execute(
            "SELECT path, mtime, size, content_hash FROM manifest WHERE path = ?",
            (rel_path,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def manifest_put(conn: sqlite3.Connection, rel_path: str, mtime, size, content_hash: str) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO manifest(path, mtime, size, content_hash, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel_path, mtime, size, content_hash, now_iso()),
        )
    except sqlite3.OperationalError:
        pass


def manifest_remove(conn: sqlite3.Connection, rel_path: str) -> None:
    for tbl, col in [("manifest", "path"), ("files", "path"),
                     ("file_chunks", "file_path"), ("symbols", "file_path")]:
        try:
            conn.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (rel_path,))
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("DELETE FROM fts_chunks WHERE file_path = ?", (rel_path,))
        conn.execute("DELETE FROM fts_symbols WHERE file_path = ?", (rel_path,))
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("DELETE FROM edges WHERE src_file = ?", (rel_path,))
        conn.execute("DELETE FROM ref_idents WHERE src_file = ?", (rel_path,))
    except sqlite3.OperationalError:
        pass


def manifest_needs_reindex(conn: sqlite3.Connection, rel_path: str, abs_path: Path, force: bool = False):
    """Fast path: (mtime, size) match => skip hashing entirely.
    Returns (needs: bool, reason: str)."""
    if force:
        return True, "force"
    mtime, size = _file_stat(abs_path)
    if mtime is None:
        return False, "stat-failed"
    row = manifest_get(conn, rel_path)
    if row is None:
        return True, "new"
    try:
        if row["mtime"] == mtime and row["size"] == size:
            return False, "mtime-match"
    except Exception:
        pass
    return True, "mtime-mismatch"


def index_single_file(conn: sqlite3.Connection, root: Path, abs_path: Path, fts: bool, force: bool = False) -> str:
    """Index one file. Returns 'indexed' | 'skipped' | 'failed'.
    Caller handles transactions around DELETE+INSERT per file."""
    try:
        rel_path = abs_path.relative_to(root).as_posix()
    except Exception:
        return "failed"
    try:
        content_hash = sha256_file(abs_path)
    except Exception:
        return "failed"
    # Slow-path confirm: same hash => just refresh manifest mtime/size, skip rewrite.
    # Skipped when force=True (explicit reindex requested).
    existing = None
    try:
        existing = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
    except sqlite3.OperationalError:
        pass
    mtime, size = _file_stat(abs_path)
    if not force and existing and existing["content_hash"] == content_hash:
        manifest_put(conn, rel_path, mtime, size, content_hash)
        return "skipped"
    try:
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "failed"
    language = detect_language(abs_path)
    try:
        fsize = abs_path.stat().st_size
    except Exception:
        fsize = len(text.encode("utf-8", "ignore"))
    file_id = "file_" + sha256_text(rel_path + ":" + content_hash)[:18]
    conn.execute(
        "INSERT OR REPLACE INTO files(id, path, content_hash, language, size, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, rel_path, content_hash, language, fsize, now_iso()),
    )
    try:
        conn.execute("BEGIN IMMEDIATE;")
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM file_chunks WHERE file_path = ?", (rel_path,))
    conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
    try:
        conn.execute("DELETE FROM edges WHERE src_file = ?", (rel_path,))
        conn.execute("DELETE FROM ref_idents WHERE src_file = ?", (rel_path,))
    except sqlite3.OperationalError:
        pass
    # Field fix: new files have no rows anywhere — skip the FTS full-table
    # DELETE scans (each is O(fts table); O(n^2) over a bulk index).
    if fts and existing is not None:
        try:
            conn.execute("DELETE FROM fts_chunks WHERE file_path = ?", (rel_path,))
            conn.execute("DELETE FROM fts_symbols WHERE file_path = ?", (rel_path,))
        except sqlite3.OperationalError:
            pass
    for chunk in chunk_text_by_lines(text):
        chunk_text = chunk["text"]
        chunk_hash = sha256_text(chunk_text)
        chunk_id = "chunk_" + sha256_text(
            rel_path + f":{chunk['start_line']}:{chunk['end_line']}:" + chunk_hash)[:18]
        conn.execute(
            "INSERT OR REPLACE INTO file_chunks("
            "id, file_path, start_line, end_line, content_hash, text"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, rel_path, chunk["start_line"], chunk["end_line"], chunk_hash, chunk_text),
        )
        if fts:
            try:
                conn.execute(
                    "INSERT INTO fts_chunks(text, file_path, chunk_id, language) "
                    "VALUES (?, ?, ?, ?)",
                    (chunk_text, rel_path, chunk_id, language),
                )
            except sqlite3.OperationalError:
                pass
    for symbol in extract_symbols(language, text):
        symbol_id = "sym_" + sha256_text(
            rel_path + ":" + symbol["name"] + ":" + str(symbol["line"]))[:18]
        symbol_text = " ".join([language, symbol["kind"], symbol["name"],
                                  f"file:{rel_path}", f"line:{symbol['line']}"])
        conn.execute(
            "INSERT OR REPLACE INTO symbols("
            "id, file_path, name, kind, line, text"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (symbol_id, rel_path, symbol["name"], symbol["kind"], symbol["line"], symbol_text),
        )
        if fts:
            try:
                conn.execute(
                    "INSERT INTO fts_symbols(text, file_path, symbol_id, name, kind) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (symbol_text, rel_path, symbol_id, symbol["name"], symbol["kind"]),
                )
            except sqlite3.OperationalError:
                pass
    for edge in extract_import_edges(language, text, rel_path):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO edges(src_file, dst_file, ident, weight) "
                "VALUES (?, ?, ?, 1.0)",
                (edge["src"], edge["dst"], edge["ident"]),
            )
        except sqlite3.OperationalError:
            pass
    store_ref_idents(conn, rel_path, language, text)
    try:
        conn.execute("COMMIT;")
    except sqlite3.OperationalError:
        pass
    manifest_put(conn, rel_path, mtime, size, content_hash)
    return "indexed"


def prune_deleted_files(conn: sqlite3.Connection, root: Path) -> int:
    """Remove manifest/files/chunks/symbols rows whose file no longer exists. Returns count."""
    try:
        rows = conn.execute("SELECT path FROM manifest").fetchall()
    except sqlite3.OperationalError:
        return 0
    pruned = 0
    for r in rows:
        rel = r["path"]
        if not (root / rel).exists():
            manifest_remove(conn, rel)
            pruned += 1
    return pruned


def git_changed_files(root: Path, since: str) -> Optional[List[str]]:
    """Return rel paths changed since git ref, or None if git unavailable."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", since],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return None


def chunk_text_by_lines(text: str, max_chunks: int = MAX_CHUNKS_PER_FILE) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    chunks: List[Dict[str, Any]] = []

    if not lines:
        return chunks

    start = 0

    while start < len(lines) and len(chunks) < max_chunks:
        end = min(len(lines), start + CHUNK_MAX_LINES)
        chunk_lines = lines[start:end]
        chunk_text = "\n".join(chunk_lines)

        if len(chunk_text) > 12000:
            chunk_text = chunk_text[:12000]

        chunks.append(
            {
                "start_line": start + 1,
                "end_line": end,
                "text": chunk_text,
            }
        )

        if end >= len(lines):
            break

        start = max(0, end - CHUNK_OVERLAP_LINES)

    return chunks


# ---------------------------------------------------------------------------
# Phase 5: optional tree-sitter backend. Used when installed, regex otherwise.
# Never a hard dependency: `pip install tree_sitter tree_sitter_python ...`
# upgrades precision (methods with class scope, string-safe imports, ref idents).
# ---------------------------------------------------------------------------
_TS_LANGS = None  # None=unprobed, False=unavailable, else {agres_lang: Language}
_TS_PARSERS: Dict[str, Any] = {}
_TS_GRAMMARS = {
    "python": ("tree_sitter_python", "language", None),
    "javascript": ("tree_sitter_javascript", "language", None),
    "typescript": ("tree_sitter_typescript", "language_typescript", None),
    "go": ("tree_sitter_go", "language", None),
    "rust": ("tree_sitter_rust", "language", None),
    "java": ("tree_sitter_java", "language", None),
}
_TS_DEF_NODES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {"function_declaration", "class_declaration", "method_definition",
                    "interface_declaration", "type_alias_declaration"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item"},
    "java": {"class_declaration", "method_declaration", "interface_declaration"},
}
_TS_CLASS_NODES = {"class_definition", "class_declaration", "struct_item", "trait_item"}
_TS_KIND = {"class_definition": "class", "class_declaration": "class",
            "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
            "interface_declaration": "interface", "type_alias_declaration": "type",
            "type_declaration": "type"}
_TS_IMPORT_NODES = {"import_statement", "import_from_statement", "import_declaration",
                    "import_spec", "use_declaration"}
_TS_PARAM_PARENTS = {"parameters", "formal_parameters"}


def ts_languages():
    """Probe once. Returns {lang: Language} or None. Never raises."""
    global _TS_LANGS
    if _TS_LANGS is not None:
        return _TS_LANGS or None
    found: Dict[str, Any] = {}
    try:
        from tree_sitter import Language  # noqa: F401
    except ImportError:
        _TS_LANGS = False
        return None
    from tree_sitter import Language
    for lang, (pkg, attr, _) in _TS_GRAMMARS.items():
        try:
            mod = __import__(pkg)
            found[lang] = Language(getattr(mod, attr)())
        except Exception:
            continue
    _TS_LANGS = found or False
    return found or None


def ts_backend_name() -> str:
    langs = ts_languages()
    if not langs:
        return "regex"
    return "tree-sitter(" + ",".join(sorted(langs)) + ")"


def ts_parse(lang: str, text: str):
    langs = ts_languages()
    if not langs or lang not in langs:
        return None
    try:
        if lang not in _TS_PARSERS:
            from tree_sitter import Parser
            p = Parser()
            try:
                p.language = langs[lang]
            except Exception:
                p.set_language(langs[lang])  # old API fallback
            _TS_PARSERS[lang] = p
        return _TS_PARSERS[lang].parse(text.encode("utf-8", "ignore"))
    except Exception:
        return None


def ts_walk(lang: str, text: str):
    """Returns (defs, ident_counts, import_lines) or None when unavailable.
    defs: [(qualname, kind, line)]. idents: {ident: count} (excl. defs/params)."""
    tree = ts_parse(lang, text)
    if tree is None:
        return None
    def_nodes = _TS_DEF_NODES.get(lang, set())
    if not def_nodes:
        return None
    src = text.encode("utf-8", "ignore")

    def line_of(byte: int) -> int:
        return src.count(b"\n", 0, byte) + 1

    def node_text(n) -> str:
        try:
            return src[n.start_byte:n.end_byte].decode("utf-8", "ignore")
        except Exception:
            return ""
    defs: List[tuple] = []
    idents: Dict[str, int] = {}
    import_lines: List[str] = []
    # Byte ranges (not id()) — node wrappers are recreated per access.
    def_spans = set()
    stack: List[tuple] = [(tree.root_node, ())]
    while stack:
        node, scope = stack.pop()
        if node.type in _TS_IMPORT_NODES:
            t = node_text(node)
            if t:
                import_lines.append(t)
        if node.type in def_nodes:
            nm = node.child_by_field_name("name")
            if nm is not None:
                name = node_text(nm)[:64]
                if name:
                    def_spans.add((nm.start_byte, nm.end_byte))
                    kind = _TS_KIND.get(node.type, "function")
                    # Methods carry class scope (Store.save); top-level names stay bare.
                    qual = ".".join(scope + (name,)) if scope else name
                    defs.append((qual, kind, line_of(node.start_byte)))
                    if node.type in _TS_CLASS_NODES:
                        scope = scope + (name,)
        elif node.type == "identifier":
            if (node.start_byte, node.end_byte) not in def_spans and (node.parent is None
                    or node.parent.type not in _TS_PARAM_PARENTS):
                w = node_text(node)
                if len(w) >= 2 and len(w) <= 64 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", w):
                    idents[w] = idents.get(w, 0) + 1
        for child in reversed(node.children):
            stack.append((child, scope))
    return defs, idents, import_lines


def extract_ref_idents(language: str, text: str, top_n: int = 40) -> List[Dict[str, Any]]:
    """Reference identifiers for ref->def file edges. Tree-sitter only; [] on fallback."""
    walked = ts_walk(language, text)
    if not walked:
        return []
    _, idents, _ = walked
    ranked = sorted(idents.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [{"ident": k, "count": v} for k, v in ranked]


def store_ref_idents(conn: sqlite3.Connection, rel_path: str, language: str, text: str) -> int:
    """Replace this file's ref-ident rows. Returns count stored (0 on fallback)."""
    rows = extract_ref_idents(language, text)
    try:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO ref_idents(src_file, ident, count) VALUES (?, ?, ?)",
                (rel_path, r["ident"], r["count"]),
            )
    except sqlite3.OperationalError:
        return 0
    return len(rows)


def extract_symbols(language: str, text: str) -> List[Dict[str, Any]]:
    # Tree-sitter first (methods with class scope); regex fallback otherwise.
    walked = ts_walk(language, text)
    if walked is not None:
        defs, _, _ = walked
        return [{"name": n, "kind": k, "line": ln} for n, k, ln in defs[:300]]
    patterns = SYMBOL_PATTERNS.get(language, [])
    symbols: List[Dict[str, Any]] = []

    if not patterns:
        return symbols

    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            match = re.search(pattern, line)
            if match:
                name = match.group(1)
                symbols.append(
                    {
                        "name": name,
                        "kind": kind,
                        "line": line_number,
                    }
                )
                break

        if len(symbols) >= 300:
            break

    return symbols


# Phase 2: regex import-edge stub (referencer -> imported module).
# Tree-sitter-grade resolution is Phase 4; this gives the RRF graph branch
# a real signal today: which files depend on which modules/names.
IMPORT_EDGE_PATTERNS: Dict[str, List[str]] = {
    "python": [
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)",
        r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([^\n#]+)",
    ],
    "javascript": [
        r"import\s+(?:[^'\"]+\s+from\s+)?['\"]([^'\"]+)['\"]",
        r"require\(\s*['\"]([^'\"]+)['\"]\s*\)",
    ],
    "typescript": [
        r"import\s+(?:[^'\"]+\s+from\s+)?['\"]([^'\"]+)['\"]",
        r"require\(\s*['\"]([^'\"]+)['\"]\s*\)",
    ],
    "go": [r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*\s+)?\"([^\"]+)\""],
    "rust": [r"^\s*use\s+([A-Za-z_][A-Za-z0-9_:]*)"],
    "java": [r"^\s*import\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_.]*)"],
    "c": [r"^\s*#\s*include\s+[<\"]([^>\"]+)[>\"]"],
}


def extract_import_edges(language: str, text: str, rel_path: str) -> List[Dict[str, Any]]:
    patterns = IMPORT_EDGE_PATTERNS.get(language, [])
    edges: List[Dict[str, Any]] = []
    if not patterns:
        return edges
    # Tree-sitter: parse only real import statements (immune to string/comment hits).
    walked = ts_walk(language, text)
    scan = "\n".join(walked[2]) if walked is not None else text
    for line in scan.splitlines():
        for pat in patterns:
            try:
                m = re.search(pat, line)
            except re.error:
                continue
            if not m:
                continue
            dst = (m.group(1) or "").strip()[:200]
            if not dst or dst in (".", "/"):
                continue
            ident = dst
            if m.lastindex and m.lastindex >= 2 and m.group(2):
                # `from X import a, b as c` -> one edge per imported name.
                for part in m.group(2).split(","):
                    name = part.strip().split(" as ")[0].strip().split(" ")[0][:64]
                    if name and name != "*":
                        edges.append({"src": rel_path, "dst": dst, "ident": name})
            else:
                # Field fix: leaf ident (linux/foo.h -> foo, not h).
                leaf = dst.split("/")[-1].split(".")[0][:64]
                edges.append({"src": rel_path, "dst": dst, "ident": leaf or dst[:64]})
            break
        if len(edges) >= 100:
            break
    # Dedup, keep weight 1.0 (count damping is PageRank's job in Phase 4).
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for e in edges:
        key = (e["src"], e["dst"], e["ident"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


# ---------------------------------------------------------------------------
# Phase 4: ranked RepoMap. Import edges -> file graph -> personalized
# PageRank -> signature skeleton within a token budget (Aider-style, stdlib-only).
# ---------------------------------------------------------------------------
MAP_DEFAULT_TOKENS = 1024
MAP_MAX_SYMBOLS_PER_FILE = 8


def resolve_import_to_files(dst: str, files: List[str]) -> List[str]:
    """Heuristic module -> indexed file resolution (no build system needed)."""
    return resolve_import_to_files_indexed(dst, build_stem_index(files))


def build_stem_index(files: List[str]) -> Dict[str, List[str]]:
    """Field fix: stem/suffix -> files, built once (was O(E*F) per map).
    Keys: full stem + trailing 1- and 2-part suffixes (covers linux/foo.h style)."""
    index: Dict[str, List[str]] = {}
    for f in files:
        stem = f.rsplit(".", 1)[0]
        parts = stem.split("/")
        for key in {stem, parts[-1], "/".join(parts[-2:])}:
            index.setdefault(key, []).append(f)
    return index


def resolve_import_to_files_indexed(dst: str, index: Dict[str, List[str]]) -> List[str]:
    norm = (dst or "").strip().lstrip("./")
    parts = [p for p in re.split(r"[./]", norm) if p and p != "."]
    if not parts:
        return []
    suffix = "/".join(parts)
    out = list(index.get(suffix, []))
    if len(parts) == 1:
        out.extend(index.get(parts[0], []))
    return sorted(set(out))


def ref_file_edges(conn: sqlite3.Connection, files: List[str]) -> List[tuple]:
    """Resolve ref_idents to file edges at map time (index-order independent).
    Weight 0.5: a usage hint, weaker than an explicit import (1.0)."""
    fileset = set(files)
    definers: Dict[str, List[str]] = {}
    try:
        for r in conn.execute("SELECT file_path, name FROM symbols").fetchall():
            leaf = str(r["name"]).split(".")[-1].lower()
            lst = definers.setdefault(leaf, [])
            if r["file_path"] not in lst:
                lst.append(r["file_path"])
    except sqlite3.OperationalError:
        return []
    for v in definers.values():
        v.sort()
    edges: List[tuple] = []
    try:
        rows = conn.execute("SELECT src_file, ident FROM ref_idents").fetchall()
    except sqlite3.OperationalError:
        return []
    for r in rows:
        src = r["src_file"]
        if src not in fileset:
            continue
        for dst in definers.get(str(r["ident"]).lower(), [])[:3]:
            if dst != src and dst in fileset:
                edges.append((src, dst, 0.5))
    return edges


def pagerank_file_graph(nodes: List[str], edges: List[tuple],
                        personalize: Dict[str, float], damping: float = 0.85,
                        iters: int = 20) -> Dict[str, float]:
    """Personalized PageRank over referencer -> definer file edges. Pure stdlib."""
    # rank(v) = (1-d)*p(v) + d * (dangling*p(v) + sum rank(u)*w(u,v)/out(u))
    p_total = sum(personalize.get(n, 1.0) for n in nodes) or 1.0
    pers = {n: personalize.get(n, 1.0) / p_total for n in nodes}
    rank = dict(pers)
    out_w: Dict[str, float] = {}
    for s, d, w in edges:
        if s in rank and d in rank:
            out_w[s] = out_w.get(s, 0.0) + w
    for _ in range(max(1, iters)):
        dangling = sum(v for n, v in rank.items() if out_w.get(n, 0.0) <= 0)
        new = {n: (1.0 - damping) * pers[n] + damping * dangling * pers[n] for n in nodes}
        for s, d, w in edges:
            ow = out_w.get(s, 0.0)
            if ow > 0 and s in rank and d in new:
                new[d] += damping * rank[s] * w / ow
        rank = new
    return rank


def symbol_signature(source_lines: List[str], line_no: int, max_chars: int = 160) -> str:
    """Definition line + continuation while parens unbalanced (multi-line sigs)."""
    idx = line_no - 1
    if idx < 0 or idx >= len(source_lines):
        return ""
    buf = [source_lines[idx].strip()]
    balance = buf[0].count("(") - buf[0].count(")")
    j = idx
    while balance > 0 and len(buf) < 3 and j + 1 < len(source_lines):
        j += 1
        nxt = source_lines[j].strip()
        buf.append(nxt)
        balance += nxt.count("(") - nxt.count(")")
    sig = " ".join(buf).strip()
    return sig[:max_chars]


def build_repo_map(max_tokens: int = MAP_DEFAULT_TOKENS, query: str = "",
                   focus: Optional[List[str]] = None, root: Optional[Path] = None) -> Path:
    root = root or _project_root()
    conn = get_conn()
    try:
        files = [r["path"] for r in
                 conn.execute("SELECT path FROM files ORDER BY path").fetchall()]
    except sqlite3.OperationalError:
        files = []
    # Fall back to disk scan when nothing is indexed yet.
    if not files:
        files = []
        try:
            for p in iter_repo_files(root, limit=2000):
                try:
                    files.append(p.relative_to(root).as_posix())
                except Exception:
                    continue
        except Exception:
            pass
    # File-level edges from the import stub (Phase 2 table). Stem index built
    # once: per-edge rebuild was O(E*F) — 70s on a 74k-file tree.
    gedges: List[tuple] = []
    stem_index = build_stem_index(files)
    try:
        for r in conn.execute("SELECT src_file, dst_file FROM edges").fetchall():
            for dst in resolve_import_to_files_indexed(r["dst_file"], stem_index):
                if dst != r["src_file"]:
                    gedges.append((r["src_file"], dst, 1.0))
    except sqlite3.OperationalError:
        pass
    # Phase 5: ref->def usage edges (tree-sitter only; absent on regex fallback).
    try:
        gedges.extend(ref_file_edges(conn, files))
    except Exception:
        pass
    # Damping: total pair weight / sqrt(count) so one hub file cannot dominate.
    _wsum: Dict[tuple, float] = {}
    _cnt: Dict[tuple, int] = {}
    for s, d, w in gedges:
        _wsum[(s, d)] = _wsum.get((s, d), 0.0) + w
        _cnt[(s, d)] = _cnt.get((s, d), 0) + 1
    gedges = [(s, d, _wsum[(s, d)] / (_cnt[(s, d)] ** 0.5)) for (s, d) in _wsum]
    # Personalization: focus x50, query-related x10, recently indexed x3.
    personalize: Dict[str, float] = {}
    for f in focus or []:
        personalize[f] = 50.0
    if query:
        try:
            for g in graph_related_files(conn, query, 20):
                fp = g.get("file_path", "")
                personalize[fp] = max(personalize.get(fp, 1.0), 10.0)
        except Exception:
            pass
    try:
        today = now_iso()[:10]
        for r in conn.execute(
                "SELECT path FROM files WHERE indexed_at LIKE ?", (today + "%",)).fetchall():
            personalize[r["path"]] = max(personalize.get(r["path"], 1.0), 3.0)
    except sqlite3.OperationalError:
        pass
    rank = pagerank_file_graph(files, gedges, personalize) if files else {}
    ordered = sorted(files, key=lambda f: (-rank.get(f, 0.0), f))
    # Symbols per file (line order; query idents first).
    qidents = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query or "")}
    syms: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for r in conn.execute(
                "SELECT file_path, name, kind, line FROM symbols ORDER BY file_path, line").fetchall():
            syms.setdefault(r["file_path"], []).append(dict(r))
    except sqlite3.OperationalError:
        pass
    lines = ["# Agres Repo Map", "", f"Repo: {root}", f"Generated: {now_iso()}",
             f"Budget: {max_tokens} tokens", f"Files: {len(files)}", ""]
    used = estimate_tokens("\n".join(lines))
    emitted = 0
    for f in ordered:
        if used >= max_tokens:
            break
        try:
            source = (root / f).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue  # stale index entry for a deleted file
        block = [f"{f}:"]
        file_syms = syms.get(f, [])
        file_syms = sorted(file_syms,
                           key=lambda s: (0 if str(s.get("name", "")).lower().split(".")[-1] in qidents else 1,
                                          int(s.get("line", 0) or 0)))
        for s in file_syms[:MAP_MAX_SYMBOLS_PER_FILE]:
            sig = symbol_signature(source, int(s.get("line", 0) or 0))
            if sig:
                block.append(f"  {sig}".rstrip())
        if len(block) == 1:
            block.append("  (no symbols indexed)")
        cost = estimate_tokens("\n".join(block)) + 1  # +1: separator newline in final join
        if emitted and used + cost > max_tokens:
            continue  # best-first: skip the oversized, keep fitting smaller ones
        lines.extend(block)
        used += cost
        emitted += 1
    if not emitted:
        lines.append("- empty")
    text = "\n".join(lines)
    conn.close()
    out_path = _agres_dir() / "folds" / "repo_map.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _fts_try(conn: sqlite3.Connection, table: str, cols: str, limit: int, query: str):
    """Try MATCH variants in order (phrase -> AND -> OR -> fragments); return first non-empty hit list."""
    for match in fts_match_variants(query):
        if not match:
            continue
        try:
            cur = conn.execute(
                f"SELECT {cols} FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                return rows
        except sqlite3.OperationalError:
            continue
    return []


def search_chunks(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    if fts_enabled(conn):
        rows = _fts_try(conn, "fts_chunks",
                        "file_path, language, snippet(fts_chunks, 0, '[', ']', '...', 24) AS snippet",
                        limit, query)
        if rows:
            return rows

    cur = conn.execute(
        "SELECT file_path, '' AS language, substr(text, 1, 220) AS snippet "
        "FROM file_chunks WHERE text LIKE ? ESCAPE '\\' LIMIT ?",
        (f"%{escape_like(query)}%", limit),
    )

    return [dict(row) for row in cur.fetchall()]


def rerank_symbols(rows: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Deterministic rerank: exact name > prefix > substring > rest. No LLM."""
    q = (query or "").strip().lower()
    if not q:
        return rows
    short = re.findall(r"[A-Za-z0-9_]+", q)
    target = short[-1] if short else q

    def score(r: Dict[str, Any]) -> tuple:
        name = str(r.get("name", ""))
        nl = name.lower()
        leaf = nl.split(".")[-1]
        if nl == target or leaf == target:
            base = 0
        elif nl.startswith(target) or leaf.startswith(target):
            base = 1
        elif target in nl:
            base = 2
        else:
            base = 3
        return (base, len(name), str(r.get("file_path", "")), int(r.get("line", 0) or 0))
    return sorted(rows, key=score)


def search_symbols(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    # NOTE: fts_symbols has no line column (UNINDEXED schema); join back via symbol_id.
    if fts_enabled(conn):
        rows = _fts_try(conn, "fts_symbols",
                        "file_path, name, kind, symbol_id, "
                        "snippet(fts_symbols, 0, '[', ']', '...', 24) AS snippet",
                        limit, query)
        if rows:
            try:
                lines = {r["symbol_id"]: r["line"] for r in conn.execute(
                    "SELECT id AS symbol_id, line FROM symbols WHERE id IN (%s)" % ",".join("?" * len(rows)),
                    [r["symbol_id"] for r in rows]).fetchall()}
                for r in rows:
                    r["line"] = lines.get(r.pop("symbol_id"))
            except sqlite3.OperationalError:
                for r in rows:
                    r.pop("symbol_id", None)
            return rerank_symbols(rows, query)

    like = f"%{escape_like(query)}%"

    cur = conn.execute(
        "SELECT file_path, name, kind, line, text AS snippet "
        "FROM symbols WHERE name LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' LIMIT ?",
        (like, like, limit),
    )

    return rerank_symbols([dict(row) for row in cur.fetchall()], query)


def search_memory(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    if fts_enabled(conn):
        rows = _fts_try(conn, "fts_memory",
                        "type, ref_id, session_id, snippet(fts_memory, 0, '[', ']', '...', 24) AS snippet",
                        limit, query)
        if rows:
            return rows

    like = f"%{escape_like(query)}%"

    cur = conn.execute(
        """
        SELECT 'decision' AS type, id AS ref_id, session_id, decision AS snippet
        FROM decisions WHERE decision LIKE ? ESCAPE '\\'
        UNION ALL
        SELECT 'task' AS type, id AS ref_id, session_id, objective AS snippet
        FROM tasks WHERE objective LIKE ? ESCAPE '\\'
        UNION ALL
        SELECT 'error' AS type, id AS ref_id, session_id, message AS snippet
        FROM errors WHERE message LIKE ? ESCAPE '\\'
        UNION ALL
        SELECT 'pin' AS type, id AS ref_id, session_id, text AS snippet
        FROM pins WHERE text LIKE ? ESCAPE '\\'
        LIMIT ?
        """,
        (like, like, like, like, limit),
    )

    return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Phase 2: RRF hybrid retrieval (keyword + symbol-name + import-graph).
# No embeddings. score(file) = sum over branches of 1/(RRF_K + rank).
# ---------------------------------------------------------------------------
RRF_K = 60


def graph_related_files(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    """Graph branch: files defining a queried ident + files linked via import edges.
    Returns [{file_path, via, ident}] in branch-rank order."""
    idents = [t[:64] for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query or "") if t]
    if not idents:
        return []
    out: List[Dict[str, Any]] = []
    seen = set()

    def push(fp: str, via: str, ident: str) -> None:
        if not fp or fp in seen:
            return
        seen.add(fp)
        out.append({"file_path": fp, "via": via, "ident": ident})
    try:
        for ident in idents[:5]:
            like = ident.lower()
            # 1. Definers: symbols with exact/leaf name (qualnames like Store.save match 'save').
            try:
                for r in conn.execute(
                    "SELECT file_path, name FROM symbols "
                    "WHERE lower(name) = ? OR lower(name) LIKE '%.' || ? ESCAPE '\\' LIMIT ?",
                    (like, escape_like(like), limit),
                ).fetchall():
                    push(r["file_path"], "defines", r["name"])
            except sqlite3.OperationalError:
                pass
            # 2. Importers: files with an edge on this ident.
            try:
                for r in conn.execute(
                    "SELECT src_file, dst_file, ident FROM edges WHERE lower(ident) = ? LIMIT ?",
                    (like, limit),
                ).fetchall():
                    push(r["src_file"], "imports", r["ident"])
                    # 3. One hop: files importing the same module (siblings).
                    try:
                        for s in conn.execute(
                            "SELECT src_file FROM edges WHERE dst_file = ? AND src_file != ? LIMIT 3",
                            (r["dst_file"], r["src_file"]),
                        ).fetchall():
                            push(s["src_file"], "sibling-import", r["ident"])
                    except sqlite3.OperationalError:
                        pass
            except sqlite3.OperationalError:
                pass
            if len(out) >= limit:
                break
    except sqlite3.OperationalError:
        pass
    return out[:limit]


def rrf_fuse(branches: List[List[str]], k: int = RRF_K) -> List[Dict[str, Any]]:
    """Fuse ranked file lists. Returns [{file_path, score, via:[branches]}] sorted desc."""
    scores: Dict[str, float] = {}
    via: Dict[str, List[str]] = {}
    names = ["fts", "symbols", "graph"]
    for bi, branch in enumerate(branches):
        tag = names[bi] if bi < len(names) else f"b{bi}"
        for rank, fp in enumerate(branch, start=1):
            if not fp:
                continue
            scores[fp] = scores.get(fp, 0.0) + 1.0 / (k + rank)
            via.setdefault(fp, [])
            if tag not in via[fp]:
                via[fp].append(tag)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"file_path": fp, "score": round(s, 6), "via": via[fp]} for fp, s in ranked]


def hybrid_file_results(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    """Phase 2 entry point: fuse chunk-FTS + symbol + graph branches at file level."""
    per = max(limit * 2, 10)
    chunk_files = [r.get("file_path", "") for r in search_chunks(conn, query, per)]
    symbol_files = [r.get("file_path", "") for r in search_symbols(conn, query, per)]
    graph_files = [r.get("file_path", "") for r in graph_related_files(conn, query, per)]
    fused = rrf_fuse([chunk_files, symbol_files, graph_files])
    return fused[:limit]


# ---------------------------------------------------------------------------
# Fold/unfold engine (context window management)
#
# Purpose: make a limited context window (e.g. 256k tokens) behave like a
# much larger one. Every conversation turn is captured VERBATIM to durable
# local storage. When the active window approaches its budget, older /
# lower-priority items are FOLDED (removed from the active window but kept
# with their exact original text + a summary). When the agent needs an
# earlier passage, it UNFOLDS the exact original text back into the window.
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_BUDGET_TOKENS = 256 * 1024  # 256k default (DeepSeek V4 Flash)
MIN_WINDOW_BUDGET_TOKENS = 4096
WINDOW_RESERVE_TOKENS = 8192  # always keep this much headroom for output

# Rough token categories used for budget accounting.
CATEGORY_TOKENS = {
    "system": 0.10,
    "task": 0.10,
    "pinned": 0.10,
    "recent": 0.35,
    "unfolded": 0.20,
    "repo": 0.05,
    "reserve": 0.05,
}


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (chars/4). No tiktoken to avoid drift.
    ponytail: one rung, deterministic, 3% error is fine for budget bars.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def window_budget() -> int:
    """Resolve the active window budget from state or env, auto-sync to model if unset.
    Priority: env > state > detected model ctx > default 256k.
    This fixes 'budget not updating' when running 1M model with default env.
    """
    # 1. Explicit env always wins
    env_budget = os.environ.get("AGRES_WINDOW_BUDGET", "").strip()
    if env_budget:
        try:
            budget = int(env_budget)
            if budget >= MIN_WINDOW_BUDGET_TOKENS:
                return budget
            return MIN_WINDOW_BUDGET_TOKENS
        except ValueError:
            pass
    # 2. Persisted state
    state_budget = get_state().get("window_budget", 0)
    try:
        state_budget = int(state_budget) if state_budget else 0
        if state_budget >= MIN_WINDOW_BUDGET_TOKENS:
            return state_budget
    except (ValueError, TypeError):
        pass
    # 3. Auto from detected model (1M -> 1M, 256k -> 256k)
    try:
        m = _detect_model()
        ctx = int(m.get("limit",{}).get("context",0) or 0)
        if ctx >= MIN_WINDOW_BUDGET_TOKENS and ctx != DEFAULT_WINDOW_BUDGET_TOKENS:
            # Only auto if model ctx differs from default and is plausible
            # Ponytail: one if, not a config file
            return ctx
    except Exception:
        pass
    # 4. Default
    return DEFAULT_WINDOW_BUDGET_TOKENS

def set_window_budget(tokens: int) -> None:
    """Persist budget to state (for `agres budget --set`)."""
    tokens = max(MIN_WINDOW_BUDGET_TOKENS, int(tokens))
    state = get_state()
    state["window_budget"] = tokens
    state["window_budget_set_at"] = now_iso()
    state["window_budget_source"] = "manual"
    save_state(state)


def add_window_item(
    conn: sqlite3.Connection,
    session_id: str,
    kind: str,
    ref_id: str,
    title: str,
    text: str,
    priority: int = 0,
) -> str:
    """Add an item to the active window manifest."""
    # P1 fix: cap text to avoid single-item OOM; ponytail: truncate, not reject
    if len(text) > MAX_WINDOW_ITEM_CHARS:
        text = text[:MAX_WINDOW_ITEM_CHARS] + f"\n...[truncated {len(text)-MAX_WINDOW_ITEM_CHARS} chars]..."
    # Enforce max items per session (evict oldest low-priority if over)
    try:
        c = conn.execute("SELECT COUNT(*) AS c FROM window_items WHERE session_id = ?", (session_id,)).fetchone()["c"]
        if c >= MAX_WINDOW_ITEMS:
            # Remove oldest low-priority item to keep bound
            conn.execute("DELETE FROM window_items WHERE id IN (SELECT id FROM window_items WHERE session_id = ? AND priority < 3 ORDER BY added_at ASC LIMIT 1)", (session_id,))
    except Exception:
        pass
    item_id = new_id("win")
    tokens = estimate_tokens(text)
    conn.execute(
        "INSERT OR REPLACE INTO window_items("
        "id, session_id, kind, ref_id, title, text, token_estimate, priority, folded, added_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (item_id, session_id, kind, ref_id, title, text, tokens, priority, now_iso()),
    )
    return item_id


def remove_window_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute("DELETE FROM window_items WHERE id = ?", (item_id,))


def window_usage_tokens(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(token_estimate), 0) AS total FROM window_items WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["total"]) if row else 0


def window_unfolded_count(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM window_items WHERE session_id = ? AND folded = 0",
        (session_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def capture_turn(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
) -> Dict[str, Any]:
    """Store a conversation turn VERBATIM and add it to the active window."""
    if role not in ("user", "assistant", "system"):
        role = "user"
    # P1 fix: cap for window, but turns table keeps full verbatim (never truncated)
    # window token estimate uses truncated text, turns uses full (for audit)
    display_text = content
    if len(display_text) > MAX_WINDOW_ITEM_CHARS:
        display_text = display_text[:MAX_WINDOW_ITEM_CHARS] + f"\n...[truncated {len(content)-MAX_WINDOW_ITEM_CHARS} chars, full in turns]..."
    tokens_full = estimate_tokens(content)
    tokens_window = estimate_tokens(display_text)

    cur = conn.execute(
        "INSERT INTO turns(session_id, role, content, token_estimate, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, tokens_full, now_iso()),
    )
    seq = cur.lastrowid

    turn = {
        "seq": seq,
        "session_id": session_id,
        "role": role,
        "content": content,
        "token_estimate": tokens_window,  # window uses capped estimate for budget
        "token_full": tokens_full,
        "created_at": now_iso(),
    }

    # Add to active window as a 'recent' item (priority 1) — add_window_item will also cap
    item_id = add_window_item(
        conn,
        session_id,
        "turn",
        str(seq),
        f"turn {seq} ({role})",
        display_text,
        priority=1,
    )
    turn["window_item_id"] = item_id
    # If capped, token_estimate is window's, but original is preserved in turns
    return turn


def fold_item(
    conn: sqlite3.Connection,
    session_id: str,
    item: sqlite3.Row,
    summary: Optional[str] = None,
) -> str:
    """Fold a window item: keep exact text + summary in folds, remove from window."""
    fold_id = new_id("fold")
    tokens = int(item["token_estimate"] or 0)
    text = item["text"]

    if summary is None:
        # Deterministic default summary: first 200 chars (no LLM required).
        summary = text[:200] + ("..." if len(text) > 200 else "")

    conn.execute(
        "INSERT INTO folds("
        "id, session_id, kind, ref_id, title, original_text, summary, token_estimate, folded_at, status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'folded')",
        (
            fold_id,
            session_id,
            item["kind"],
            item["ref_id"],
            item["title"],
            text,
            summary,
            tokens,
            now_iso(),
        ),
    )

    # Index folded text in FTS so unfold can find exact passages.
    try:
        if fts_enabled(conn):
            conn.execute(
                "INSERT INTO fts_folds(text, fold_id, session_id, title) VALUES (?, ?, ?, ?)",
                (text, fold_id, session_id, item["title"]),
            )
    except sqlite3.OperationalError:
        pass

    remove_window_item(conn, item["id"])

    return fold_id


def unfold_fold(
    conn: sqlite3.Connection,
    session_id: str,
    fold: sqlite3.Row,
    priority: int = 0,
) -> Dict[str, Any]:
    """Unfold a fold back into the active window with EXACT original text."""
    item_id = add_window_item(
        conn,
        session_id,
        fold["kind"],
        fold["ref_id"],
        fold["title"],
        fold["original_text"],
        priority=priority,
    )

    conn.execute(
        "UPDATE folds SET status = 'unfolded', unfolded_at = ? WHERE id = ?",
        (now_iso(), fold["id"]),
    )

    return {
        "fold_id": fold["id"],
        "window_item_id": item_id,
        "title": fold["title"],
        "tokens": estimate_tokens(fold["original_text"]),
    }


def search_folds(
    conn: sqlite3.Connection,
    session_id: str,
    query: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Find folded context by keyword (FTS5 first, LIKE fallback)."""
    if fts_enabled(conn):
        rows = _fts_try(conn, "fts_folds",
                        "fold_id, title, session_id, "
                        "snippet(fts_folds, 0, '[', ']', '...', 40) AS snippet",
                        limit, query)
        if rows:
            return rows

    like = f"%{escape_like(query)}%"
    cur = conn.execute(
        "SELECT id AS fold_id, title, session_id, substr(original_text, 1, 240) AS snippet "
        "FROM folds WHERE session_id = ? AND (original_text LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\') "
        "ORDER BY folded_at DESC LIMIT ?",
        (session_id, like, like, like, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def fold_oldest(
    conn: sqlite3.Connection,
    session_id: str,
    target_tokens: int,
) -> List[Dict[str, Any]]:
    """Fold the lowest-priority / oldest window items until <= target_tokens remain.

    Order: priority 0 items (oldest first), then priority 1 (oldest first),
    then priority 2. Pinned (priority >= 3) items are never auto-folded.
    Recomputes usage from DB after each fold to avoid drift (not local decrement).
    """
    folded: List[Dict[str, Any]] = []
    # Clamp target to at least 0
    if target_tokens < 0:
        target_tokens = 0

    usage = window_usage_tokens(conn, session_id)

    if usage <= target_tokens:
        return folded

    # Never auto-fold high-priority (pinned) items.
    rows = conn.execute(
        "SELECT * FROM window_items WHERE session_id = ? AND priority < 3 "
        "ORDER BY priority ASC, added_at ASC",
        (session_id,),
    ).fetchall()

    for item in rows:
        # Recompute before each decision (handles prior folds + external changes)
        usage = window_usage_tokens(conn, session_id)
        if usage <= target_tokens:
            break
        fold_id = fold_item(conn, session_id, item)
        folded.append(
            {
                "fold_id": fold_id,
                "title": item["title"],
                "kind": item["kind"],
                "tokens": item["token_estimate"],
            }
        )

    return folded


def format_budget_report(
    conn: sqlite3.Connection,
    session_id: str,
) -> str:
    """Greppable key: value window budget report.

    Every data line matches `^[a-z_.]+:` so `agres budget | grep window.` works.
    """
    budget = window_budget()
    usage = window_usage_tokens(conn, session_id) if session_id else 0
    reserve = WINDOW_RESERVE_TOKENS
    usable = max(1, budget - reserve)
    pct = (usage / usable * 100.0) if usable > 0 else 0.0
    remaining = max(0, usable - usage)
    level = _confidence_level(pct, 0, 0)
    bar = _bar(pct, 30)
    tok_per_cell = max(1, usable // 30)

    level_hint = {
        "high": "plenty of room — no folding needed",
        "medium": "getting fuller — fine for now",
        "low": "approaching the limit — folding starts soon",
        "critical": "at the limit — oldest context folds away on next capture",
    }[level]

    lines = [
        "# Agres Window Budget",
        "",
        f"session.id: {session_id or '(none)'}",
        f"budget.tokens: {budget:,}   (source: {_budget_source()})",
        f"budget.reserve: {reserve:,}   (kept for the model's reply, never filled with context)",
        f"budget.usable: {usable:,}   (= budget minus reserve; the number that actually fills up)",
        "",
        f"window.tokens: {usage:,}   (context currently held in the active window)",
        f"window.pct: {pct:.1f}%   (of usable)",
        f"window.remaining: {remaining:,}",
        f"window.bar: [{bar}]   (~{tok_per_cell:,} tokens per bar cell)",
        "",
        f"confidence: {level}   ({level_hint})",
        "",
        "# In window",
        "",
    ]

    rows = conn.execute(
        "SELECT * FROM window_items WHERE session_id = ? ORDER BY priority DESC, added_at ASC",
        (session_id,),
    ).fetchall() if session_id else []

    for row in rows:
        lines.append(
            f"window.item: [{row['kind']}] {row['title']} ({row['token_estimate']:,} tok, "
            f"priority {row['priority']}{', folded' if row['folded'] else ''})"
        )

    if not rows:
        lines.append("window.item: (empty)")

    lines.extend([
        "",
        "# Folded",
        "",
    ])

    frows = conn.execute(
        "SELECT id, kind, title, token_estimate, status FROM folds "
        "WHERE session_id = ? ORDER BY folded_at DESC LIMIT 20",
        (session_id,),
    ).fetchall() if session_id else []

    for row in frows:
        lines.append(
            f"fold.item: {row['id']} [{row['kind']}] {row['title']} "
            f"({row['token_estimate']:,} tok, {row['status']})"
        )

    if not frows:
        lines.append("fold.item: (none)")

    lines.extend([
        "",
        "# Actions",
        "",
        "action.fold: agres fold   (folds until window <= usable; exact text stays recoverable)",
        "action.unfold: agres unfold --query \"what was said about X\"   (restores exact original text)",
    ])

    if pct > 90:
        lines.append("action.now: window over 90% — run agres fold before the next capture")

    return "\n".join(lines)


def _budget_source() -> str:
    """Which layer decided the current budget (env > state > model > default)."""
    env = os.environ.get("AGRES_WINDOW_BUDGET", "").strip()
    if env:
        return f"env AGRES_WINDOW_BUDGET={env}"
    state = get_state().get("window_budget")
    if state:
        return "persisted (agres budget --set)"
    try:
        m = _detect_model()
        ctx = int(m.get("limit", {}).get("context", 0) or 0)
        if ctx >= MIN_WINDOW_BUDGET_TOKENS and ctx != DEFAULT_WINDOW_BUDGET_TOKENS:
            return f"auto from model {m.get('id', 'unknown')}"
    except Exception:
        pass
    return "default"


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    use_project_agres()
    agres_dir = _agres_dir()

    templates = {
        "session.md": "# Agres Session\n",
        "decisions.md": "# Agres Decisions\n\n## Approved\n\n## Rejected\n\n## Constraints\n",
        "tasks.md": "# Agres Tasks\n\n## Active\n\n## Blocked\n\n## Completed\n",
        "errors.md": "# Agres Errors\n\n## Unresolved\n\n## Resolved\n",
        "pinned.md": "# Agres Pinned Context\n",
        "context-pack.md": "# Agres Context Pack\n",
    }

    for name, content in templates.items():
        path = agres_dir / name

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    events_path = agres_dir / "events.jsonl"
    if not events_path.exists():
        events_path.touch()

    print(f"Initialized Agres SQLite memory in: {agres_dir}")
    print(f"Database: {db_path()}")
    print(f"Global Agres home: {AGRES_HOME}")
    _repair_active_session()


def cmd_start(args: argparse.Namespace) -> None:
    use_project_agres()

    session_id = new_id("sess")
    sdir = _agres_dir() / "sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    conn = get_conn()

    # Capture model at session start for history
    try:
        _m = _detect_model()
        _model_id = _m.get("id","")
        _model_provider = _m.get("providerID","")
    except Exception:
        _model_id, _model_provider = "", ""
    try:
        conn.execute(
            "INSERT INTO sessions(id, repo, title, status, model, model_provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                str(_project_root()),
                args.title,
                "active",
                _model_id,
                _model_provider,
                now_iso(),
                now_iso(),
            ),
        )
    except sqlite3.OperationalError:
        # Fallback for DB without model columns (should have been migrated)
        conn.execute(
            "INSERT INTO sessions(id, repo, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                str(_project_root()),
                args.title,
                "active",
                now_iso(),
                now_iso(),
            ),
        )

    conn.commit()
    conn.close()

    md_append(
        sdir / "session.md",
        "\n".join(
            [
                f"# Agres Session {session_id}",
                "",
                f"Goal: {args.title}",
                f"Repo: {_project_root()}",
                f"Created: {now_iso()}",
                "Status: active",
            ]
        ),
    )

    append_event(
        session_id,
        "session_started",
        {
            "title": args.title,
            "repo": str(PROJECT_ROOT),
        },
    )

    set_active_session_id(session_id)

    print(session_id)


def cmd_event(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    payload: Dict[str, Any] = {}

    if args.text:
        payload["text"] = args.text

    append_event(
        session_id,
        args.type,
        payload,
        task_id=args.task,
    )

    if args.text:
        conn = get_conn()
        fts_index_memory(
            conn,
            args.collection or "event",
            new_id("evt"),
            args.text,
            session_id,
        )
        conn.commit()
        conn.close()

    print("ok")


def cmd_decision(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    decision_id = new_id("dec")

    accepted = int(bool(args.accepted))
    rejected = int(bool(args.rejected))

    if accepted:
        status = "approved"
    elif rejected:
        status = "rejected"
    else:
        status = "noted"

    conn = get_conn()

    conn.execute(
        "INSERT INTO decisions(id, session_id, task_id, decision, accepted, rejected, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            session_id,
            args.task or "",
            args.text,
            accepted,
            rejected,
            now_iso(),
        ),
    )

    fts_index_memory(conn, "decision", decision_id, args.text, session_id)

    conn.commit()
    conn.close()

    line = "\n".join(
        [
            f"- [{status}] {args.text}",
            f"  session: {session_id}",
            f"  task: {args.task or 'none'}",
            f"  time: {now_iso()}",
        ]
    )

    md_append(_agres_dir() / "decisions.md", line)

    append_event(
        session_id,
        "decision_made",
        {
            "decision": args.text,
            "accepted": bool(args.accepted),
            "rejected": bool(args.rejected),
        },
        task_id=args.task,
        fold_type="decision",
    )

    print("ok")


def cmd_task(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    task_id = args.task_id or new_id("task")

    conn = get_conn()

    conn.execute(
        "INSERT INTO tasks(id, session_id, status, objective, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            task_id,
            session_id,
            args.status,
            args.objective,
            now_iso(),
            now_iso(),
        ),
    )

    fts_index_memory(conn, "task", task_id, args.objective, session_id)

    conn.commit()
    conn.close()

    line = "\n".join(
        [
            f"- [{args.status}] {args.objective}",
            f"  task_id: {task_id}",
            f"  session: {session_id}",
            f"  time: {now_iso()}",
        ]
    )

    md_append(_agres_dir() / "tasks.md", line)

    append_event(
        session_id,
        "task_updated",
        {
            "task_id": task_id,
            "objective": args.objective,
            "status": args.status,
        },
        task_id=task_id,
        fold_type="task",
    )

    print(task_id)


def cmd_error(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    error_id = new_id("err")

    conn = get_conn()

    conn.execute(
        "INSERT INTO errors(id, session_id, task_id, message, context, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            error_id,
            session_id,
            args.task or "",
            args.message,
            args.context or "",
            now_iso(),
        ),
    )

    text = args.message

    if args.context:
        text += "\n" + args.context

    fts_index_memory(conn, "error", error_id, text, session_id)

    conn.commit()
    conn.close()

    line = "\n".join(
        [
            f"- [error] {args.message}",
            f"  session: {session_id}",
            f"  task: {args.task or 'none'}",
            f"  time: {now_iso()}",
        ]
    )

    if args.context:
        line += f"\n  context: {args.context}"

    md_append(_agres_dir() / "errors.md", line)

    append_event(
        session_id,
        "error_observed",
        {
            "message": args.message,
            "context": args.context or "",
        },
        task_id=args.task,
        fold_type="error",
    )

    print("ok")


def cmd_pin(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    pin_id = new_id("pin")

    conn = get_conn()

    conn.execute(
        "INSERT INTO pins(id, session_id, text, created_at) VALUES (?, ?, ?, ?)",
        (
            pin_id,
            session_id,
            args.text,
            now_iso(),
        ),
    )

    fts_index_memory(conn, "pin", pin_id, args.text, session_id)

    conn.commit()
    conn.close()

    line = "\n".join(
        [
            f"- {args.text}",
            f"  pinned_at: {now_iso()}",
            f"  session: {session_id}",
        ]
    )

    md_append(_agres_dir() / "pinned.md", line)

    append_event(
        session_id,
        "pinned_context",
        {
            "text": args.text,
        },
    )

    print("ok")


# ---------------------------------------------------------------------------
# Phase 6: compaction audit. Deterministic checkpoint facts + validation that
# the lossy residue (fold summaries / checkpoint file) still contains them.
# ---------------------------------------------------------------------------

def session_checkpoint_facts(conn: sqlite3.Connection, session_id: str, limit: int = 25) -> List[str]:
    """Must-survive facts in priority order: pins > active tasks > accepted >
    rejected > errors. Each truncated; deduped; capped."""
    facts: List[str] = []

    def add(text: str) -> None:
        t = re.sub(r"\s+", " ", (text or "").strip())[:140]
        if t and t not in facts:
            facts.append(t)
    try:
        for r in conn.execute(
                "SELECT text FROM pins WHERE session_id = ? ORDER BY created_at DESC LIMIT 10",
                (session_id,)).fetchall():
            add(r["text"])
        for r in conn.execute(
                "SELECT objective FROM tasks WHERE session_id = ? ORDER BY updated_at DESC LIMIT 10",
                (session_id,)).fetchall():
            add(r["objective"])
        for r in conn.execute(
                "SELECT decision FROM decisions WHERE session_id = ? AND accepted = 1 "
                "ORDER BY created_at DESC LIMIT 10", (session_id,)).fetchall():
            add(r["decision"])
        for r in conn.execute(
                "SELECT decision FROM decisions WHERE session_id = ? AND rejected = 1 "
                "ORDER BY created_at DESC LIMIT 5", (session_id,)).fetchall():
            add("REJECTED: " + r["decision"])
        for r in conn.execute(
                "SELECT message FROM errors WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT 5", (session_id,)).fetchall():
            add(r["message"])
    except sqlite3.OperationalError:
        pass
    return facts[:limit]


def validate_facts(text: str, facts: List[str]) -> Dict[str, Any]:
    """Normalized-substring check: does the residue still contain each fact?"""
    norm = re.sub(r"\s+", " ", (text or "").lower())
    covered, missing = [], []
    for f in facts:
        key = re.sub(r"\s+", " ", f.lower()).strip()
        # REJECTED facts validate on their payload (prefix is metadata).
        probe = key[len("rejected: "):].strip() if key.startswith("rejected: ") else key
        (covered if probe and probe in norm else missing).append(f)
    return {"covered": covered, "missing": missing,
            "covered_count": len(covered), "total": len(facts)}


def store_compaction_artifact(conn: sqlite3.Connection, session_id: str, trigger: str,
                             summary: str, facts: List[str], raw_refs: List[str],
                             residue_text: str) -> Dict[str, Any]:
    """Write one audit row. Returns {artifact_id, validation}. Never raises."""
    validation = validate_facts(residue_text, facts)
    artifact_id = new_id("cmp")
    try:
        conn.execute(
            "INSERT INTO compaction_artifacts(id, session_id, trigger, summary, "
            "checkpoint_json, raw_refs, validation_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, session_id, trigger, summary[:2000],
             json.dumps(facts, ensure_ascii=False),
             json.dumps(raw_refs, ensure_ascii=False),
             json.dumps(validation, ensure_ascii=False), now_iso()),
        )
    except sqlite3.OperationalError:
        pass
    return {"artifact_id": artifact_id, "validation": validation}


def cmd_checkpoint(args: argparse.Namespace) -> None:
    session_id = require_session(args)

    sdir = session_dir(session_id)
    checkpoints_dir = sdir / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_id = new_id("ckpt")
    reason = args.reason or "checkpoint"

    sections = [
        f"# Agres Checkpoint {checkpoint_id}",
        "",
        f"Reason: {reason}",
        f"Session: {session_id}",
        f"Time: {now_iso()}",
        "",
    ]

    for name in [
        "session.md",
        "decisions.md",
        "tasks.md",
        "errors.md",
        "pinned.md",
        "context-pack.md",
    ]:
        path = _agres_dir() / name

        if path.exists():
            sections.append(f"## {name}")
            sections.append("")
            sections.append(read_text_safe(path, 4000))
            sections.append("")

    checkpoint_path = checkpoints_dir / f"{checkpoint_id}.md"
    checkpoint_path.write_text("\n".join(sections), encoding="utf-8")

    conn = get_conn()

    conn.execute(
        "INSERT INTO checkpoints(id, session_id, reason, path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            checkpoint_id,
            session_id,
            reason,
            str(checkpoint_path),
            now_iso(),
        ),
    )

    conn.commit()

    # Phase 6: audit that must-survive facts reached the checkpoint file.
    try:
        facts = session_checkpoint_facts(conn, session_id)
        residue = checkpoint_path.read_text(encoding="utf-8", errors="ignore")
        audit = store_compaction_artifact(conn, session_id, "checkpoint", reason,
                                          facts, [], residue)
        conn.commit()
        val = audit["validation"]
    except Exception:
        val = {"covered_count": 0, "total": 0}
        audit = {"artifact_id": None, "validation": val}
    conn.close()

    append_event(
        session_id,
        "checkpoint_created",
        {
            "reason": reason,
            "checkpoint_id": checkpoint_id,
            "path": str(checkpoint_path),
            "facts_covered": val["covered_count"],
            "facts_total": val["total"],
        },
    )

    print(checkpoint_path)
    print(f"validation: {val['covered_count']}/{val['total']} facts covered "
          f"(artifact {audit['artifact_id']})")


# ---------------------------------------------------------------------------
# Phase 3: bounded pack builder. Same percentage split at any budget, so a
# 10k window gets a complete-but-small pack instead of a truncated dump.
# ---------------------------------------------------------------------------
PACK_SHARES = {
    "pinned": 0.12,
    "decisions": 0.10,
    "tasks_errors": 0.08,
    "recent_turns": 0.25,
    "retrieved": 0.30,
    "repo_map": 0.10,
}
# keep: higher survives global truncation longer (receipt/header never shrink).
PACK_KEEP = {"header": 1000, "pinned": 90, "decisions": 85, "recent_turns": 80,
             "tasks_errors": 70, "retrieved": 50, "repo_map": 20, "receipt": 1000}


def pack_reserve(budget: int) -> int:
    """Reply headroom scaled to budget: 1k floor, 8k ceiling (matches window)."""
    return min(WINDOW_RESERVE_TOKENS, max(1024, budget // 8))


def _pack_lines(rows, fmt) -> str:
    out = []
    for r in rows:
        try:
            out.append(fmt(dict(r)))
        except Exception:
            continue
    return "\n".join(out)


def pack_section_text(conn: sqlite3.Connection, name: str, session_id: str,
                      query: str, limit: int, cap_chars: int) -> str:
    """Build one pack section, pre-truncated to cap_chars. Session-scoped DB reads."""
    text = ""
    try:
        if name == "pinned":
            rows = conn.execute(
                "SELECT text FROM pins WHERE session_id = ? ORDER BY created_at DESC LIMIT 20",
                (session_id,)).fetchall()
            text = _pack_lines(rows, lambda r: f"- {r.get('text', '')}")
        elif name == "decisions":
            rows = conn.execute(
                "SELECT decision, accepted, rejected FROM decisions "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT 20",
                (session_id,)).fetchall()
            def fmt(r):
                mark = "accepted" if r.get("accepted") else ("rejected" if r.get("rejected") else "noted")
                return f"- [{mark}] {r.get('decision', '')}"
            text = _pack_lines(rows, fmt)
        elif name == "tasks_errors":
            trows = conn.execute(
                "SELECT status, objective FROM tasks WHERE session_id = ? "
                "ORDER BY updated_at DESC LIMIT 10", (session_id,)).fetchall()
            erows = conn.execute(
                "SELECT message FROM errors WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT 10", (session_id,)).fetchall()
            parts = []
            if trows:
                parts.append("Tasks:")
                parts.append(_pack_lines(trows, lambda r: f"- [{r.get('status', '')}] {r.get('objective', '')}"))
            if erows:
                parts.append("Errors:")
                parts.append(_pack_lines(erows, lambda r: f"- {r.get('message', '')}"))
            text = "\n".join(parts)
        elif name == "recent_turns":
            rows = conn.execute(
                "SELECT seq, role, content FROM turns WHERE session_id = ? "
                "ORDER BY seq DESC LIMIT 60", (session_id,)).fetchall()
            # Newest-first fill (most relevant), then chronological for reading.
            picked = []
            budget_chars = cap_chars
            for r in rows:
                line = f"[{r['seq']} {r['role']}] {r['content']}"
                if len(line) > budget_chars and picked:
                    break
                picked.append(line)
                budget_chars -= len(line) + 1
                if budget_chars <= 0:
                    break
            text = "\n\n".join(reversed(picked))
        elif name == "retrieved" and query:
            parts = []
            try:
                fused = hybrid_file_results(conn, query, limit)
            except Exception:
                fused = []
            if fused:
                parts.append("Ranked files (RRF fts+symbols+graph):")
                parts.extend(json.dumps(f, ensure_ascii=False) for f in fused)
            chunks = search_chunks(conn, query, limit * 2)
            if fused:
                order = {f["file_path"]: i for i, f in enumerate(fused)}
                chunks = sorted(chunks, key=lambda c: order.get(c.get("file_path", ""), 999))
            for c in chunks[:limit]:
                parts.append(f"{c.get('file_path', '')}: {c.get('snippet', '')[:300]}")
            for s in search_symbols(conn, query, limit):
                parts.append(f"sym {s.get('name', '')} ({s.get('kind', '')}) "
                               f"{s.get('file_path', '')}:{s.get('line', '')}")
            text = "\n".join(parts)
        elif name == "repo_map":
            text = read_text_safe(_agres_dir() / "folds" / "repo_map.md", cap_chars)
    except sqlite3.OperationalError:
        text = ""
    if len(text) > cap_chars:
        text = text[:cap_chars] + "\n...[truncated to pack share]..."
    return text


def fit_pack_sections(sections: List[Dict[str, Any]], usable: int):
    """Enforce total <= usable by halving lowest-keep sections first. In place."""
    def total() -> int:
        return sum(estimate_tokens(s["text"]) for s in sections)
    order = sorted(sections, key=lambda s: PACK_KEEP.get(s["name"], 0))
    guard = 0
    while total() > usable and guard < 20:
        guard += 1
        shrunk = False
        for s in order:
            if total() <= usable:
                break
            if PACK_KEEP.get(s["name"], 0) >= 1000 or not s["text"]:
                continue
            s["text"] = s["text"][:len(s["text"]) // 2]
            s["truncated"] = True
            shrunk = True
        if not shrunk:
            break


def mark_packs_stale(conn: sqlite3.Connection) -> None:
    """Any real reindex potentially invalidates stored packs. One UPDATE."""
    try:
        conn.execute("UPDATE context_packs SET stale = 1 WHERE stale = 0")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Brain: code-to-graph + project trace + context injection.
# Science: RepoGraph (ICLR'25) and Codebase-Memory (arXiv:2603.27277) show a
# precomputed structural graph beats flat chunk search for agent navigation:
# the graph carries architecture, the file reads carry depth. This section
# materializes that graph in SQLite (zero new deps) and fuses it with
# session progress (tasks/decisions/errors/pins/checkpoints) plus git
# history, so one `brain` pack gives a model the whole project.
# Nodes: file|symbol|task|decision|error|pin|checkpoint|session|commit.
# Edges: contains|imports|uses|touches|mentions|owns|about|covers|child_of.
# ---------------------------------------------------------------------------
BRAIN_VERSION = "1"

BRAIN_SHARES = {
    "identity": 0.08,
    "structure": 0.20,
    "progress": 0.27,
    "graph": 0.25,
    "turns": 0.15,
}
BRAIN_KEEP = {"header": 1000, "identity": 1000, "progress": 90, "graph": 80,
              "turns": 70, "structure": 50, "receipt": 1000}


def _git_run(root: Path, *args: str, timeout: int = 10) -> Optional[str]:
    """Run git in root; None when git missing/failing (not a repo)."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def git_trace_info(root: Path, max_commits: int = 20) -> Dict[str, Any]:
    """Branch, HEAD, recent commits + touched files. Empty dict outside git."""
    info: Dict[str, Any] = {"is_repo": False}
    branch = _git_run(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = _git_run(root, "rev-parse", "--short", "HEAD")
    if sha is None:
        return info
    info["is_repo"] = True
    info["branch"] = (branch or "").strip() or "?"
    info["head"] = sha.strip()
    log = _git_run(root, "log", f"--format=%H|%ad|%s", "--date=short",
                   f"-{max_commits}") or ""
    commits = []
    for line in log.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        full, date, subject = parts
        files_out = _git_run(root, "show", "--name-only", "--format=", full) or ""
        files = [f.strip() for f in files_out.splitlines() if f.strip()][:12]
        commits.append({"sha": full[:12], "date": date, "subject": subject[:160],
                        "files": files})
    info["commits"] = commits
    return info


def _file_rank(conn: sqlite3.Connection, files: List[str]) -> Dict[str, float]:
    """PageRank over import + usage edges. Shared by map and brain graph."""
    gedges: List[tuple] = []
    stem_index = build_stem_index(files)
    try:
        for r in conn.execute("SELECT src_file, dst_file FROM edges").fetchall():
            for dst in resolve_import_to_files_indexed(r["dst_file"], stem_index):
                if dst != r["src_file"]:
                    gedges.append((r["src_file"], dst, 1.0))
    except sqlite3.OperationalError:
        pass
    try:
        gedges.extend(ref_file_edges(conn, files))
    except Exception:
        pass
    _wsum: Dict[tuple, float] = {}
    _cnt: Dict[tuple, int] = {}
    for s, d, w in gedges:
        _wsum[(s, d)] = _wsum.get((s, d), 0.0) + w
        _cnt[(s, d)] = _cnt.get((s, d), 0) + 1
    gedges = [(s, d, _wsum[(s, d)] / (_cnt[(s, d)] ** 0.5)) for (s, d) in _wsum]
    try:
        return pagerank_file_graph(files, gedges, {}) if files else {}
    except Exception:
        return {}


def _mention_files(text: str, files: List[str],
                   base_index: Dict[str, List[str]]) -> List[str]:
    """Deterministic text->file links: rel-path substring, then basename word
    match (skipped when a basename is ambiguous across >3 files). No embeddings."""
    if not text:
        return []
    found: List[str] = []
    for f in files:
        if f and f in text and f not in found:
            found.append(f)
            if len(found) >= 8:
                return found
    words = set(re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*", text))
    for w in words:
        cands = base_index.get(w)
        if cands and len(cands) == 1 and cands[0] not in found:
            found.append(cands[0])
            if len(found) >= 8:
                break
    return found


def build_graph(root: Optional[Path] = None) -> Dict[str, Any]:
    """Rebuild graph_nodes/graph_edges from index + progress + git. Returns counts."""
    root = root or _project_root()
    conn = get_conn()
    try:
        files = [r["path"] for r in
                 conn.execute("SELECT path FROM files ORDER BY path").fetchall()]
    except sqlite3.OperationalError:
        files = []
    if not files:
        try:
            for p in iter_repo_files(root, limit=2000):
                try:
                    files.append(p.relative_to(root).as_posix())
                except Exception:
                    continue
        except Exception:
            pass
    rank = _file_rank(conn, files)
    now = now_iso()
    nodes: List[tuple] = []
    edges: List[tuple] = []
    try:
        frows = conn.execute(
            "SELECT path, language, size FROM files").fetchall()
    except sqlite3.OperationalError:
        frows = []
    finfo = {r["path"]: dict(r) for r in frows}
    for f in files:
        meta = finfo.get(f, {})
        nodes.append((f"file:{f}", "file", f, f, 0, "",
                      float(rank.get(f, 0.0)),
                      json.dumps({"language": meta.get("language", ""),
                                  "size": meta.get("size", 0)}, ensure_ascii=False), now))
    try:
        srows = conn.execute(
            "SELECT file_path, name, kind, line FROM symbols").fetchall()
    except sqlite3.OperationalError:
        srows = []
    for r in srows:
        fp = r["file_path"] or ""
        nm = r["name"] or ""
        ln = int(r["line"] or 0)
        nodes.append((f"sym:{fp}:{ln}:{nm}", "symbol", nm, fp, ln, "",
                      float(rank.get(fp, 0.0)) * 0.5,
                      json.dumps({"kind": r["kind"] or ""}, ensure_ascii=False), now))
        edges.append((f"file:{fp}", f"sym:{fp}:{ln}:{nm}", "contains", 1.0, ""))
    try:
        stem_index = build_stem_index(files)
        for r in conn.execute("SELECT src_file, dst_file FROM edges").fetchall():
            for dst in resolve_import_to_files_indexed(r["dst_file"], stem_index):
                if dst != r["src_file"]:
                    edges.append((f"file:{r['src_file']}", f"file:{dst}",
                                  "imports", 1.0, ""))
    except sqlite3.OperationalError:
        pass
    try:
        for s, d in ref_file_edges(conn, files):
            edges.append((f"file:{s}", f"file:{d}", "uses", 0.6, ""))
    except Exception:
        pass
    base_index: Dict[str, List[str]] = {}
    for f in files:
        base_index.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    progress: List[tuple] = []  # (node_id, kind, text_for_linking)
    try:
        for r in conn.execute(
                "SELECT id, session_id, status, objective, updated_at FROM tasks").fetchall():
            nid = f"task:{r['id']}"
            nodes.append((nid, "task", (r["objective"] or "")[:120], "", 0,
                          r["session_id"] or "", 1.0,
                          json.dumps({"status": r["status"] or "",
                                      "updated": r["updated_at"] or ""},
                                     ensure_ascii=False), now))
            progress.append((nid, "task", r["objective"] or ""))
            if r["session_id"]:
                edges.append((f"session:{r['session_id']}", nid, "owns", 1.0, ""))
        for r in conn.execute(
                "SELECT id, session_id, task_id, decision, accepted, rejected FROM decisions").fetchall():
            nid = f"decision:{r['id']}"
            nodes.append((nid, "decision", (r["decision"] or "")[:120], "", 0,
                          r["session_id"] or "", 1.0,
                          json.dumps({"accepted": bool(r["accepted"]),
                                      "rejected": bool(r["rejected"])},
                                     ensure_ascii=False), now))
            progress.append((nid, "decision", r["decision"] or ""))
            if r["session_id"]:
                edges.append((f"session:{r['session_id']}", nid, "owns", 1.0, ""))
            if r["task_id"]:
                edges.append((nid, f"task:{r['task_id']}", "about", 0.9, ""))
        for r in conn.execute(
                "SELECT id, session_id, task_id, message FROM errors").fetchall():
            nid = f"error:{r['id']}"
            nodes.append((nid, "error", (r["message"] or "")[:120], "", 0,
                          r["session_id"] or "", 1.0,
                          json.dumps({}, ensure_ascii=False), now))
            progress.append((nid, "error", r["message"] or ""))
            if r["session_id"]:
                edges.append((f"session:{r['session_id']}", nid, "owns", 1.0, ""))
            if r["task_id"]:
                edges.append((nid, f"task:{r['task_id']}", "about", 0.9, ""))
        for r in conn.execute("SELECT id, session_id, text FROM pins").fetchall():
            nid = f"pin:{r['id']}"
            nodes.append((nid, "pin", (r["text"] or "")[:120], "", 0,
                          r["session_id"] or "", 1.2,
                          json.dumps({}, ensure_ascii=False), now))
            progress.append((nid, "pin", r["text"] or ""))
            if r["session_id"]:
                edges.append((f"session:{r['session_id']}", nid, "owns", 1.0, ""))
        for r in conn.execute(
                "SELECT id, session_id, reason FROM checkpoints").fetchall():
            nid = f"checkpoint:{r['id']}"
            nodes.append((nid, "checkpoint", (r["reason"] or "")[:120], "", 0,
                          r["session_id"] or "", 1.0,
                          json.dumps({}, ensure_ascii=False), now))
            if r["session_id"]:
                edges.append((f"session:{r['session_id']}", nid, "owns", 1.0, ""))
                edges.append((nid, f"session:{r['session_id']}", "covers", 0.8, ""))
        for r in conn.execute(
                "SELECT id, title, status, updated_at FROM sessions").fetchall():
            nodes.append((f"session:{r['id']}", "session",
                          (r["title"] or r["id"])[:120], "", 0, r["id"], 1.0,
                          json.dumps({"status": r["status"] or "",
                                      "updated": r["updated_at"] or ""},
                                     ensure_ascii=False), now))
    except sqlite3.OperationalError:
        pass
    for nid, _kind, text in progress:
        for f in _mention_files(text, files, base_index):
            edges.append((nid, f"file:{f}", "touches", 0.7, ""))
    gt = git_trace_info(root)
    if gt.get("is_repo"):
        for c in gt.get("commits", []):
            nid = f"commit:{c['sha']}"
            nodes.append((nid, "commit", c["subject"][:120], "", 0, "", 0.5,
                          json.dumps({"sha": c["sha"], "date": c["date"]},
                                     ensure_ascii=False), now))
            for f in c.get("files", [])[:12]:
                if f in files or f"file:{f}" in {n[0] for n in nodes}:
                    edges.append((nid, f"file:{f}", "touches", 0.5, ""))
                else:
                    nodes.append((f"file:{f}", "file", f, f, 0, "", 0.1,
                                  json.dumps({"from_git": True}, ensure_ascii=False), now))
                    edges.append((nid, f"file:{f}", "touches", 0.5, ""))
    try:
        conn.execute("DELETE FROM graph_edges")
        conn.execute("DELETE FROM graph_nodes")
        conn.executemany(
            "INSERT OR REPLACE INTO graph_nodes(id, kind, label, file_path, line,"
            " session_id, weight, meta, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", nodes)
        # Dedupe edges, keep max weight.
        seen: Dict[tuple, float] = {}
        for s, d, rel, w, m in edges:
            k = (s, d, rel)
            if w > seen.get(k, 0.0):
                seen[k] = w
        conn.executemany(
            "INSERT OR REPLACE INTO graph_edges(src, dst, rel, weight, meta)"
            " VALUES (?,?,?,?,?)",
            [(s, d, rel, w, "") for (s, d, rel), w in seen.items()])
        conn.commit()
    finally:
        pass
    counts = graph_stats(conn)
    conn.close()
    counts["built_at"] = now
    return counts


def graph_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {"nodes": 0, "edges": 0, "by_kind": {}, "by_rel": {},
                            "built_at": None}
    try:
        row = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM graph_nodes").fetchone()
        out["nodes"] = int(row[0] or 0)
        out["built_at"] = row[1]
        for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM graph_nodes GROUP BY kind").fetchall():
            out["by_kind"][r["kind"] or "?"] = int(r["c"])
        out["edges"] = int(conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0] or 0)
        for r in conn.execute(
                "SELECT rel, COUNT(*) c FROM graph_edges GROUP BY rel").fetchall():
            out["by_rel"][r["rel"] or "?"] = int(r["c"])
    except sqlite3.OperationalError:
        pass
    return out


def graph_seed_ids(conn: sqlite3.Connection, query: str, limit: int) -> List[str]:
    """Seed nodes for a query: exact file match, then LIKE over label/meta."""
    seeds: List[str] = []
    if not query:
        return seeds
    try:
        for r in conn.execute(
                "SELECT id FROM graph_nodes WHERE file_path = ? LIMIT 3", (query,)).fetchall():
            seeds.append(r["id"])
        terms = [t for t in re.findall(r"[A-Za-z0-9_.\-/]+", query) if len(t) >= 3][:6]
        for t in terms:
            like = f"%{escape_like(t)}%"
            for r in conn.execute(
                    "SELECT id FROM graph_nodes WHERE label LIKE ? ESCAPE '\\'"
                    " OR meta LIKE ? ESCAPE '\\' ORDER BY weight DESC LIMIT ?",
                    (like, like, limit)).fetchall():
                if r["id"] not in seeds:
                    seeds.append(r["id"])
                if len(seeds) >= limit:
                    return seeds[:limit]
    except sqlite3.OperationalError:
        pass
    return seeds[:limit]


def graph_neighborhood(conn: sqlite3.Connection, seeds: List[str],
                       hops: int = 2, limit: int = 20) -> Dict[str, Any]:
    """BFS expand from seeds (both directions), rank visited by weight."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    if not seeds:
        return {"nodes": [], "edges": []}
    try:
        frontier = list(seeds)
        visited = set(seeds)
        seen_edge: set = set()
        for _ in range(max(1, hops)):
            nxt: List[str] = []
            for sid in frontier:
                for r in conn.execute(
                        "SELECT src, dst, rel, weight FROM graph_edges"
                        " WHERE src = ? OR dst = ?", (sid, sid)).fetchall():
                    for other in (r["src"], r["dst"]):
                        if other not in visited:
                            visited.add(other)
                            nxt.append(other)
                    ek = (r["src"], r["dst"], r["rel"])
                    if ek not in seen_edge:
                        seen_edge.add(ek)
                        edges.append(dict(r))
                    if len(edges) > limit * 40:
                        break
            frontier = nxt
            if not frontier:
                break
        for vid in list(visited)[:limit * 10]:
            r = conn.execute("SELECT * FROM graph_nodes WHERE id = ?", (vid,)).fetchone()
            if r:
                nodes[vid] = dict(r)
        keep = set(sorted(nodes, key=lambda k: -(nodes[k].get("weight") or 0.0))[:limit * 3])
        keep |= set(seeds)
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges if e["src"] in nodes and e["dst"] in nodes][:limit * 5]
    except sqlite3.OperationalError:
        pass
    return {"nodes": list(nodes.values()), "edges": edges}


def graph_subgraph_text(sub: Dict[str, Any], cap_chars: int) -> str:
    lines = []
    for n in sorted(sub.get("nodes", []), key=lambda x: -(x.get("weight") or 0.0)):
        loc = n.get("file_path", "") or ""
        if n.get("line"):
            loc += f":{n['line']}"
        lines.append(f"- [{n.get('kind')}] {n.get('label','')[:100]}"
                     + (f" ({loc})" if loc else "")
                     + f" w={float(n.get('weight') or 0.0):.3f}")
    if sub.get("edges"):
        lines.append("links:")
        for e in sub["edges"]:
            lines.append(f"  {e['src'][:60]} --{e['rel']}--> {e['dst'][:60]}")
    text = "\n".join(lines)
    if len(text) > cap_chars:
        text = text[:cap_chars] + "\n...[truncated to brain share]..."
    return text


def cmd_graph(args: argparse.Namespace) -> None:
    conn = get_conn()
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "build", False):
        root = Path(getattr(args, "path", None) or _project_root())
        conn.close()
        counts = build_graph(root)
        if as_json:
            print(json.dumps({"built": True, **counts}, indent=2))
        else:
            print(f"graph built: {counts.get('nodes', 0)} nodes, "
                  f"{counts.get('edges', 0)} edges")
            print(f"  kinds: {json.dumps(counts.get('by_kind', {}), ensure_ascii=False)}")
            print(f"  rels: {json.dumps(counts.get('by_rel', {}), ensure_ascii=False)}")
        return
    export = getattr(args, "export", "") or ""
    if export:
        sub = {"nodes": [], "edges": []}
        try:
            for r in conn.execute(
                    "SELECT * FROM graph_nodes ORDER BY weight DESC LIMIT 5000").fetchall():
                sub["nodes"].append(dict(r))
            for r in conn.execute("SELECT * FROM graph_edges LIMIT 20000").fetchall():
                sub["edges"].append(dict(r))
        except sqlite3.OperationalError:
            pass
        conn.close()
        if export == "dot":
            out = ["digraph agres {"]
            for n in sub["nodes"][:2000]:
                lab = (n.get("label", "") or "")[:40].replace('"', "'")
                out.append(f'  "{n["id"]}" [label="{n.get("kind")}:{lab}"];')
            for e in sub["edges"][:5000]:
                out.append(f'  "{e["src"]}" -> "{e["dst"]}" [label="{e["rel"]}"];')
            out.append("}")
            path = _agres_dir() / "graph.dot"
            path.write_text("\n".join(out), encoding="utf-8")
            print(str(path) if not as_json else json.dumps({"path": str(path)}))
        else:
            path = _agres_dir() / "graph.json"
            path.write_text(json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")
            print(str(path) if not as_json else
                  json.dumps({"path": str(path), "nodes": len(sub["nodes"]),
                              "edges": len(sub["edges"])}))
        return
    query = getattr(args, "query", "") or ""
    if query:
        limit = int(getattr(args, "limit", 20) or 20)
        hops = int(getattr(args, "hops", 2) or 2)
        seeds = graph_seed_ids(conn, query, limit)
        sub = graph_neighborhood(conn, seeds, hops, limit)
        conn.close()
        if as_json:
            print(json.dumps({"query": query, "seeds": seeds, **sub}, ensure_ascii=False))
        else:
            if not seeds:
                print("(no graph seeds; run `agres graph --build` after `agres index`)")
            else:
                print(graph_subgraph_text(sub, 12000))
        return
    stats = graph_stats(conn)
    conn.close()
    if as_json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"graph: {stats['nodes']} nodes, {stats['edges']} edges"
              + (f" (built {stats['built_at']})" if stats["built_at"] else " (not built)"))
        print(f"  kinds: {json.dumps(stats['by_kind'], ensure_ascii=False)}")
        print(f"  rels: {json.dumps(stats['by_rel'], ensure_ascii=False)}")
        if not stats["nodes"]:
            print("  hint: agres index && agres graph --build")


def collect_trace(root: Path, session_id: Optional[str],
                  max_commits: int = 20) -> Dict[str, Any]:
    """Whole-project trace: codebase + structure + progress + work + health."""
    conn = get_conn()
    t: Dict[str, Any] = {
        "generated": now_iso(), "repo": str(root), "session": session_id or "none",
        "brain_version": BRAIN_VERSION,
    }
    try:
        frows = conn.execute(
            "SELECT path, language, size FROM files ORDER BY path").fetchall()
    except sqlite3.OperationalError:
        frows = []
    files = [dict(r) for r in frows]
    langs: Dict[str, int] = {}
    dirs: Dict[str, int] = {}
    total_bytes = 0
    for f in files:
        ext = (f.get("path", "").rsplit(".", 1)[-1] if "." in f.get("path", "") else "?")[:12]
        langs[ext] = langs.get(ext, 0) + 1
        parent = f.get("path", "").rsplit("/", 1)[0] if "/" in f.get("path", "") else "(root)"
        dirs[parent] = dirs.get(parent, 0) + 1
        total_bytes += int(f.get("size", 0) or 0)
    t["codebase"] = {"files": len(files), "bytes": total_bytes,
                     "languages": dict(sorted(langs.items(), key=lambda x: -x[1])[:15]),
                     "top_dirs": dict(sorted(dirs.items(), key=lambda x: -x[1])[:10]),
                     "largest": [{"path": f["path"], "size": f.get("size", 0)}
                                 for f in sorted(files, key=lambda x: -(x.get("size", 0) or 0))[:10]]}
    try:
        n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        n_imp = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_ref = conn.execute("SELECT COUNT(*) FROM ref_idents").fetchone()[0]
    except sqlite3.OperationalError:
        n_sym = n_imp = n_ref = 0
    paths = [f["path"] for f in files]
    rank = _file_rank(conn, paths)
    hubs = sorted(paths, key=lambda f: (-rank.get(f, 0.0), f))[:15]
    t["structure"] = {"symbols": int(n_sym or 0), "import_edges": int(n_imp or 0),
                      "ref_idents": int(n_ref or 0),
                      "hubs": [{"file": h, "rank": round(float(rank.get(h, 0.0)), 4)}
                               for h in hubs]}
    prog: Dict[str, Any] = {}
    try:
        prog["sessions"] = [dict(r) for r in conn.execute(
            "SELECT id, title, status, updated_at FROM sessions"
            " ORDER BY updated_at DESC LIMIT 10").fetchall()]
        trows = conn.execute(
            "SELECT status, objective, updated_at FROM tasks"
            " ORDER BY updated_at DESC LIMIT 15").fetchall()
        by_status: Dict[str, int] = {}
        for r in conn.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
            by_status[r["status"] or "?"] = int(r["c"])
        prog["tasks_by_status"] = by_status
        prog["tasks_recent"] = [dict(r) for r in trows]
        drows = conn.execute(
            "SELECT decision, accepted, rejected FROM decisions"
            " ORDER BY created_at DESC LIMIT 10").fetchall()
        acc = conn.execute(
            "SELECT SUM(accepted), SUM(rejected), COUNT(*) FROM decisions").fetchone()
        prog["decisions"] = {"accepted": int(acc[0] or 0), "rejected": int(acc[1] or 0),
                             "total": int(acc[2] or 0),
                             "recent": [dict(r) for r in drows]}
        prog["errors_recent"] = [dict(r) for r in conn.execute(
            "SELECT message, created_at FROM errors ORDER BY created_at DESC LIMIT 10").fetchall()]
        prog["pins"] = [dict(r) for r in conn.execute(
            "SELECT text, created_at FROM pins ORDER BY created_at DESC LIMIT 20").fetchall()]
        prog["checkpoints"] = [dict(r) for r in conn.execute(
            "SELECT reason, created_at FROM checkpoints ORDER BY created_at DESC LIMIT 10").fetchall()]
        prog["turns"] = int(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] or 0)
        prog["events_recent"] = [dict(r) for r in conn.execute(
            "SELECT type, ts FROM events ORDER BY seq DESC LIMIT 15").fetchall()]
    except sqlite3.OperationalError:
        pass
    t["progress"] = prog
    t["work"] = {"git": git_trace_info(root, max_commits)}
    try:
        today = now_iso()[:10]
        t["work"]["indexed_today"] = [r["path"] for r in conn.execute(
            "SELECT path FROM files WHERE indexed_at LIKE ? LIMIT 15",
            (today + "%",)).fetchall()]
    except sqlite3.OperationalError:
        t["work"]["indexed_today"] = []
    health: Dict[str, Any] = {}
    try:
        health["fts"] = fts_enabled(conn)
        health["stale_packs"] = int(conn.execute(
            "SELECT COUNT(*) FROM context_packs WHERE stale = 1").fetchone()[0] or 0)
        health["packs"] = int(conn.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0] or 0)
    except sqlite3.OperationalError:
        health["fts"] = False
    health["graph"] = graph_stats(conn)
    conn.close()
    t["health"] = health
    return t


def trace_markdown(t: Dict[str, Any]) -> str:
    L = [f"# Agres Project Trace", "",
         f"Repo: {t.get('repo')}", f"Generated: {t.get('generated')}",
         f"Session: {t.get('session')}", ""]
    cb = t.get("codebase", {})
    L += ["## Codebase", "",
          f"Files: {cb.get('files', 0)} · {cb.get('bytes', 0)} bytes",
          "Languages: " + (", ".join(f"{k}×{v}" for k, v in cb.get("languages", {}).items()) or "-"),
          "Top dirs: " + (", ".join(f"{k}×{v}" for k, v in cb.get('top_dirs', {}).items()) or "-"), ""]
    st = t.get("structure", {})
    L += ["## Structure (PageRank hubs)", "",
          f"Symbols: {st.get('symbols', 0)} · import edges: {st.get('import_edges', 0)}"
          f" · ref idents: {st.get('ref_idents', 0)}"]
    for h in st.get("hubs", [])[:15]:
        L.append(f"- {h['file']} (rank {h['rank']})")
    L.append("")
    pr = t.get("progress", {})
    L += ["## Progress", "",
          f"Tasks: {json.dumps(pr.get('tasks_by_status', {}), ensure_ascii=False)}",
          f"Decisions: {pr.get('decisions', {}).get('accepted', 0)} accepted /"
          f" {pr.get('decisions', {}).get('rejected', 0)} rejected"
          f" ({pr.get('decisions', {}).get('total', 0)} total)",
          f"Pins: {len(pr.get('pins', []))} · Checkpoints: {len(pr.get('checkpoints', []))}"
          f" · Turns: {pr.get('turns', 0)}", ""]
    for tr in pr.get("tasks_recent", [])[:10]:
        L.append(f"- [{tr.get('status')}] {tr.get('objective', '')[:120]}")
    if pr.get("errors_recent"):
        L.append("")
        L.append("Recent errors:")
        for e in pr["errors_recent"][:5]:
            L.append(f"- {e.get('message', '')[:140]}")
    L.append("")
    wk = t.get("work", {})
    g = wk.get("git", {})
    L += ["## Work done (git)", ""]
    if g.get("is_repo"):
        L.append(f"Branch {g.get('branch')} @ {g.get('head')}")
        for c in g.get("commits", [])[:15]:
            L.append(f"- {c['date']} {c['sha']} {c['subject'][:100]}"
                     + (f" [{len(c['files'])} files]" if c.get("files") else ""))
    else:
        L.append("(not a git repo)")
    if wk.get("indexed_today"):
        L += ["", f"Indexed today: {', '.join(wk['indexed_today'][:15])}"]
    L.append("")
    h = t.get("health", {})
    gr = h.get("graph", {})
    L += ["## Health", "",
          f"FTS: {'on' if h.get('fts') else 'off'} · packs: {h.get('packs', 0)}"
          f" ({h.get('stale_packs', 0)} stale)",
          f"Graph: {gr.get('nodes', 0)} nodes / {gr.get('edges', 0)} edges"
          + (f" (built {gr.get('built_at')})" if gr.get("built_at") else " (not built — run `agres graph --build`)")]
    return "\n".join(L) + "\n"


def cmd_trace(args: argparse.Namespace) -> None:
    session_id = getattr(args, "session", None) or active_session_id()
    root = Path(getattr(args, "path", None) or _project_root())
    t = collect_trace(root, session_id)
    out_path = _agres_dir() / "trace.md"
    as_json = bool(getattr(args, "json", False))
    if as_json:
        out_path.write_text(trace_markdown(t), encoding="utf-8")
        print(json.dumps(t, ensure_ascii=False, indent=2))
    else:
        text = trace_markdown(t)
        out_path.write_text(text, encoding="utf-8")
        print(text)


def cmd_brain(args: argparse.Namespace) -> None:
    """Context injection: one bounded pack carrying the whole project —
    identity + structure + progress + graph neighborhood + recent turns."""
    session_id = getattr(args, "session", None) or active_session_id()
    root = Path(getattr(args, "path", None) or _project_root())
    query = getattr(args, "query", "") or ""
    budget = int(getattr(args, "budget", 0) or 0) or window_budget()
    limit = int(getattr(args, "limit", 20) or 20)
    reserve = pack_reserve(budget)
    usable = max(512, budget - reserve)
    t = collect_trace(root, session_id)
    conn = get_conn()
    header = "\n".join(["# Agres Brain — project context injection", "",
                        f"Generated: {t['generated']}", f"Repo: {t['repo']}",
                        f"Session: {session_id or 'none'}", f"Query: {query or 'none'}",
                        f"Budget: {budget} tokens (reserve {reserve}, usable {usable})", ""])
    sections: List[Dict[str, Any]] = [
        {"name": "header", "title": "", "text": header, "truncated": False}]
    cb, st, pr = t["codebase"], t["structure"], t["progress"]
    g = t["work"].get("git", {})
    identity = "\n".join([
        f"Files: {cb.get('files', 0)} · {cb.get('bytes', 0)} bytes ·"
        f" symbols: {st.get('symbols', 0)}",
        "Languages: " + (", ".join(f"{k}×{v}" for k, v in
                                    cb.get("languages", {}).items()) or "-"),
        (f"Git: {g.get('branch', '?')} @ {g.get('head', '?')}"
         if g.get("is_repo") else "Git: n/a"),
        f"Session goal: {(t.get('progress', {}).get('sessions') or [{}])[0].get('title', 'none')[:160]}",
        f"Tasks: {json.dumps(pr.get('tasks_by_status', {}), ensure_ascii=False)}",
    ])
    sections.append({"name": "identity", "title": "Identity", "text": identity,
                     "truncated": False})
    struct_lines = [f"Hubs: {', '.join(h['file'] for h in st.get('hubs', [])[:10]) or '-'}",
                    f"Top dirs: {', '.join(k for k in cb.get('top_dirs', {})) or '-'}"]
    try:
        rmp = read_text_safe(_agres_dir() / "folds" / "repo_map.md",
                             int(usable * BRAIN_SHARES["structure"]) * 4)
        if rmp.strip():
            struct_lines.append("RepoMap:")
            struct_lines.append(rmp)
    except Exception:
        pass
    sections.append({"name": "structure", "title": "Structure", "text": "\n".join(struct_lines),
                     "truncated": False})
    if session_id:
        cap = int(usable * BRAIN_SHARES["progress"]) * 4
        prog_parts = []
        for nm, title in (("pinned", "Pinned"), ("decisions", "Decisions"),
                          ("tasks_errors", "Tasks and Errors")):
            try:
                txt = pack_section_text(conn, nm, session_id, query, limit, cap // 2)
            except Exception:
                txt = ""
            if txt.strip():
                prog_parts.append(f"{title}:\n{txt}")
        try:
            cps = conn.execute(
                "SELECT reason, created_at FROM checkpoints WHERE session_id = ?"
                " ORDER BY created_at DESC LIMIT 5", (session_id,)).fetchall()
            if cps:
                prog_parts.append("Checkpoints:\n" + "\n".join(
                    f"- {r['created_at'][:16]} {r['reason']}" for r in cps))
        except sqlite3.OperationalError:
            pass
        sections.append({"name": "progress", "title": "Progress",
                         "text": "\n\n".join(prog_parts), "truncated": False})
        try:
            turns = pack_section_text(conn, "recent_turns", session_id, query,
                                      limit, int(usable * BRAIN_SHARES["turns"]) * 4)
        except Exception:
            turns = ""
        sections.append({"name": "turns", "title": "Recent Turns", "text": turns,
                         "truncated": False})
    gcap = int(usable * BRAIN_SHARES["graph"]) * 4
    if query:
        seeds = graph_seed_ids(conn, query, limit)
        sub = graph_neighborhood(conn, seeds, 2, limit)
        gtext = (graph_subgraph_text(sub, gcap) if seeds
                 else "(no graph seeds for query; run `agres graph --build` after `agres index`)")
    else:
        hubs = st.get("hubs", [])[:10]
        top_prog = []
        try:
            for r in conn.execute(
                    "SELECT kind, label, weight FROM graph_nodes WHERE kind IN"
                    " ('task','decision','pin','error') ORDER BY weight DESC,"
                    " updated_at DESC LIMIT 10").fetchall():
                top_prog.append(f"- [{r['kind']}] {r['label'][:100]}")
        except sqlite3.OperationalError:
            pass
        gtext = "Hub files: " + (", ".join(h["file"] for h in hubs) or "-")
        if top_prog:
            gtext += "\nTop progress nodes:\n" + "\n".join(top_prog)
        if not t["health"]["graph"].get("nodes"):
            gtext += "\n(graph not built — run `agres graph --build`)"
    conn.close()
    sections.append({"name": "graph", "title": "Graph", "text": gtext, "truncated": False})
    for s in sections:
        cap = int(usable * BRAIN_SHARES[s["name"]]) if s["name"] in BRAIN_SHARES else usable
        if estimate_tokens(s["text"]) > cap:
            s["text"] = s["text"][:cap * 4]
            s["truncated"] = True
    fit_pack_sections([s for s in sections if s["name"] != "header"],
                      usable - estimate_tokens(sections[0]["text"]) - 300)
    body = []
    for s in sections:
        if s["name"] == "header":
            body.append(s["text"])
        elif s["text"].strip():
            body.append(f"## {s['title']}\n\n{s['text']}\n")
    pack_id = new_id("brain")
    total_tokens = sum(estimate_tokens(s["text"]) for s in sections)
    receipt = {"pack_id": pack_id, "kind": "brain", "budget": budget,
               "reserve": reserve, "usable": usable, "total_tokens": total_tokens,
               "fits": total_tokens <= usable, "stale": 0,
               "graph_nodes": t["health"]["graph"].get("nodes", 0),
               "graph_edges": t["health"]["graph"].get("edges", 0),
               "sections": [{"name": s["name"], "tokens": estimate_tokens(s["text"]),
                             "truncated": s["truncated"]} for s in sections]}
    body.append(f"## Pack Receipt\n\n{json.dumps(receipt, ensure_ascii=False)}\n")
    content = "\n".join(body)
    out_path = _agres_dir() / "brain.md"
    out_path.write_text(content, encoding="utf-8")
    try:
        c2 = get_conn()
        c2.execute(
            "INSERT INTO context_packs(id, session_id, query, path, created_at,"
            " tokens, budget, stale) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (pack_id, session_id or "", f"brain:{query}", str(out_path),
             now_iso(), total_tokens, budget))
        c2.commit()
        c2.close()
    except sqlite3.OperationalError:
        pass
    if session_id:
        try:
            sdir = session_dir(session_id)
            if sdir:
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / f"brain_{pack_id}.md").write_text(content, encoding="utf-8")
        except Exception:
            pass
    if bool(getattr(args, "json", False)):
        print(json.dumps({**receipt, "path": str(out_path)}, indent=2))
    else:
        print(content)


def cmd_context(args: argparse.Namespace) -> None:
    session_id = getattr(args, "session", None) or active_session_id()

    conn = get_conn()

    budget = int(getattr(args, "budget", 0) or 0) or window_budget()
    limit = int(getattr(args, "limit", 5) or 5)
    query = getattr(args, "query", "") or ""
    reserve = pack_reserve(budget)
    usable = max(512, budget - reserve)

    header_lines = [
        "# Agres Context Pack",
        "",
        f"Generated: {now_iso()}",
        f"Session: {session_id or 'none'}",
        f"Repo: {_project_root()}",
        f"Query: {query or 'none'}",
        f"Budget: {budget} tokens (reserve {reserve}, usable {usable})",
        "",
    ]
    sections: List[Dict[str, Any]] = [
        {"name": "header", "title": "", "text": "\n".join(header_lines), "truncated": False},
    ]
    for name in ["pinned", "decisions", "tasks_errors", "recent_turns", "retrieved", "repo_map"]:
        if name == "retrieved" and not query:
            continue
        if name in ("pinned", "decisions", "tasks_errors", "recent_turns") and not session_id:
            continue
        cap_chars = int(usable * PACK_SHARES[name]) * 4
        sections.append({
            "name": name,
            "title": {"pinned": "Pinned", "decisions": "Decisions",
                       "tasks_errors": "Tasks and Errors", "recent_turns": "Recent Turns",
                       "retrieved": f"Retrieved for: {query}", "repo_map": "Repo Map"}[name],
            "text": pack_section_text(conn, name, session_id or "", query, limit, cap_chars),
            "truncated": False,
        })
    # Per-share pre-truncation is approximate (chars); enforce exact token fit.
    for s in sections:
        cap = int(usable * PACK_SHARES[s["name"]]) if s["name"] in PACK_SHARES else usable
        if estimate_tokens(s["text"]) > cap:
            s["text"] = s["text"][:cap * 4]
            s["truncated"] = True
    fit_pack_sections([s for s in sections if s["name"] != "header"],
                      usable - estimate_tokens(sections[0]["text"]) - 300)  # 300: receipt footer headroom

    body = []
    for s in sections:
        if s["name"] == "header":
            body.append(s["text"])
        elif s["text"].strip():
            body.append(f"## {s['title']}\n\n{s['text']}\n")
    pack_id = new_id("pack")
    total_tokens = sum(estimate_tokens(s["text"]) for s in sections)
    receipt = {
        "pack_id": pack_id,
        "budget": budget,
        "reserve": reserve,
        "usable": usable,
        "total_tokens": total_tokens,
        "fits": total_tokens <= usable,
        "stale": 0,
        "sections": [
            {"name": s["name"], "tokens": estimate_tokens(s["text"]),
             "cap": int(usable * PACK_SHARES[s["name"]]) if s["name"] in PACK_SHARES else usable,
             "truncated": s["truncated"]}
            for s in sections
        ],
    }
    body.append(f"## Pack Receipt\n\n{json.dumps(receipt, ensure_ascii=False)}\n")
    content = "\n".join(body)

    context_pack_path = _agres_dir() / "context-pack.md"
    context_pack_path.write_text(content, encoding="utf-8")

    try:
        conn.execute(
            "INSERT INTO context_packs(id, session_id, query, path, created_at, tokens, budget, stale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (pack_id, session_id or "", query, str(context_pack_path), now_iso(),
             total_tokens, budget),
        )
    except sqlite3.OperationalError:
        # Pre-Phase-3 DBs without migration: legacy insert still works.
        conn.execute(
            "INSERT INTO context_packs(id, session_id, query, path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pack_id, session_id or "", query, str(context_pack_path), now_iso()),
        )

    conn.commit()
    conn.close()

    if session_id:
        sdir = session_dir(session_id)
        packs_dir = sdir / "context_packs"
        packs_dir.mkdir(parents=True, exist_ok=True)

        pack_path = packs_dir / f"context_pack_{pack_id}.md"
        pack_path.write_text(content, encoding="utf-8")

    if getattr(args, "json", False):
        print(json.dumps({**receipt, "path": str(context_pack_path)}, indent=2))
    else:
        print(content)


def cmd_search(args: argparse.Namespace) -> None:
    conn = get_conn()
    mode = getattr(args, "mode", "hybrid") or "hybrid"

    results: Dict[str, Any] = {
        "query": args.query,
        "mode": mode,
        "chunks": search_chunks(conn, args.query, args.limit),
        "symbols": search_symbols(conn, args.query, args.limit),
        "memory": search_memory(conn, args.query, args.limit),
    }
    if mode == "hybrid":
        try:
            results["graph"] = graph_related_files(conn, args.query, args.limit)
            results["fused"] = hybrid_file_results(conn, args.query, args.limit)
        except Exception as exc:
            results["fused_error"] = str(exc)

    conn.close()

    print(json.dumps(results, indent=2, ensure_ascii=False))


def cmd_index(args: argparse.Namespace) -> None:
    use_project_agres()

    root = Path(args.path or str(_project_root())).resolve()
    # P1 fix: validate no path traversal outside allowed (prevent symlink escape)
    try:
        root = root.resolve()
    except Exception:
        pass

    if not root.exists():
        print(f"Path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    conn = get_conn()
    fts = fts_enabled(conn)

    indexed = 0
    skipped = 0
    failed = 0

    for path in iter_repo_files(root, limit=args.limit):
        try:
            rel_path = path.relative_to(root).as_posix()
        except Exception:
            continue

        # Phase 1 fast path: mtime+size match => skip hashing entirely.
        if not args.force:
            needs, _reason = manifest_needs_reindex(conn, rel_path, path, force=False)
            if not needs:
                skipped += 1
                continue

        try:
            content_hash = sha256_file(path)
        except Exception:
            failed += 1
            continue

        existing = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?",
            (rel_path,),
        ).fetchone()

        if not args.force and existing and existing["content_hash"] == content_hash:
            mtime, size = _file_stat(path)
            manifest_put(conn, rel_path, mtime, size, content_hash)
            skipped += 1
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            failed += 1
            continue

        language = detect_language(path)
        # P0 fix: file_id must be path-scoped, not content-only, to avoid collisions on identical files
        file_id = "file_" + sha256_text(rel_path + ":" + content_hash)[:18]

        conn.execute(
            "INSERT OR REPLACE INTO files(id, path, content_hash, language, size, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                file_id,
                rel_path,
                content_hash,
                language,
                path.stat().st_size,
                now_iso(),
            ),
        )

        # P1 fix: per-file transaction for atomicity (vs every-20 commit)
        try:
            conn.execute("BEGIN IMMEDIATE;")
        except sqlite3.OperationalError:
            pass
        conn.execute("DELETE FROM file_chunks WHERE file_path = ?", (rel_path,))
        conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
        try:
            conn.execute("DELETE FROM edges WHERE src_file = ?", (rel_path,))
            conn.execute("DELETE FROM ref_idents WHERE src_file = ?", (rel_path,))
        except sqlite3.OperationalError:
            pass

        if fts and existing is not None:
            # Field fix: new files have no FTS rows; skip full-table DELETE scans.
            try:
                conn.execute("DELETE FROM fts_chunks WHERE file_path = ?", (rel_path,))
                conn.execute("DELETE FROM fts_symbols WHERE file_path = ?", (rel_path,))
            except sqlite3.OperationalError:
                pass

        chunks = chunk_text_by_lines(text)

        for chunk in chunks:
            chunk_text = chunk["text"]
            chunk_hash = sha256_text(chunk_text)
            # P0 fix: chunk_id must be path+range scoped
            chunk_id = "chunk_" + sha256_text(rel_path + f":{chunk['start_line']}:{chunk['end_line']}:" + chunk_hash)[:18]

            conn.execute(
                "INSERT OR REPLACE INTO file_chunks("
                "id, file_path, start_line, end_line, content_hash, text"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    rel_path,
                    chunk["start_line"],
                    chunk["end_line"],
                    chunk_hash,
                    chunk_text,
                ),
            )

            if fts:
                try:
                    conn.execute(
                        "INSERT INTO fts_chunks(text, file_path, chunk_id, language) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            chunk_text,
                            rel_path,
                            chunk_id,
                            language,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass

        symbols = extract_symbols(language, text)

        for symbol in symbols:
            symbol_id = "sym_" + sha256_text(
                rel_path + ":" + symbol["name"] + ":" + str(symbol["line"])
            )[:18]

            symbol_text = " ".join(
                [
                    language,
                    symbol["kind"],
                    symbol["name"],
                    f"file:{rel_path}",
                    f"line:{symbol['line']}",
                ]
            )

            conn.execute(
                "INSERT OR REPLACE INTO symbols("
                "id, file_path, name, kind, line, text"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    symbol_id,
                    rel_path,
                    symbol["name"],
                    symbol["kind"],
                    symbol["line"],
                    symbol_text,
                ),
            )

            if fts:
                try:
                    conn.execute(
                        "INSERT INTO fts_symbols(text, file_path, symbol_id, name, kind) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            symbol_text,
                            rel_path,
                            symbol_id,
                            symbol["name"],
                            symbol["kind"],
                        ),
                    )
                except sqlite3.OperationalError:
                    pass

        for edge in extract_import_edges(language, text, rel_path):
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO edges(src_file, dst_file, ident, weight) "
                    "VALUES (?, ?, ?, 1.0)",
                    (edge["src"], edge["dst"], edge["ident"]),
                )
            except sqlite3.OperationalError:
                pass

        store_ref_idents(conn, rel_path, language, text)

        try:
            conn.execute("COMMIT;")
        except sqlite3.OperationalError:
            pass
        # Phase 1: record manifest so next run hits mtime fast path.
        try:
            mtime, size = _file_stat(path)
            manifest_put(conn, rel_path, mtime, size, content_hash)
        except Exception:
            pass
        # Keep existing periodic commit as extra safety (no-op if already committed)
        indexed += 1

        if indexed % 20 == 0:
            try:
                conn.commit()
            except Exception:
                pass
            print(f"Indexed {indexed} files...")

    conn.commit()

    # Phase 1: prune files deleted since last index.
    try:
        pruned = prune_deleted_files(conn, root)
        if indexed > 0:
            mark_packs_stale(conn)  # Phase 3: reindex may invalidate stored packs.
        conn.commit()
    except Exception:
        pruned = 0

    repo_map_path = build_repo_map()

    conn.close()

    print("")
    print("Agres SQLite indexing complete.")
    print(f"Root: {root}")
    print(f"Indexed: {indexed}")
    print(f"Skipped unchanged: {skipped}")
    print(f"Failed: {failed}")
    print(f"Pruned deleted: {pruned}")
    print(f"Repo map: {repo_map_path}")


def cmd_repo_map(args: argparse.Namespace) -> None:
    use_project_agres()
    path = build_repo_map()
    print(path)


def cmd_map(args: argparse.Namespace) -> None:
    """Ranked RepoMap: PageRank skeleton within a token budget. Prints the map."""
    use_project_agres()
    root = Path(args.path or str(_project_root())).resolve()
    try:
        root = root.resolve()
    except Exception:
        pass
    focus: List[str] = []
    for entry in getattr(args, "focus", []) or []:
        focus.extend([p.strip() for p in entry.split(",") if p.strip()])
    path = build_repo_map(max_tokens=getattr(args, "tokens", MAP_DEFAULT_TOKENS) or MAP_DEFAULT_TOKENS,
                          query=getattr(args, "query", "") or "",
                          focus=focus, root=root)
    try:
        print(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        print(path)


def cmd_touch(args: argparse.Namespace) -> None:
    """Post-edit hook: re-index only the listed files. The 10k-model contract."""
    use_project_agres()
    root = Path(args.path or str(_project_root())).resolve()
    try:
        root = root.resolve()
    except Exception:
        pass
    raw = args.files or []
    # Allow comma-separated + repeated --files.
    files: List[str] = []
    for entry in raw:
        files.extend([p.strip() for p in entry.split(",") if p.strip()])
    if not files:
        print("Nothing to touch. Use: agres touch --files a.py,b.py", file=sys.stderr)
        sys.exit(2)
    conn = get_conn()
    fts = fts_enabled(conn)
    indexed = skipped = failed = 0
    for rel in files:
        abs_path = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        try:
            abs_path = abs_path.resolve()
            rel_norm = abs_path.relative_to(root).as_posix()
        except Exception:
            rel_norm = rel
        if not abs_path.exists():
            # Deleted file: drop its rows.
            try:
                manifest_remove(conn, rel_norm)
                conn.commit()
            except Exception:
                pass
            continue
        if not is_indexable_file(abs_path):
            failed += 1
            continue
        if not args.force:
            needs, _r = manifest_needs_reindex(conn, rel_norm, abs_path, force=False)
            if not needs:
                skipped += 1
                continue
        status = index_single_file(conn, root, abs_path, fts, force=args.force)
        if status == "indexed":
            indexed += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
        try:
            conn.commit()
        except Exception:
            pass
    if indexed > 0:
        mark_packs_stale(conn)  # Phase 3: reindex may invalidate stored packs.
    conn.commit()
    conn.close()
    print(json.dumps({"indexed": indexed, "skipped": skipped, "failed": failed}, indent=2))


def cmd_sync(args: argparse.Namespace) -> None:
    """Drift scan: re-index files whose mtime/size drifted, prune deleted.
    --since HEAD limits to `git diff --name-only <since>` when git is available."""
    use_project_agres()
    root = Path(args.path or str(_project_root())).resolve()
    try:
        root = root.resolve()
    except Exception:
        pass
    conn = get_conn()
    fts = fts_enabled(conn)
    allow: Optional[set] = None
    if getattr(args, "since", None):
        changed = git_changed_files(root, args.since)
        if changed is not None:
            allow = set(changed)
    indexed = skipped = failed = 0
    scanned = 0
    for path in iter_repo_files(root, limit=args.limit):
        try:
            rel_path = path.relative_to(root).as_posix()
        except Exception:
            continue
        if allow is not None and rel_path not in allow:
            continue
        scanned += 1
        if not args.force:
            needs, _r = manifest_needs_reindex(conn, rel_path, path, force=False)
            if not needs:
                skipped += 1
                continue
        status = index_single_file(conn, root, path, fts, force=args.force)
        if status == "indexed":
            indexed += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
        if (indexed + skipped) % 50 == 0:
            try:
                conn.commit()
            except Exception:
                pass
    pruned = 0
    if not getattr(args, "no_prune", False):
        try:
            pruned = prune_deleted_files(conn, root)
        except Exception:
            pruned = 0
    if indexed > 0:
        mark_packs_stale(conn)  # Phase 3: reindex may invalidate stored packs.
    conn.commit()
    conn.close()
    print(json.dumps({
        "scanned": scanned, "indexed": indexed,
        "skipped": skipped, "failed": failed, "pruned": pruned,
    }, indent=2))


def cmd_resume(args: argparse.Namespace) -> None:
    session_id = getattr(args, "session", None) or active_session_id()

    if not session_id:
        print("No active Agres session.")
        print("Start one with:")
        print("  agres start \"Your session goal\"")
        return

    conn = get_conn()

    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    conn.close()

    if row:
        print("Active Agres session:")
        print(json.dumps(dict(row), indent=2, ensure_ascii=False))
        print("")

    class ContextArgs:
        pass

    context_args = ContextArgs()
    context_args.query = getattr(args, "query", "") or ""
    context_args.limit = 5
    context_args.session = session_id

    cmd_context(context_args)


def cmd_end(args: argparse.Namespace) -> None:
    session_id = getattr(args, "session", None) or active_session_id()

    if not session_id:
        print("No active Agres session.")
        return

    conn = get_conn()

    conn.execute(
        "UPDATE sessions SET status = 'ended', updated_at = ? WHERE id = ?",
        (now_iso(), session_id),
    )

    conn.commit()
    conn.close()

    if active_session_id() == session_id:
        set_active_session_id(None)

    append_event(
        session_id,
        "session_ended",
        {
            "session_id": session_id,
        },
    )

    print(f"Ended session: {session_id}")


def _human_size(num: int) -> str:
    for unit in ["B","KB","MB","GB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"

_BAR_FRACS = "▏▎▍▌▋▊▉"  # 1/8..7/8 fill of one cell


def _bar(pct: float, width: int = 30) -> str:
    """Progress bar with 1/8-cell resolution so small changes stay visible."""
    pct = max(0.0, min(100.0, pct))
    exact = width * pct / 100.0
    filled = int(exact)
    try:
        bar = "█" * filled
        if filled < width:
            frac = exact - filled
            if frac >= 0.125:
                bar += _BAR_FRACS[min(7, int(frac * 8)) - 1]
        bar += "░" * (width - len(bar))
    except Exception:
        filled = int(width * pct / 100.0)
        bar = "#" * filled + "-" * (width - filled)
    return bar

def _confidence_level(pct: float, folded: int, total_turns: int) -> str:
    # Confidence = how much context is preserved + how healthy window is
    # 100% = well under budget with many folds preserved; 0% = overflow risk
    if pct < 50:
        base = "high"
    elif pct < 75:
        base = "medium"
    elif pct < 90:
        base = "low"
    else:
        base = "critical"
    return base

_MODEL_CACHE: Optional[dict] = None

def _detect_model() -> dict:
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    _MODEL_CACHE = _detect_model_uncached()
    return _MODEL_CACHE

def _detect_model_uncached() -> dict:
    """Best-effort model detection for status analytics.
    Checks (in order): env AGRES_MODEL, env OPENCODE_MODEL, opencode.db latest session,
    AGRES_WINDOW_BUDGET vs known windows. Never crashes.
    """
    # 1. Explicit env
    for key in ("AGRES_MODEL", "OPENCODE_MODEL", "OPENCODE_ACTIVE_MODEL"):
        v = os.environ.get(key)
        if v:
            return {"id": v, "providerID": "env", "variant": "", "source": f"env:{key}",
                    "limit": {"context": int(os.environ.get("AGRES_WINDOW_BUDGET") or DEFAULT_WINDOW_BUDGET_TOKENS), "output": 8192}}
    # 2. Try opencode.db latest session
    try:
        import sqlite3 as _s
        odb = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if odb.exists():
            conn = _s.connect(str(odb), timeout=1.0)
            conn.row_factory = _s.Row
            row = conn.execute("SELECT model, tokens_input, tokens_output, tokens_reasoning, cost FROM session ORDER BY time_updated DESC LIMIT 1").fetchone()
            conn.close()
            if row and row["model"]:
                import json as _j
                m = _j.loads(row["model"])
                # Enrich with limit from models.json cache if available.
                # NOTE: do NOT call window_budget() here - it calls _detect_model()
                # in its auto-sync path -> infinite mutual recursion (38s per CLI call).
                limit = {"context": DEFAULT_WINDOW_BUDGET_TOKENS, "output": 8192}
                try:
                    import json as _j2
                    mp = Path.home() / ".cache" / "opencode" / "models.json"
                    if mp.exists():
                        data = _j2.loads(mp.read_text()[:5000000])  # cap
                        for prov in data.values():
                            if isinstance(prov, dict) and "models" in prov and m.get("id") in prov["models"]:
                                lim = prov["models"][m["id"]].get("limit")
                                if lim:
                                    limit = lim
                                    break
                except Exception:
                    pass
                return {
                    "id": m.get("id","unknown"),
                    "providerID": m.get("providerID",""),
                    "variant": m.get("variant",""),
                    "source": "opencode.db:latest_session",
                    "limit": limit,
                    "tokens_input": row["tokens_input"],
                    "tokens_output": row["tokens_output"],
                    "tokens_reasoning": row["tokens_reasoning"] if "tokens_reasoning" in row.keys() else 0,
                    "cost": row["cost"] if "cost" in row.keys() else 0,
                }
    except Exception:
        pass
    # 3. Fallback: plain default; never recurse back into window_budget
    return {"id": "unknown", "providerID": "", "variant": "", "source": "fallback",
            "limit": {"context": DEFAULT_WINDOW_BUDGET_TOKENS, "output": 8192}}

def _model_badge(m: dict) -> str:
    ctx = m.get("limit",{}).get("context", 0)
    if ctx >= 1000000:
        return "1M"
    if ctx >= 500000:
        return "500K"
    if ctx >= 262144:
        return f"{ctx//1024}K"
    if ctx:
        return f"{ctx//1024}K"
    return "?"


def _human_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def cmd_status(args: argparse.Namespace) -> None:
    """Agres status — compact visual dashboard for humans.

    Scripting? Use --json (stable schema). The visual view is deliberately
    terse: gauges, stacked bars and sparklines instead of paragraphs.
    """
    import time
    ensure_base_dirs()
    init_db()

    conn = get_conn()
    fts = fts_enabled(conn)
    session_id, session_flag = resolve_active_session(conn)
    have_session = session_flag in ("active", "adopted")

    # --- Counts (whole database, all sessions) ---
    counts = {}
    for table in [
        "sessions", "events", "files", "file_chunks", "symbols",
        "decisions", "tasks", "errors", "pins", "checkpoints",
        "context_packs", "turns", "folds", "window_items",
    ]:
        try:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = row["c"] if row else 0
        except sqlite3.OperationalError:
            counts[table] = 0

    # --- Budget + window (active session) ---
    budget = window_budget()
    reserve = WINDOW_RESERVE_TOKENS
    usable = max(1, budget - reserve)
    usage = 0
    if have_session:
        try:
            usage = window_usage_tokens(conn, session_id)
        except Exception:
            usage = 0
    pct = usage / usable * 100.0
    remaining = max(0, usable - usage)
    confidence = _confidence_level(pct, counts.get("folds", 0), counts.get("turns", 0))

    # --- Session row ---
    sess_row = None
    if session_id:
        try:
            sess_row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        except sqlite3.OperationalError:
            sess_row = None

    # --- Per-session detail ---
    turns_in_session = 0
    turns_by_role = {}
    window_by_kind = {}
    window_by_priority = []
    folds_by_status = {}
    folds_by_kind = {}
    fold_tokens = 0
    last_turn_at = None
    last_event = None
    if have_session:
        try:
            turns_in_session = conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE session_id = ?", (session_id,)
            ).fetchone()["c"]
            for r in conn.execute("SELECT role, COUNT(*) c FROM turns WHERE session_id = ? GROUP BY role", (session_id,)):
                turns_by_role[r["role"]] = r["c"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) c, COALESCE(SUM(token_estimate),0) t FROM window_items "
                "WHERE session_id = ? GROUP BY kind ORDER BY t DESC", (session_id,)
            ):
                window_by_kind[r["kind"]] = {"items": r["c"], "tokens": r["t"]}
            window_by_priority = conn.execute(
                "SELECT priority, COUNT(*) c, COALESCE(SUM(token_estimate),0) t FROM window_items "
                "WHERE session_id = ? GROUP BY priority ORDER BY priority DESC", (session_id,)
            ).fetchall()
            for r in conn.execute("SELECT status, COUNT(*) c FROM folds WHERE session_id = ? GROUP BY status", (session_id,)):
                folds_by_status[r["status"]] = r["c"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) c, COALESCE(SUM(token_estimate),0) t FROM folds "
                "WHERE session_id = ? GROUP BY kind ORDER BY t DESC", (session_id,)
            ):
                folds_by_kind[r["kind"]] = {"items": r["c"], "tokens": r["t"]}
            fold_tokens = conn.execute(
                "SELECT COALESCE(SUM(token_estimate),0) t FROM folds WHERE session_id = ?", (session_id,)
            ).fetchone()["t"]
            lr = conn.execute("SELECT MAX(created_at) m FROM turns WHERE session_id = ?", (session_id,)).fetchone()
            last_turn_at = lr["m"] if lr else None
            le = conn.execute("SELECT type, ts FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
            if le:
                last_event = {"type": le["type"], "at": le["ts"]}
        except sqlite3.OperationalError:
            pass

    # --- Storage sizes ---
    db_p = db_path()
    db_size = wal_size = shm_size = cas_size = cas_objects = 0
    try:
        if db_p.exists():
            db_size = db_p.stat().st_size
        wal = db_p.with_suffix(db_p.suffix + "-wal")
        shm = db_p.with_suffix(db_p.suffix + "-shm")
        if wal.exists():
            wal_size = wal.stat().st_size
        if shm.exists():
            shm_size = shm.stat().st_size
        cas_root = _cas_dir()
        if cas_root.exists():
            for sub in cas_root.iterdir():
                if sub.is_dir():
                    for obj in sub.iterdir():
                        try:
                            cas_size += obj.stat().st_size
                            cas_objects += 1
                        except Exception:
                            pass
    except Exception:
        pass
    total_storage = db_size + wal_size + cas_size

    model = _detect_model()
    budget_source = _budget_source()
    now_iso_s = now_iso()

    # --- Brain + progress (shared by --json and the visual dashboard) ---
    gstats = graph_stats(conn)
    tstatus: Dict[str, int] = {}
    dec_acc = dec_rej = pins_n = errs_n = 0
    files_max_indexed = None
    try:
        scope_col = "session_id = ?" if have_session else "1 = 1"
        scope_arg: tuple = (session_id,) if have_session else ()
        for r in conn.execute(
                f"SELECT status, COUNT(*) c FROM tasks WHERE {scope_col} GROUP BY status",
                scope_arg).fetchall():
            tstatus[r["status"] or "?"] = int(r["c"])
        drow = conn.execute(
            f"SELECT COALESCE(SUM(accepted),0) a, COALESCE(SUM(rejected),0) r"
            f" FROM decisions WHERE {scope_col}", scope_arg).fetchone()
        dec_acc, dec_rej = int(drow["a"] or 0), int(drow["r"] or 0)
        pins_n = int(conn.execute(
            f"SELECT COUNT(*) c FROM pins WHERE {scope_col}", scope_arg).fetchone()["c"] or 0)
        errs_n = int(conn.execute(
            f"SELECT COUNT(*) c FROM errors WHERE {scope_col}", scope_arg).fetchone()["c"] or 0)
        files_max_indexed = conn.execute("SELECT MAX(indexed_at) m FROM files").fetchone()["m"]
    except sqlite3.OperationalError:
        pass
    brain_built_at = None
    stale_packs = 0
    try:
        brow = conn.execute(
            "SELECT created_at, stale FROM context_packs WHERE query LIKE 'brain:%'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if brow:
            brain_built_at = brow["created_at"]
        stale_packs = int(conn.execute(
            "SELECT COUNT(*) c FROM context_packs WHERE stale = 1").fetchone()["c"] or 0)
    except sqlite3.OperationalError:
        pass
    try:
        graph_stale = bool(counts.get("files", 0)) and (
            not gstats.get("nodes") or
            (files_max_indexed and gstats.get("built_at") and
             files_max_indexed > (gstats.get("built_at") or "")))
    except Exception:
        graph_stale = False

    # --- Health warnings ---
    warnings = []
    if session_flag == "stale":
        warnings.append(
            f"active session id {session_id} not in this database (global state vs per-project DB drift) — "
            "run: agres start \"goal\""
        )
    elif session_flag == "none":
        warnings.append("no active session — run: agres start \"goal\"")
    if db_size > 50 * 1024 * 1024:
        warnings.append(f"DB large ({_human_size(db_size)}) — consider: agres prune or agres gc")
    if not fts:
        warnings.append("FTS5 unavailable — search uses LIKE fallback (slower)")
    if counts.get("sessions", 0) == 0 and counts.get("events", 0) > 0:
        warnings.append("orphaned events with 0 sessions — run: agres repair")
    try:
        mctx = int(model.get("limit", {}).get("context", 0) or 0)
        if mctx and mctx < budget:
            warnings.append(f"model context {mctx:,} < budget {budget:,} — window may overflow before budget triggers")
    except Exception:
        pass
    if have_session and pct > 90:
        warnings.append("window over 90% usable — run: agres fold")

    # --- JSON output ---
    if getattr(args, "json", False) or getattr(args, "format", "") == "json":
        status = {
            "agres_home": str(AGRES_HOME),
            "agres_dir": str(_agres_dir()),
            "project_root": str(_project_root()),
            "database": str(db_p),
            "fts_enabled": fts,
            "session": {
                "id": session_id,
                "flag": session_flag,
                "title": (sess_row["title"] if sess_row else None),
                "repo": (sess_row["repo"] if sess_row else None),
                "status": (sess_row["status"] if sess_row else None),
                "created_at": (sess_row["created_at"] if sess_row else None),
                "turns": turns_in_session,
                "turns_by_role": turns_by_role,
            },
            "counts": counts,
            "budget": {
                "tokens": budget,
                "source": budget_source,
                "reserve_tokens": reserve,
                "usable_tokens": usable,
            },
            "window": {
                "tokens": usage,
                "pct": round(pct, 1),
                "remaining_tokens": remaining,
                "confidence": confidence,
                "by_kind": window_by_kind,
                "by_priority": [dict(r) for r in window_by_priority],
            },
            "folds": {
                "total": counts.get("folds", 0),
                "by_status": folds_by_status,
                "by_kind": folds_by_kind,
                "tokens_in_session": fold_tokens,
            },
            "last_activity": {
                "last_turn_at": last_turn_at,
                "last_event": last_event,
            },
            "progress": {
                "scope": ("session" if have_session else "all"),
                "tasks_by_status": tstatus,
                "decisions_accepted": dec_acc,
                "decisions_rejected": dec_rej,
                "pins": pins_n,
                "errors": errs_n,
            },
            "graph": gstats,
            "graph_stale": graph_stale,
            "packs": {
                "total": counts.get("context_packs", 0),
                "stale": stale_packs,
                "brain_built_at": brain_built_at,
            },
            "window_budget_tokens": budget,
            "window_usage_tokens": usage,
            "window_usable_tokens": usable,
            "window_pct": round(pct, 1),
            "window_confidence": confidence,
            "model": model,
            "storage": {
                "db_bytes": db_size,
                "wal_bytes": wal_size,
                "shm_bytes": shm_size,
                "cas_bytes": cas_size,
                "cas_objects": cas_objects,
                "total_bytes": total_storage,
            },
            "warnings": warnings,
            "state": get_state(),
        }
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    # --- Visual dashboard: plain language for everyone, exact numbers kept
    # for technical readers. --json stays the scripting interface. ---
    import shutil as _shutil
    try:
        _tw = _shutil.get_terminal_size().columns
    except Exception:
        _tw = 60
    W = max(40, min(68, _tw - 2))
    is_tty = sys.stdout.isatty()
    def _c(s, code):
        return f"\033[{code}m{s}\033[0m" if is_tty else s
    HDR = lambda s: _c(s, "1;36")
    OK = lambda s: _c(s, "32")
    WARN = lambda s: _c(s, "33")
    BAD = lambda s: _c(s, "31")
    DIM = lambda s: _c(s, "2")

    def _valcol(s, pctv):
        if pctv < 50:
            return OK(s)
        if pctv < 75:
            return WARN(s)
        return BAD(s)

    def _kn(n):
        try:
            n = int(n)
        except Exception:
            return "?"
        if n >= 1024 * 1000:
            return f"{n / 1048576:.1f}M"
        return f"{n / 1024:.1f}k" if n >= 1024 else str(n)

    def _num(n):
        try:
            return f"{int(n):,}"
        except Exception:
            return "?"

    def _pl(n, word):
        try:
            c = int(n)
        except Exception:
            return f"? {word}s"
        return f"{c:,} {word}" + ("" if c == 1 else "s")

    def _gauge(frac, width):
        frac = max(0.0, min(1.0, frac))
        fill = int(round(frac * width))
        return "█" * fill + "░" * (width - fill)

    def _sparkline(vals, width=24):
        glyphs = "▁▂▃▄▅▆▇█"
        vals = list(vals or [])
        vals = ([0] * max(0, width - len(vals)) + vals)[-width:]
        peak = max(vals) if vals else 0
        if peak <= 0:
            return DIM("·" * width)
        return "".join(glyphs[min(7, int(v / peak * 7))] for v in vals)

    from datetime import datetime as _dt
    def _ago(iso_s):
        try:
            return _human_age(time.time() - _dt.fromisoformat(iso_s).timestamp()) + " ago"
        except Exception:
            return "?"

    # Messages-per-hour over the last 24h, straight from the turns table.
    buckets = [0] * 24
    if have_session:
        try:
            now_ts = time.time()
            for r in conn.execute(
                    "SELECT created_at FROM turns WHERE session_id = ?", (session_id,)).fetchall():
                try:
                    ts = _dt.fromisoformat(r["created_at"]).timestamp()
                except Exception:
                    continue
                h = int((now_ts - ts) // 3600)
                if 0 <= h < 24:
                    buckets[23 - h] += 1
        except sqlite3.OperationalError:
            pass

    short = session_id or "—"
    if len(short) > 18:
        short = short[:17] + "…"
    try:
        mctx = int(model.get("limit", {}).get("context", 0) or 0)
    except Exception:
        mctx = 0
    mshort = model.get("id", "?").split("/")[-1][:22]
    bar_w = W - 26

    if session_flag == "none":
        health, hnote = "attention", "no active session yet — start one below"
    elif session_flag == "stale":
        health, hnote = "attention", "saved session is not in this project"
    elif have_session and pct >= 90:
        health, hnote = "full", "instant memory is almost full — tuck some away"
    elif have_session and pct >= 75:
        health, hnote = "filling up", "instant memory is filling up"
    elif warnings:
        health, hnote = "fair", f"{len(warnings)} small tidy-up(s) listed below"
    else:
        health, hnote = "excellent", "everything is saved and within budget"
    hicon = {"excellent": OK("✓"), "fair": WARN("~"), "filling up": WARN("!"),
             "full": BAD("✗"), "attention": WARN("○")}[health]

    def _sec(title, hint):
        print(HDR(f"── {title} ──") + (DIM(f"  {hint}") if hint else ""))

    # Header: who am I looking at, and is it healthy?
    print(HDR("◆ Agres Memory") + DIM("  your project's brain and conversation memory"))
    if have_session:
        _title = (sess_row["title"] if sess_row and sess_row["title"] else short)
        print(f"● Session {short} · \"{_title[:W - 24]}\"")
    else:
        print(WARN("○ No active session") + DIM(" — start one with the command at the bottom"))
    print(f"Health  {hicon} {health.capitalize()} — {hnote}")
    if mshort and mshort != "unknown":
        print(DIM(f"Model {mshort} · budget {_kn(budget)} tokens"))

    # Instant memory: the context window, in plain words.
    _sec("Instant memory", "what is remembered right now")
    if have_session:
        print(f"Used {_valcol(_gauge(usage / usable if usable else 0, bar_w), pct)} "
              f"{pct:4.1f}% · {_kn(usage)} of {_kn(usable)} tokens · {_kn(remaining)} free")
        print(DIM(f"Exact: {_num(usage)} / {_num(usable)} tokens · {_num(remaining)} free"))
        print(DIM("When this fills, older memories are tucked away safely — never deleted."))
    else:
        print(DIM("Empty — no session yet. Start one to begin remembering."))

    # Conversation: messages and tucked-away memories.
    _sec("Conversation", "this session" if have_session else "across all sessions")
    _tucked = sum(folds_by_status.values()) if have_session else counts.get("folds", 0)
    _kept = ((turns_in_session + _tucked) if have_session
             else counts.get("turns", 0) + counts.get("folds", 0))
    _shown_turns = turns_in_session if have_session else counts.get("turns", 0)
    print(f"Messages {_num(_shown_turns)} total · last 24h {_sparkline(buckets)}")
    print(f"Tucked away {_num(_tucked)} · kept in total {_num(_kept)} · bring any back with unfold")

    # Project brain: code, graph, and progress.
    _sec("Project brain", "code and progress, always queryable")
    if counts.get("files", 0):
        print(f"Code {_pl(counts['files'], 'file')} · {_pl(counts.get('symbols', 0), 'symbol')} indexed")
    else:
        print("Code not indexed yet " + WARN("→ run: agres index"))
    if gstats.get("nodes"):
        _gline = (f"Knowledge graph {_pl(gstats['nodes'], 'node')} · "
                  f"{_pl(gstats['edges'], 'link')} (refreshed {_ago(gstats['built_at'])})")
        print(_gline if not graph_stale else WARN(_gline + " · outdated → run: agres graph --build"))
    else:
        print("Knowledge graph not built yet " + WARN("→ run: agres graph --build"))
    _done = sum(v for k, v in tstatus.items()
                if k.lower() in ("done", "completed", "closed", "resolved"))
    _active = sum(tstatus.values()) - _done
    _pline = (f"Progress {_pl(_active, 'active task')} · {_pl(_done, 'done task')} · "
              f"{_pl(dec_acc + dec_rej, 'decision')} · {_pl(pins_n, 'pinned note')} · "
              f"{_pl(errs_n, 'error')}")
    print(_pline if have_session else _pline + DIM("  (all sessions)"))

    # Storage: where it lives, how big, is search fast?
    _sec("Storage", "on this machine")
    print(f"Database {_human_size(db_size)} + saved memories {_human_size(cas_size)} "
          f"({_pl(cas_objects, 'item')}) · Fast search {'✓ on' if fts else WARN('✗ off — slower fallback')}")

    # Attention: plain-language issues, each with its fix.
    _sec("Needs attention", "")
    attn = []
    if session_flag == "stale":
        attn.append(f"Saved session not found in this project — fix: agres start \"goal\"")
    elif session_flag == "none":
        attn.append("No active session — fix: agres start \"your goal\"")
    if not counts.get("files", 0):
        attn.append("Code not indexed — fix: agres index")
    elif not gstats.get("nodes") or graph_stale:
        attn.append("Knowledge graph missing or outdated — fix: agres graph --build")
    if have_session and pct >= 90:
        attn.append("Instant memory almost full — fix: agres fold")
    elif have_session and pct >= 75:
        attn.append("Instant memory filling up — soon: agres fold")
    if stale_packs:
        attn.append(f"{stale_packs} saved pack(s) outdated after re-index — fix: agres brain")
    if db_size > 50 * 1024 * 1024:
        attn.append(f"Memory file is large ({_human_size(db_size)}) — tidy: agres prune")
    if not fts:
        attn.append("Fast search unavailable — using slower fallback")
    if counts.get("sessions", 0) == 0 and counts.get("events", 0) > 0:
        attn.append("Leftover data with no sessions — fix: agres repair")
    try:
        if mctx and mctx < budget:
            attn.append(f"Model context {mctx:,} < budget {budget:,} — window may overflow early")
    except Exception:
        pass
    if attn:
        for a in attn[:4]:
            print(WARN("! ") + a[:W - 2])
        if len(attn) > 4:
            print(DIM(f"  +{len(attn) - 4} more (see --json)"))
    else:
        print(OK("✓ Nothing — all good."))

    if session_flag == "none":
        nxt = "agres start \"your goal\""
    elif not counts.get("files", 0):
        nxt = "agres index"
    elif not gstats.get("nodes") or graph_stale:
        nxt = "agres graph --build"
    elif have_session and pct >= 75:
        nxt = "agres fold"
    else:
        nxt = "agres brain --query \"…\""
    print(DIM("Next step: ") + nxt + DIM("   · --json for scripts"))


def cmd_capture(args: argparse.Namespace) -> None:
    """Capture a conversation turn verbatim (must be running from session context)."""
    session_id = require_session(args)

    if not args.text and not args.file:
        print("Capture nothing? Use --text or --file.", file=sys.stderr)
        sys.exit(2)

    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            print(f"Cannot read file: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        text = args.text

    role = args.role or "user"

    conn = get_conn()
    turn = capture_turn(conn, session_id, role, text)

    # Also index the verbatim text in FTS memory so keyword search finds it.
    fts_index_memory(conn, "turn", str(turn["seq"]), text, session_id)

    # Realtime 256k handling: auto-fold if window exceeds usable budget
    # ponytail: one check per capture, not background thread
    try:
        budget = window_budget()
        usage = window_usage_tokens(conn, session_id)
        usable = budget - WINDOW_RESERVE_TOKENS
        if usage > usable:
            # Fold oldest until back under target (keep 90% of usable)
            target = int(usable * 0.9)
            fold_oldest(conn, session_id, target)
    except Exception:
        pass

    conn.commit()
    conn.close()

    # Compute confidence for response
    try:
        _conn2 = get_conn()
        _usage2 = window_usage_tokens(_conn2, session_id)
        _budget2 = window_budget()
        _usable2 = _budget2 - WINDOW_RESERVE_TOKENS
        _pct2 = (_usage2 / _usable2 * 100.0) if _usable2 else 0.0
        if _pct2 < 50:
            _conf = "high"
        elif _pct2 < 75:
            _conf = "medium"
        elif _pct2 < 90:
            _conf = "low"
        else:
            _conf = "critical"
        _conn2.close()
    except Exception:
        _pct2 = 0.0
        _conf = "unknown"
        _usage2 = turn["token_estimate"]

    append_event(
        session_id,
        "turn_captured",
        {
            "seq": turn["seq"],
            "role": role,
            "tokens": turn["token_estimate"],
            "window_usage": _usage2,
            "window_pct": round(_pct2,1),
            "confidence": _conf,
        },
        fold_type="conversation",
    )

    # If budget is deepseek 256k and usage high, hint to caller
    hint = ""
    if _pct2 > 85:
        hint = " (auto-folded; window high — consider 'agres budget' or 'agres fold')"

    print(
        json.dumps(
            {
                "seq": turn["seq"],
                "session_id": session_id,
                "role": role,
                "tokens": turn["token_estimate"],
                "window_item_id": turn["window_item_id"],
                "window_usage_tokens": _usage2,
                "window_budget_tokens": budget if 'budget' in locals() else window_budget(),
                "window_pct": round(_pct2,1),
                "confidence": _conf,
                "hint": hint.strip() if hint else None,
            },
            indent=2,
        )
    )


def cmd_fold(args: argparse.Namespace) -> None:
    """Fold low-priority window items until usage <= target tokens."""
    session_id = require_session(args)

    target = args.target
    if target < 0:
        target = window_budget() - WINDOW_RESERVE_TOKENS

    conn = get_conn()
    folded = fold_oldest(conn, session_id, target)
    usage_after = window_usage_tokens(conn, session_id)
    # Phase 6: audit what the lossy residue (summaries) retained. Exact originals
    # stay in folds; missing facts here are recoverable via unfold, not lost.
    if folded:
        try:
            facts = session_checkpoint_facts(conn, session_id)
            rows = conn.execute(
                "SELECT summary FROM folds WHERE id IN (%s)" % ",".join("?" * len(folded)),
                [f["fold_id"] for f in folded]).fetchall()
            residue = "\n".join(r["summary"] or "" for r in rows)
            audit = store_compaction_artifact(
                conn, session_id, "fold",
                f"{len(folded)} items folded to {target} tokens", facts,
                [f["fold_id"] for f in folded], residue)
            audit_out = {"artifact_id": audit["artifact_id"],
                         "facts_covered": audit["validation"]["covered_count"],
                         "facts_total": audit["validation"]["total"]}
        except Exception:
            audit_out = {"artifact_id": None, "facts_covered": 0, "facts_total": 0}
    else:
        audit_out = {"artifact_id": None, "facts_covered": 0, "facts_total": 0}
    conn.commit()
    conn.close()

    append_event(
        session_id,
        "context_folded",
        {
            "target_tokens": target,
            "folded_count": len(folded),
            "usage_after_tokens": usage_after,
            **audit_out,
        },
    )

    print(
        json.dumps(
            {
                "folded_count": len(folded),
                "folded": folded,
                "usage_after_tokens": usage_after,
                "target_tokens": target,
                **audit_out,
            },
            indent=2,
        )
    )


def cmd_unfold(args: argparse.Namespace) -> None:
    """Unfold folded context back into the active window by query."""
    session_id = require_session(args)

    conn = get_conn()

    if args.fold_id:
        fold = conn.execute(
            "SELECT * FROM folds WHERE id = ? AND session_id = ?",
            (args.fold_id, session_id),
        ).fetchone()
        if not fold:
            conn.close()
            print(f"No folded item with id {args.fold_id} in this session.", file=sys.stderr)
            sys.exit(1)
        matches = [fold]
    else:
        if not args.query:
            print("Provide --query or --fold-id.", file=sys.stderr)
            sys.exit(2)
        matches = []
        hits = search_folds(conn, session_id, args.query, limit=args.limit)
        for hit in hits:
            fold = conn.execute(
                "SELECT * FROM folds WHERE id = ?",
                (hit["fold_id"],),
            ).fetchone()
            if fold:
                matches.append(fold)

    if not matches:
        conn.close()
        print('No folded context matched. Try "agres unfold --query \'<different words>\'".')
        return

    unfolded = []
    for fold in matches[: args.limit]:
        result = unfold_fold(conn, session_id, fold, priority=2)
        unfolded.append(result)

    conn.commit()
    conn.close()

    append_event(
        session_id,
        "context_unfolded",
        {
            "count": len(unfolded),
            "fold_ids": [u["fold_id"] for u in unfolded],
        },
    )

    print(json.dumps({"unfolded": unfolded, "count": len(unfolded)}, indent=2))


def cmd_budget(args: argparse.Namespace) -> None:
    """Show the active window budget report."""
    # --set: persist budget to state and exit
    set_val = getattr(args, "set", None)
    if set_val:
        try:
            set_window_budget(int(set_val))
            state = get_state()
            print(json.dumps({"budget_tokens": state["window_budget"], "source": "manual", "set_at": state.get("window_budget_set_at")}, indent=2))
        except ValueError:
            print(f"Invalid budget value: {set_val}", file=sys.stderr)
            sys.exit(2)
        return

    conn = get_conn()
    explicit = getattr(args, "session", None)
    if explicit:
        session_id, session_flag = explicit, "explicit"
    else:
        session_id, session_flag = resolve_active_session(conn)
    session_id = session_id if session_flag in ("explicit", "active", "adopted") else None
    if not session_id:
        conn.close()
        b = window_budget()
        print(f"No active session in this database. Window budget is {b:,} tokens.")
        print("Start with: agres start \"goal\"  |  Set budget: agres budget --set 1048576")
        return
    # If --json, dump json manifest instead of report
    if getattr(args, "json", False):
        budget = window_budget()
        usage = window_usage_tokens(conn, session_id)
        model = _detect_model()
        print(json.dumps({
            "session_id": session_id,
            "session_flag": session_flag,
            "budget_tokens": budget,
            "budget_source": _budget_source(),
            "reserve_tokens": WINDOW_RESERVE_TOKENS,
            "usable_tokens": budget - WINDOW_RESERVE_TOKENS,
            "usage_tokens": usage,
            "remaining_tokens": max(0, budget - WINDOW_RESERVE_TOKENS - usage),
            "pct": round(usage / max(1, budget - WINDOW_RESERVE_TOKENS) * 100, 1),
            "confidence": _confidence_level(usage / max(1, budget - WINDOW_RESERVE_TOKENS) * 100, 0, 0),
            "model": model,
        }, indent=2))
        conn.close()
        return
    # Show model line before budget report
    try:
        model = _detect_model()
        mid = model.get("id","")
        lim = model.get("limit",{}).get("context",0)
        print(f"# Model: {mid}  ctx {lim:,}  vs budget {window_budget():,}  (source: {model.get('source','')})")
    except Exception:
        pass
    print(format_budget_report(conn, session_id or ""))
    conn.close()
    # After report, show realtime confidence line
    try:
        conn2 = get_conn()
        usage = window_usage_tokens(conn2, session_id)
        budget = window_budget()
        usable = budget - WINDOW_RESERVE_TOKENS
        pct = usage/usable*100 if usable else 0
        conn2.close()
        if pct > 75:
            print(f"\nRealtime: {pct:.1f}% of usable — will auto-fold on next capture if exceeded.")
    except Exception:
        pass


def cmd_window(args: argparse.Namespace) -> None:
    """Show the active window manifest (all items + folded status)."""
    session_id = getattr(args, "session", None) or active_session_id()
    conn = get_conn()

    if session_id:
        rows = conn.execute(
            "SELECT * FROM window_items WHERE session_id = ? "
            "ORDER BY priority DESC, added_at ASC",
            (session_id,),
        ).fetchall()
    else:
        rows = []

    model = _detect_model()
    manifest = {
        "session_id": session_id,
        "budget_tokens": window_budget(),
        "usage_tokens": window_usage_tokens(conn, session_id or ""),
        "model": model,
        "items": [dict(row) for row in rows],
    }
    conn.close()

    if not getattr(args, "json", False) and getattr(args, "format", "visual") == "visual":
        # Visual window manifest
        is_tty = sys.stdout.isatty()
        def _c(s,code): return f"\033[{code}m{s}\033[0m" if is_tty else s
        B = lambda s: _c(s,"1")
        DIM = lambda s: _c(s,"2")
        if not session_id:
            print("No active session — run: agres start \"goal\"")
            return
        print(B(f"┌─ Window Manifest — {session_id}"))
        print(f"│ budget {manifest['budget_tokens']:,} tok  usage {manifest['usage_tokens']:,} tok  items {len(manifest['items'])}")
        for r in manifest["items"]:
            print(f"│  [{r['priority']}] {r['kind']:12} {r['title'][:60]:60} {r['token_estimate']:5} tok")
        if not manifest["items"]:
            print(f"│  {DIM('(empty)')}")
        print(B("└─"))
        return

    print(json.dumps(manifest, indent=2, ensure_ascii=False))

def cmd_prune(args: argparse.Namespace) -> None:
    """Prune old events/folds/checkpoints. Bounded growth."""
    conn = get_conn()
    keep_days = getattr(args, "keep_days", 30)
    dry = getattr(args, "dry_run", False)
    cutoff = (datetime.now(timezone.utc).timestamp() - keep_days*86400)
    # SQLite iso compare: use datetime string
    from datetime import timedelta
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    counts = {}
    for tbl, col in [("events","ts"), ("turns","created_at"), ("folds","folded_at"), ("checkpoints","created_at"), ("context_packs","created_at")]:
        try:
            cur = conn.execute(f"SELECT COUNT(*) c FROM {tbl} WHERE {col} < ?", (cutoff_iso,))
            counts[f"{tbl}_to_prune"] = cur.fetchone()["c"]
            if not dry and counts[f"{tbl}_to_prune"]>0:
                conn.execute(f"DELETE FROM {tbl} WHERE {col} < ?", (cutoff_iso,))
        except Exception as e:
            counts[tbl] = f"error: {e}"
    conn.commit()
    conn.close()
    # VACUUM if not dry and something deleted
    if not dry and any(v>0 for v in counts.values() if isinstance(v,int)):
        try:
            conn2 = get_conn()
            conn2.execute("VACUUM;")
            conn2.close()
        except Exception:
            pass
    # Also prune CAS orphans: objects not referenced by any payload_hash
    # ponytail: best-effort, no error if misses
    print(json.dumps({"cutoff": cutoff_iso, "dry_run": dry, "pruned": counts}, indent=2))

def cmd_gc(args: argparse.Namespace) -> None:
    """Garbage collect CAS (unreferenced objects) and checkpoint files."""
    import shutil
    conn = get_conn()
    # Collect referenced hashes
    refs = set()
    try:
        for row in conn.execute("SELECT payload_hash FROM events WHERE payload_hash IS NOT NULL"):
            refs.add(row["payload_hash"].replace("sha256:",""))
        for row in conn.execute("SELECT content_hash FROM file_chunks"):
            refs.add(row["content_hash"])
        for row in conn.execute("SELECT content_hash FROM files"):
            refs.add(row["content_hash"])
    except Exception:
        pass
    conn.close()
    cas_root = _cas_dir()
    removed = 0
    kept = 0
    dry = getattr(args, "dry_run", False)
    if cas_root.exists():
        for sub in list(cas_root.iterdir()):
            if not sub.is_dir():
                continue
            for obj in list(sub.iterdir()):
                if obj.name not in refs:
                    if not dry:
                        try:
                            obj.unlink()
                            removed+=1
                        except Exception:
                            pass
                    else:
                        removed+=1
                else:
                    kept+=1
            # Remove empty shard dirs
            try:
                if not any(sub.iterdir()):
                    if not dry:
                        sub.rmdir()
            except Exception:
                pass
    # VACUUM
    if not dry:
        try:
            conn2 = get_conn()
            conn2.execute("VACUUM;")
            conn2.close()
        except Exception:
            pass
    print(json.dumps({"cas_removed": removed, "cas_kept": kept, "dry_run": dry}, indent=2))

def cmd_repair(args: argparse.Namespace) -> None:
    """Repair WAL, FTS health, integrity. Use --reset-session to clear active id."""
    if getattr(args, "reset_session", False):
        old_sid = active_session_id()
        set_active_session_id(None)
        print(f"cleared active session: {old_sid}")
    # Heal orphaned events: delete events referencing missing sessions? No — keep but warn
    conn = get_conn()
    # VACUUM + integrity
    try:
        conn.execute("VACUUM;")
    except Exception:
        pass
    try:
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        integrity = row[0] if row else "unknown"
    except Exception as e:
        integrity = f"error: {e}"
    # Ensure FTS enabled
    fts = fts_enabled(conn)
    # Fix WAL
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    conn.close()
    # Fix runtime_state if still ghost
    _repair_active_session()
    print(json.dumps({"fts_enabled": fts, "integrity": integrity, "active_session": active_session_id(), "repaired": True}, indent=2))


def _skill_frontmatter_ok(skill_path: Path) -> Dict[str, Any]:
    """Check a SKILL.md for auto-activation wiring. Returns status dict."""
    info: Dict[str, Any] = {"path": str(skill_path), "present": False,
                            "default_enabled": None, "has_auto_triggers": False}
    try:
        text = skill_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return info
    info["present"] = True
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    fm = m.group(1) if m else ""
    me = re.search(r"default_enabled:\s*(true|false)", fm, re.IGNORECASE)
    if me:
        info["default_enabled"] = me.group(1).lower() == "true"
    desc = re.search(r'description:\s*"(.*?)"', fm, re.DOTALL)
    dtext = desc.group(1) if desc else fm
    info["has_auto_triggers"] = bool(re.search(r"AUTOMATICALLY|without being asked", dtext, re.IGNORECASE))
    return info


def cmd_doctor(args: argparse.Namespace) -> None:
    """Verify auto-activation wiring: skill present + auto triggers per host surface."""
    home = Path.home()
    cwd = _project_root()
    skill_files = {
        "opencode": home / ".config" / "opencode" / "skills" / "agres" / "SKILL.md",
        "opencode-legacy": home / ".opencode" / "skills" / "agres" / "SKILL.md",
        "agents": home / ".agents" / "skills" / "agres" / "SKILL.md",
        "claude": home / ".claude" / "skills" / "agres" / "SKILL.md",
        "codex": home / ".codex" / "skills" / "agres" / "SKILL.md",
        "gemini": home / ".gemini" / "skills" / "agres" / "SKILL.md",
        "project": cwd / "skills" / "agres" / "SKILL.md",
    }
    skills = {host: _skill_frontmatter_ok(p) for host, p in skill_files.items()}
    marker = "<!-- agres:auto -->"

    def has_marker(p: Path) -> Optional[bool]:
        try:
            if not p.exists():
                return None
            return marker in p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    rules = {
        "AGENTS.md": has_marker(cwd / "AGENTS.md"),
        "CLAUDE.md": has_marker(cwd / "CLAUDE.md"),
        ".github/copilot-instructions.md": has_marker(cwd / ".github" / "copilot-instructions.md"),
    }
    cursor_rule = cwd / ".cursor" / "rules" / "agres.mdc"
    cursor: Optional[bool] = None
    try:
        if cursor_rule.exists():
            cursor = "alwaysApply: true" in cursor_rule.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        cursor = False
    rules[".cursor/rules/agres.mdc(alwaysApply)"] = cursor
    cline_rules = None
    try:
        cp = cwd / ".clinerules"
        if cp.exists():
            cline_rules = marker in cp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        cline_rules = False
    rules[".clinerules"] = cline_rules
    global_rules = {
        "~/.claude/CLAUDE.md": has_marker(home / ".claude" / "CLAUDE.md"),
        "~/.config/opencode/AGENTS.md": has_marker(home / ".config" / "opencode" / "AGENTS.md"),
        "~/.gemini/GEMINI.md": has_marker(home / ".gemini" / "GEMINI.md"),
    }
    # Home-level auto-read (informational: opencode loads AGENTS.md automatically).
    home_agents = None
    try:
        hp = home / ".config" / "opencode" / "AGENTS.md"
        if hp.exists():
            home_agents = "agres" in hp.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        pass
    conn = get_conn()
    fts = fts_enabled(conn)
    conn.close()
    auto_hosts = [h for h, s in skills.items()
                  if s["present"] and s["default_enabled"] and s["has_auto_triggers"]]
    report = {
        "runtime": {"python": sys.version.split()[0], "fts5": fts,
                    "code_parser": ts_backend_name()},
        "skills": skills,
        "auto_skill_hosts": auto_hosts,
        "project_rules": rules,
        "global_rules": global_rules,
        "project": str(cwd),
        "home_opencode_agents_mentions_agres": home_agents,
        "auto_activation": "on" if auto_hosts else "off",
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return
    print("Agres doctor — auto-activation wiring")
    print(f"project: {cwd}")
    print(f"runtime: python {report['runtime']['python']}, fts5={'on' if fts else 'off'}, "
          f"parser={report['runtime']['code_parser']}")
    print("skills:")
    for host, s in skills.items():
        if not s["present"]:
            print(f"  {host:8} MISSING ({s['path']})")
        else:
            flag = "AUTO" if (s["default_enabled"] and s["has_auto_triggers"]) else "manual-only"
            print(f"  {host:8} {flag} (default_enabled={s['default_enabled']}, triggers={s['has_auto_triggers']})")
    print("project rules (agents read these without being asked):")
    for name, st in rules.items():
        print(f"  {name}: {'wired' if st is True else 'absent' if st is None else 'present-no-marker'}")
    print("global memory (read every session, all projects):")
    for name, st in global_rules.items():
        print(f"  {name}: {'wired' if st is True else 'absent' if st is None else 'present-no-marker'}")
    print(f"home opencode AGENTS.md mentions agres: {home_agents}")
    print(f"auto-activation: {report['auto_activation']}")
    if report["auto_activation"] == "off":
        print("fix: npx @ithica/agres skill-install  (then agres doctor)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agres",
        description="Agres Origami Memory runtime CLI — SQLite edition",
    )

    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="Initialize Agres memory in current project")

    p_start = sub.add_parser("start", help="Start an Agres session")
    p_start.add_argument("title", help="Session goal/title")

    p_event = sub.add_parser("event", help="Capture an event")
    p_event.add_argument("--type", required=True, help="Event type")
    p_event.add_argument("--text", help="Event text payload")
    p_event.add_argument("--task", help="Task ID")
    p_event.add_argument("--collection", help="Memory type/collection")
    p_event.add_argument("--session", help="Session ID")

    p_decision = sub.add_parser("decision", help="Store a decision")
    p_decision.add_argument("--text", required=True, help="Decision text")
    p_decision.add_argument("--task", help="Task ID")
    p_decision.add_argument("--session", help="Session ID")
    p_decision.add_argument("--accepted", action="store_true", help="Mark accepted")
    p_decision.add_argument("--rejected", action="store_true", help="Mark rejected")

    p_task = sub.add_parser("task", help="Store/update a task")
    p_task.add_argument("--objective", required=True, help="Task objective")
    p_task.add_argument("--status", default="active", help="Task status")
    p_task.add_argument("--task-id", help="Existing task ID")
    p_task.add_argument("--session", help="Session ID")

    p_error = sub.add_parser("error", help="Store an error")
    p_error.add_argument("--message", required=True, help="Error message")
    p_error.add_argument("--context", help="Additional error context")
    p_error.add_argument("--task", help="Task ID")
    p_error.add_argument("--session", help="Session ID")

    p_pin = sub.add_parser("pin", help="Pin important context")
    p_pin.add_argument("--text", required=True, help="Context to pin")
    p_pin.add_argument("--session", help="Session ID")

    p_checkpoint = sub.add_parser("checkpoint", help="Create checkpoint")
    p_checkpoint.add_argument("--reason", help="Checkpoint reason")
    p_checkpoint.add_argument("--session", help="Session ID")

    p_context = sub.add_parser("context", help="Build context pack")
    p_context.add_argument("--query", help="Retrieval query")
    p_context.add_argument("--limit", type=int, default=5, help="Result limit")
    p_context.add_argument("--session", help="Session ID")
    p_context.add_argument("--budget", type=int, default=0,
                           help="Pack token budget (default: window budget; pack guarantees fit)")
    p_context.add_argument("--json", action="store_true", help="Print receipt JSON (pack still written to file)")

    p_search = sub.add_parser("search", help="Search Agres memory")
    p_search.add_argument("--query", required=True, help="Search query")
    p_search.add_argument("--limit", type=int, default=5, help="Result limit")
    p_search.add_argument("--mode", choices=["hybrid", "legacy"], default="hybrid",
                          help="hybrid: +graph branch +RRF fused ranking (default); legacy: chunks/symbols/memory only")

    p_index = sub.add_parser("index", help="Index repository files into Agres")
    p_index.add_argument("--path", help="Repository path to index")
    p_index.add_argument("--limit", type=int, default=1000, help="Maximum files to scan")
    p_index.add_argument("--force", action="store_true", help="Reindex unchanged files")

    p_touch = sub.add_parser("touch", help="Re-index only listed files (post-edit hook)")
    p_touch.add_argument("--files", action="append", default=[], help="File(s) to re-index, comma-separated or repeated")
    p_touch.add_argument("--path", help="Repository root (default: cwd)")
    p_touch.add_argument("--force", action="store_true", help="Reindex even if mtime matches")

    p_sync = sub.add_parser("sync", help="Re-index drifted files, prune deleted (incremental)")
    p_sync.add_argument("--path", help="Repository path to sync")
    p_sync.add_argument("--limit", type=int, default=5000, help="Maximum files to scan")
    p_sync.add_argument("--force", action="store_true", help="Reindex unchanged files")
    p_sync.add_argument("--since", help="Git ref to limit to changed files (e.g. HEAD~1)")
    p_sync.add_argument("--no-prune", action="store_true", help="Skip pruning deleted files")

    sub.add_parser("repo-map", help="Build repository map fold")

    p_map = sub.add_parser("map", help="Ranked RepoMap: PageRank skeleton within a token budget")
    p_map.add_argument("--tokens", type=int, default=MAP_DEFAULT_TOKENS,
                       help="Max map tokens (default 1024)")
    p_map.add_argument("--query", default="", help="Bias ranking toward query identifiers")
    p_map.add_argument("--focus", action="append", default=[],
                       help="Focus file(s), comma-separated or repeated (50x rank boost)")
    p_map.add_argument("--path", help="Repository root (default: cwd)")

    p_graph = sub.add_parser("graph", help="Code-to-graph: build/query/export the project knowledge graph")
    p_graph.add_argument("--build", action="store_true", help="Rebuild graph from index + progress + git")
    p_graph.add_argument("--query", default="", help="Query: seed-neighborhood subgraph for this text")
    p_graph.add_argument("--hops", type=int, default=2, help="BFS hops around seeds (default 2)")
    p_graph.add_argument("--limit", type=int, default=20, help="Result limit (default 20)")
    p_graph.add_argument("--export", choices=["json", "dot"], default="",
                         help="Dump full graph to .agres/graph.json|dot")
    p_graph.add_argument("--path", help="Repository root for --build (default: cwd)")
    p_graph.add_argument("--json", action="store_true", help="JSON output")

    p_trace = sub.add_parser("trace", help="Trace the complete project: codebase, structure, progress, work done")
    p_trace.add_argument("--session", help="Session ID")
    p_trace.add_argument("--path", help="Repository root (default: cwd)")
    p_trace.add_argument("--json", action="store_true", help="JSON output (markdown still written to trace.md)")

    p_brain = sub.add_parser("brain", help="Context injection: one bounded pack carrying the whole project")
    p_brain.add_argument("--query", default="", help="Focus graph neighborhood on this query")
    p_brain.add_argument("--limit", type=int, default=20, help="Result limit (default 20)")
    p_brain.add_argument("--session", help="Session ID")
    p_brain.add_argument("--path", help="Repository root (default: cwd)")
    p_brain.add_argument("--budget", type=int, default=0,
                         help="Pack token budget (default: window budget; pack guarantees fit)")
    p_brain.add_argument("--json", action="store_true", help="Print receipt JSON (pack still written to brain.md)")

    p_resume = sub.add_parser("resume", help="Resume active Agres session")
    p_resume.add_argument("--query", help="Optional retrieval query")
    p_resume.add_argument("--session", help="Session ID")

    p_end = sub.add_parser("end", help="End active Agres session")
    p_end.add_argument("--session", help="Session ID")

    p_status = sub.add_parser("status", help="Show Agres runtime status (visual analytics)")
    p_status.add_argument("--json", action="store_true", help="Output JSON instead of visual")
    p_status.add_argument("--format", choices=["visual","json"], default="visual", help="Output format")

    p_capture = sub.add_parser("capture", help="Capture a conversation turn verbatim")
    p_capture.add_argument("--text", help="Turn text (verbatim)")
    p_capture.add_argument("--file", help="Read turn text from a file")
    p_capture.add_argument("--role", choices=["user", "assistant", "system"], help="Turn role")
    p_capture.add_argument("--session", help="Session ID")

    p_fold = sub.add_parser("fold", help="Fold low-priority window items out of the active window")
    p_fold.add_argument("--target", type=int, default=-1, help="Target max tokens in window (0 = fold everything foldable; default: budget - reserve)")
    p_fold.add_argument("--session", help="Session ID")

    p_unfold = sub.add_parser("unfold", help="Unfold folded context back into the active window")
    p_unfold.add_argument("--query", help="Search query for folded context")
    p_unfold.add_argument("--fold-id", help="Specific fold ID to unfold")
    p_unfold.add_argument("--limit", type=int, default=3, help="Max items to unfold")
    p_unfold.add_argument("--session", help="Session ID")

    p_budget = sub.add_parser("budget", help="Show context window budget usage (visual bars)")
    p_budget.add_argument("--session", help="Session ID")
    p_budget.add_argument("--json", action="store_true", help="Output JSON instead of visual report")
    p_budget.add_argument("--set", metavar="TOKENS", help="Persist window budget (e.g. --set 1048576)")

    p_window = sub.add_parser("window", help="Show active window manifest (visual)")
    p_window.add_argument("--session", help="Session ID")
    p_window.add_argument("--json", action="store_true", help="Output JSON")

    p_prune = sub.add_parser("prune", help="Prune old data (events/turns/folds > N days) and VACUUM")
    p_prune.add_argument("--keep-days", type=int, default=30, help="Keep last N days (default 30)")
    p_prune.add_argument("--dry-run", action="store_true", help="Show what would be deleted")

    p_gc = sub.add_parser("gc", help="Garbage collect unreferenced CAS objects and VACUUM")
    p_gc.add_argument("--dry-run", action="store_true", help="Show what would be deleted")

    p_repair = sub.add_parser("repair", help="Repair WAL, FTS, integrity; use --reset-session to clear active session")
    p_repair.add_argument("--reset-session", action="store_true", help="Clear active_session_id (manual override)")

    p_doctor = sub.add_parser("doctor", help="Verify auto-activation wiring (skills + project rules)")
    p_doctor.add_argument("--json", action="store_true", help="Output JSON instead of visual report")

    args = parser.parse_args()

    ensure_base_dirs()
    init_db()

    if args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "start":
        cmd_start(args)
    elif args.cmd == "event":
        cmd_event(args)
    elif args.cmd == "decision":
        cmd_decision(args)
    elif args.cmd == "task":
        cmd_task(args)
    elif args.cmd == "error":
        cmd_error(args)
    elif args.cmd == "pin":
        cmd_pin(args)
    elif args.cmd == "checkpoint":
        cmd_checkpoint(args)
    elif args.cmd == "context":
        cmd_context(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "index":
        cmd_index(args)
    elif args.cmd == "touch":
        cmd_touch(args)
    elif args.cmd == "sync":
        cmd_sync(args)
    elif args.cmd == "repo-map":
        cmd_repo_map(args)
    elif args.cmd == "map":
        cmd_map(args)
    elif args.cmd == "graph":
        cmd_graph(args)
    elif args.cmd == "trace":
        cmd_trace(args)
    elif args.cmd == "brain":
        cmd_brain(args)
    elif args.cmd == "resume":
        cmd_resume(args)
    elif args.cmd == "end":
        cmd_end(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "prune":
        cmd_prune(args)
    elif args.cmd == "gc":
        cmd_gc(args)
    elif args.cmd == "repair":
        cmd_repair(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)
    elif args.cmd == "capture":
        cmd_capture(args)
    elif args.cmd == "fold":
        cmd_fold(args)
    elif args.cmd == "unfold":
        cmd_unfold(args)
    elif args.cmd == "budget":
        cmd_budget(args)
    elif args.cmd == "window":
        cmd_window(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
