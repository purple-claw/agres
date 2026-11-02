"""Origami: fold text out of the model's window into the store, unfold the exact text by ID.

Four kinds of fold share one table (ids `F<n>`):

| kind   | made by                                  | body                          |
|--------|------------------------------------------|-------------------------------|
| tool   | PostToolUse hook, big Bash output        | the full output (redacted)    |
| agent  | PostToolUse hook on a subagent's return  | prompt + returned result      |
| span   | PostCompact hook, the compacted span     | none: `meta.turns` → turns    |
| manual | `agres fold` (the agent's own summary)   | --turns range or --stdin text |

The raw store stays complete; only the model's view is folded. Previews are deterministic
(head, salient lines, tail — verbatim), never an LLM summary. See docs/adr/0002.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .util import now_iso, redact, search_text, truncate

KINDS = ("tool", "agent", "span", "manual")
SCHEMA = """
CREATE TABLE IF NOT EXISTS folds(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('tool','agent','span','manual')),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT,
  meta TEXT,
  session_id TEXT,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS folds_created ON folds(created_at);
"""
SALIENT = re.compile(r"\b(error|errors|fail|failed|failure|FAILED|FAIL|exception|traceback|warning|panic|"
                     r"fatal|segfault|assert(ion)?|denied|undefined|undeclared|not found)\b", re.I)
SALIENT_MAX = 30
SALIENT_CHARS = 300
UNFOLD_LINES = 200
UNFOLD_CHARS = 16_000
INDEX_BODY_CHARS = 200_000
MIN_FOLD_CHARS = 1000  # matches the fast path in __main__.py
AGRES_CMD = re.compile(r"(^|[\s;&|(/])agres(\s|$)")


# --------------------------------------------------------------------- store
def add_fold(store, kind: str, title: str, summary: str, body: Optional[str] = None,
             meta: Optional[dict] = None, session_id: Optional[str] = None, source: str = "hook") -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown fold kind {kind!r}")
    title = truncate(redact(title or kind).strip() or kind, 80)
    summary = redact(summary or "").strip()
    body = redact(body) if body is not None else None
    with store.tx():
        cur = store.x("INSERT INTO folds(kind,title,summary,body,meta,session_id,source,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (kind, title, summary, body, json.dumps(meta) if meta else None, session_id, source,
                       now_iso()))
        fid = int(cur.lastrowid)
        store.x("DELETE FROM fts_folds WHERE rowid=?", (fid,))
        store.x("INSERT INTO fts_folds(rowid, body) VALUES(?,?)",
                (fid, search_text(title, summary, (body or "")[:INDEX_BODY_CHARS])))
    return fid


def get(store, fid: int):
    return store.one("SELECT * FROM folds WHERE id=?", (fid,))


def delete(store, fid: int) -> None:
    with store.tx():
        store.x("DELETE FROM fts_folds WHERE rowid=?", (fid,))
        store.x("DELETE FROM folds WHERE id=?", (fid,))


def recent(store, limit: int = 8) -> list:
    """Newest first; summaries (span/agent/manual) before raw tool folds."""
    return store.q("SELECT * FROM folds ORDER BY (kind='tool'), created_at DESC, id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------- preview
def preview(text: str, head: int = 40, tail: int = 20) -> str:
    """Top lines, salient lines from the middle, bottom lines — all verbatim, with line numbers for the
    middle so `unfold --lines` can jump straight there."""
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    top, mid, bottom = lines[:head], lines[head:len(lines) - tail], lines[len(lines) - tail:]
    sal = []
    for i, line in enumerate(mid, start=head + 1):
        if SALIENT.search(line):
            sal.append(f"{i}: {truncate(line, SALIENT_CHARS)}")
            if len(sal) >= SALIENT_MAX:
                break
    out = top + [f"… [{len(mid)} lines folded"
                 + (f"; {len(sal)} salient line(s) kept below]" if sal else "]")]
    out += sal
    if sal:
        out.append("…")
    return "\n".join(out + bottom)


def line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def stub(fid: int, text: str) -> str:
    n = line_count(text)
    kb = len(text.encode("utf-8", "replace")) / 1024
    return (f"[agres fold F{fid}: {n} lines, {kb:.0f} KB folded out of context · exact text: "
            f"`agres unfold F{fid} --lines A-B` | `--grep PATTERN` | `--all`]")


# --------------------------------------------------------------- tool folds
def tool_fold(store, payload: dict, cfg: dict, session_id: Optional[str] = None) -> Optional[dict]:
    """PostToolUse: return the replacement tool_response, or None to leave the output alone."""
    if payload.get("tool_name") not in (cfg.get("fold_tools") or ["Bash"]):
        return None
    resp = payload.get("tool_response")
    if not isinstance(resp, dict) or resp.get("interrupted") or resp.get("isImage"):
        return None
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    if AGRES_CMD.search(cmd):
        return None  # unfold output must stay unfolded
    limit = max(MIN_FOLD_CHARS, int(cfg.get("fold_chars") or 12000))
    new = dict(resp)
    changed = False
    for key in ("stdout", "stderr"):
        text = resp.get(key)
        if not isinstance(text, str) or len(text) <= limit:
            continue
        if key == "stdout":
            text = _persisted(resp) or text
        fid = add_fold(store, "tool", f"{payload.get('tool_name')} {key}: {truncate(cmd, 60)}",
                       f"{key} of `{truncate(cmd, 200)}`", body=text,
                       meta={"tool": payload.get("tool_name"), "stream": key, "command": cmd,
                             "lines": line_count(text), "chars": len(text),
                             "tool_use_id": payload.get("tool_use_id")},
                       session_id=session_id)
        new[key] = preview(text, int(cfg.get("fold_head") or 40), int(cfg.get("fold_tail") or 20)) \
            + "\n" + stub(fid, text)
        changed = True
    return new if changed else None


PERSISTED_MAX = 50 * 1024 * 1024


def _persisted(resp: dict) -> Optional[str]:
    """Claude Code keeps an output over ~30k chars in a file and gives hooks only its first 30,000 chars
    (`persistedOutputPath`, `persistedOutputSize`; seen on 2.1.316). Fold the whole file so unfold is exact."""
    path = resp.get("persistedOutputPath")
    if not isinstance(path, str) or not path:
        return None
    try:
        import os
        if os.path.getsize(path) > PERSISTED_MAX:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def agent_fold(store, payload: dict, session_id: Optional[str] = None) -> Optional[int]:
    """PostToolUse on a subagent: keep its brief and result (branch → return) so they survive compaction."""
    ti = payload.get("tool_input") or {}
    result = _flatten(payload.get("tool_response")).strip()
    if not result:
        return None
    desc = ti.get("description") or ti.get("subagent_type") or "subagent"
    prompt = str(ti.get("prompt") or "")
    body = f"## Brief\n{prompt}\n\n## Result\n{result}"
    return add_fold(store, "agent", f"subagent: {desc}", truncate(" ".join(result.split()), 400), body=body,
                    meta={"subagent_type": ti.get("subagent_type"), "chars": len(result),
                          "tool_use_id": payload.get("tool_use_id")}, session_id=session_id)


def _flatten(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "\n".join(_flatten(x) for x in obj if x is not None)
    if isinstance(obj, dict):
        for key in ("result", "text", "content", "output", "message"):
            if key in obj:
                return _flatten(obj[key])
        return ""
    return ""


# -------------------------------------------------------------------- unfold
def full_text(store, f) -> str:
    meta = json.loads(f["meta"]) if f["meta"] else {}
    parts = []
    if f["body"] is not None:
        parts.append(f["body"])
    turns = meta.get("turns")
    if turns:
        lo, hi = min(turns), max(turns)
        for t in store.q("SELECT id, role, text FROM turns WHERE id BETWEEN ? AND ? "
                         + ("AND session_id=? " if meta.get("session") else "") + "ORDER BY id",
                         (lo, hi, meta["session"]) if meta.get("session") else (lo, hi)):
            parts.append(f"--- M{t['id']} {t['role']}\n{t['text']}")
        if len(parts) == (1 if f["body"] is not None else 0):
            parts.append(f"(turns M{lo}–M{hi} are no longer stored)")
    return "\n".join(parts)


def unfold(store, fid: int, lines: Optional[tuple[int, int]] = None, grep: Optional[str] = None,
           context: int = 0, all_: bool = False) -> str:
    f = get(store, fid)
    if not f:
        raise ValueError(f"no fold F{fid}")
    text = full_text(store, f)
    if all_ and not lines and not grep:
        return text
    rows = text.splitlines()
    total = len(rows)
    if grep:
        try:
            pat = re.compile(grep, re.I)
        except re.error:
            pat = re.compile(re.escape(grep), re.I)
        keep: set[int] = set()
        for i, line in enumerate(rows):
            if pat.search(line):
                keep.update(range(max(0, i - context), min(total, i + context + 1)))
        out = [f"{i + 1}: {rows[i]}" for i in sorted(keep)]
        if not out:
            return f"F{fid}: no line matches {grep!r} ({total} lines)"
        return _bounded(out, all_, f"F{fid}: {len(out)} matching line(s) of {total}")
    lo, hi = lines if lines else (1, total)
    lo, hi = max(1, lo), min(total, hi)
    chosen = rows[lo - 1:hi]
    body = _bounded(chosen, all_, None)
    shown = body.count("\n") + 1 if body else 0
    if lines or all_:
        return body
    return body + f"\n[F{fid}: showing lines 1–{min(shown, total)} of {total}; " \
                  f"more: `agres unfold F{fid} --lines A-B` | `--grep PATTERN` | `--all`]"


def _bounded(rows: list[str], all_: bool, footer: Optional[str]) -> str:
    if all_:
        out = rows
    else:
        out, used = [], 0
        for r in rows[:UNFOLD_LINES]:
            if used + len(r) + 1 > UNFOLD_CHARS:
                break
            out.append(r)
            used += len(r) + 1
    text = "\n".join(out)
    if footer:
        text += "\n" + footer + ("" if len(out) == len(rows) else f" (first {len(out)} shown; narrow it)")
    return text


def parse_range(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*[Mm]?(\d+)\s*(?:[-–:]\s*[Mm]?(\d+))?\s*", spec or "")
    if not m:
        raise ValueError(f"not a range: {spec!r} (use A-B)")
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (a, b) if a <= b else (b, a)


def format_line(f) -> str:
    meta = json.loads(f["meta"]) if f["meta"] else {}
    if f["kind"] == "tool":
        extra = f"{meta.get('lines', '?')} lines"
    elif f["kind"] == "span" and meta.get("turns"):
        extra = f"M{min(meta['turns'])}–M{max(meta['turns'])}"
    else:
        extra = f["kind"]
    return f"F{f['id']} [{extra}] {truncate(f['title'] if f['kind'] == 'tool' else f['summary'] or f['title'], 110)}"
