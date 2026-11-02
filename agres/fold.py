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
LINE_CHARS = 300        # any previewed line is cut to this (one minified-JSON line must not flood the window)
PREVIEW_CHARS = 6000    # hard cap on a whole preview
UNFOLD_LINES = 200
UNFOLD_CHARS = 16_000
INDEX_BODY_CHARS = 200_000
MIN_FOLD_CHARS = 1000  # matches the fast path in __main__.py
# `agres` in command position (start, after ; & | ( ` ' " $( or a path), or `python -m agres`. A path or an
# argument that merely contains "agres" (cd ~/Dev/agres, grep agres x) is not a match.
AGRES_CMD = re.compile(r"""(?:^|[;&|(`'"]|\$\()\s*(?:[\w.~\-]*/)*agres(?=$|[\s"'`);|&])|-m\s+agres\b""")


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
def split_lines(text: str) -> list[str]:
    """Lines as a shell counts them: split on \\n only (\\r and form feeds stay inside a line)."""
    rows = text.split("\n")
    if rows and rows[-1] == "" and text.endswith("\n"):
        rows.pop()
    return rows


def _cut(line: str, limit: int = LINE_CHARS) -> str:
    return line if len(line) <= limit else line[:limit] + f" …[+{len(line) - limit} chars]"


def preview(text: str, head: int = 40, tail: int = 20) -> str:
    """Top lines, salient lines from the middle, bottom lines — verbatim but each cut to LINE_CHARS, the
    whole capped at PREVIEW_CHARS — with line numbers on the salient lines for `unfold --lines`."""
    lines = split_lines(text)
    n = len(lines)
    top_idx = list(range(min(head, n)))
    bot_idx = list(range(max(len(top_idx), n - tail), n))
    out_top, used = [], 0
    for i in top_idx:  # top gets up to ~45% of the budget
        line = _cut(lines[i])
        if used + len(line) + 1 > PREVIEW_CHARS * 0.45 and out_top:
            break
        out_top.append(line)
        used += len(line) + 1
    shown_top = len(out_top)
    out_bot, bused = [], 0
    for i in reversed(bot_idx):  # bottom up to ~25%
        line = _cut(lines[i])
        if bused + len(line) + 1 > PREVIEW_CHARS * 0.25 and out_bot:
            break
        out_bot.insert(0, line)
        bused += len(line) + 1
    first_bot = n - len(out_bot)
    sal, sused = [], 0
    for i in range(shown_top, first_bot):
        if SALIENT.search(lines[i][:2000]):
            line = f"{i + 1}: {_cut(lines[i])}"
            if len(sal) >= SALIENT_MAX or sused + len(line) + 1 > PREVIEW_CHARS * 0.3:
                break
            sal.append(line)
            sused += len(line) + 1
    hidden = first_bot - shown_top
    mid = [f"… [{hidden} line(s) folded" + (f"; {len(sal)} salient line(s) kept below]" if sal else "]")] \
        if hidden > 0 else []
    if sal:
        mid += sal + ["…"]
    return "\n".join(out_top + mid + out_bot)


def line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def stub(fid: int, text: str) -> str:
    n = line_count(text)
    kb = len(text.encode("utf-8", "replace")) / 1024
    return (f"[agres fold F{fid}: {n} lines, {kb:.0f} KB folded out of context · exact text: "
            f"`agres unfold F{fid} --lines A-B` | `--grep TEXT` | `--all`]")


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
        rows = store.q("SELECT id, role, text FROM turns WHERE id BETWEEN ? AND ? "
                       + ("AND session_id=? " if meta.get("session") else "") + "ORDER BY id",
                       (lo, hi, meta["session"]) if meta.get("session") else (lo, hi))
        for t in rows:
            parts.append(f"--- M{t['id']} {t['role']}\n{t['text']}")
        want = int(meta.get("count") or (hi - lo + 1))
        if not rows:
            parts.append(f"(turns M{lo}–M{hi} are no longer stored)")
        elif len(rows) < want:
            parts.append(f"({len(rows)} of {want} turns still stored; the rest were forgotten or pruned)")
    return "\n".join(parts)


REGEX_LINE_CHARS = 4000  # --regex only looks at this much of each line (bounds pathological patterns)


def unfold(store, fid: int, lines: Optional[tuple[int, int]] = None, grep=None, context: int = 0,
           all_: bool = False, regex: Optional[str] = None) -> str:
    """`grep`: one literal or a list of literals (any matches), case-insensitive — safe on any input.
    `regex`: opt-in Python regex, matched against the first REGEX_LINE_CHARS of each line."""
    f = get(store, fid)
    if not f:
        raise ValueError(f"no fold F{fid}")
    text = full_text(store, f)
    if all_ and not lines and not grep and not regex:
        return text
    rows = split_lines(text)
    total = len(rows)
    if grep or regex:
        needles = [g.lower() for g in ([grep] if isinstance(grep, str) else list(grep or [])) if g]
        try:
            pat = re.compile(regex, re.I) if regex else None
        except re.error as e:
            raise ValueError(f"bad --regex {regex!r}: {e}")
        keep: set[int] = set()
        context = max(0, min(int(context or 0), 1000))
        for i, line in enumerate(rows):
            low = line.lower()
            if any(n in low for n in needles) or (pat is not None and pat.search(line[:REGEX_LINE_CHARS])):
                keep.update(range(max(0, i - context), min(total, i + context + 1)))
        out = [f"{i + 1}: {rows[i]}" for i in sorted(keep)]
        what = regex if regex else (needles[0] if len(needles) == 1 else needles)
        if not out:
            return f"F{fid}: no line matches {what!r} ({total} lines)"
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
                if not out:  # never return nothing: show the first line, cut
                    out.append(r[:UNFOLD_CHARS] + f" …[line cut at {UNFOLD_CHARS} chars of {len(r)}; "
                                                  "use --all]")
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


FOLD_TAG = {"agent": "[subagent] ", "span": "[compact] "}


def format_line(f) -> str:
    meta = json.loads(f["meta"]) if f["meta"] else {}
    if f["kind"] == "tool":
        extra = f"{meta.get('lines', '?')} lines"
    elif f["kind"] == "span" and meta.get("turns"):
        extra = f"M{min(meta['turns'])}–M{max(meta['turns'])}"
    else:
        extra = f["kind"]
    gist = f["title"] if f["kind"] == "tool" else f["summary"] or f["title"]
    return f"F{f['id']} [{extra}] {FOLD_TAG.get(f['kind'], '')}{truncate(gist, 110)}"
