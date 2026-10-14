"""v0 -> v1 migration of a project store.

A v0 store (`.agres/agres.db`, user_version 0) is converted in place: every v0 artifact moves
to `.agres/v0/` (nothing is deleted), a fresh v1 `agres.db` is created and the memory tables
are imported with the typed mapping of spec R12 (see `v0.load`). Stray nested v0 stores under the
root are merged (items tagged `origin=<relpath>`) and archived to `.agres/v0/nested/<relpath>/`.
The old global `~/.agres` goes through `migrate_global` (re-exported here).

Read-only entry points (`find_v0_stores`, `plan`) never write, not even a -wal/-shm next to a
store. `apply` validates and parses everything before it moves a file and undoes its moves if the
import fails; `rollback` reverses a finished migration.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Optional

from . import memory, paths
from .db import Store
from .migrate_global import apply_routes, archive_global, global_routes  # noqa: F401  (public API)
from .util import DEFAULT_IGNORE_DIRS, now_iso
from .v0 import (MANIFEST, SOURCE, MigrateError, check_root, checkpoint, iso, key_of, kind_of, load, move, open_ro,
                 read_manifest, write_manifest)

V0_DIR = "v0"
ROLLBACK_PREFIX = "v1-rolled-back-"


def _walk_v0(start: str, within_project: bool = False) -> list:
    """v0 store roots at/under `start`. Heavy dirs are pruned and symlinks never followed; with
    `within_project` a subdirectory that is its own git checkout is a separate project, not a stray."""
    start = os.path.realpath(start)
    found = []
    for dp, dns, _ in os.walk(start, followlinks=False):
        dns[:] = sorted(d for d in dns if d not in DEFAULT_IGNORE_DIRS and not os.path.islink(os.path.join(dp, d)))
        if within_project and dp != start and os.path.exists(os.path.join(dp, ".git")):
            dns[:] = []
            continue
        store = os.path.join(dp, paths.STORE_DIR)
        if not os.path.islink(store) and kind_of(os.path.join(store, paths.DB_NAME)) == "v0":
            found.append(dp)
    return found


def find_v0_stores(start: str) -> list:
    """Roots of every v0 store at or under `start`, sorted."""
    return sorted(_walk_v0(start))


def _nested(root: str) -> list:
    return sorted(os.path.relpath(p, root) for p in _walk_v0(root, within_project=True) if p != root)




def _tally(records: list, loaded_stores: list) -> dict:
    n = lambda kind, status=None: sum(1 for r in records if r["kind"] == kind and (status is None or r["status"] == status))
    return {
        "sessions": sum(len(s["sessions"]) for s in loaded_stores), "pins": n("pin"),
        "decisions_accepted": n("decision", "accepted"), "decisions_rejected": n("decision", "rejected"),
        "notes": n("note"), "tasks_open": n("task", "open"), "tasks_done": n("task", "done"),
        "tasks_dropped": n("task", "dropped"), "errors_open": n("error", "open"), "errors_fixed": n("error", "fixed"),
        "handoffs": n("handoff"), "handoffs_skipped": sum(s["skipped_handoffs"] for s in loaded_stores),
        "turns": sum(len(s["turns"]) for s in loaded_stores), "items": len(records)}


def _assemble(loaded: list, seen: set) -> dict:
    """Merge stores (root first). Nested items that repeat an item already imported are dropped."""
    records, dups = [], 0
    for ld in loaded:
        for r in ld["records"]:
            key = key_of(r["kind"], r["text"])
            if r["kind"] != "handoff" and ld["origin"] is not None and key in seen:
                dups += 1
                continue
            seen.add(key)
            records.append(r)
    records.sort(key=lambda r: (r["when"], r["order"], r["rowid"]))
    sessions, ids = [], set()
    for ld in loaded:
        for s in ld["sessions"]:
            if s["id"] not in ids:
                ids.add(s["id"])
                sessions.append(s)
    turns = sorted((t for ld in loaded for t in ld["turns"]), key=lambda t: (t["when"], t["rowid"]))
    prompts: dict = {}
    for t in turns:
        if t["role"] == "user":
            prompts[t["session_id"]] = prompts.get(t["session_id"], 0) + 1
    for s in sessions:
        s["prompts"] = prompts.get(s["id"], 0)
    totals = _tally(records, [{"sessions": sessions, "turns": turns,
                               "skipped_handoffs": sum(ld["skipped_handoffs"] for ld in loaded)}])
    totals["duplicates_skipped"] = dups
    totals["empty_skipped"] = sum(ld["empty_skipped"] for ld in loaded)
    return {"records": records, "sessions": sessions, "turns": turns, "totals": totals}


def _existing_keys(db_file: str) -> set:
    with open_ro(db_file) as c:
        return {key_of(r["kind"], r["text"]) for r in c.execute("SELECT kind, text FROM items")}


# --------------------------------------------------------------------- plan
def _archive_pairs(root: str, state: str, nested: list) -> list:
    sdir = paths.store_dir(root)
    pairs = []
    if state == "v0":
        for name in _root_entries(sdir):
            pairs.append({"from": f"{paths.STORE_DIR}/{name}", "to": f"{paths.STORE_DIR}/{V0_DIR}/{name}"})
    for rel in nested:
        pairs.append({"from": f"{rel}/{paths.STORE_DIR}", "to": f"{paths.STORE_DIR}/{V0_DIR}/nested/{rel}"})
    return pairs


def _gather(root: str, state: str, nested: list) -> tuple:
    todo = ([("", paths.db_path(root))] if state == "v0" else []) + \
           [(rel, paths.db_path(os.path.join(root, rel))) for rel in nested]
    loaded = [load(f, rel or None) for rel, f in todo]
    seen = _existing_keys(paths.db_path(root)) if state == "v1" else set()
    return loaded, _assemble(loaded, seen)


def plan(root: str) -> dict:
    """Dry-run report for the store at `root` plus nested v0 stores under it. Writes nothing."""
    root = check_root(root)
    state = kind_of(paths.db_path(root))
    nested = _nested(root)
    loaded, asm = _gather(root, state, nested) if state in ("v0", "v1", "none") else ([], None)
    stores = [{"relpath": ld["origin"] or "", "db": ld["db"], "origin": ld["origin"],
               "size": os.path.getsize(ld["db"]), "tables": ld["tables"],
               "mapping": _tally(ld["records"], [ld])} for ld in loaded]
    rep = {"root": root, "state": state, "stores": stores, "nested": nested,
           "totals": asm["totals"] if asm else {}, "archive": _archive_pairs(root, state, nested),
           "review": {"tasks_done_by_text": [t[:100] for ld in loaded for t in ld["done_by_text"]]},
           "actions": [], "warnings": []}
    if state == "v0":
        rep["actions"].append("checkpoint WAL, move .agres/* to .agres/v0/, create v1 agres.db, import")
    if nested:
        rep["actions"].append(f"merge {len(nested)} nested store(s) with origin=<relpath>, archive them under .agres/v0/nested/")
    if state == "v1" and not nested:
        rep["warnings"].append("already a v1 store; nothing to migrate")
    if state in ("newer", "unknown", "bad"):
        rep["warnings"].append(f"store at root is {state}; migrate refuses it")
    return rep


# -------------------------------------------------------------------- apply
def _root_entries(sdir: str) -> list:
    if not os.path.isdir(sdir):
        return []
    keep = {V0_DIR, "off", "hook.log"}  # v1 runtime files (off switch, hook log) stay where they are
    return [n for n in sorted(os.listdir(sdir)) if n not in keep and not n.startswith(ROLLBACK_PREFIX)]


def _move_in(root: str, state: str, nested: list, journal: list) -> dict:
    """Archive the v0 artifacts. Every rename is appended to `journal` so a failure can undo it."""
    sdir, v0dir = paths.store_dir(root), os.path.join(paths.store_dir(root), V0_DIR)
    prev = read_manifest(v0dir)
    manifest = prev or {"version": 1, "created_at": now_iso(), "root_state": state, "root": [], "nested": []}
    names = _root_entries(sdir) if state == "v0" else []
    manifest["root"] = list(manifest["root"]) + names
    for rel in sorted(nested, key=lambda r: (r.count("/"), r)):  # shallow first: a parent store's dir may hold a child
        dest, n = os.path.join(v0dir, "nested", rel), 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(v0dir, "nested", f"{rel}.{n}")
        manifest["nested"].append({"relpath": rel, "archived": os.path.relpath(dest, v0dir)})
        journal.append((os.path.join(root, rel, paths.STORE_DIR), dest))
    for name in names:
        journal.insert(0, (os.path.join(sdir, name), os.path.join(v0dir, name)))
    write_manifest(v0dir, manifest)
    for src, dst in journal:
        move(src, dst)
    return manifest


def _import(store: Store, asm: dict) -> dict:
    inserted = {"items": 0, "turns": 0, "sessions": 0}
    with store.tx():
        for s in asm["sessions"]:
            inserted["sessions"] += memory.add_session(
                store, s["id"], agent=s["agent"], title=s["title"], cwd=s["cwd"], started_at=iso(s["started"]),
                last_seen_at=iso(s["ended"]), ended_at=iso(s["ended"]), end_reason="migrated", prompts=s["prompts"])
        cur = store.one("SELECT id, created_at FROM items WHERE kind='handoff' AND status='active' "
                        "ORDER BY created_at DESC, id DESC LIMIT 1")
        prev, prev_ts = (cur["id"], cur["created_at"]) if cur else (None, "")
        for r in asm["records"]:
            ts, upd = iso(r["when"]), iso(r["updated"])
            kw = dict(session_id=r["session_id"], source=SOURCE, origin=r["origin"], created_at=ts)
            if r["kind"] == "handoff":  # newest stays active; each older one is superseded by the next
                if ts < prev_ts:
                    memory.add_item(store, "handoff", r["text"], status="superseded", **kw)
                else:
                    prev, prev_ts = memory.add_item(store, "handoff", r["text"], replaces=prev, **kw), ts
            else:
                memory.add_item(store, r["kind"], r["text"], status=r["status"], updated_at=upd, **kw)
            inserted["items"] += 1
        for t in asm["turns"]:
            inserted["turns"] += memory.add_turn(store, t["session_id"], t["role"], t["text"],
                                                 created_at=iso(t["when"])) is not None
        store.set_meta("migrated_from", "v0")
        store.set_meta("migrated_at", now_iso())
        store.set_meta("migrated_totals", json.dumps(asm["totals"], sort_keys=True))
    return inserted


def apply(root: str) -> dict:
    """Migrate the store at `root` (and any nested v0 stores) to v1 in place."""
    t0 = time.time()
    root = check_root(root)
    state = kind_of(paths.db_path(root))
    nested = _nested(root)
    if state in ("newer", "unknown", "bad"):
        raise MigrateError(f"{paths.db_path(root)} is not a v0 or v1 agres store ({state}); refusing to touch it")
    if state == "v1" and not nested:
        return {"status": "already-v1", "root": root, "totals": {}, "nested": [], "seconds": 0.0}
    if state == "none" and not nested:
        raise MigrateError(f"no v0 store at {root}")
    sdir, v0dir = paths.store_dir(root), os.path.join(paths.store_dir(root), V0_DIR)
    if state == "v0" and os.path.isdir(v0dir) and os.listdir(v0dir):
        raise MigrateError(f"{v0dir} already exists; roll back or remove it first")
    todo = ([paths.db_path(root)] if state == "v0" else []) + [paths.db_path(os.path.join(root, r)) for r in nested]
    for f in todo:
        checkpoint(f)
    loaded, asm = _gather(root, state, nested)  # parse everything before moving a single file
    pre = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    old_manifest = read_manifest(v0dir)
    journal: list = []
    store = None
    try:
        manifest = _move_in(root, state, nested, journal)
        store = Store(root, create=True)
        inserted = _import(store, asm)
        store.close()
    except BaseException as exc:
        if store:
            store.close()
        _undo(root, journal, state != "v1", pre, old_manifest)
        if isinstance(exc, Exception) and not isinstance(exc, MigrateError):
            raise MigrateError(f"migration failed and was undone: {exc}") from exc
        raise
    return {"status": "migrated", "root": root, "totals": asm["totals"], "imported": inserted, "nested": nested,
            "archived": manifest, "v0_dir": v0dir, "seconds": round(time.time() - t0, 2)}


def _undo(root: str, journal: list, created_v1: bool, pre: set, old_manifest: Optional[dict]) -> None:
    sdir, v0dir = paths.store_dir(root), os.path.join(paths.store_dir(root), V0_DIR)
    if created_v1:  # a same-named file is the new v1 one only if the v0 original already moved (or never existed)
        for name in ("agres.db", "agres.db-wal", "agres.db-shm", ".gitignore"):
            if os.path.exists(os.path.join(sdir, name)) and (name not in pre or os.path.exists(os.path.join(v0dir, name))):
                os.remove(os.path.join(sdir, name))
    for src, dst in reversed(journal):
        if os.path.exists(dst) and not os.path.exists(src):
            move(dst, src)
    if old_manifest is not None:
        write_manifest(v0dir, old_manifest)
    else:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(v0dir, MANIFEST))
    _prune_empty(v0dir)


def _prune_empty(top: str) -> None:
    for dp, _, _ in os.walk(top, topdown=False):
        with contextlib.suppress(OSError):
            os.rmdir(dp)


# ----------------------------------------------------------------- rollback
def _synth_manifest(v0dir: str) -> dict:
    nested_dir = os.path.join(v0dir, "nested")
    found = []
    for dp, dns, fns in os.walk(nested_dir):
        if paths.DB_NAME in fns:
            rel = os.path.relpath(dp, nested_dir)
            found.append({"relpath": rel, "archived": os.path.join("nested", rel)})
    found.sort(key=lambda e: (e["relpath"].count("/"), e["relpath"]))
    root = [n for n in sorted(os.listdir(v0dir)) if n not in ("nested", MANIFEST)]
    return {"version": 1, "root_state": "v0", "root": root, "nested": found}


def rollback(root: str) -> dict:
    """Undo a migration: v1 db aside to `.agres/v1-rolled-back-<ts>/`, v0 files back where they were."""
    root = check_root(root)
    sdir, v0dir = paths.store_dir(root), os.path.join(paths.store_dir(root), V0_DIR)
    if not os.path.isdir(v0dir) or not os.listdir(v0dir):
        raise MigrateError(f"nothing to roll back: {v0dir} does not exist")
    manifest = read_manifest(v0dir) or _synth_manifest(v0dir)
    aside = None
    if manifest.get("root_state") != "v1":  # the v1 store was created by the migration
        current = _root_entries(sdir)
        if current:
            with contextlib.suppress(MigrateError):
                checkpoint(paths.db_path(root))
            aside = os.path.join(sdir, ROLLBACK_PREFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            n = 1
            while os.path.exists(aside):
                n += 1
                aside = f"{aside.rsplit('.', 1)[0]}.{n}"
            for name in current:
                move(os.path.join(sdir, name), os.path.join(aside, name))
    restored = []
    for name in manifest["root"]:
        if os.path.exists(os.path.join(v0dir, name)):
            move(os.path.join(v0dir, name), os.path.join(sdir, name))
            restored.append(name)
    nested_back = []
    for e in reversed(manifest["nested"]):  # deepest first
        src, dst = os.path.join(v0dir, e["archived"]), os.path.join(root, e["relpath"], paths.STORE_DIR)
        if os.path.exists(src):
            if os.path.exists(dst):  # a new stray store appeared meanwhile; keep it, do not clobber
                move(dst, os.path.join(aside or sdir, "conflicts", e["relpath"]))
            move(src, dst)
            nested_back.append(e["relpath"])
    with contextlib.suppress(OSError):
        os.remove(os.path.join(v0dir, MANIFEST))
    _prune_empty(v0dir)
    res = {"status": "rolled-back", "root": root, "moved_aside": aside, "restored": restored,
           "nested_restored": nested_back}
    if manifest.get("root_state") == "v1":
        res["note"] = "v1 store left in place; nested items already merged into it stay (re-merging skips duplicates)"
    return res


# ---------------------------------------------------------------------- cli
def _summary(rep: dict) -> str:
    t = rep.get("totals") or {}
    parts = [f"{k}={v}" for k, v in sorted(t.items()) if v]
    return ", ".join(parts) or "nothing to import"


def _project_roots(start: str) -> list:
    """Repo roots of every agres store (v0 or v1) under `start` — candidates for global routing."""
    roots = set()
    for d, dirs, _ in os.walk(start):
        dirs[:] = [x for x in dirs if x not in DEFAULT_IGNORE_DIRS or x == paths.STORE_DIR]
        if os.path.basename(d) == paths.STORE_DIR:
            dirs[:] = []
            root = paths.find_root(os.path.dirname(d))
            if root:
                roots.add(root)
    return sorted(roots)


def cli(a) -> int:
    home = paths.home()
    try:
        if a.global_ or a.routes:
            gdir = os.path.join(home, ".agres")
            out = os.path.join(gdir, "routes.json")
            if a.routes:
                with open(a.routes, encoding="utf-8") as fh:
                    routes = json.load(fh)
                res = apply_routes(routes)
                print(f"imported {res['imported']}, skipped {res['skipped']}, duplicates {res['duplicates']}")
                for root, n in sorted(res["by_root"].items()):
                    print(f"  {n:4}  {root}")
                if not a.dry_run:
                    archive_global(gdir)
                    print(f"old global store archived under {gdir}/archive-v0/")
                return 1 if res["errors"] else 0
            routes = global_routes(gdir, _project_roots(a.target or home))
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(routes, fh, indent=1)
            for r in routes:
                where = "JUNK" if r["junk"] else (r.get("suggested_root") or "?")
                print(f"{'✓' if r.get('accept') else ' '} {r['kind']:8} {where[-40:]:40} {r['text'][:70]}")
            print(f"\n{len(routes)} items; proposal written to {out}. Edit accept/target_root there, then "
                  f"`agres migrate --routes {out}`.")
            return 0
        here = paths.git_root(os.getcwd()) or paths.find_root(os.getcwd())  # strays sit below the git root
        if a.rollback:
            root = a.target or here
            res = rollback(root)
            print(f"rolled back {res['root']}; v1 store moved to {res.get('moved_aside') or '(kept)'}")
            return 0
        roots = ([r for r in find_v0_stores(a.target or home)] if a.all
                 else [a.target or here])
        if a.all:  # nested strays go with their enclosing project; the global ~/.agres has its own path
            skipped = [r for r in roots if paths.is_forbidden_root(r)]
            roots = sorted({paths.git_root(r) or r for r in roots if not paths.is_forbidden_root(r)})
            roots = [r for r in roots if not any(r != o and r.startswith(o + os.sep) for o in roots)]
            if skipped:
                print(f"(skipping {', '.join(skipped)}: the old global store — use `agres migrate --global`)")
        if not roots or roots == [None]:
            print("no agres project here")
            return 1
        for root in roots:
            if a.dry_run:
                rep = plan(root)
                print(f"{root} [{rep['state']}]: {_summary(rep)}"
                      + (f"; nested: {', '.join(rep['nested'])}" if rep["nested"] else ""))
                for w in rep["warnings"]:
                    print(f"  ! {w}")
            else:
                res = apply(root)
                print(f"{root}: {res['status']} ({_summary(res)}) in {res['seconds']}s")
        return 0
    except MigrateError as e:
        print(f"agres migrate: {e}")
        return 1
