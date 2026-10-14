"""The injection pack: what a fresh session (or one just compacted) needs to know.

Only what can't be re-read from the code: the last handoff, constraints,
rejected ideas, open work, recent decisions — full text for the few that must
hold, a one-line index (`ID title`) for the rest, recall commands at the end.
Budgeted in characters (Claude Code caps hook context at 10k chars); items are
dropped whole, never cut mid-word.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from typing import Iterable, Optional

from .memory import fmt_id, live_clause
from .util import ago, parse_iso, truncate

DEFAULT_CHARS = 6000
MAX_CHARS = 9000
ITEM_CHARS = 160
CRIT_CHARS = 300
HANDOFF_CHARS = 700
INDEX_TITLE = 48
INDEX_LINE = 200
STALE_DAYS = 21
PINNED_MAX = 12
INDEX_SHARE = 0.2
DECISIONS_MAX = 8
REJECTED_MAX = 10
TASKS_MAX = 8
ERRORS_MAX = 5

FOOTER = ("—\n"
          "Recall: `agres search \"<words>\"` · exact text: `agres show <ID>` · code map: `agres map`\n"
          "Record: `agres decide|reject|pin|task|note|handoff \"<text>\"` · close: `agres done|drop <ID>`"
          " · replace: `--replaces <ID>`")
_LIVE_SQL = live_clause()
EMPTY = "No memory recorded yet for this project — record decisions, constraints and open tasks as you work."


class _Body:
    def __init__(self, limit: int):
        self.lines: list[str] = []
        self.used = 0
        self.limit = limit

    def room(self, text: str) -> bool:
        return self.used + len(text) + 1 <= self.limit

    def add(self, text: str) -> None:
        self.lines.append(text)
        self.used += len(text) + 1

    def section(self, title: str, entries: Iterable[tuple[int, str]]) -> list[int]:
        shown, opened = [], False
        for iid, line in entries:
            need = len(line) + 1 + (0 if opened else len(title) + 2)
            if self.used + need > self.limit:
                continue  # drop this whole item; a shorter later one may still fit
            if not opened:
                self.add("")
                self.add(title)
                opened = True
            self.add(line)
            shown.append(iid)
        return shown


def git_state(root: str, timeout: float = 1.0) -> tuple[Optional[str], Optional[str], list[str]]:
    """(branch, short sha, dirty tracked files) — best effort, never raises."""
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain=v2", "--branch", "-uno"],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, None, []
    if out.returncode != 0:
        return None, None, []
    branch = sha = None
    dirty = []
    for line in out.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            sha = None if oid.startswith("(") else oid[:7]
        elif line[:2] in ("1 ", "2 "):
            parts = line.split(" ", 8 if line[0] == "1" else 9)
            dirty.append(parts[-1].split("\t")[0])
    return branch, sha, dirty


def _age_days(ts: str) -> float:
    try:
        return (_dt.datetime.now(_dt.timezone.utc) - parse_iso(ts)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.0


def _short_ago(ts: str) -> str:
    return ago(ts).replace(" ago", "")


def _focus_score(it, focus: list[str]) -> int:
    if not focus:
        return 0
    files = set(json.loads(it["files"])) if it["files"] else set()
    text = it["text"]
    score = 0
    for f in focus:
        base = os.path.basename(f)
        if f in files or (len(base) >= 4 and base in text):
            score += 1
    return score


def build(store, budget_chars: int = DEFAULT_CHARS, query: Optional[str] = None,
          focus_files: Optional[Iterable[str]] = None, notice: Optional[str] = None, git: bool = True) -> str:
    budget = max(400, min(int(budget_chars), MAX_CHARS))
    branch, sha, dirty = git_state(store.root) if git else (None, None, [])
    focus = list(focus_files) if focus_files is not None else dirty

    where = f"{branch}@{sha}" if branch and sha else (branch or sha or "")
    meta = ", ".join(x for x in (where, f"{len(dirty)} dirty" if dirty else "") if x)
    header = f"# Agres memory — {os.path.basename(store.root)}" + (f" ({meta})" if meta else "")
    top = [header] + ([notice] if notice else [])
    body = _Body(budget - sum(len(t) + 1 for t in top) - len(FOOTER) - 2)

    def rows(sql: str, args=()):
        return store.q(sql, args)

    shown: set[int] = set()

    # 1. last handoff
    hs = rows("SELECT * FROM items WHERE kind='handoff' AND status='active' ORDER BY created_at DESC, id DESC")
    written = [r for r in hs if r["source"] != "auto"]
    h = written[0] if written else (hs[0] if hs else None)
    if h:
        text = h["text"] if len(h["text"]) <= HANDOFF_CHARS else truncate(h["text"], HANDOFF_CHARS)
        newer = [r for r in hs if r["source"] == "auto" and (r["created_at"], r["id"]) > (h["created_at"], h["id"])]
        if newer:
            a = newer[0]
            text += f"\nSince then ({fmt_id('handoff', a['id'])}, {ago(a['created_at'])}, auto): {truncate(a['text'], 300)}"
        shown.update(body.section(f"## Last handoff ({fmt_id('handoff', h['id'])}, {ago(h['created_at'])})",
                                  [(h["id"], text)]))

    # 2. critical pins, 3. rejected decisions
    crit = rows("SELECT * FROM items WHERE kind='pin' AND status='active' AND priority>0 ORDER BY updated_at DESC, id DESC")
    shown.update(body.section("## Constraints (must hold)",
                              [(r["id"], f"- P{r['id']} {truncate(r['text'], CRIT_CHARS)}") for r in crit]))
    rej = rows("SELECT * FROM items WHERE kind='decision' AND status='rejected' ORDER BY updated_at DESC, id DESC")
    shown.update(body.section("## Rejected — don't re-propose", [
        (r["id"], f"- D{r['id']} " + truncate(r["text"] + (f" — why: {r['why']}" if r["why"] else ""), ITEM_CHARS))
        for r in rej[:REJECTED_MAX]]))

    # 4. open work (stale tasks fall through to the index)
    tasks = [r for r in rows("SELECT * FROM items WHERE kind='task' AND status='open' ORDER BY updated_at DESC, id DESC")
             if _age_days(r["updated_at"]) <= STALE_DAYS]
    errs = rows("SELECT * FROM items WHERE kind='error' AND status='open' ORDER BY updated_at DESC, id DESC")
    shown.update(body.section("## Open work", [
        (r["id"], f"- {fmt_id(r['kind'], r['id'])} {truncate(r['text'], ITEM_CHARS)} · {_short_ago(r['updated_at'])}")
        for r in tasks[:TASKS_MAX] + errs[:ERRORS_MAX]]))

    # 5. related to an explicit query
    if query:
        from .search import search  # local import keeps the hook path light when no query
        hits = [x for x in search(store, query, include_turns=False, include_code=False, limit=10)]
        rel = []
        for x in hits:
            iid = int(x["id"][1:])
            if iid not in shown:
                rel.append((iid, f"- {x['id']} {truncate(x['snippet'], ITEM_CHARS)}"))
        shown.update(body.section(f"## Related to \"{truncate(query, 60)}\"", rel[:6]))

    # 6-7 may not starve the index: keep ~20% for it when more items exist than these sections hold
    live = store.one("SELECT COUNT(*) FROM items WHERE kind!='handoff' AND " + _LIVE_SQL)[0]
    reserve = int(budget * INDEX_SHARE) if live > len(shown) + PINNED_MAX + DECISIONS_MAX else 0
    body.limit -= reserve

    # 6. other pins, focus-ranked
    pins = [r for r in rows("SELECT * FROM items WHERE kind='pin' AND status='active' AND priority=0 "
                            "ORDER BY updated_at DESC, id DESC") if r["id"] not in shown]
    pins.sort(key=lambda r: -_focus_score(r, focus))  # stable: recency breaks ties
    shown.update(body.section("## Pinned", [(r["id"], f"- P{r['id']} {truncate(r['text'], ITEM_CHARS)}")
                                            for r in pins[:PINNED_MAX]]))

    # 7. recent accepted decisions
    decs = [r for r in rows("SELECT * FROM items WHERE kind='decision' AND status='accepted' ORDER BY updated_at DESC, id DESC")
            if r["id"] not in shown]
    shown.update(body.section("## Recent decisions", [(r["id"], f"- D{r['id']} {truncate(r['text'], ITEM_CHARS)}")
                                                      for r in decs[:DECISIONS_MAX]]))

    body.limit += reserve

    # 8. index of everything else that still holds
    rest = [r for r in rows(
        "SELECT * FROM items WHERE kind!='handoff' AND " + _LIVE_SQL + " ORDER BY CASE kind WHEN 'pin' THEN 0 WHEN 'decision' THEN 1 "
        "WHEN 'task' THEN 2 WHEN 'error' THEN 3 ELSE 4 END, updated_at DESC, id DESC") if r["id"] not in shown]
    _index(body, rest)

    if not body.lines:
        body.add("")
        body.add(EMPTY)
    return "\n".join(top + body.lines + ["", FOOTER])


def _index(body: _Body, rest) -> None:
    """`ID title · ID title …` lines until the budget, then a `+N more` pointer."""
    if not rest:
        return
    entries = []
    for r in rest:
        stale = " (stale)" if r["kind"] == "task" and _age_days(r["updated_at"]) > STALE_DAYS else ""
        mark = " ✗" if r["status"] == "rejected" else ""
        entries.append(f"{fmt_id(r['kind'], r['id'])}{stale}{mark} {truncate(r['title'], INDEX_TITLE)}")
    title = "## More (`agres show <ID>`)"
    reserve = 60  # room for the "+N more" pointer
    if body.used + len(title) + 2 + reserve > body.limit:
        return
    body.add("")
    body.add(title)
    placed, line, pending, full = 0, "", 0, False
    for e in entries:
        cand = f"{line} · {e}" if line else e
        if len(cand) <= INDEX_LINE:
            line, pending = cand, pending + 1
            continue
        if body.used + len(line) + 1 + reserve > body.limit:
            full = True
            break
        body.add(line)
        placed += pending
        line, pending = e, 1
    if line and not full and body.room(line) and (pending == len(entries) - placed
                                                  or body.used + len(line) + 1 + reserve <= body.limit):
        body.add(line)
        placed += pending
    left = len(entries) - placed
    if left:
        tail = f"… +{left} more: `agres search \"<words>\"`"
        if body.room(tail):
            body.add(tail)
