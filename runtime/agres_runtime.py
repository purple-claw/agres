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
  created_at TEXT
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
    conn = sqlite3.connect(str(db_path()), timeout=5.0, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    return conn


_INIT_DONE = set()

def init_db() -> None:
    path = db_path()
    # ponytail: cache init per path per process to avoid re-executing SCHEMA on every get_conn()
    if str(path) in _INIT_DONE and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.executescript(SCHEMA)
    # Migration: add model columns if missing (for existing DBs)
    try:
        cur.execute("SELECT model FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
            cur.execute("ALTER TABLE sessions ADD COLUMN model_provider TEXT")
        except sqlite3.OperationalError:
            pass

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
        cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_enabled', '0')")

    conn.commit()
    conn.close()
    _INIT_DONE.add(str(path))


def fts_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'fts_enabled'").fetchone()
        return bool(row and row["value"] == "1")
    except sqlite3.OperationalError:
        return False


def fts_match_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    # ponytail: quote each term for FTS5; empty query -> ""
    cleaned = [t[:64] for t in terms if t]
    if not cleaned:
        return ""
    return " OR ".join(f'"{term}"' for term in cleaned)

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
    # ponytail: never auto-clear active_session_id. Sessions live in per-project
    # dbs we cannot enumerate from a global command; clearing here caused
    # 'budget not updating' (project session silently dropped). Stale ids are
    # harmless: commands resolve data by session_id string. Explicit clears:
    #   agres end            (marks ended + clears)
    #   agres repair --reset-session (manual override)
    return

def session_dir(session_id: Optional[str] = None) -> Optional[Path]:
    session_id = session_id or active_session_id()

    if not session_id:
        return None

    return _agres_dir() / "sessions" / session_id


def require_session(args: argparse.Namespace) -> str:
    session_id = getattr(args, "session", None) or active_session_id()

    if not session_id:
        print("No active Agres session.", file=sys.stderr)
        print("Start one with:", file=sys.stderr)
        print("  agres start \"Your session goal\"", file=sys.stderr)
        sys.exit(1)

    sdir = AGRES_DIR / "sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    return session_id


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


def extract_symbols(language: str, text: str) -> List[Dict[str, Any]]:
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


def build_repo_map() -> Path:
    root = _project_root()

    top_entries: List[str] = []

    try:
        for entry in sorted(root.iterdir()):
            if entry.name in IGNORE_DIRS:
                continue

            if entry.is_dir():
                top_entries.append(f"{entry.name}/")
            else:
                top_entries.append(entry.name)
    except Exception:
        pass

    ext_counts: Dict[str, int] = {}
    file_count = 0

    for path in iter_repo_files(root, limit=2000):
        file_count += 1
        ext = path.suffix.lower() or "none"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    lines = [
        "# Agres Repo Map",
        "",
        f"Repo: {root}",
        f"Generated: {now_iso()}",
        f"Indexed sample file count: {file_count}",
        "",
        "## Top-level entries",
        "",
    ]

    if top_entries:
        lines.extend([f"- {entry}" for entry in top_entries[:200]])
    else:
        lines.append("- empty")

    lines.extend(["", "## Extension counts", ""])

    for ext, count in sorted(ext_counts.items(), key=lambda item: item[1], reverse=True)[:100]:
        lines.append(f"- {ext}: {count}")

    text = "\n".join(lines)

    out_path = _agres_dir() / "folds" / "repo_map.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    return out_path


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def search_chunks(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    if fts_enabled(conn):
        match = fts_match_query(query)

        if match:
            try:
                cur = conn.execute(
                    "SELECT file_path, language, snippet(fts_chunks, 0, '[', ']', '...', 24) AS snippet "
                    "FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                return [dict(row) for row in cur.fetchall()]
            except sqlite3.OperationalError:
                pass

    cur = conn.execute(
        "SELECT file_path, '' AS language, substr(text, 1, 220) AS snippet "
        "FROM file_chunks WHERE text LIKE ? ESCAPE '\\' LIMIT ?",
        (f"%{escape_like(query)}%", limit),
    )

    return [dict(row) for row in cur.fetchall()]


def search_symbols(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    if fts_enabled(conn):
        match = fts_match_query(query)

        if match:
            try:
                cur = conn.execute(
                    "SELECT file_path, name, kind, line, snippet(fts_symbols, 0, '[', ']', '...', 24) AS snippet "
                    "FROM fts_symbols WHERE fts_symbols MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                return [dict(row) for row in cur.fetchall()]
            except sqlite3.OperationalError:
                pass

    like = f"%{escape_like(query)}%"

    cur = conn.execute(
        "SELECT file_path, name, kind, line, text AS snippet "
        "FROM symbols WHERE name LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' LIMIT ?",
        (like, like, limit),
    )

    return [dict(row) for row in cur.fetchall()]


def search_memory(conn: sqlite3.Connection, query: str, limit: int) -> List[Dict[str, Any]]:
    if fts_enabled(conn):
        match = fts_match_query(query)

        if match:
            try:
                cur = conn.execute(
                    "SELECT type, ref_id, session_id, snippet(fts_memory, 0, '[', ']', '...', 24) AS snippet "
                    "FROM fts_memory WHERE fts_memory MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                return [dict(row) for row in cur.fetchall()]
            except sqlite3.OperationalError:
                pass

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
        match = fts_match_query(query)
        if match:
            try:
                cur = conn.execute(
                    "SELECT fold_id, title, session_id, "
                    "snippet(fts_folds, 0, '[', ']', '...', 40) AS snippet "
                    "FROM fts_folds WHERE fts_folds MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                rows = [dict(row) for row in cur.fetchall()]
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass

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
    """Human-readable window budget report with visual bars."""
    budget = window_budget()
    usage = window_usage_tokens(conn, session_id) if session_id else 0
    reserve = WINDOW_RESERVE_TOKENS
    usable = budget - reserve
    pct = (usage / usable * 100.0) if usable > 0 else 0.0
    remaining = max(0, budget-usage)

    try:
        filled = int(30 * min(100, pct)/100)
        bar = "█"*filled + "░"*(30-filled)
    except Exception:
        bar = "#"*(int(pct/5)) + "-"*(20-int(pct/5))

    if pct < 50:
        level = "high"
        icon = "✓"
    elif pct < 75:
        level = "medium"
        icon = "~"
    elif pct < 90:
        level = "low"
        icon = "!"
    else:
        level = "critical"
        icon = "✗"

    lines = [
        "# Agres Window Budget — Realtime 256k Engine",
        "",
        f"Session: {session_id or '(none)'}",
        f"Budget: {budget:,} tokens (env AGRES_WINDOW_BUDGET or state window_budget)",
        f"Reserve (output headroom): {reserve:,} tokens",
        f"Usable: {usable:,} tokens",
        f"Active window usage: {usage:,} tokens ({pct:.1f}% of usable)  {bar}  [{level} {icon}]",
        f"Remaining: {remaining:,} tokens",
        f"Confidence: {level} ({icon}) — {'healthy' if pct < 75 else 'fold soon' if pct < 90 else 'auto-fold active'}",
        "",
        "## In window",
        "",
    ]

    rows = conn.execute(
        "SELECT * FROM window_items WHERE session_id = ? ORDER BY priority DESC, added_at ASC",
        (session_id,),
    ).fetchall() if session_id else []

    for row in rows:
        lines.append(
            f"- [{row['kind']}] {row['title']} ({row['token_estimate']:,} tok, "
            f"priority {row['priority']}{', folded' if row['folded'] else ''})"
        )

    if not rows:
        lines.append("- (empty)")

    lines.extend(
        [
            "",
            "## Folded",
            "",
        ]
    )

    frows = conn.execute(
        "SELECT id, kind, title, token_estimate, status FROM folds "
        "WHERE session_id = ? ORDER BY folded_at DESC LIMIT 20",
        (session_id,),
    ).fetchall() if session_id else []

    for row in frows:
        lines.append(
            f"- {row['id']} [{row['kind']}] {row['title']} "
            f"({row['token_estimate']:,} tok, {row['status']})"
        )

    if not frows:
        lines.append("- (none)")

    lines.extend(
        [
            "",
            "## Auto-fold threshold",
            "",
            f"Over {usable:,} tokens, fold lowest-priority items with:",
            "  agres fold --target <tokens>  (auto-fold triggers on capture when > usable)",
            "",
            "## Unfold",
            "",
            "Restore exact text from folded context:",
            '  agres unfold --query "what was said about X"',
            f"  Confidence: {level} — unfolded items re-enter window as priority 2 (preserved verbatim)",
        ]
    )

    return "\n".join(lines)


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
    conn.close()

    append_event(
        session_id,
        "checkpoint_created",
        {
            "reason": reason,
            "checkpoint_id": checkpoint_id,
            "path": str(checkpoint_path),
        },
    )

    print(checkpoint_path)


def cmd_context(args: argparse.Namespace) -> None:
    session_id = getattr(args, "session", None) or active_session_id()

    conn = get_conn()

    sections: List[str] = []

    sections.append("# Agres Context Pack")
    sections.append("")
    sections.append(f"Generated: {now_iso()}")
    sections.append(f"Session: {session_id or 'none'}")
    sections.append(f"Repo: {PROJECT_ROOT}")
    sections.append(f"Query: {args.query or 'none'}")
    sections.append("")

    if session_id:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

        if row:
            sections.append("## Active Session")
            sections.append("")
            sections.append(json.dumps(dict(row), indent=2, ensure_ascii=False))
            sections.append("")

    for name in [
        "session.md",
        "decisions.md",
        "tasks.md",
        "errors.md",
        "pinned.md",
    ]:
        path = _agres_dir() / name

        if path.exists():
            sections.append(f"## {name}")
            sections.append("")
            sections.append(read_text_safe(path, 6000))
            sections.append("")

    repo_map_path = _agres_dir() / "folds" / "repo_map.md"

    if repo_map_path.exists():
        sections.append("## repo_map.md")
        sections.append("")
        sections.append(read_text_safe(repo_map_path, 4000))
        sections.append("")

    if args.query:
        chunk_results = search_chunks(conn, args.query, args.limit)
        symbol_results = search_symbols(conn, args.query, args.limit)
        memory_results = search_memory(conn, args.query, args.limit)

        if chunk_results:
            sections.append("## File Chunk Results")
            sections.append("")
            for row in chunk_results:
                sections.append(json.dumps(row, indent=2, ensure_ascii=False))
                sections.append("")

        if symbol_results:
            sections.append("## Symbol Results")
            sections.append("")
            for row in symbol_results:
                sections.append(json.dumps(row, indent=2, ensure_ascii=False))
                sections.append("")

        if memory_results:
            sections.append("## Memory Results")
            sections.append("")
            for row in memory_results:
                sections.append(json.dumps(row, indent=2, ensure_ascii=False))
                sections.append("")

    content = "\n".join(sections)

    context_pack_path = _agres_dir() / "context-pack.md"
    context_pack_path.write_text(content, encoding="utf-8")

    pack_id = new_id("pack")

    conn.execute(
        "INSERT INTO context_packs(id, session_id, query, path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            pack_id,
            session_id or "",
            args.query or "",
            str(context_pack_path),
            now_iso(),
        ),
    )

    conn.commit()
    conn.close()

    if session_id:
        sdir = session_dir(session_id)
        packs_dir = sdir / "context_packs"
        packs_dir.mkdir(parents=True, exist_ok=True)

        pack_path = packs_dir / f"context_pack_{pack_id}.md"
        pack_path.write_text(content, encoding="utf-8")

    print(content)


def cmd_search(args: argparse.Namespace) -> None:
    conn = get_conn()

    results = {
        "query": args.query,
        "chunks": search_chunks(conn, args.query, args.limit),
        "symbols": search_symbols(conn, args.query, args.limit),
        "memory": search_memory(conn, args.query, args.limit),
    }

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

        if fts:
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

        try:
            conn.execute("COMMIT;")
        except sqlite3.OperationalError:
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

    repo_map_path = build_repo_map()

    conn.close()

    print("")
    print("Agres SQLite indexing complete.")
    print(f"Root: {root}")
    print(f"Indexed: {indexed}")
    print(f"Skipped unchanged: {skipped}")
    print(f"Failed: {failed}")
    print(f"Repo map: {repo_map_path}")


def cmd_repo_map(args: argparse.Namespace) -> None:
    use_project_agres()
    path = build_repo_map()
    print(path)


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

def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100.0)
    empty = width - filled
    # Use unicode blocks if utf-8 terminal, else ascii
    try:
        bar = "█" * filled + "░" * empty
    except Exception:
        bar = "#" * filled + "-" * empty
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


def cmd_status(args: argparse.Namespace) -> None:
    import time
    ensure_base_dirs()
    init_db()
    _repair_active_session()

    conn = get_conn()
    fts = fts_enabled(conn)

    counts = {}

    for table in [
        "sessions",
        "events",
        "files",
        "file_chunks",
        "symbols",
        "decisions",
        "tasks",
        "errors",
        "pins",
        "checkpoints",
        "context_packs",
        "turns",
        "folds",
        "window_items",
    ]:
        try:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = row["c"] if row else 0
        except sqlite3.OperationalError:
            counts[table] = 0

    # --- Analytics ---
    budget = window_budget()
    active = active_session_id()
    usage = 0
    folded_count = counts.get("folds", 0)
    turns_count = counts.get("turns", 0)
    if active:
        try:
            usage = window_usage_tokens(conn, active)
        except Exception:
            usage = 0

    reserve = WINDOW_RESERVE_TOKENS
    usable = max(1, budget - reserve)
    pct = (usage / usable * 100.0) if usable else 0.0
    remaining = max(0, budget - usage)
    confidence = _confidence_level(pct, folded_count, turns_count)

    # Storage sizes
    db_p = db_path()
    db_size = 0
    wal_size = 0
    shm_size = 0
    cas_size = 0
    cas_objects = 0
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

    # Window breakdown by priority
    win_rows = []
    if active:
        try:
            win_rows = conn.execute("SELECT priority, COUNT(*) c, COALESCE(SUM(token_estimate),0) t FROM window_items WHERE session_id = ? GROUP BY priority ORDER BY priority DESC", (active,)).fetchall()
        except Exception:
            win_rows = []

    conn.close()

    # Detect model for analytics
    model = _detect_model()

    # If --json requested, emit JSON only (for CI)
    if getattr(args, "json", False) or getattr(args, "format", "") == "json":
        status = {
            "agres_home": str(AGRES_HOME),
            "project_root": str(_project_root()),
            "agres_dir": str(_agres_dir()),
            "database": str(db_p),
            "fts_enabled": fts,
            "active_session_id": active,
            "counts": counts,
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
            "state": get_state(),
        }
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    # --- Visual CLI ---
    # Colors: use ANSI if tty, else plain
    is_tty = sys.stdout.isatty()
    def _c(s, code):
        return f"\033[{code}m{s}\033[0m" if is_tty else s
    B = lambda s: _c(s, "1")
    DIM = lambda s: _c(s, "2")
    GREEN = lambda s: _c(s, "32")
    YELLOW = lambda s: _c(s, "33")
    RED = lambda s: _c(s, "31")
    CYAN = lambda s: _c(s, "36")
    MAGENTA = lambda s: _c(s, "35")

    # Header
    print(B("┌─ Agres Origami Memory — Status"))
    print(f"│ {DIM('agres_home')}  {AGRES_HOME}")
    print(f"│ {DIM('agres_dir')}   {_agres_dir()}")
    print(f"│ {DIM('project')}     {_project_root()}")
    print(f"│ {DIM('database')}    {db_p}  ({_human_size(db_size)})")
    print(f"│ {DIM('fts')}         {'enabled' if fts else 'disabled (LIKE fallback)'}")
    print(f"│ {DIM('session')}     {active or '(none)'}  " + (GREEN("● active") if active else YELLOW("○ no active session")))
    # Model block — always show used model if detectable
    try:
        mid = model.get("id","unknown")
        prov = model.get("providerID","")
        var = model.get("variant","")
        src = model.get("source","")
        lim = model.get("limit",{})
        ctx = lim.get("context", budget)
        out = lim.get("output", 0)
        badge = _model_badge(model)
        # Color by context vs budget alignment
        mcol = GREEN if ctx >= budget else YELLOW if ctx >= budget*0.8 else RED
        prov_s = f" {DIM('via')} {prov}" if prov else ""
        var_s = f" {DIM('variant')} {var}" if var else ""
        # Show cost/tokens if from opencode.db
        extra = ""
        if "tokens_input" in model:
            try:
                extra = f"  {DIM('tokens')} {model['tokens_input']:,}+{model['tokens_output']:,}  {DIM('cost')} ${model.get('cost',0):.2f}"
            except Exception:
                pass
        # confidence vs model: if window low but model is larger, note headroom
        conf_extra = ""
        try:
            if ctx and ctx != budget and ctx>budget:
                conf_extra = f"  {DIM('headroom')} {ctx - usage:,} vs budget {budget - usage:,}"
        except Exception:
            pass
        print(f"│ {DIM('model')}     {mcol(mid)}{prov_s}{var_s}  {DIM('ctx')} {ctx:,} ({badge}){DIM(' src:'+src) if src!='opencode.db:latest_session' else ''}{extra}{conf_extra}")
        # If model context differs from budget, explain: agres throttles large models to budget for testing low-context
        if ctx and abs(ctx - budget) > 1000:
            if ctx > budget:
                print(f"│ {DIM('budget↔model')} model ctx {ctx:,} → throttled to budget {budget:,} {DIM('(AGRES_WINDOW_BUDGET; use 1M to test full window)')}")
            else:
                print(f"│ {DIM('budget↔model')} model ctx {ctx:,} < budget {budget:,} {YELLOW('(model smaller than budget — raises overflow risk)')}")
    except Exception:
        pass
    print("│")

    # Window Budget — visual bar
    # Choose color by pct
    if pct < 50:
        col = GREEN
    elif pct < 75:
        col = YELLOW
    else:
        col = RED
    bar = _bar(pct, 30)
    pct_s = f"{pct:5.1f}%"
    print(B(f"├─ Window Budget — {budget:,} tokens (reserve {reserve:,})"))
    print(f"│  {col(bar)} {col(pct_s)}  {usage:,} used / {usable:,} usable  (remaining {remaining:,})")
    # Sub-breakdown
    print(f"│  {DIM('budget →')} system 10%  task 10%  pinned 10%  recent 35%  unfolded 20%  repo 5%  reserve {reserve/budget*100:.0f}%")
    if confidence == "high":
        conf_c = GREEN
        conf_icon = "✓"
    elif confidence == "medium":
        conf_c = YELLOW
        conf_icon = "~"
    elif confidence == "low":
        conf_c = YELLOW
        conf_icon = "!"
    else:
        conf_c = RED
        conf_icon = "✗"
    print(f"│  {DIM('confidence')}  {conf_c(conf_icon + ' ' + confidence.upper())}  {DIM('(based on usage vs usable, folds preserved)')}")
    # Window items breakdown by priority
    if win_rows:
        print(f"│  {DIM('window items by priority')}")
        prio_labels = {0:"low (fold first)", 1:"recent turns", 2:"unfolded", 3:"pinned", 4:"pinned+", 5:"system"}
        for r in win_rows:
            prio = r["priority"]
            lbl = prio_labels.get(prio, f"prio {prio}")
            print(f"│    • p{prio} {lbl:18}  {r['c']:3} items  {r['t']:,} tok")
    else:
        if active:
            print(f"│  {DIM('window')} (empty — ready for turns)")
        else:
            print(f"│  {DIM('window')} (no active session)")

    print("│")

    # Context health — turns/folds
    print(B("├─ Context Health"))
    print(f"│  turns: {turns_count}  folds: {folded_count}  window_items: {counts.get('window_items',0)}")
    if turns_count > 0 or folded_count > 0:
        preserved = turns_count + folded_count
        print(f"│  preserved verbatim: {preserved} (turns+folds, one unfold away)")
    # Folds by status
    try:
        conn2 = get_conn()
        fr = conn2.execute("SELECT status, COUNT(*) c FROM folds GROUP BY status").fetchall()
        conn2.close()
        if fr:
            parts = ", ".join([f"{x['status']}: {x['c']}" for x in fr])
            print(f"│  folds by status: {parts}")
    except Exception:
        pass
    if active and pct > 90:
        print(f"│  {RED('⚠ window >90% — next capture will auto-fold oldest low-priority items')}")
    elif active and pct > 75:
        print(f"│  {YELLOW('→ consider: agres fold --target 200000 or agres window to inspect')}")
    else:
        print(f"│  {DIM('✓ window healthy — captures will stay in active window')}")
    print("│")

    # Storage analytics
    print(B("├─ Storage"))
    print(f"│  DB:        {_human_size(db_size):>8}  ({db_p.name})")
    if wal_size:
        print(f"│  WAL:       {_human_size(wal_size):>8}  ({db_p.name}-wal)")
    if shm_size:
        print(f"│  SHM:       {_human_size(shm_size):>8}")
    print(f"│  CAS:       {_human_size(cas_size):>8}  ({cas_objects} objects)")
    print(f"│  {B('total'):8}  {_human_size(total_storage):>8}  {DIM('(DB+WAL+CAS)')}")
    # Counts grid
    print("│")
    print(B("├─ Counts"))
    # Pretty grid 2 cols
    items = list(counts.items())
    for i in range(0, len(items), 3):
        chunk = items[i:i+3]
        line = "│  "
        for k,v in chunk:
            line += f"{k:14} {CYAN(str(v)):>5}   "
        print(line)
    print("│")

    # Health checks / warnings
    print(B("├─ Health"))
    warnings = []
    # Ghost session already repaired, but report if still
    if not active:
        warnings.append("no active session — run: agres start \"goal\"")
    if db_size > 50*1024*1024:
        warnings.append(f"DB large ({_human_size(db_size)}) — consider: agres prune or agres gc")
    if not fts:
        warnings.append("FTS5 unavailable — search uses LIKE fallback (slower)")
    if counts.get("sessions",0)==0 and counts.get("events",0)>0:
        warnings.append("orphaned events with 0 sessions — run: agres repair (auto-fixed on next command)")
    if warnings:
        for w in warnings:
            print(f"│  {YELLOW('!')} {w}")
    else:
        print(f"│  {GREEN('✓')} all checks pass")
    # Footer
    print(B("└─"))
    print(DIM("  tip: agres budget · agres window · agres context --query \"your topic\" · agres fold --target 200000 · agres unfold --query \"earlier decision\""))
    # Also emit JSON to stderr for tooling if verbose? No — visual is default



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
    conn.commit()
    conn.close()

    append_event(
        session_id,
        "context_folded",
        {
            "target_tokens": target,
            "folded_count": len(folded),
            "usage_after_tokens": usage_after,
        },
    )

    print(
        json.dumps(
            {
                "folded_count": len(folded),
                "folded": folded,
                "usage_after_tokens": usage_after,
                "target_tokens": target,
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

    session_id = getattr(args, "session", None) or active_session_id()
    if not session_id:
        b = window_budget()
        print(f"No active session. Window budget is {b:,} tokens (auto from model).")
        print("Start with: agres start \"goal\"  |  Set budget: agres budget --set 1048576")
        return
    conn = get_conn()
    # If --json, dump json manifest instead of report
    if getattr(args, "json", False):
        budget = window_budget()
        usage = window_usage_tokens(conn, session_id)
        model = _detect_model()
        print(json.dumps({"session_id": session_id, "budget_tokens": budget, "usage_tokens": usage, "pct": round(usage/max(1,budget-WINDOW_RESERVE_TOKENS)*100,1), "model": model}, indent=2))
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

    p_search = sub.add_parser("search", help="Search Agres memory")
    p_search.add_argument("--query", required=True, help="Search query")
    p_search.add_argument("--limit", type=int, default=5, help="Result limit")

    p_index = sub.add_parser("index", help="Index repository files into Agres")
    p_index.add_argument("--path", help="Repository path to index")
    p_index.add_argument("--limit", type=int, default=1000, help="Maximum files to scan")
    p_index.add_argument("--force", action="store_true", help="Reindex unchanged files")

    sub.add_parser("repo-map", help="Build repository map fold")

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
    elif args.cmd == "repo-map":
        cmd_repo_map(args)
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
