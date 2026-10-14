"""Incremental indexer: parse phase (scan / touch) -> persist. Resolve lives in resolve.py.

Two-phase design (important for correctness *and* speed):

1. **Parse phase** is incremental: only files whose (size, mtime) changed *and*
   whose content hash changed are re-parsed. Raw per-file *facts* (imports, call
   names, base names, referenced identifiers) are stored compressed.
2. **Resolve phase** (`resolve.py`) is global but cheap (dict lookups over stored
   facts): it rebuilds the *desired* edge set and diffs it against the active
   edges in the DB. Edges that disappear are *closed* (valid_to) rather than
   deleted, edges that appear are opened. This keeps cross-file edges correct
   when a file you did not touch gains/loses a symbol, and preserves history.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Iterable, Optional

from . import langs, resolve as _resolve
from .discover import discover, file_state, indexable_name, read_text as _read_text, sync_lock  # noqa: F401
from .langs import Facts, parse_source
from .store import Store, pack
from ..util import IgnoreRules, is_test_path, now_iso, sha1_bytes


# ------------------------------------------------------------------- indexer


class Indexer:
    def __init__(self, store: Store):
        self.s = store
        self.root = store.root
        self.stats: dict[str, Any] = {}
        self._journal: list[tuple[str, str, dict]] = []

    # ---------------------------------------------------------- phase 1: parse
    def scan(self, full: bool = False) -> dict:
        s = self.s
        found = discover(self.root)
        db_files = {r["path"]: r for r in s.q("SELECT path, hash, size, mtime_ns, node_id FROM files")}
        found_set = {p for p, _, _ in found}
        added, modified, deleted = [], [], []
        todo = [(rel, size, mtime) for rel, size, mtime in found
                if rel not in db_files or full or db_files[rel]["size"] != size or db_files[rel]["mtime_ns"] != mtime]
        deleted.extend(rel for rel in db_files if rel not in found_set)
        journal: list[tuple[str, str, dict]] = []
        parsers: dict[str, int] = {}
        with s.tx():
            for rel, size, mtime in todo:
                old = db_files.get(rel)
                status, detail = self._ingest(rel, size, mtime, old, full, parsers)
                if status == "gone":
                    if old is not None:
                        deleted.append(rel)
                elif status in ("add", "modify"):
                    (added if status == "add" else modified).append(rel)
                    journal.append((status, rel, detail))
            for rel in deleted:
                self._delete_file(rel)
                journal.append(("delete", rel, {}))
        self.stats.update(added=len(added), modified=len(modified), deleted=len(set(deleted)),
                          parsed=len(added) + len(modified),
                          files=len(found_set) - len(set(deleted) & found_set), parsers=parsers)
        self._journal = journal
        return self.stats

    def _ingest(self, rel: str, size: int, mtime: int, old: Any, full: bool = False,
                parsers: Optional[dict] = None) -> tuple[str, dict]:
        """Read, hash and (if the content changed) parse + persist one file.

        Returns ('gone' | 'same' | 'add' | 'modify', journal detail).
        """
        data, text = _read_text(os.path.join(self.root, rel))
        if data is None or text is None:
            return "gone", {}
        h = sha1_bytes(data)
        if old is not None and old["hash"] == h and not full:
            self.s.x("UPDATE files SET size=?, mtime_ns=? WHERE path=?", (size, mtime, rel))
            return "same", {}
        facts, err = parse_source(rel, text)
        lang = langs.detect_lang(rel)
        if facts is None:
            facts = Facts(lang=lang or "text", loc=text.count("\n") + 1)
        if parsers is not None:
            name = facts.extra.get("parser", "-")
            parsers[name] = parsers.get(name, 0) + 1
        detail = self._persist_file(rel, h, size, mtime, lang or "text", facts, err, old)
        return ("add" if old is None else "modify"), detail

    # ------------------------------------------------------------------ touch
    def rel_path(self, p: str) -> Optional[str]:
        """Repo-relative posix path for an absolute or root-relative path; None if outside the root."""
        if not p:
            return None
        root = self.root
        ap = os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))
        if not ap.startswith(root + os.sep):
            # a symlinked parent can hide that the file is inside the root
            ap = os.path.join(os.path.realpath(os.path.dirname(ap)), os.path.basename(ap))
            if not ap.startswith(root + os.sep):
                return None
        return os.path.relpath(ap, root).replace(os.sep, "/")

    def _git_ignored(self, rels: list[str]) -> set[str]:
        if not rels:
            return set()
        try:
            r = subprocess.run(["git", "-C", self.root, "check-ignore", "-z", "--stdin"],
                               input=("\0".join(rels) + "\0").encode(), capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return set()
        if r.returncode not in (0, 1):
            return set()
        return {x.decode("utf-8", "replace") for x in r.stdout.split(b"\0") if x}

    def touch(self, paths: Iterable[str]) -> int:
        """Re-index only `paths`; never resolves. Returns how many files were (re)parsed or removed."""
        s = self.s
        cand = list(dict.fromkeys(r for r in (self.rel_path(p) for p in paths) if r))
        if not cand:
            return 0
        ign = IgnoreRules(self.root)
        known: dict[str, Any] = {}
        for i in range(0, len(cand), 400):
            chunk = cand[i:i + 400]
            for r in s.q("SELECT path, hash, size, mtime_ns, node_id FROM files WHERE path IN (%s)"
                         % ",".join("?" * len(chunk)), chunk):
                known[r["path"]] = r
        work: list[tuple[str, tuple[int, int]]] = []
        gone: list[str] = []
        for rel in cand:
            state = file_state(self.root, rel) if indexable_name(rel, ign) else None
            if state is None:
                if rel in known:  # missing, now ignored, or no longer indexable
                    gone.append(rel)
            else:
                work.append((rel, state))
        skip = self._git_ignored([rel for rel, _ in work if rel not in known])
        n = 0
        with s.tx():
            for rel, (size, mtime) in work:
                if rel in skip:
                    continue
                status, detail = self._ingest(rel, size, mtime, known.get(rel))
                if status == "gone":
                    if rel in known:
                        gone.append(rel)
                elif status != "same":
                    n += 1
            for rel in dict.fromkeys(gone):
                self._delete_file(rel)
                n += 1
            if n:
                s.set_meta("resolve_dirty", "1")
                s.set_meta("last_change", now_iso())
        return n

    def _persist_file(self, rel: str, h: str, size: int, mtime: int, lang: str, facts: Facts,
                      err: Optional[str], old: Any) -> dict:
        s = self.s
        is_test = is_test_path(rel)
        fid = s.upsert_node(
            f"f:{rel}", "file", os.path.basename(rel), qualname=rel, path=rel, lang=lang, doc=facts.doc, hash=h,
            line=1, end_line=facts.loc,
            attrs={"loc": facts.loc, "test": is_test, "entry": facts.entry, "parser": facts.extra.get("parser"),
                   "manifest": facts.extra.get("manifest"), "name": facts.extra.get("name")})
        s.index_text(fid, os.path.basename(rel), rel, facts.doc)
        # symbol diff
        prev = {r["key"]: r for r in s.q(
            "SELECT id,key,hash,sig FROM nodes WHERE path=? AND kind NOT IN ('file','dir') AND deleted_at IS NULL", (rel,))}
        cur_keys = set()
        plus, tilde = [], []
        for sym in facts.symbols:
            key = f"s:{rel}::{sym.qualname}"
            cur_keys.add(key)
            nid = s.upsert_node(
                key, sym.kind, sym.name, qualname=sym.qualname, path=rel, line=sym.line, end_line=sym.end_line,
                lang=lang, sig=sym.sig, doc=sym.doc, hash=sym.body_hash,
                attrs={"parent": sym.parent, "decorators": sym.decorators[:4] or None, "test": is_test})
            s.index_text(nid, sym.name, sym.qualname, rel, sym.sig, sym.doc)
            if key not in prev:
                plus.append(sym.qualname)
            elif prev[key]["hash"] != sym.body_hash:
                tilde.append(sym.qualname)
        gone = [r for k, r in prev.items() if k not in cur_keys]
        if gone:
            s.soft_delete_nodes([r["id"] for r in gone])
        s.x("INSERT INTO files(path,node_id,hash,size,mtime_ns,lang,loc,facts,parse_error,is_test,is_entry,scanned_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET node_id=excluded.node_id,"
            " hash=excluded.hash,size=excluded.size,mtime_ns=excluded.mtime_ns,lang=excluded.lang,loc=excluded.loc,"
            " facts=excluded.facts,parse_error=excluded.parse_error,is_test=excluded.is_test,"
            " is_entry=excluded.is_entry,scanned_at=excluded.scanned_at",
            (rel, fid, h, size, mtime, lang, facts.loc, pack(facts.to_json()), err, int(is_test),
             int(facts.entry), now_iso()))
        detail: dict[str, Any] = {"loc": facts.loc}
        if plus:
            detail["+"] = plus[:25]
        if tilde:
            detail["~"] = tilde[:25]
        if gone:
            detail["-"] = [r["key"].split("::", 1)[-1] for r in gone][:25]
        return detail

    def _delete_file(self, rel: str) -> None:
        s = self.s
        ids = [r["id"] for r in s.q("SELECT id FROM nodes WHERE path=? AND kind!='dir' AND deleted_at IS NULL", (rel,))]
        s.soft_delete_nodes(ids)
        s.x("DELETE FROM files WHERE path=?", (rel,))

    # --------------------------------------------------------- phase 2: resolve
    def resolve(self) -> dict:
        return _resolve.resolve(self.s)
