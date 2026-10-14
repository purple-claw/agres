"""Recall: one ranked search over memory items, verbatim turns and code; exact `show`.

Each source is ranked on its own (FTS5 bm25, AND-matches before OR-matches),
then fused with weighted reciprocal-rank fusion so a strong decision beats a
chatty turn that happens to share the words.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterable, Optional

from .memory import KINDS, LIVE, fmt_id, live_clause, parse_id
from .util import ago, terms, truncate

WEIGHT = {"item": 1.0, "turn": 0.6, "code": 0.5}
RRF_K = 20
SNIPPET = 160


def _words(query: str) -> list[str]:
    w = sorted(terms(query))
    if not w:
        w = sorted({t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) >= 2})
    return w


def _ranked(store, table: str, join_sql: str, where_sql: str, args: list, words: list[str],
            limit: int) -> list[sqlite3.Row]:
    """Rows from `table` joined via `join_sql`, AND-matches first, then OR-matches."""
    out: list[sqlite3.Row] = []
    seen: set[int] = set()
    if store.fts:
        passes = ([" AND "] if len(words) > 1 else []) + [" OR "]
        for op in passes:
            match = op.join('"%s"*' % w.replace('"', "") for w in words)
            try:
                rows = store.q(f"SELECT t.*, bm25({table}) AS _r FROM {table} JOIN {join_sql} "
                               f"WHERE {table} MATCH ?{where_sql} ORDER BY _r LIMIT ?",
                               [match, *args, limit])
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
        return out[:limit]
    want = set(words)
    rows = store.q(f"SELECT t.*, {table}.body AS _body FROM {table} JOIN {join_sql} WHERE 1=1{where_sql}", args)
    scored = []
    for r in rows:
        shared = len(want & terms(r["_body"])) or sum(1 for w in want if w in r["_body"].lower())
        if shared:
            scored.append((shared, r["id"], r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def _snippet(text: str, words: Iterable[str]) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    pos = min((p for p in (low.find(w) for w in words) if p >= 0), default=0)
    start = max(0, pos - SNIPPET // 3)
    piece = flat[start:start + SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(flat) else "")


def search(store, query: str, kinds: Optional[Iterable[str]] = None, include_turns: bool = True,
           include_code: bool = True, include_closed: bool = False, limit: int = 12) -> list[dict]:
    words = _words(query)
    if not words:
        return []
    kinds = [k for k in (kinds or []) if k]
    item_kinds = [k for k in kinds if k in KINDS]
    lists: list[tuple[str, list[dict]]] = []

    if not kinds or item_kinds:
        where, args = "", []
        if not include_closed:
            where += " AND " + live_clause("t")
        if item_kinds:
            where += " AND t.kind IN (%s)" % ",".join("?" * len(item_kinds))
            args += item_kinds
        rows = _ranked(store, "fts_items", "items t ON t.id=fts_items.rowid", where, args, words, limit * 2)
        lists.append(("item", [{
            "id": fmt_id(r["kind"], r["id"]), "kind": r["kind"], "status": r["status"],
            "priority": r["priority"], "title": r["title"], "snippet": truncate(r["text"], SNIPPET),
            "created_at": r["created_at"]} for r in rows]))

    if include_turns and (not kinds or "turn" in kinds):
        rows = _ranked(store, "fts_turns", "turns t ON t.id=fts_turns.rowid", "", [], words, limit * 2)
        lists.append(("turn", [{
            "id": fmt_id("turn", r["id"]), "kind": "turn", "status": r["role"], "title": "",
            "snippet": _snippet(r["text"], words), "created_at": r["created_at"]} for r in rows]))

    if include_code and (not kinds or "code" in kinds):
        lists.append(("code", _code_hits(store, query, limit)))

    fused: dict[str, dict] = {}
    for source, hits in lists:
        for rank, h in enumerate(hits):
            key = h["id"]
            h = fused.setdefault(key, dict(h, score=0.0))
            h["score"] += WEIGHT[source] / (RRF_K + rank + 1)
    return sorted(fused.values(), key=lambda h: -h["score"])[:limit]


def _code_hits(store, query: str, limit: int) -> list[dict]:
    try:
        if not store.one("SELECT 1 FROM files LIMIT 1"):
            return []
        from .code import engine  # lazy: keeps hooks and memory-only commands fast
        hits = engine.search_code(store, query, limit=limit)
    except Exception:
        return []
    out = []
    for h in hits:
        loc = f"{h.get('path') or ''}:{h.get('line') or ''}".rstrip(":")
        out.append({"id": f"code:{h.get('id')}", "kind": "code", "status": h.get("kind") or "",
                    "title": h.get("name") or "", "snippet": f"{loc} {h.get('sig') or h.get('name') or ''}".strip(),
                    "created_at": None})
    return out


def format_hits(hits: list[dict]) -> str:
    if not hits:
        return "no matches"
    lines = []
    for h in hits:
        if h["kind"] == "turn":
            lines.append(f"{h['id']} {h['status']} · {ago(h['created_at'])} · {h['snippet']}")
        elif h["kind"] == "code":
            lines.append(f"code {h['status']} {h['snippet']}")
        else:
            mark = {"rejected": " ✗ rejected", "superseded": " (superseded)", "retracted": " (retracted)",
                    "done": " ✓ done", "fixed": " ✓ fixed", "dropped": " (dropped)", "retired": " (retired)"}
            flag = mark.get(h["status"], "") + (" !critical" if h.get("priority") else "")
            lines.append(f"{h['id']}{flag} · {ago(h['created_at'])} · {h['snippet']}")
    return "\n".join(lines)


# --------------------------------------------------------------------- show
def show(store, ref: str, around: int = 0) -> str:
    kind, n = parse_id(ref)
    if kind == "turn":
        return _show_turn(store, n, around)
    it = store.one("SELECT * FROM items WHERE id=?", (n,))
    if not it or (kind and it["kind"] != kind):
        raise ValueError(f"no item {ref}")
    iid = fmt_id(it["kind"], it["id"])
    head = [iid, it["kind"], it["status"] + (" · critical" if it["priority"] else ""),
            f"{it['created_at'][:10]} ({ago(it['created_at'])})", f"source {it['source']}"]
    if it["origin"]:
        head.append(f"origin {it['origin']}")
    lines = [" · ".join(head), "", it["text"]]
    if it["why"]:
        lines += ["", f"why: {it['why']}"]
    if it["files"]:
        lines.append(f"files: {', '.join(json.loads(it['files']))}")
    for r in store.q("SELECT id, kind FROM items WHERE superseded_by=? ORDER BY id", (it["id"],)):
        lines.append(f"replaces {fmt_id(r['kind'], r['id'])}")
    if it["superseded_by"]:
        nxt = store.one("SELECT kind FROM items WHERE id=?", (it["superseded_by"],))
        lines.append(f"superseded by {fmt_id(nxt['kind'] if nxt else it['kind'], it['superseded_by'])}")
    if it["closed_at"] and it["status"] not in LIVE[it["kind"]]:
        lines.append(f"closed {it['closed_at'][:10]}" + (f": {it['close_note']}" if it["close_note"] else ""))
    return "\n".join(lines)


def _show_turn(store, tid: int, around: int) -> str:
    t = store.one("SELECT * FROM turns WHERE id=?", (tid,))
    if not t:
        raise ValueError(f"no turn M{tid}")
    rows = [t]
    if around > 0:
        before = store.q("SELECT * FROM turns WHERE session_id=? AND id<? ORDER BY id DESC LIMIT ?",
                         (t["session_id"], tid, around))
        after = store.q("SELECT * FROM turns WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                        (t["session_id"], tid, around))
        rows = list(reversed(before)) + [t] + list(after)
    out = []
    for r in rows:
        mark = ">>" if r["id"] == tid else "--"
        text = r["text"] if r["id"] == tid else truncate(r["text"], 2000)
        out.append(f"{mark} M{r['id']} {r['role']} · {r['created_at']} · session {r['session_id'] or '-'}\n{text}")
    return "\n\n".join(out)
