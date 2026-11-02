"""Code engine facade: the only module other agres phases should import from agres.code.

    sync(store, full=False, use_git=True) -> dict     full incremental index + resolve + ranks + git
    touch(store, paths) -> int                         re-parse only these files; resolve is deferred
    ensure_resolved(store) -> bool                     run the deferred global resolve if needed
    repo_map(store, query=None, focus_files=(), tokens=1024) -> str
    search_code(store, query, limit=10) -> list[dict]
    counts(store) -> dict                              files / loc / symbols / edges
"""
from __future__ import annotations

import os
import subprocess
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


RECONCILE_MAX = 200


def reconcile(store: Store, timeout: float = 2.0) -> int:
    """Catch edits no hook reported (shell `sed -i`, generators, `git mv`): re-touch every path git sees
    as modified, added, deleted, renamed or untracked. Unchanged files cost a stat (touch compares
    size/mtime/hash). No-op without a code index or without git. Returns files re-parsed or removed."""
    if not store.one("SELECT 1 FROM files LIMIT 1"):
        return 0
    try:
        top = subprocess.run(["git", "-C", store.root, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=timeout)
        # paths in `git status` are relative to the repo top, which may be above the store root (monorepo)
        out = subprocess.run(["git", "-C", store.root, "status", "--porcelain=v1", "-z", "-unormal", "--", "."],
                             capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 0
    if out.returncode != 0 or top.returncode != 0:
        return 0
    base = top.stdout.strip() or store.root
    paths: list[str] = []
    fields = out.stdout.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        paths.append(f[3:])
        if f[0] in "RC" or f[1] in "RC":  # rename/copy: next field is the old path
            if i < len(fields) and fields[i]:
                paths.append(fields[i])
            i += 1
    full = [os.path.join(base, p) for p in paths]
    full = [p for p in full if os.path.realpath(p).startswith(os.path.realpath(store.root) + os.sep)]
    expanded: list[str] = []
    for p in full:  # -unormal lists an untracked dir once ("new/"): walk it, bounded
        if os.path.isdir(p):
            for d, _, fs in os.walk(p):
                expanded += [os.path.join(d, f) for f in fs]
                if len(expanded) > RECONCILE_MAX:
                    break
        else:
            expanded.append(p)
    n = touch(store, expanded[:RECONCILE_MAX])
    store.set_meta("last_reconcile", now_iso())
    return n


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
