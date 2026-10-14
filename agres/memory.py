"""Project memory: typed items with lifecycles, verbatim turns, sessions.

Items are never hard-deleted. Replacing one marks the old row `superseded` and
links it to the new one (Graphiti-style invalidation), so "what did we decide
before?" stays answerable. Sessions are provenance only — nothing is filtered
by session.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
from typing import Iterable, Optional

from .util import now_iso, redact, search_text, sha1_text, terms, truncate

KINDS = {
    #            prefix  default      statuses
    "pin":      ("P", "active",   ("active", "retired", "superseded")),
    "decision": ("D", "accepted", ("accepted", "rejected", "superseded", "retracted")),
    "task":     ("T", "open",     ("open", "done", "dropped", "superseded")),
    "error":    ("E", "open",     ("open", "fixed", "dropped", "superseded")),
    "note":     ("N", "active",   ("active", "retired", "superseded")),
    "handoff":  ("H", "active",   ("active", "superseded")),
}
PREFIX = {k: v[0] for k, v in KINDS.items()}
PREFIX["turn"] = "M"
KIND_BY_PREFIX = {v: k for k, v in PREFIX.items()}

# statuses that still "hold" (shown in the pack, matched by similar())
LIVE = {"pin": ("active",), "decision": ("accepted", "rejected"), "task": ("open",),
        "error": ("open",), "note": ("active",), "handoff": ("active",)}
_CLOSE = {("task", "done"): "done", ("error", "done"): "fixed",
          ("task", "drop"): "dropped", ("error", "drop"): "dropped",
          ("pin", "drop"): "retired", ("note", "drop"): "retired",
          ("decision", "drop"): "retracted", ("handoff", "drop"): "superseded"}
_FORGET = {"pin": "retired", "decision": "retracted", "task": "dropped", "error": "dropped",
           "note": "retired", "handoff": "superseded"}

MAX_TURN_CHARS = 64_000
TITLE_CHARS = 80
SESSION_WINDOW_H = 12


# ---------------------------------------------------------------- ids/titles
def fmt_id(kind: str, iid: int) -> str:
    return f"{PREFIX[kind]}{iid}"


def parse_id(text: str) -> tuple[Optional[str], int]:
    m = re.fullmatch(r"\s*([A-Za-z]?)(\d+)\s*", str(text))
    if not m:
        raise ValueError(f"not an agres id: {text!r}")
    p = m.group(1).upper()
    if p and p not in KIND_BY_PREFIX:
        raise ValueError(f"unknown id prefix {p!r} (use P D T E N H for items, M for turns)")
    return (KIND_BY_PREFIX[p] if p else None), int(m.group(2))


def derive_title(text: str, limit: int = TITLE_CHARS) -> str:
    first = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    m = re.match(r"(.+?[.!?])(\s|$)", first)
    if m and len(m.group(1)) <= limit:
        return m.group(1)
    return truncate(first, limit)


# --------------------------------------------------------------------- items
def _fts_put(store, table: str, rowid: int, body: str) -> None:
    store.x(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
    store.x(f"INSERT INTO {table}(rowid, body) VALUES(?,?)", (rowid, body))


def add_item(store, kind: str, text: str, *, status: Optional[str] = None, title: Optional[str] = None,
             why: Optional[str] = None, files: Optional[Iterable[str]] = None, priority: int = 0,
             session_id: Optional[str] = None, source: str = "agent", origin: Optional[str] = None,
             replaces: Optional[int] = None, created_at: Optional[str] = None,
             updated_at: Optional[str] = None, closed_at: Optional[str] = None,
             close_note: Optional[str] = None) -> int:
    """Add an item. `created_at`/`updated_at`/`closed_at`/`close_note` exist for imports (migration)
    that must keep an item's original history; normal writes leave them unset."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} (one of: {', '.join(KINDS)})")
    _, default, allowed = KINDS[kind]
    status = status or default
    if status not in allowed:
        raise ValueError(f"{kind} status must be one of {allowed}, not {status!r}")
    text = redact(text or "").strip()
    if not text:
        raise ValueError("text is empty")
    title = truncate(redact(title), TITLE_CHARS) if title else derive_title(text)
    why = redact(why).strip() if why else None
    file_list = sorted({f.strip() for f in files or () if f and f.strip()})
    ts = created_at or now_iso()
    upd = updated_at or ts
    note = redact(close_note).strip() if close_note else None
    with store.tx():
        old = None
        if replaces is not None:
            old = store.one("SELECT id, kind FROM items WHERE id=?", (replaces,))
            if not old:
                raise ValueError(f"cannot replace {replaces}: no such item")
        cur = store.x(
            "INSERT INTO items(kind,status,title,text,why,files,priority,session_id,source,origin,"
            "close_note,created_at,updated_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, status, title, text, why, json.dumps(file_list) if file_list else None, int(priority),
             session_id, source, origin, note, ts, upd,
             None if status in LIVE[kind] else (closed_at or upd)))
        iid = int(cur.lastrowid)
        _fts_put(store, "fts_items", iid, search_text(title, text, why, " ".join(file_list)))
        if old:
            store.x("UPDATE items SET status='superseded', superseded_by=?, updated_at=?, closed_at=? WHERE id=?",
                    (iid, ts, ts, old["id"]))
    return iid


def get_item(store, iid: int) -> Optional[sqlite3.Row]:
    return store.one("SELECT * FROM items WHERE id=?", (iid,))


def close_item(store, iid: int, verb: str, note: Optional[str] = None) -> tuple[str, str]:
    """verb 'done' (task→done, error→fixed) or 'drop' (task/error→dropped, pin/note→retired,
    decision→retracted). Returns (old_status, new_status)."""
    it = get_item(store, iid)
    if not it:
        raise ValueError(f"no item {iid}")
    new = _CLOSE.get((it["kind"], verb))
    if not new:
        raise ValueError(f"`{verb}` does not apply to a {it['kind']} ({fmt_id(it['kind'], iid)})")
    if it["status"] not in LIVE[it["kind"]]:
        raise ValueError(f"{fmt_id(it['kind'], iid)} is already {it['status']}")
    ts = now_iso()
    store.x("UPDATE items SET status=?, close_note=?, updated_at=?, closed_at=? WHERE id=?",
            (new, redact(note).strip() if note else None, ts, ts, iid))
    return it["status"], new


def forget(store, iid: int) -> None:
    """Redact an item for good (e.g. a secret slipped in); the tombstone row stays."""
    it = get_item(store, iid)
    if not it:
        raise ValueError(f"no item {iid}")
    ts = now_iso()
    with store.tx():
        store.x("UPDATE items SET title='[forgotten]', text='[forgotten]', why=NULL, close_note=NULL,"
                " files=NULL, status=?, updated_at=?, closed_at=COALESCE(closed_at, ?) WHERE id=?",
                (_FORGET[it["kind"]], ts, ts, iid))
        store.x("DELETE FROM fts_items WHERE rowid=?", (iid,))


def live_clause(alias: str = "") -> str:
    a = alias + "." if alias else ""
    parts = [f"({a}kind='{k}' AND {a}status IN ({','.join(repr(s) for s in v)}))" for k, v in LIVE.items()]
    return "(" + " OR ".join(parts) + ")"


def fts_query(words: Iterable[str]) -> str:
    return " OR ".join('"%s"' % w.replace('"', "") for w in words)


def similar(store, text: str, kind: Optional[str] = None, limit: int = 3,
            exclude_id: Optional[int] = None, kinds: Optional[Iterable[str]] = None,
            min_overlap: int = 2, min_ratio: float = 0.34) -> list[sqlite3.Row]:
    """Live items that say roughly the same thing: candidates from FTS, then kept only
    if they share >= min_overlap significant terms covering >= min_ratio of the query."""
    q = terms(text)
    if len(q) < min_overlap:
        return []
    want = [kind] if kind else list(kinds or [])
    kind_sql = (" AND i.kind IN (%s)" % ",".join("?" * len(want))) if want else ""
    if store.fts:
        try:
            rows = store.q(
                "SELECT i.* FROM fts_items f JOIN items i ON i.id=f.rowid WHERE fts_items MATCH ? AND "
                + live_clause("i") + kind_sql + " ORDER BY bm25(fts_items) LIMIT 30",
                [fts_query(q), *want])
        except sqlite3.OperationalError:
            rows = []
    else:
        rows = store.q("SELECT i.* FROM items i WHERE " + live_clause("i") + kind_sql
                       + " ORDER BY i.updated_at DESC LIMIT 2000", want)
    scored = []
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        shared = len(q & terms(f"{r['title']} {r['text']} {r['why'] or ''}"))
        if shared >= min_overlap and shared / len(q) >= min_ratio:
            scored.append((shared, r["updated_at"], r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


# --------------------------------------------------------------------- turns
def add_turn(store, session_id: Optional[str], role: str, text: str, prompt_id: Optional[str] = None,
             created_at: Optional[str] = None) -> Optional[int]:
    """Store a verbatim turn (redacted, capped). Returns the id, or None if empty/duplicate."""
    text = redact(text or "").strip()
    if not text:
        return None
    if len(text) > MAX_TURN_CHARS:
        text = text[:MAX_TURN_CHARS] + f"\n[truncated {len(text) - MAX_TURN_CHARS} chars]"
    with store.tx():
        cur = store.x("INSERT OR IGNORE INTO turns(session_id,role,text,hash,prompt_id,created_at) "
                      "VALUES(?,?,?,?,?,?)",
                      (session_id or "", role, text, sha1_text(text), prompt_id, created_at or now_iso()))
        if cur.rowcount == 0:
            return None
        tid = int(cur.lastrowid)
        _fts_put(store, "fts_turns", tid, search_text(text))
    return tid


# ------------------------------------------------------------------ sessions
def touch_session(store, sid: str, agent: Optional[str] = None, cwd: Optional[str] = None,
                  title: Optional[str] = None, prompt: bool = False) -> None:
    ts = now_iso()
    store.x(
        "INSERT INTO sessions(id,agent,title,cwd,started_at,last_seen_at,prompts) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
        "agent=COALESCE(excluded.agent, sessions.agent), cwd=COALESCE(excluded.cwd, sessions.cwd), "
        "title=COALESCE(excluded.title, sessions.title), prompts=sessions.prompts+?, "
        "ended_at=NULL, end_reason=NULL",
        (sid, agent, title, cwd, ts, ts, 1 if prompt else 0, 1 if prompt else 0))


def add_session(store, sid: str, *, agent: Optional[str] = None, title: Optional[str] = None,
                cwd: Optional[str] = None, started_at: Optional[str] = None, last_seen_at: Optional[str] = None,
                ended_at: Optional[str] = None, end_reason: Optional[str] = None, prompts: int = 0) -> bool:
    """Insert a session with explicit history (imports). Existing ids are left alone; True if inserted."""
    ts = started_at or now_iso()
    cur = store.x(
        "INSERT OR IGNORE INTO sessions(id,agent,title,cwd,started_at,last_seen_at,ended_at,end_reason,prompts) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, agent, redact(title).strip() if title else None, cwd, ts, last_seen_at or ts, ended_at, end_reason,
         int(prompts)))
    return cur.rowcount > 0


def end_session(store, sid: str, reason: Optional[str] = None) -> None:
    ts = now_iso()
    store.x("UPDATE sessions SET ended_at=?, end_reason=?, last_seen_at=? WHERE id=?", (ts, reason, ts, sid))


def resolve_session(store, explicit: Optional[str] = None) -> Optional[str]:
    """Session to stamp on CLI writes (provenance only)."""
    if explicit:
        return explicit
    env = os.environ.get("AGRES_SESSION")
    if env:
        return env
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=SESSION_WINDOW_H)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    r = store.one("SELECT id FROM sessions WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT 1", (since,))
    return r["id"] if r else None
