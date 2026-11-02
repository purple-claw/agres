"""`agres status`: one screen that answers four questions —
what will the next session see, what needs attention, are the hooks working, how fresh is it all.

Every warning names the command that fixes it. Colour only on a TTY (NO_COLOR respected).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from typing import Optional

from . import __version__, config, db, pack, paths
from .memory import LIVE, fmt_id, live_clause
from .util import DEFAULT_IGNORE_DIRS, ago, now_iso, parse_iso  # noqa: F401  (now_iso re-exported for tests)

STALE_DAYS = pack.STALE_DAYS
WEEK = 7
LABEL = 13


# ------------------------------------------------------------------ helpers
def use_color(force: Optional[bool] = None) -> bool:
    if force is not None:
        return force
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _since(days: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pl(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _hooks_wired():
    """'plugin' or True when Claude Code will run agres hooks, False when it won't, None without settings."""
    p = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        enabled = json.loads(text).get("enabledPlugins") or {}
    except (ValueError, AttributeError):
        enabled = {}
    if any(k.split("@", 1)[0] == "agres" and v for k, v in enabled.items()):
        return "plugin"
    return bool(re.search(r"agres\S* hook session-start", text))


def _origami(s: db.Store, cfg: dict) -> dict:
    has = s.one("SELECT 1 FROM sqlite_master WHERE name='folds'")
    by = {r["kind"]: r["n"] for r in s.q("SELECT kind, COUNT(*) n FROM folds GROUP BY kind")} if has else {}
    saved = s.one("SELECT COALESCE(SUM(LENGTH(body)),0) FROM folds WHERE kind='tool'")[0] if by.get("tool") else 0
    return {"on": bool(cfg.get("fold")), "fold_chars": cfg.get("fold_chars"), "folds": sum(by.values()),
            "by_kind": by, "tool_chars_folded": saved}


def _hook_errors(store_dir: str, hours: int = 24) -> tuple[int, Optional[str]]:
    log = os.path.join(store_dir, "hook.log")
    if not os.path.exists(log):
        return 0, None
    cut = _since(hours / 24)
    n, last = 0, None
    with open(log, encoding="utf-8", errors="replace") as fh:
        entries = fh.read().split("--- ")[1:]
    for e in entries:
        lines = e.strip().splitlines()
        ts = lines[0].split(" ", 1)[0] if lines else ""
        if ts >= cut:
            n += 1
            last = next((l.strip() for l in reversed(lines) if l.strip()), None)
    return n, last


def _user_version(f: str) -> int:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def _v0_strays(root: str, max_dirs: int = 4000) -> list[str]:
    """Leftover 0.x stores below the root (cheap, bounded walk)."""
    out, seen = [], 0
    for d, dirs, _ in os.walk(root):
        seen += 1
        if seen > max_dirs:
            break
        if d != root and os.path.basename(d) == paths.STORE_DIR:
            dirs[:] = []
            f = os.path.join(d, paths.DB_NAME)
            if os.path.isfile(f) and _user_version(f) == 0:  # v1 nested stores (e.g. zeus topics) are fine
                out.append(os.path.relpath(os.path.dirname(d), root))
            continue
        dirs[:] = [x for x in dirs if x not in DEFAULT_IGNORE_DIRS or x == paths.STORE_DIR]
        if d == root:
            dirs[:] = [x for x in dirs if x != paths.STORE_DIR]
    return sorted(out)


# ------------------------------------------------------------------ collect
def collect(s: db.Store, deep: bool = False) -> dict:
    cfg = config.load(s.root)
    one = lambda sql, a=(): s.one(sql, a)[0]  # noqa: E731
    count = lambda kind, st: one("SELECT COUNT(*) FROM items WHERE kind=? AND status=?", (kind, st))  # noqa: E731
    mem = {
        "pins": count("pin", "active"),
        "critical_pins": one("SELECT COUNT(*) FROM items WHERE kind='pin' AND status='active' AND priority>0"),
        "decisions": count("decision", "accepted"), "rejected": count("decision", "rejected"),
        "tasks_open": count("task", "open"), "errors_open": count("error", "open"), "notes": count("note", "active"),
        "closed_or_superseded": one("SELECT COUNT(*) FROM items WHERE kind!='handoff' AND NOT " + live_clause()),
    }
    h = s.one("SELECT id, created_at, source FROM items WHERE kind='handoff' AND status='active' "
              "ORDER BY (source='auto'), created_at DESC, id DESC LIMIT 1")
    mem["handoff"] = {"id": fmt_id("handoff", h["id"]), "at": h["created_at"], "source": h["source"]} if h else None

    budget = int(cfg.get("budget_chars") or pack.DEFAULT_CHARS)
    text = pack.build(s, budget_chars=budget)
    live_ids = {fmt_id(r["kind"], r["id"]) for r in s.q("SELECT id, kind FROM items WHERE kind!='handoff' AND "
                                                         + live_clause())}
    in_pack = set(re.findall(r"\b[PDTENH]\d+\b", text)) & live_ids
    full = len(set(re.findall(r"(?m)^- ([PDTENH]\d+) ", text)) & live_ids)
    nxt = {"chars": len(text), "budget": min(budget, pack.MAX_CHARS), "live_items": len(live_ids),
           "reachable": len(in_pack), "in_full": full}

    stale = [fmt_id("task", r["id"]) for r in s.q(
        "SELECT id FROM items WHERE kind='task' AND status='open' AND updated_at < ? ORDER BY updated_at",
        (_since(STALE_DAYS),))]
    old_err = [fmt_id("error", r["id"]) for r in s.q(
        "SELECT id FROM items WHERE kind='error' AND status='open' AND updated_at < ?", (_since(WEEK),))]
    week = _since(WEEK)
    added = {r["kind"] + ("_rejected" if r["status"] == "rejected" else ""): r["n"] for r in s.q(
        "SELECT kind, status, COUNT(*) n FROM items WHERE created_at >= ? AND kind!='handoff' GROUP BY kind, "
        "status='rejected'", (week,))}
    act = {
        "added": added,
        "closed": one("SELECT COUNT(*) FROM items WHERE closed_at >= ? AND kind!='handoff'", (week,)),
        "turns": one("SELECT COUNT(*) FROM turns WHERE created_at >= ?", (week,)),
        "sessions": one("SELECT COUNT(*) FROM sessions WHERE last_seen_at >= ?", (week,)),
        "last_prompt": s.one("SELECT MAX(created_at) FROM turns WHERE role='user'")[0],
    }
    silent = one("SELECT COUNT(*) FROM sessions se WHERE se.last_seen_at >= ? AND se.prompts > 0 AND NOT EXISTS "
                 "(SELECT 1 FROM turns t WHERE t.session_id = se.id AND t.role='user')", (week,))
    errs, last_err = _hook_errors(s.dir)
    hooks = {"wired": _hooks_wired(), "errors_24h": errs, "last_error": last_err,
             "silent_sessions": silent if cfg.get("capture") in ("all", "prompts") else 0,
             "last_capture": s.one("SELECT MAX(created_at) FROM turns")[0]}
    code = {"files": one("SELECT COUNT(*) FROM files"),
            "symbols": one("SELECT COUNT(*) FROM nodes WHERE deleted_at IS NULL AND kind NOT IN ('file','dir','package')"),
            "edges": one("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL"),
            "last_sync": s.get_meta("last_sync"), "last_reconcile": s.get_meta("last_reconcile"),
            "resolve_pending": s.get_meta("resolve_dirty") == "1"}
    size = sum(os.path.getsize(s.path + x) for x in ("", "-wal") if os.path.exists(s.path + x))
    strays = _v0_strays(s.root)

    att = []  # (level, message, fix)
    if config.is_off(s.root):
        att.append(("warn", "agres is OFF here: nothing is captured or injected", "agres on"))
    if mem["pins"] and not mem["critical_pins"]:
        att.append(("warn", "no critical pins (none guaranteed in full each session)",
                    "agres pin --critical \"…\" --replaces P<id>"))
    if stale:
        att.append(("warn", f"{_pl(len(stale), 'stale task')} (>{STALE_DAYS}d): "
                    + ", ".join(stale[:6]) + (" …" if len(stale) > 6 else ""), f"agres done|drop {stale[0]}"))
    if old_err:
        att.append(("warn", f"{_pl(len(old_err), 'open error')} older than {WEEK}d: " + ", ".join(old_err[:6]),
                    f"agres done {old_err[0]}"))
    if errs:
        att.append(("err", f"{_pl(errs, 'hook error')} in 24h: {last_err}", "agres status --deep"))
    if hooks["silent_sessions"]:
        att.append(("err", f"{_pl(silent, 'recent session')}: prompts seen but none captured", "agres doctor"))
    if hooks["wired"] is False:
        att.append(("warn", "Claude Code hooks are not wired: no injection or capture", "agres install --dry-run"))
    if strays:
        att.append(("warn", f"{_pl(len(strays), 'old 0.x store')} below the root: " + ", ".join(strays[:4]),
                    "agres migrate"))
    if nxt["live_items"] > nxt["reachable"] and nxt["chars"] >= nxt["budget"] * 0.95:
        att.append(("info", f"pack full: {nxt['live_items'] - nxt['reachable']} items only via search",
                    "close/retire old items, or agres config budget_chars 8000"))

    data = {"version": __version__, "project": {"root": s.root, "name": os.path.basename(s.root),
                                                "off": config.is_off(s.root)},
            "memory": mem, "next_session": nxt, "attention": [{"level": l, "msg": m, "fix": f} for l, m, f in att],
            "activity": act, "hooks": hooks, "code": code,
            "store": {"path": s.path, "size_bytes": size, "fts5": s.fts, "schema": s.one("PRAGMA user_version")[0],
                      "v0_strays": strays},
            "config": cfg, "origami": _origami(s, cfg)}
    if deep:
        log = os.path.join(s.dir, "hook.log")
        tail = []
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                tail = fh.read().splitlines()[-15:]
        data["hook_log_tail"] = tail
    return data


# ------------------------------------------------------------------- render
def render(s: db.Store, deep: bool = False, as_json: bool = False, color: Optional[bool] = None) -> str:
    d = collect(s, deep)
    if as_json:
        return json.dumps(d, indent=2, sort_keys=True)
    on = use_color(color)

    def c(code: str, t: str) -> str:
        return f"\x1b[{code}m{t}\x1b[0m" if on else t

    dim, bold = (lambda t: c("2", t)), (lambda t: c("1", t))
    mark = {"warn": c("33", "⚠"), "err": c("31", "✗"), "info": c("36", "·"), "ok": c("32", "✓")}
    out: list[str] = []

    def row(label: str, *lines: str) -> None:
        for i, line in enumerate(lines):
            out.append((bold(label.ljust(LABEL)) if i == 0 else " " * LABEL) + line)

    p, m, n, a, h, k = (d[x] for x in ("project", "memory", "next_session", "activity", "hooks", "code"))
    state = c("33;1", "OFF") if p["off"] else c("32", "on")
    out.append(f"{bold('agres')} {d['version']} · {bold(p['name'])} · {state}  {dim(p['root'])}")

    crit = f" ({m['critical_pins']} critical)" if m["pins"] else ""
    line1 = " · ".join([_pl(m["pins"], "pin") + crit, _pl(m["decisions"], "decision"), f"{m['rejected']} rejected",
                        _pl(m["tasks_open"], "open task"), _pl(m["errors_open"], "open error"), _pl(m["notes"], "note")])
    ho = (f"handoff {m['handoff']['id']} {ago(m['handoff']['at'])}" + (" (auto)" if m["handoff"]["source"] == "auto" else "")
          if m["handoff"] else "no handoff yet")
    row("memory", line1, dim(f"{ho} · {m['closed_or_superseded']:,} closed/superseded kept for search"))

    row("next session", f"{n['chars']:,} / {n['budget']:,} chars · {n['reachable']} of {_pl(n['live_items'], 'item')} "
                        f"reachable ({n['in_full']} in full)")

    if d["attention"]:
        row("attention", *[f"{mark[x['level']]} {x['msg']}  {dim('→ ' + x['fix'])}" for x in d["attention"]])
    else:
        row("attention", f"{mark['ok']} nothing needs attention")

    added = a["added"]
    parts = [f"{v} rejected" if k == "decision_rejected" else _pl(v, k) for k, v in
             sorted(added.items(), key=lambda kv: -kv[1])]
    total = sum(added.values())
    row("activity", f"{WEEK}d: " + (f"+{total} items ({', '.join(parts)})" if total else "no new items")
        + f" · {a['closed']} closed · {_pl(a['turns'], 'turn')} in {_pl(a['sessions'], 'session')}")

    wired = {"plugin": mark["ok"] + " wired (plugin)", True: mark["ok"] + " wired", False: mark["warn"] + " not wired",
             None: dim("no Claude Code settings")}[h["wired"]]
    cap = f"last capture {ago(h['last_capture'])}" if h["last_capture"] else "nothing captured yet"
    errs = f" · {mark['err']} {h['errors_24h']} errors/24h" if h["errors_24h"] else ""
    row("hooks", f"{wired} · {cap}{errs}")

    if k["files"]:
        synced = f"synced {ago(k['last_sync'])}" if k["last_sync"] else "partial (edited files only) → agres index"
        rec = f" · shell edits reconciled {ago(k['last_reconcile'])}" if k.get("last_reconcile") else ""
        row("code", f"{k['files']:,} files · {k['symbols']:,} symbols · {k['edges']:,} edges · {synced}{rec}"
                    + (dim(" · resolve pending") if k["resolve_pending"] else ""))
    else:
        row("code", dim("not indexed (on demand: `agres map` / `agres index`)"))

    cfg = d["config"]
    o = d["origami"]
    kinds = ", ".join(f"{v} {k}" for k, v in sorted(o["by_kind"].items(), key=lambda kv: (kv[0] != "tool", kv[0])))
    row("origami", f"fold {'on' if o['on'] else 'off'} (Bash output > {o['fold_chars']:,} chars) · "
                   + (f"{_pl(o['folds'], 'fold')} ({kinds})" if o["folds"] else "no folds yet")
                   + (f" · {o['tool_chars_folded'] / 1024:,.0f} KB kept out of context" if o["tool_chars_folded"] else ""))
    days = int(cfg.get("turn_retention_days") or 0)
    extra = len(cfg.get("redact_patterns") or [])
    row("privacy", f"capture {cfg['capture']} · turns kept {f'{days}d' if days else 'forever'} · "
                   "redaction is best effort" + (f" (+{_pl(extra, 'custom pattern')})" if extra else ""),
        dim("delete: agres forget M<id> | --session SID · agres gc --turns-older-than N --dry-run"))

    st = d["store"]
    row("store", dim(f"{os.path.relpath(st['path'], p['root'])} · {st['size_bytes'] / 1_048_576:.1f} MB · "
                     f"{'fts5' if st['fts5'] else 'LIKE search'} · budget {cfg['budget_chars']} · "
                     f"guard {'on' if cfg['guard'] else 'off'} · schema {st['schema']}"))
    if deep:
        row("hook.log", *([dim(x) for x in d["hook_log_tail"]] or [dim("empty")]))
    return "\n".join(out)
