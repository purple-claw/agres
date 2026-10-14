"""`agres status`: what is stored, how fresh the code index is, which settings apply."""
from __future__ import annotations

import json
import os

from . import __version__, config, db
from .util import ago


def collect(s: db.Store, deep: bool = False) -> dict:
    items: dict[str, dict[str, int]] = {}
    for r in s.q("SELECT kind, status, COUNT(*) n FROM items GROUP BY kind, status"):
        items.setdefault(r["kind"], {})[r["status"]] = r["n"]
    crit = s.one("SELECT COUNT(*) FROM items WHERE kind='pin' AND status='active' AND priority>0")[0]
    last = s.one("SELECT id, agent, last_seen_at, prompts, ended_at FROM sessions ORDER BY last_seen_at DESC LIMIT 1")
    size = sum(os.path.getsize(s.path + ext) for ext in ("", "-wal") if os.path.exists(s.path + ext))
    data = {
        "version": __version__, "root": s.root, "store": s.path, "size_bytes": size,
        "schema": s.one("PRAGMA user_version")[0], "fts5": s.fts, "off": config.is_off(s.root),
        "items": items, "critical_pins": crit,
        "turns": s.one("SELECT COUNT(*) FROM turns")[0],
        "sessions": s.one("SELECT COUNT(*) FROM sessions")[0],
        "last_session": dict(last) if last else None,
        "code": {
            "files": s.one("SELECT COUNT(*) FROM files")[0],
            "symbols": s.one("SELECT COUNT(*) FROM nodes WHERE deleted_at IS NULL AND kind NOT IN "
                             "('file','dir','package')")[0],
            "edges": s.one("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL")[0],
            "last_sync": s.get_meta("last_sync"),
            "resolve_pending": s.get_meta("resolve_dirty") == "1",
        },
        "config": config.load(s.root),
    }
    if deep:
        log = os.path.join(s.dir, "hook.log")
        tail = []
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                tail = fh.read().splitlines()[-12:]
        data["hook_log_tail"] = tail
    return data


def _n(items: dict, kind: str, status: str) -> int:
    return items.get(kind, {}).get(status, 0)


def render(s: db.Store, deep: bool = False, as_json: bool = False) -> str:
    d = collect(s, deep)
    if as_json:
        return json.dumps(d, indent=2, sort_keys=True)
    it = d["items"]
    mb = d["size_bytes"] / 1_048_576
    lines = [
        f"agres {d['version']} · {d['root']}",
        f"store: {os.path.relpath(d['store'], d['root'])} · {mb:.1f} MB · schema {d['schema']} · "
        f"{'fts5' if d['fts5'] else 'no fts5 (LIKE search)'} · {'OFF' if d['off'] else 'on'}",
        "memory: " + " · ".join([
            f"{_n(it, 'pin', 'active')} pins ({d['critical_pins']} critical)",
            f"{_n(it, 'decision', 'accepted')} decisions + {_n(it, 'decision', 'rejected')} rejected",
            f"{_n(it, 'task', 'open')} open tasks ({_n(it, 'task', 'done')} done)",
            f"{_n(it, 'error', 'open')} open errors",
            f"{_n(it, 'note', 'active')} notes",
            f"{sum(it.get('handoff', {}).values())} handoffs",
        ]),
    ]
    ls = d["last_session"]
    conv = f"conversation: {d['turns']} turns across {d['sessions']} sessions"
    if ls:
        conv += f"; last {ago(ls['last_seen_at'])} ({ls['agent'] or '?'}, {ls['prompts']} prompts)"
    lines.append(conv)
    c = d["code"]
    if c["files"]:
        lines.append(f"code index: {c['files']} files · {c['symbols']} symbols · {c['edges']} edges · "
                     f"synced {ago(c['last_sync'])}" + (" · resolve pending" if c["resolve_pending"] else ""))
    else:
        lines.append("code index: empty (`agres index` builds it; `agres map` does so on first use)")
    cfg = d["config"]
    lines.append(f"settings: budget {cfg['budget_chars']} chars · guard {'on' if cfg['guard'] else 'off'} · "
                 f"capture {cfg['capture']} · auto_init {'on' if cfg['auto_init'] else 'off'}")
    if deep:
        lines.append("hook.log (tail):" if d["hook_log_tail"] else "hook.log: empty")
        lines += ["  " + x for x in d["hook_log_tail"]]
    return "\n".join(lines)
