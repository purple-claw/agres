"""Routing the old global `~/.agres` store: suggest a project per item, import the confirmed ones, archive the rest."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Optional

from . import memory, paths
from .db import Store, StoreError
from .util import DEFAULT_IGNORE_DIRS, now_iso, terms
from .v0 import (SOURCE, MigrateError, check_root, checkpoint, iso, key_of, kind_of, load, move, read_manifest,
                 write_manifest)

GLOBAL_ARCHIVE = "archive-v0"
GLOBAL_KEEP = {GLOBAL_ARCHIVE, "runtime"}  # `runtime/` = the old runtime copy the old shim still runs

_GENERIC = frozenset("""src lib test tests docs doc index main app data config readme utils util build scripts script
assets package json models model core common types type api server client web html css node modules setup init
new old tmp temp misc""".split())
_JUNK_TEXT = [re.compile(p, re.I) for p in (
    r"%_", r"\btest[\s_-]*\d{3,}\b", r"\bdeepseek\b.{0,30}\b(?:decision|pin|task)\b",
    r"\bcritical (?:decision|pin|task|error) [A-Z0-9]$", r"^\s*(?:dummy|lorem ipsum|asdf|foobar|foo bar)\b")]
_JUNK_GOAL = re.compile(r"^\s*(?:test|testing)\b|\bsimulat|\b(?:sim|dummy|fixture|synthetic|mock)\b|"
                        r"\b\d+k\b.*\b(?:realtime|engine|sim)\b", re.I)
_PATHISH = re.compile(r"[\w./\-]+\.[A-Za-z0-9]{1,6}\b|[\w\-]+/[\w./\-]+")
MIN_ROUTE_SCORE, MIN_ROUTE_MARGIN = 2.0, 1.5


def _name_terms(name: str) -> set:
    return terms(re.sub(r"[^A-Za-z0-9_]+", " ", os.path.splitext(name)[0]))


def _signature(root: str, cap: int = 40000) -> dict:
    """What a project looks like: terms from its dir/file names, its relative paths, its name."""
    sig = {"root": root, "terms": set(), "paths": set(), "by_base": {}, "name_terms": _name_terms(os.path.basename(root))}
    count = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in DEFAULT_IGNORE_DIRS and not os.path.islink(os.path.join(dp, d))]
        rel = "" if dp == root else os.path.relpath(dp, root).replace(os.sep, "/").lower()
        for name in dns + fns:
            p = f"{rel}/{name.lower()}" if rel else name.lower()
            sig["paths"].add(p)
            sig["by_base"].setdefault(name.lower(), []).append(p)
            sig["terms"] |= _name_terms(name)
        count += len(fns)
        if count > cap:
            break
    return sig


def _score(text: str, sig: dict, df: dict) -> tuple:
    why, score = [], 0.0
    it = terms(text)
    low = text.lower()
    if sig["root"].lower() in low:
        score += 6
        why.append("mentions project path")
    names = {t for t in it & sig["name_terms"] if t not in _GENERIC}
    if names:
        score += 3 * len(names)
        why.append("project name " + ", ".join(sorted(names)))
    hits = sorted(t for t in it & sig["terms"] if t not in _GENERIC and t not in names)
    if hits:
        score += sum(1.0 / df[t] for t in hits)
        why.append("terms " + ", ".join(hits[:6]))
    for tok in {m.strip(".,;:()\"'`").lower() for m in _PATHISH.findall(text)}:
        base = tok.rsplit("/", 1)[-1]
        found = [c for c in sig["by_base"].get(base, ()) if c == tok or c.endswith("/" + tok)]
        if found:
            score += 4 if tok in sig["paths"] else 3
            why.append("path " + tok)
    return score, why


def _junk(text: str, sid: Optional[str], goals: dict) -> Optional[str]:
    if any(p.search(text) for p in _JUNK_TEXT):
        return "text looks like a test fixture"
    goal = goals.get(sid or "")
    if goal and _JUNK_GOAL.search(goal):
        return f"recorded in a test/simulation session ({goal[:40]!r})"
    return None


def global_routes(global_agres_dir: str, project_roots: list) -> list:
    """Suggest a target project for each pin/decision/task/error in the old global v0 store. Writes nothing."""
    db_file = os.path.join(os.path.realpath(global_agres_dir), paths.DB_NAME)
    if kind_of(db_file) != "v0":
        raise MigrateError(f"{db_file} is not a v0 store")
    ld = load(db_file, "global")
    goals = {s["id"]: s["title"] for s in ld["sessions"]}
    sigs = [_signature(os.path.realpath(r)) for r in project_roots]
    df: dict = {}
    for s in sigs:
        for t in s["terms"] | s["name_terms"]:
            df[t] = df.get(t, 0) + 1
    routes = []
    for r in ld["records"]:
        if r["kind"] == "handoff":
            continue
        scored = sorted(((*_score(r["text"], s, df), s["root"]) for s in sigs), key=lambda t: t[0], reverse=True)
        best = scored[0] if scored else (0.0, [], None)
        second = scored[1][0] if len(scored) > 1 else 0.0
        confident = best[2] is not None and best[0] >= MIN_ROUTE_SCORE and best[0] >= MIN_ROUTE_MARGIN * second
        junk = _junk(r["text"], r["session_id"], goals)
        if confident:
            reason = "; ".join(best[1])
        else:
            reason = f"no confident match (best {os.path.basename(best[2] or '-')} {best[0]:.1f})" if scored else "no projects"
        routes.append({
            "kind": r["kind"], "status": r["status"], "text": r["text"], "created_at": iso(r["when"]),
            "updated_at": iso(r["updated"]), "session_id": r["session_id"],
            "suggested_root": best[2] if confident else None, "score": round(best[0], 2), "reason": reason,
            "junk": bool(junk), "junk_reason": junk,
            "scores": {s[2]: round(s[0], 2) for s in scored}, "accept": bool(confident and not junk)})
    return routes


def apply_routes(routes: list) -> dict:
    """Import routes whose `accept` is true into `target_root` (else `suggested_root`) with origin='global'."""
    res = {"imported": 0, "skipped": 0, "duplicates": 0, "errors": [], "by_root": {}}
    groups: dict = {}
    for r in routes:
        target = r.get("target_root") or r.get("suggested_root")
        if not r.get("accept") or not target:
            res["skipped"] += 1
        else:
            groups.setdefault(os.path.realpath(target), []).append(r)
    for root, items in groups.items():
        try:
            root = check_root(root)
            state = kind_of(paths.db_path(root))
            if state not in ("none", "v1"):
                raise MigrateError(f"{root} has a {state} store: migrate it first" if state == "v0"
                                   else f"{root} has an unusable store ({state})")
            new = dup = 0
            with Store(root, create=True) as store:
                seen = {key_of(r["kind"], r["text"]) for r in store.q("SELECT kind, text FROM items WHERE origin='global'")}
                with store.tx():
                    for r in items:
                        key = key_of(r["kind"], memory.redact(r["text"]))
                        if key in seen:
                            dup += 1
                            continue
                        seen.add(key)
                        ts = r.get("created_at") or now_iso()
                        memory.add_item(store, r["kind"], r["text"], status=r.get("status"), session_id=r.get("session_id"),
                                        source=SOURCE, origin="global", created_at=ts, updated_at=r.get("updated_at") or ts)
                        new += 1
            res["imported"] += new
            res["duplicates"] += dup
            res["by_root"][root] = new
        except (MigrateError, StoreError, ValueError, sqlite3.Error) as exc:
            res["errors"].append({"root": root, "error": str(exc), "items": len(items)})
    return res


def archive_global(global_agres_dir: str) -> dict:
    """Move the global v0 store files into `<dir>/archive-v0/` (never deletes; leaves `runtime/`)."""
    gdir = os.path.realpath(global_agres_dir)
    if not os.path.isdir(gdir):
        raise MigrateError(f"{gdir} is not a directory")
    db_file = os.path.join(gdir, paths.DB_NAME)
    if kind_of(db_file) not in ("none", "v0"):
        raise MigrateError(f"{db_file} is not a v0 store; refusing to archive it")
    arch = os.path.join(gdir, GLOBAL_ARCHIVE)
    names = [n for n in sorted(os.listdir(gdir)) if n not in GLOBAL_KEEP]
    kept = [n for n in sorted(os.listdir(gdir)) if n in GLOBAL_KEEP]
    if not names:
        return {"archive": arch, "moved": [], "kept": kept}
    if os.path.isfile(db_file):
        checkpoint(db_file)
    os.makedirs(arch, exist_ok=True)
    moved = []
    for n in names:
        dest = os.path.join(arch, n)
        if os.path.exists(dest):
            dest += time.strftime(".%Y%m%dT%H%M%SZ", time.gmtime())
        move(os.path.join(gdir, n), dest)
        moved.append(n)
    prev = read_manifest(arch) or {"version": 1, "moved": []}
    prev["moved"] = prev["moved"] + [{"name": n, "at": now_iso()} for n in moved]
    write_manifest(arch, prev)
    return {"archive": arch, "moved": moved, "kept": kept}

