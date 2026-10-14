"""Reading v0 stores: read-only access, v0 -> v1 record mapping (spec R12), small fs helpers.

Shared by `migrate` (in-place project migration) and `migrate_global` (routing of the old global store).
Nothing here writes next to a store except `checkpoint`/`move`/manifest helpers, which callers use explicitly.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from . import paths
from .db import SCHEMA_VERSION

SOURCE = "migrate:v0"
MANIFEST = "MANIFEST.json"
V0_TABLES = ("meta", "sessions", "events", "files", "file_chunks", "symbols", "decisions", "tasks", "errors",
             "pins", "checkpoints", "context_packs", "turns", "folds", "window_items", "manifest", "edges",
             "ref_idents", "compaction_artifacts", "graph_nodes", "graph_edges")
_V0_MARKERS = {"sessions", "decisions", "tasks", "pins", "turns", "checkpoints", "folds", "window_items"}
_UTC = _dt.timezone.utc
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_UTC)



class MigrateError(Exception):
    pass



@contextlib.contextmanager
def open_ro(db_file: str) -> Iterator[sqlite3.Connection]:
    """Read-only view of a store that never writes beside it. A live -wal is read from a temp copy."""
    db_file = os.path.realpath(db_file)
    tmp = None
    try:
        wal = db_file + "-wal"
        if os.path.isfile(wal) and os.path.getsize(wal) > 0:
            tmp = tempfile.mkdtemp(prefix="agres-v0-")
            for suffix in ("", "-wal"):
                if os.path.isfile(db_file + suffix):
                    shutil.copy2(db_file + suffix, os.path.join(tmp, "agres.db" + suffix))
            conn = sqlite3.connect(os.path.join(tmp, "agres.db"))
        else:
            conn = sqlite3.connect(Path(db_file).as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def kind_of(db_file: str) -> str:
    """none | v0 | v1 | newer | unknown | bad"""
    if not os.path.isfile(db_file):
        return "none"
    try:
        with open_ro(db_file) as c:
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        return "bad"
    if ver == SCHEMA_VERSION:
        return "v1"
    if ver > SCHEMA_VERSION:
        return "newer"
    return "v0" if ver == 0 and tables & _V0_MARKERS else "unknown"


def check_root(root: str) -> str:
    root = os.path.realpath(root)
    if paths.is_forbidden_root(root):
        raise MigrateError(f"{root} can never be a project root (global store: use global_routes/archive_global)")
    if not os.path.isdir(root):
        raise MigrateError(f"{root} is not a directory")
    return root



def parse_ts(ts: Any) -> Optional[_dt.datetime]:
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)", s)
        if not m:
            return None
        d = _dt.datetime(*map(int, m.groups()))
    return (d if d.tzinfo else d.replace(tzinfo=_UTC)).astimezone(_UTC)


def iso(d: _dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


_DONE_RE = re.compile(r"\b(complete|completed|done|finished|shipped|resolved)\b", re.I)
_IMPERATIVE_COMPLETE = re.compile(r"^\s*complete\s+(?![:\-–—])", re.I)  # "Complete the audit" is a to-do
_FIXED_RE = re.compile(r"\b(fixed|resolved|solved|done|completed)\b", re.I)
_DONE_STATUS = {"done", "complete", "completed", "finished", "shipped", "resolved", "closed", "fixed", "merged"}
_DROP_STATUS = {"dropped", "cancelled", "canceled", "abandoned", "wontfix", "obsolete"}


def _looks_done(text: str) -> bool:
    lead = len(text) - len(text.lstrip()) if _IMPERATIVE_COMPLETE.match(text) else -1
    return any(m.start() != lead for m in _DONE_RE.finditer(text))


def _task_status(status: str, text: str) -> str:
    st = (status or "").strip().lower()
    if st in _DROP_STATUS:
        return "dropped"
    return "done" if st in _DONE_STATUS or _looks_done(text) else "open"


_SNAPSHOT = re.compile(
    r"^\s*(checkpoint\b|phase\s*\d+\s*(done|complete|completed)\b|.{0,40}\b(live\s+)?progress\b|"
    r"status\b|.{0,30}\bstatus\s+\d{4}-\d\d-\d\d|verification at checkpoint|audit\s+\d{4}-\d\d-\d\d|"
    r"(?-i:[A-Z0-9 _-]{3,40}\b(DONE|COMPLETE|COMPLETED|LIVE)\b[^:(]{0,20}[:(]))", re.I)


def is_progress_snapshot(text: Optional[str]) -> bool:
    """'CHECKPOINT 2026-10-13: …', 'Phase 5 COMPLETE: …', 'PULSAR STATUS …' — a dated log line, not a rule."""
    return bool(text) and bool(_SNAPSHOT.match(text))


def _generic_reason(reason: str) -> bool:
    key = re.sub(r"[\s_\-]+", "", (reason or "").lower())
    return key in {"", "sessionend", "sessionstop", "precompact", "postcompact", "compact", "autocompact",
                   "stop", "checkpoint"}


def _rows(conn: sqlite3.Connection, have: set, table: str) -> list:
    if table not in have:
        return []
    return [dict(r) for r in conn.execute(f"SELECT rowid AS _rowid, * FROM {table}")]


def load(db_file: str, origin: Optional[str]) -> dict:
    """Classify one v0 store into v1 records, sessions and turns (pure read)."""
    with open_ro(db_file) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in V0_TABLES if t in have}
        raw = {t: _rows(c, have, t) for t in ("pins", "decisions", "tasks", "errors", "checkpoints", "sessions", "turns")}
    out = {"origin": origin, "db": db_file, "tables": tables, "records": [], "sessions": [], "turns": [],
           "skipped_handoffs": 0, "empty_skipped": 0, "done_by_text": []}

    def add(kind, status, text, r, order, updated=None):
        text = (text or "").strip()
        if not text:
            out["empty_skipped"] += 1
            return
        when = parse_ts(r.get("created_at")) or _EPOCH
        upd = parse_ts(updated) or when
        out["records"].append({"kind": kind, "status": status, "text": text, "origin": origin, "when": when,
                               "updated": upd, "session_id": r.get("session_id") or None,
                               "order": order, "rowid": r["_rowid"]})

    for r in raw["pins"]:  # v0 habit: progress snapshots were pinned; they're history, not must-hold facts
        add("note" if is_progress_snapshot(r.get("text")) else "pin", "active", r.get("text"), r, 0)
    for r in raw["decisions"]:
        text = r.get("decision") or r.get("text")
        if r.get("rejected"):
            add("decision", "rejected", text, r, 1)
        elif r.get("accepted"):
            add("decision", "accepted", text, r, 1)
        else:  # neither flag: v0 called it "noted"; never promote it to a decision the agent must honour
            add("note", "active", text, r, 1)
    for r in raw["tasks"]:
        text = r.get("objective") or r.get("text") or ""
        status = _task_status(r.get("status"), text)
        if status == "done" and (r.get("status") or "").strip().lower() not in _DONE_STATUS:
            out["done_by_text"].append(text.strip())
        add("task", status, text, r, 2, updated=r.get("updated_at"))
    for r in raw["errors"]:
        text = (r.get("message") or "").strip()
        if (r.get("context") or "").strip():
            text += "\n" + r["context"].strip()
        add("error", "fixed" if _FIXED_RE.search(text) else "open", text, r, 3)
    for r in raw["checkpoints"]:
        if _generic_reason(r.get("reason")):
            out["skipped_handoffs"] += 1
        else:
            add("handoff", "active", r.get("reason"), r, 4)
    for r in raw["sessions"]:
        started = parse_ts(r.get("created_at")) or _EPOCH
        out["sessions"].append({
            "id": r["id"], "agent": (r.get("model") or r.get("model_provider") or "").strip() or None,
            "title": r.get("title"), "cwd": r.get("repo"), "started": started,
            "ended": parse_ts(r.get("updated_at")) or started})
    for r in raw["turns"]:
        role = r.get("role") if r.get("role") in ("user", "assistant", "system") else "system"
        out["turns"].append({"session_id": r.get("session_id") or "", "role": role, "text": r.get("content") or "",
                             "when": parse_ts(r.get("created_at")) or _EPOCH, "rowid": r["_rowid"]})
    return out


def key_of(kind: str, text: str) -> tuple:
    return kind, " ".join(text.split()).casefold()



def move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(src, dst)


def checkpoint(db_file: str) -> None:
    try:
        conn = sqlite3.connect(db_file, timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise MigrateError(f"cannot checkpoint {db_file}: {exc}") from exc


def read_manifest(v0dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(v0dir, MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_manifest(v0dir: str, manifest: dict) -> None:
    os.makedirs(v0dir, exist_ok=True)
    with open(os.path.join(v0dir, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)

