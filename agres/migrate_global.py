"""Routing the old global `~/.agres` store: suggest a project per item, import the confirmed ones, archive the rest."""
from __future__ import annotations

import datetime as _dt
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
_PATHISH = re.compile(r"[\w./\-]+\.[A-Za-z0-9]{1,6}\b|[\w\-]+/[\w./\-]+|[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+")
MIN_SCORE, MIN_MARGIN, WEAK_ONLY_MIN = 2.0, 1.5, 5.0  # plain-word overlap alone must reach WEAK_ONLY_MIN
WINDOW = _dt.timedelta(hours=12)  # junk-by-session and neighbour smoothing both look this far around an item


def _name_terms(name: str) -> set:
    return terms(re.sub(r"[^A-Za-z0-9_]+", " ", os.path.splitext(name)[0]))


def _signature(root: str, cap: int = 40000) -> dict:
    """What a project looks like: how often each term occurs in its dir/file names, its paths, its name."""
    sig = {"root": root, "terms": {}, "paths": set(), "by_name": {}, "name_terms": _name_terms(os.path.basename(root))}
    count = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in DEFAULT_IGNORE_DIRS and not os.path.islink(os.path.join(dp, d))]
        rel = "" if dp == root else os.path.relpath(dp, root).replace(os.sep, "/").lower()
        for name in dns + fns:
            low = name.lower()
            p = f"{rel}/{low}" if rel else low
            sig["paths"].add(p)
            for key in {low, os.path.splitext(low)[0]}:
                sig["by_name"].setdefault(key, []).append(p)
            for t in _name_terms(name):
                sig["terms"][t] = sig["terms"].get(t, 0) + 1
        count += len(fns)
        if count > cap:
            break
    return sig


def _score(text: str, sig: dict, df: dict) -> tuple:
    """(score, strong, why). Strong evidence = project path/name or a path/file name unique to this project;
    plain words that merely occur in file names are weak (a big project's vocabulary matches by chance)."""
    why, score, strong = [], 0.0, 0
    it, low = terms(text), text.lower()
    if sig["root"].lower() in low:
        score, strong = score + 6, strong + 1
        why.append("mentions project path")
    names = {t for t in it & sig["name_terms"] if t not in _GENERIC}
    if names:
        score, strong = score + 3 * len(names), strong + 1
        why.append("project name " + ", ".join(sorted(names)))
    weak = sorted(t for t in it & set(sig["terms"]) if t not in _GENERIC and t not in names)
    for t in weak:
        n = sig["terms"][t]
        score += (0.4 if n == 1 else 0.7 if n == 2 else 1.0) / df[t]
    if weak:
        why.append("terms " + ", ".join(weak[:6]))
    for tok in sorted({m.strip(".,;:()\"'`").lower() for m in _PATHISH.findall(text)}):
        if os.path.splitext(tok)[0] in _GENERIC:
            continue
        base = tok.rsplit("/", 1)[-1]
        hit = any(c == tok or c.endswith("/" + tok) for c in sig["by_name"].get(base, ())) if "/" in tok \
            else tok in sig["by_name"]
        if hit:
            score += (4 if tok in sig["paths"] else 3) / df[base]
            strong += df[base] == 1
            why.append("path " + tok)
    return score, strong, why


def _junk(text: str, sid: Optional[str], when: _dt.datetime, sessions: dict) -> Optional[str]:
    if any(p.search(text) for p in _JUNK_TEXT):
        return "text looks like a test fixture"
    goal, started = sessions.get(sid or "", (None, None))
    # a "test" session's pointer lingered for weeks in the old global store: only items made during it count
    if goal and _JUNK_GOAL.search(goal) and abs(when - started) <= WINDOW:
        return f"recorded in a test/simulation session ({goal[:40]!r})"
    return None


def global_routes(global_agres_dir: str, project_roots: list) -> list:
    """Suggest a target project for each pin/decision/task/error in the old global v0 store. Writes nothing.

    Confident = the best project has strong evidence (or a lot of weak overlap) and clearly beats the next.
    Unconfident items adopt the project of their neighbours in time when those agree (global items were
    written in per-project bursts). `accept` is the default for apply_routes: confident and not junk."""
    db_file = os.path.join(os.path.realpath(global_agres_dir), paths.DB_NAME)
    if kind_of(db_file) != "v0":
        raise MigrateError(f"{db_file} is not a v0 store")
    ld = load(db_file, "global")
    sessions = {s["id"]: (s["title"], s["started"]) for s in ld["sessions"]}
    sigs = [_signature(os.path.realpath(r)) for r in project_roots]
    df: dict = {}
    for s in sigs:
        for key in set(s["terms"]) | s["name_terms"] | set(s["by_name"]):
            df[key] = df.get(key, 0) + 1
    items = []
    for r in sorted((r for r in ld["records"] if r["kind"] != "handoff"),
                    key=lambda r: (r["when"], r["order"], r["rowid"])):
        scored = sorted(((*_score(r["text"], s, df), s["root"]) for s in sigs), key=lambda t: t[0], reverse=True)
        junk = _junk(r["text"], r["session_id"], r["when"], sessions)
        best = scored[0] if scored else (0.0, 0, [], None)
        second = scored[1][0] if len(scored) > 1 else 0.0
        sure = (best[3] is not None and best[0] >= MIN_SCORE and best[0] >= MIN_MARGIN * second
                and (best[1] >= 1 or best[0] >= WEAK_ONLY_MIN))
        items.append({"rec": r, "scored": scored, "junk": junk, "root": best[3] if sure else None,
                      "score": best[0], "reason": "; ".join(best[2]) if sure else
                      f"no confident match (best {os.path.basename(best[3] or '-')} {best[0]:.1f})"})
    anchors = [i for i, x in enumerate(items) if x["root"] and not x["junk"]]
    for i, x in enumerate(items):
        if x["root"] or x["junk"] or not x["scored"]:
            continue
        near = {items[j]["root"] for j in (max((a for a in anchors if a < i), default=None),
                                           min((a for a in anchors if a > i), default=None))
                if j is not None and abs(items[j]["rec"]["when"] - x["rec"]["when"]) <= WINDOW}
        own = {s[3]: s[0] for s in x["scored"]}
        if len(near) == 1:
            root = near.pop()
            if own[root] >= 1.0 and own[root] >= 0.5 * x["scored"][0][0]:
                x["root"], x["score"] = root, own[root]
                x["reason"] = f"adjacent in time to items routed to {os.path.basename(root)}; own overlap {own[root]:.1f}"
    return [{
        "kind": x["rec"]["kind"], "status": x["rec"]["status"], "text": x["rec"]["text"],
        "created_at": iso(x["rec"]["when"]), "updated_at": iso(x["rec"]["updated"]), "session_id": x["rec"]["session_id"],
        "suggested_root": x["root"], "score": round(x["score"], 2), "reason": x["reason"],
        "junk": bool(x["junk"]), "junk_reason": x["junk"], "scores": {s[3]: round(s[0], 2) for s in x["scored"]},
        "accept": bool(x["root"] and not x["junk"])} for x in items]


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

