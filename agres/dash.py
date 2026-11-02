"""`agres dash`: everything Agres knows about this project on one screen.

Panels: attention · memory · next session (what gets injected, section by section) · constraints and
rejections · open work · origami (folds) · 14-day activity · sessions · hooks · code index · privacy
and store. Two columns from 120 columns up, stacked below. Stdlib only, no curses: plain lines,
so it pipes, logs and tests like any other output. `--watch N` redraws every N seconds.

Data comes from `collect()` (a JSON-safe dict, also `agres dash --json`); `render()` is pure.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sys
import time
from typing import Optional

from . import fold as _fold
from . import pack, status
from .memory import KINDS, LIVE, fmt_id, source_tag
from .util import ago, truncate

DAYS = 14
TWO_COL = 120
SPARK = "▁▂▃▄▅▆▇█"
STYLE = {"title": "1;36", "dim": "2", "ok": "32", "warn": "33", "err": "31", "bar": "36", "acc": "35",
         "bold": "1", "": ""}
KIND_LABEL = {"pin": "pins", "decision": "decisions", "task": "tasks", "error": "errors", "note": "notes",
              "handoff": "handoffs"}


# ------------------------------------------------------------------ collect
def collect(s) -> dict:
    d = status.collect(s)
    cfg = d["config"]
    budget = int(cfg.get("budget_chars") or pack.DEFAULT_CHARS)
    text = pack.build(s, budget_chars=budget)
    d["pack_sections"] = _sections(text)
    d["pack_text_chars"] = len(text)

    rows = s.q("SELECT kind, status, COUNT(*) n FROM items GROUP BY kind, status")
    matrix = {k: {"live": 0, "closed": 0, "superseded": 0} for k in KINDS}
    for r in rows:
        bucket = "live" if r["status"] in LIVE[r["kind"]] else "superseded" if r["status"] == "superseded" else "closed"
        matrix[r["kind"]][bucket] += r["n"]
    d["matrix"] = matrix

    today = _dt.datetime.now(_dt.timezone.utc).date()
    days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]
    since = days[0] + "T00:00:00Z"

    def per_day(sql: str) -> list[int]:
        got = {r[0]: r[1] for r in s.q(sql, (since,))}
        return [got.get(day, 0) for day in days]

    has_folds = bool(s.one("SELECT 1 FROM sqlite_master WHERE name='folds'"))
    d["daily"] = {
        "days": days,
        "prompts": per_day("SELECT substr(created_at,1,10), COUNT(*) FROM turns WHERE role='user' AND created_at>=? GROUP BY 1"),
        "replies": per_day("SELECT substr(created_at,1,10), COUNT(*) FROM turns WHERE role='assistant' AND created_at>=? GROUP BY 1"),
        "items": per_day("SELECT substr(created_at,1,10), COUNT(*) FROM items WHERE kind!='handoff' AND created_at>=? GROUP BY 1"),
        "folds": per_day("SELECT substr(created_at,1,10), COUNT(*) FROM folds WHERE created_at>=? GROUP BY 1")
        if has_folds else [0] * DAYS,
    }

    d["sessions"] = [{
        "id": r["id"], "agent": r["agent"], "started": r["started_at"], "seen": r["last_seen_at"],
        "ended": r["ended_at"], "reason": r["end_reason"], "prompts": r["prompts"],
        "handoff": bool(s.one("SELECT 1 FROM items WHERE kind='handoff' AND session_id=? AND source!='auto'", (r["id"],))),
        "items": s.one("SELECT COUNT(*) FROM items WHERE session_id=? AND kind!='handoff'", (r["id"],))[0],
    } for r in s.q("SELECT * FROM sessions ORDER BY last_seen_at DESC LIMIT 8")]

    d["folds"] = [{"id": f"F{f['id']}", "kind": f["kind"], "line": _fold.format_line(f), "at": f["created_at"]}
                  for f in (_fold.recent(s, 6) if has_folds else [])]

    def items(sql: str, limit: int) -> list[dict]:
        return [{"id": fmt_id(r["kind"], r["id"]), "text": r["text"], "why": r["why"], "tag": source_tag(r["source"]),
                 "updated": r["updated_at"]} for r in s.q(sql + f" LIMIT {int(limit)}")]

    d["constraints"] = items("SELECT * FROM items WHERE kind='pin' AND status='active' AND priority>0 "
                             "ORDER BY updated_at DESC", 8)
    d["rejected"] = items("SELECT * FROM items WHERE kind='decision' AND status='rejected' ORDER BY updated_at DESC", 8)
    d["open_work"] = items("SELECT * FROM items WHERE kind IN ('task','error') AND status='open' "
                           "ORDER BY kind DESC, updated_at DESC", 10)
    h = s.one("SELECT * FROM items WHERE kind='handoff' AND status='active' ORDER BY (source='auto'), created_at DESC LIMIT 1")
    d["handoff_text"] = h["text"] if h else None

    d["langs"] = {r[0] or "?": r[1] for r in s.q("SELECT lang, COUNT(*) FROM files GROUP BY lang ORDER BY 2 DESC LIMIT 6")}
    t = s.one("SELECT COUNT(*), MIN(created_at), SUM(LENGTH(text)) FROM turns")
    d["turns"] = {"total": t[0], "oldest": t[1], "chars": t[2] or 0}
    d["width_hint"] = TWO_COL
    return d


def _sections(text: str) -> list[dict]:
    """Chars per pack section (header, each `## …`, footer) — where the injected budget goes."""
    out, cur, size = [], "header", 0
    for line in text.splitlines():
        if line.startswith("## "):
            out.append({"name": cur, "chars": size})
            cur, size = re.sub(r"\s*[(—`].*$", "", line[3:]).strip() or line[3:], 0
        elif line.startswith("—") and "Recall:" not in line and cur != "footer":
            out.append({"name": cur, "chars": size})
            cur, size = "footer", 0
        size += len(line) + 1
    out.append({"name": cur, "chars": size})
    return [x for x in out if x["chars"] > 0]


# ------------------------------------------------------------------- render
class Line:
    """A line of (text, style) segments, so width is measured on visible text only."""

    def __init__(self, *segs):
        self.segs = [(str(t), st) for t, st in segs if t != ""]

    def add(self, text, style=""):
        if text:
            self.segs.append((str(text), style))
        return self

    def __len__(self):
        return sum(len(t) for t, _ in self.segs)

    def fit(self, width: int) -> "Line":
        out, used = [], 0
        for t, st in self.segs:
            if used + len(t) > width:
                room = width - used
                if room > 1:
                    out.append((t[:room - 1] + "…", st))
                elif room == 1:
                    out.append(("…", st))
                used = width
                break
            out.append((t, st))
            used += len(t)
        line = Line(*out)
        if used < width:
            line.add(" " * (width - used))
        return line

    def show(self, color: bool) -> str:
        if not color:
            return "".join(t for t, _ in self.segs)
        return "".join(f"\x1b[{STYLE[st]}m{t}\x1b[0m" if STYLE.get(st) else t for t, st in self.segs)


def L(*segs) -> Line:
    return Line(*segs)


def bar(value: float, maximum: float, width: int) -> str:
    if width <= 0:
        return ""
    filled = 0 if maximum <= 0 else max(0, min(width, round(width * value / maximum)))
    if value > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def spark(values: list[int]) -> str:
    top = max(values) if values else 0
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, round((len(SPARK) - 1) * v / top))] for v in values)


def panel(title: str, body: list[Line], width: int) -> list[Line]:
    inner = width - 4
    head = L(("╭─ ", "dim"), (title, "title"), (" " + "─" * max(0, width - len(title) - 5) + "╮", "dim"))
    out = [head.fit(width)]
    for line in body or [L(("—", "dim"))]:
        out.append(L(("│ ", "dim")).add_line(line.fit(inner)).add(" │", "dim"))
    out.append(L(("╰" + "─" * (width - 2) + "╯", "dim")))
    return out


def _add_line(self, other: Line) -> Line:
    self.segs.extend(other.segs)
    return self


Line.add_line = _add_line


def _kv(label: str, value: str, style: str = "", lw: int = 13) -> Line:
    return L((label.ljust(lw), "dim"), (value, style))


def _p(d: dict, w: int) -> dict[str, list[Line]]:
    """Body lines for every panel at inner width `w`."""
    m, n, o, h, k, cfg = d["memory"], d["next_session"], d["origami"], d["hooks"], d["code"], d["config"]
    P: dict[str, list[Line]] = {}

    # attention
    mark = {"warn": ("!", "warn"), "err": ("✗", "err"), "info": ("·", "acc")}
    P["ATTENTION"] = [L(mark[a["level"]], (" " + a["msg"], ""), ("  → " + a["fix"], "dim")) for a in d["attention"]] \
        or [L(("✓ ", "ok"), ("nothing needs attention", ""))]

    # memory
    mx = d["matrix"]
    top = max([v["live"] for v in mx.values()] + [1])
    bw = max(4, min(12, w - 48))
    body = []
    for kind in ("pin", "decision", "task", "error", "note"):
        v = mx[kind]
        extra = f"{v['closed']} closed · {v['superseded']} superseded" if v["closed"] or v["superseded"] else ""
        live = v["live"]
        label = KIND_LABEL[kind]
        if kind == "pin" and m["critical_pins"]:
            extra = f"{m['critical_pins']} critical" + (" · " + extra if extra else "")
        if kind == "decision" and m["rejected"]:
            extra = f"{m['rejected']} rejected" + (" · " + extra if extra else "")
        body.append(L((label.ljust(10), "dim"), (bar(live, top, bw), "bar"), (f" {live:>4}", "bold"),
                      (f"  {extra}" if extra else "", "dim")))
    if not any(v["live"] + v["closed"] + v["superseded"] for v in mx.values()):
        body = [L(("nothing recorded yet — agres decide|reject|pin|task …", "dim"))]
    ho = m["handoff"]
    body.append(L(("handoff".ljust(10), "dim"),
                  (f"{ho['id']} {ago(ho['at'])}" + (" (auto)" if ho["source"] == "auto" else ""), "acc") if ho
                  else ("none yet", "warn")))
    if d.get("handoff_text"):
        body.append(L(("          " + truncate(d["handoff_text"], max(10, w - 12)), "")))
    P["MEMORY"] = body

    # next session
    used, budget = n["chars"], n["budget"]
    bw2 = max(6, w - 26)
    body = [L(("block".ljust(10), "dim"), (bar(used, budget, bw2), "bar" if used < budget * 0.95 else "warn"),
              (f" {used:,}/{budget:,}", "bold"))]
    body.append(L(("reach".ljust(10), "dim"), (f"{n['reachable']} of {n['live_items']} live items by ID", ""),
                  (f" · {n['in_full']} in full", "dim")))
    secs = d["pack_sections"]
    smax = max([x["chars"] for x in secs] + [1])
    sw = max(4, min(16, w - 34))
    for x in secs:
        body.append(L(("  " + truncate(x["name"], 16).ljust(18), "dim"), (bar(x["chars"], smax, sw), "acc"),
                      (f" {x['chars']:>5}", "")))
    P["NEXT SESSION"] = body

    # constraints & rejected
    body = []
    for it in d["constraints"]:
        body.append(L((it["id"].ljust(5), "ok"), ((it["tag"] + " ") if it["tag"] else "", "warn"), (it["text"], "")))
    for it in d["rejected"]:
        why = f" — {it['why']}" if it["why"] else ""
        body.append(L((it["id"].ljust(5), "err"), ("✗ ", "err"), ((it["tag"] + " ") if it["tag"] else "", "warn"),
                      (it["text"], ""), (why, "dim")))
    P["CONSTRAINTS & REJECTED"] = body or [L(("no critical pins or rejections", "dim"))]

    # open work
    body = []
    for it in d["open_work"]:
        age = ago(it["updated"]).replace(" ago", "")
        stale = status._since(pack.STALE_DAYS) > it["updated"]
        body.append(L((it["id"].ljust(5), "warn" if it["id"][0] == "E" else "acc"), (it["text"], ""),
                      (f" · {age}" + (" stale" if stale else ""), "warn" if stale else "dim")))
    P["OPEN WORK"] = body or [L(("no open tasks or errors", "dim"))]

    # origami
    kinds = ", ".join(f"{v} {kd}" for kd, v in sorted(o["by_kind"].items()))
    body = [L(("fold".ljust(10), "dim"), ("on" if o["on"] else "off", "ok" if o["on"] else "warn"),
              (f" · Bash output > {int(o['fold_chars'] or 0):,} chars", "dim")),
            L(("folds".ljust(10), "dim"), (f"{o['folds']:,}", "bold"), (f"  {kinds}" if kinds else "", "dim")),
            L(("kept out".ljust(10), "dim"), (f"{o['tool_chars_folded'] / 1024:,.0f} KB", "bold"),
              (" of tool output never entered the window", "dim"))]
    for f in d["folds"]:
        body.append(L(("  " + f["line"], "")))
    if not d["folds"]:
        body.append(L(("  no folds yet: big Bash output folds itself", "dim")))
    P["ORIGAMI"] = body

    # activity
    dl = d["daily"]
    sw2 = len(dl["days"])
    body = []
    for key, label in (("prompts", "prompts"), ("replies", "replies"), ("items", "items"), ("folds", "folds")):
        vals = dl[key]
        body.append(L((label.ljust(10), "dim"), (spark(vals), "bar"), (f"  {sum(vals):>5}", "bold"),
                      (f"  max {max(vals) if vals else 0}/day", "dim")))
    body.append(L((" " * 10, ""), (f"{dl['days'][0][5:]}".ljust(max(1, sw2 - 5)) + "today", "dim")))
    a = d["activity"]
    body.append(L(("7 days".ljust(10), "dim"), (f"{a['closed']} closed · {a['sessions']} sessions · "
                                                 f"{a['turns']} turns", "")))
    P[f"ACTIVITY ({DAYS}d)"] = body

    # sessions
    body = []
    for se in d["sessions"]:
        idle = not se["ended"] and se["seen"] and se["seen"] < status._since(0.5)
        state = (f"ended {se['reason'] or ''}".strip(), "dim") if se["ended"] else (("idle", "dim") if idle else ("active", "ok"))
        body.append(L((se["id"][:12].ljust(13), "acc"), (f"{ago(se['seen']):>9}", "dim"),
                      (f"  {se['prompts']:>3} prompt" + ("" if se["prompts"] == 1 else "s"), ""),
                      (f"  {se['items']:>2} item" + ("" if se["items"] == 1 else "s"), "dim"),
                      ("  H" if se["handoff"] else "   ", "ok"), ("  ", ""), state))
    P["SESSIONS"] = body or [L(("no sessions yet", "dim"))]

    # hooks
    wired = {"plugin": ("✓ wired (plugin)", "ok"), True: ("✓ wired (settings.json)", "ok"),
             False: ("✗ not wired → agres install", "err"), None: ("no Claude Code settings", "dim")}[h["wired"]]
    body = [L(("wiring".ljust(13), "dim"), wired),
            _kv("last capture", ago(h["last_capture"]) if h["last_capture"] else "never"),
            _kv("errors 24h", str(h["errors_24h"]), "err" if h["errors_24h"] else "ok")]
    if h.get("last_error"):
        body.append(_kv("last error", h["last_error"], "err"))
    if h.get("silent_sessions"):
        body.append(_kv("silent", f"{h['silent_sessions']} session(s) with prompts but no capture", "warn"))
    P["HOOKS"] = body

    # code
    if k["files"]:
        body = [_kv("indexed", f"{k['files']:,} files · {k['symbols']:,} symbols · {k['edges']:,} edges"),
                _kv("full sync", ago(k["last_sync"]) if k["last_sync"] else "partial → agres index",
                    "" if k["last_sync"] else "warn"),
                _kv("reconciled", ago(k.get("last_reconcile")) if k.get("last_reconcile") else "not yet")]
        if k.get("resolve_pending"):
            body.append(_kv("graph", "resolve pending (next agres map)", "warn"))
        lmax = max(list(d["langs"].values()) + [1])
        for lang, cnt in d["langs"].items():
            body.append(L(("  " + truncate(lang, 10).ljust(11), "dim"), (bar(cnt, lmax, max(4, min(14, w - 26))), "acc"),
                          (f" {cnt:>5}", "")))
    else:
        body = [L(("not indexed — built on demand by agres map / agres index", "dim"))]
    P["CODE INDEX"] = body

    # privacy & store
    st, t = d["store"], d["turns"]
    days = int(cfg.get("turn_retention_days") or 0)
    pats = len(cfg.get("redact_patterns") or [])
    P["PRIVACY & STORE"] = [
        _kv("capture", cfg["capture"], "warn" if cfg["capture"] == "all" else "ok"),
        _kv("retention", f"{days} days" if days else "forever (turn_retention_days 0)"),
        _kv("redaction", "best effort" + (f" + {pats} custom pattern(s)" if pats else ""), "dim"),
        _kv("turns", f"{t['total']:,} ({t['chars'] / 1024:,.0f} KB)" + (f" · oldest {ago(t['oldest'])}" if t["oldest"] else "")),
        _kv("store", f"{st['size_bytes'] / 1_048_576:.1f} MB · schema {st['schema']} · "
                     f"{'fts5' if st['fts5'] else 'LIKE search'}"),
        _kv("path", st["path"], "dim"),
        _kv("delete", "agres forget M<id>|F<id> · --session SID · gc --dry-run", "dim"),
    ]
    return P


ROWS = [("MEMORY", "NEXT SESSION"), ("CONSTRAINTS & REJECTED", "OPEN WORK"), ("ORIGAMI", None),
        ("SESSIONS", "HOOKS"), ("CODE INDEX", "PRIVACY & STORE")]


def render(d: dict, width: int = 100, color: bool = False) -> str:
    width = max(40, int(width))
    two = width >= TWO_COL
    col = (width - 1) // 2 if two else width
    bodies = _p(d, col - 4)
    full = _p(d, width - 4)
    act = next(key for key in bodies if key.startswith("ACTIVITY"))
    rows = [("ATTENTION", None)] + [r if r != ("ORIGAMI", None) else ("ORIGAMI", act) for r in ROWS]
    if not two:
        rows = [(a, None) for pair in rows for a in pair if a]

    p, cfg = d["project"], d["config"]
    branch, sha, dirty = pack.git_state(p["root"])
    where = f"{branch}@{sha}" if branch and sha else (branch or "")
    head = L((" agres ", "title"), (d["version"], "dim"), ("  ", ""), (p["name"], "bold"),
             ("  " + ("OFF" if p["off"] else "on"), "warn" if p["off"] else "ok"),
             (f"  {where}" if where else "", "acc"), (f" · {len(dirty)} dirty" if dirty else "", "dim"),
             (f"  {time.strftime('%H:%M:%S')}", "dim"))
    out = [head.fit(width)]

    for a, b in rows:
        if b is None:
            out += panel(a, full[a], width)
            continue
        left, right = panel(a, bodies[a], col), panel(b, bodies[b], col)
        hgt = max(len(left), len(right))
        left = _pad_panel(left, hgt, col)
        right = _pad_panel(right, hgt, col)
        out += [L().add_line(x).add(" " * (width - 2 * col)).add_line(y) for x, y in zip(left, right)]

    out.append(L(("search · show <ID> · unfold F<id> · status --deep · dash --watch 5 · dash --json", "dim")).fit(width))
    return "\n".join(line.show(color) for line in out)


def _pad_panel(lines: list[Line], height: int, width: int) -> list[Line]:
    """Grow a boxed panel to `height` by inserting empty body rows before its bottom border."""
    if len(lines) >= height:
        return lines
    blank = L(("│", "dim"), (" " * (width - 2), ""), ("│", "dim"))
    return lines[:-1] + [blank] * (height - len(lines)) + lines[-1:]


# ---------------------------------------------------------------------- cli
def cli(a, store) -> int:
    def width() -> int:
        return a.width or shutil.get_terminal_size((100, 40)).columns

    color = status.use_color(False if a.no_color else None)
    if a.json:
        print(json.dumps(collect(store), indent=2, sort_keys=True, default=str))
        return 0
    if not a.watch:
        print(render(collect(store), width(), color))
        return 0
    try:
        while True:
            frame = render(collect(store), width(), color)
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(max(1.0, float(a.watch)))
    except KeyboardInterrupt:
        return 0
