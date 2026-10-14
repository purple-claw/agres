"""Brain: the orchestrator that keeps the graph in sync with reality."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from . import gitwalk, rank
from .indexer import Indexer, sync_lock
from .store import Store, dumps
from ..util import now_iso


def find_root(start: str | None = None) -> str:
    """Project root: git toplevel, else nearest ancestor holding .agres/brain.db, else cwd."""
    start = os.path.abspath(start or os.getcwd())
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".agres", "brain.db")):
            return cur
        nxt = os.path.dirname(cur)
        if nxt == cur:
            break
        cur = nxt
    try:
        out = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"], capture_output=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.decode().strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return start


class Brain:
    def __init__(self, root: str | None = None):
        self.root = find_root(root)
        self.store = Store(self.root)

    @property
    def s(self) -> Store:
        return self.store

    # ------------------------------------------------------------------ sync
    def sync(self, full: bool = False, use_git: bool = True, force_resolve: bool = False) -> dict:
        t0 = time.time()
        s = self.s
        with sync_lock(s) as ok:
            if not ok:
                return {"skipped": "another sync is running"}
            idx = Indexer(s)
            st = idx.scan(full=full)
            changed = bool(st["added"] or st["modified"] or st["deleted"])
            first = s.get_meta("resolved") != "1"
            res = {}
            if changed or first or full or force_resolve:
                res = idx.resolve()
                s.set_meta("resolved", "1")
            g = {}
            if use_git:
                g = gitwalk.ingest(s)
            if changed or first or full or g.get("commits") or force_resolve:
                rank.compute_ranks(s)
                rank.compute_communities(s)
            info = gitwalk.head_info(self.root) if use_git else {"head": "", "branch": ""}
            dirty = len(gitwalk.dirty_files(self.root)) if use_git else 0
            counts = self.counts()
            with s.tx():
                cur = s.x("INSERT INTO scans(ts,head,branch,dirty,files,symbols,edges,loc,added,modified,deleted) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (now_iso(), info["head"], info["branch"], dirty, counts["files"], counts["symbols"],
                           counts["edges"], counts["loc"], st["added"], st["modified"], st["deleted"]))
                sid = cur.lastrowid
                ts = now_iso()
                s.many("INSERT INTO changes(scan_id,ts,kind,path,detail) VALUES(?,?,?,?,?)",
                       [(sid, ts, k, p, dumps(d)) for k, p, d in getattr(idx, "_journal", [])])
                # bounded growth: keep scan history compact
                s.x("DELETE FROM scans WHERE id < (SELECT MAX(id) FROM scans) - 400 AND added=0 AND modified=0 AND deleted=0")
                s.x("DELETE FROM changes WHERE scan_id NOT IN (SELECT id FROM scans)")
            s.set_meta("last_sync", now_iso())
            if changed:
                s.set_meta("last_change", now_iso())
            return {**st, **res, "git": g, "counts": counts, "seconds": round(time.time() - t0, 2), "scan_id": sid,
                    "changed": changed}

    def counts(self) -> dict:
        s = self.s
        files = s.one("SELECT COUNT(*) c, COALESCE(SUM(loc),0) l FROM files")
        syms = s.one("SELECT COUNT(*) c FROM nodes WHERE deleted_at IS NULL AND kind NOT IN "
                     "('file','dir','package','decision','task','error','pin','note','journal','goal')")
        edges = s.one("SELECT COUNT(*) c FROM edges WHERE valid_to IS NULL")
        return {"files": files["c"], "loc": files["l"], "symbols": syms["c"], "edges": edges["c"]}

    def is_stale(self) -> bool:
        """Cheap check: does disk differ from the last indexed state?"""
        from .indexer import discover
        db = {r["path"]: (r["size"], r["mtime_ns"]) for r in self.s.q("SELECT path,size,mtime_ns FROM files")}
        cur = {p: (sz, mt) for p, sz, mt in discover(self.root)}
        return db != cur
