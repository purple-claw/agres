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
from typing import Any, Optional

from . import langs, resolve as _resolve
from .discover import discover, read_text as _read_text, sync_lock  # noqa: F401  (re-exported)
from .langs import Facts, parse_source
from .store import Store, dumps, pack
from ..util import is_test_path, now_iso, sha1_bytes


# ------------------------------------------------------------------- indexer


class Indexer:
    def __init__(self, store: Store):
        self.s = store
        self.root = store.root
        self.stats: dict[str, Any] = {}

    # ---------------------------------------------------------- phase 1: parse
    def scan(self, full: bool = False) -> dict:
        s = self.s
        found = discover(self.root)
        db_files = {r["path"]: r for r in s.q("SELECT path, hash, size, mtime_ns, node_id FROM files")}
        found_set = {p for p, _, _ in found}
        added, modified, deleted = [], [], []
        todo: list[tuple[str, int, int]] = []
        for rel, size, mtime in found:
            old = db_files.get(rel)
            if old is None:
                todo.append((rel, size, mtime))
            elif full or old["size"] != size or old["mtime_ns"] != mtime:
                todo.append((rel, size, mtime))
        for rel in db_files:
            if rel not in found_set:
                deleted.append(rel)
        journal: list[tuple[str, str, dict]] = []
        parsed = 0
        parsers: dict[str, int] = {}
        with s.tx():
            for rel, size, mtime in todo:
                data, text = _read_text(os.path.join(self.root, rel))
                if data is None or text is None:
                    if rel in db_files:
                        deleted.append(rel)
                    continue
                h = sha1_bytes(data)
                old = db_files.get(rel)
                if old is not None and old["hash"] == h and not full:
                    s.x("UPDATE files SET size=?, mtime_ns=? WHERE path=?", (size, mtime, rel))
                    continue
                facts, err = parse_source(rel, text)
                lang = langs.detect_lang(rel)
                if facts is None:
                    facts = Facts(lang=lang or "text", loc=text.count("\n") + 1)
                parsers[facts.extra.get("parser", "-")] = parsers.get(facts.extra.get("parser", "-"), 0) + 1
                detail = self._persist_file(rel, h, size, mtime, lang or "text", facts, err, old)
                parsed += 1
                if old is None:
                    added.append(rel)
                    journal.append(("add", rel, detail))
                else:
                    modified.append(rel)
                    journal.append(("modify", rel, detail))
            for rel in deleted:
                self._delete_file(rel)
                journal.append(("delete", rel, {}))
        self.stats.update(added=len(added), modified=len(modified), deleted=len(set(deleted)), parsed=parsed,
                          files=len(found_set) - len(set(deleted) & found_set), parsers=parsers)
        self._journal = journal
        return self.stats

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
