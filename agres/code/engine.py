"""Code engine facade: the only module other agres phases should import from agres.code.

    sync(store, full=False, use_git=True) -> dict     full incremental index + resolve + ranks + git
    touch(store, paths) -> int                         re-parse only these files; resolve is deferred
    ensure_resolved(store) -> bool                     run the deferred global resolve if needed
    repo_map(store, query=None, focus_files=(), tokens=1024) -> str
    search_code(store, query, limit=10) -> list[dict]
    counts(store) -> dict                              files / loc / symbols / edges
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

from . import gitwalk, rank, repomap
from .discover import sync_lock
from .indexer import Indexer
from .resolve import needs_resolve
from .store import Store, dumps
from ..util import now_iso

NON_SYMBOL_KINDS = ("file", "dir", "package")


def counts(store: Store) -> dict:
    files = store.one("SELECT COUNT(*) c, COALESCE(SUM(loc),0) l FROM files")
    syms = store.one("SELECT COUNT(*) c FROM nodes WHERE deleted_at IS NULL AND kind NOT IN (?,?,?)", NON_SYMBOL_KINDS)
    edges = store.one("SELECT COUNT(*) c FROM edges WHERE valid_to IS NULL")
    return {"files": files["c"], "loc": files["l"], "symbols": syms["c"], "edges": edges["c"]}


def touch(store: Store, paths: Iterable[str]) -> int:
    """Re-parse only `paths` (absolute or root-relative); the global resolve is deferred.

    Files outside the root, ignored, secret, binary or unreadable are skipped (a missing file that was
    indexed is marked deleted). Returns how many files were re-parsed or removed; when that is > 0
    meta `resolve_dirty` becomes 1 so the next `ensure_resolved` rebuilds the edges.
    """
    return Indexer(store).touch(paths)


def ensure_resolved(store: Store) -> bool:
    """Run the deferred resolve + PageRank if the graph is stale. True if it ran."""
    if not needs_resolve(store):
        return False
    with sync_lock(store) as ok:
        if not ok or not needs_resolve(store):  # another process is resolving / already did
            return False
        Indexer(store).resolve()
        rank.compute_ranks(store)
    return True


def sync(store: Store, full: bool = False, use_git: bool = True) -> dict:
    t0 = time.time()
    with sync_lock(store) as ok:
        if not ok:
            return {"skipped": "another sync is running", "added": 0, "modified": 0, "deleted": 0,
                    "changed": False, "seconds": round(time.time() - t0, 2), "counts": counts(store)}
        idx = Indexer(store)
        st = idx.scan(full=full)
        changed = bool(st["added"] or st["modified"] or st["deleted"])
        res: dict = {}
        resolved = False
        if changed or full or needs_resolve(store):
            res = idx.resolve()
            resolved = True
        g = gitwalk.ingest(store) if use_git else {}
        if resolved or g.get("commits"):
            rank.compute_ranks(store)
            rank.compute_communities(store)
        info = gitwalk.head_info(store.root) if use_git else {"head": "", "branch": ""}
        dirty = len(gitwalk.dirty_files(store.root)) if use_git else 0
        cnt = counts(store)
        with store.tx():
            cur = store.x(
                "INSERT INTO scans(ts,head,branch,dirty,files,symbols,edges,loc,added,modified,deleted) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), info["head"], info["branch"], dirty, cnt["files"], cnt["symbols"], cnt["edges"],
                 cnt["loc"], st["added"], st["modified"], st["deleted"]))
            sid = cur.lastrowid
            ts = now_iso()
            store.many("INSERT INTO changes(scan_id,ts,kind,path,detail) VALUES(?,?,?,?,?)",
                       [(sid, ts, k, p, dumps(d)) for k, p, d in getattr(idx, "_journal", [])])
            # bounded growth: keep the scan history compact
            store.x("DELETE FROM scans WHERE id < (SELECT MAX(id) FROM scans) - 400 "
                    "AND added=0 AND modified=0 AND deleted=0")
            store.x("DELETE FROM changes WHERE scan_id NOT IN (SELECT id FROM scans)")
            store.set_meta("last_sync", now_iso())
            if changed:
                store.set_meta("last_change", now_iso())
        return {**st, **res, "git": g, "counts": cnt, "seconds": round(time.time() - t0, 2), "scan_id": sid,
                "changed": changed, "resolved": resolved}


def search_code(store: Store, query: str, limit: int = 10) -> list[dict]:
    """Best-first symbol/file hits: dicts with id, kind, name, path, line, sig, score.

    Searches the parse-time FTS index, which edges do not influence, so a pending resolve is not run.
    """
    return repomap.search(store, query, limit)


def repo_map(store: Store, query: Optional[str] = None, focus_files: Iterable[str] = (), tokens: int = 1024) -> str:
    """Aider-style skeleton: files in (personalized) PageRank order with their top signatures.

    `focus_files` (absolute or root-relative) and identifiers/terms in `query` seed the PageRank
    teleport vector. The text is fitted to about tokens * 3.5 characters. Resolves pending edges first.
    """
    ensure_resolved(store)
    idx = Indexer(store)
    focus = [r for r in (idx.rel_path(p) for p in focus_files) if r]
    return repomap.build_map(store, query, focus, tokens)
