"""Resolve phase: turn stored per-file facts into the global edge set.

Cheap (dict lookups over parsed facts) but global, so it runs lazily: after a
sync, or on the next query once `touch` has marked the graph dirty.
"""
from __future__ import annotations

import os
import posixpath
from typing import Any, Optional

from . import langs
from .langs import STDLIB, Facts
from .store import Store, unpack
from ..util import now_iso

STRUCT_KINDS = {"class", "function", "struct", "interface", "enum", "type", "trait", "object", "mixin",
                "record", "protocol", "module", "table"}
MANAGED_EDGES = ("contains", "imports", "depends", "calls", "inherits", "references")
JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".json", ".mts", ".cts")
NODE_BUILTINS = {"fs", "path", "os", "http", "https", "url", "util", "crypto", "stream", "events", "child_process",
                 "assert", "buffer", "net", "zlib", "readline", "tty", "dns", "cluster", "module", "process",
                 "querystring", "string_decoder", "timers", "vm", "worker_threads", "perf_hooks", "fs/promises"}
SWIFT_STD = {"Foundation", "UIKit", "SwiftUI", "Combine", "Swift", "CoreData", "CoreGraphics", "XCTest", "AppKit"}


# --------------------------------------------------------- phase 2: resolve
def resolve(s: Store) -> dict:
    """Rebuild the desired edge set from stored facts and diff it against the active edges."""
    rows = s.q("SELECT path, node_id, lang, is_test, facts FROM files")
    facts: dict[str, Facts] = {}
    meta: dict[str, Any] = {}
    for r in rows:
        blob = unpack(r["facts"])
        if blob is None:
            continue
        facts[r["path"]] = Facts.from_json(blob)
        meta[r["path"]] = r
    paths = sorted(facts)
    pset = set(paths)
    file_id = {p: meta[p]["node_id"] for p in paths}
    # symbol tables
    sym_rows = s.q("SELECT id, path, name, qualname, kind FROM nodes WHERE deleted_at IS NULL "
                   "AND kind NOT IN ('file','dir','package')")
    sym_id: dict[tuple[str, str], int] = {}
    top_syms: dict[str, list[str]] = {}
    by_name: dict[str, list[tuple[int, str, str, str]]] = {}
    for r in sym_rows:
        if not r["path"] or r["path"] not in pset:
            continue
        sym_id[(r["path"], r["qualname"])] = r["id"]
        if "." not in r["qualname"]:
            top_syms.setdefault(r["path"], []).append(r["qualname"])
        by_name.setdefault(r["name"], []).append((r["id"], r["path"], r["kind"], r["qualname"]))
    # path indexes
    suffix_idx: dict[str, list[str]] = {}
    pymods: dict[str, list[str]] = {}
    dirs: set[str] = set()
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
        stem = posixpath.splitext(p)[0]
        sp = stem.split("/")
        for i in range(len(sp)):
            suffix_idx.setdefault("/".join(sp[i:]), []).append(p)
        if p.endswith((".py", ".pyi")):
            mp = sp[:-1] if sp[-1] == "__init__" else sp
            for i in range(len(mp)):
                pymods.setdefault(".".join(mp[i:]), []).append(p)
    go_mod = ""
    dart_pkg = ""
    for p in paths:
        fx = facts[p].extra
        if p.endswith("go.mod") and fx.get("name") and (not go_mod or p == "go.mod"):
            go_mod = fx["name"]
        if p.endswith("pubspec.yaml") and fx.get("name") and (not dart_pkg or p == "pubspec.yaml"):
            dart_pkg = fx["name"]
    dir_files: dict[str, list[str]] = {}
    for p in paths:
        dir_files.setdefault(posixpath.dirname(p), []).append(p)

    desired: dict[tuple[int, int, str], tuple[float, Optional[dict]]] = {}
    node_cache: dict[str, int] = {}

    def put(src: int, dst: int, kind: str, w: float = 1.0) -> None:
        if src == dst:
            return
        k = (src, dst, kind)
        if k not in desired or desired[k][0] < w:
            desired[k] = (w, None)

    def dir_node(d: str) -> int:
        key = f"d:{d or '.'}"
        if key not in node_cache:
            node_cache[key] = s.upsert_node(
                key, "dir", posixpath.basename(d) or os.path.basename(s.root) or ".", qualname=d or ".",
                path=d or ".", attrs={"root": d == ""})
            s.index_text(node_cache[key], d or os.path.basename(s.root))
        return node_cache[key]

    def pkg_node(name: str) -> int:
        key = f"x:{name}"
        if key not in node_cache:
            node_cache[key] = s.upsert_node(key, "package", name, qualname=name)
            s.index_text(node_cache[key], name, "package dependency external")
        return node_cache[key]

    def near(cands: list[str], frm: str) -> Optional[str]:
        if not cands:
            return None
        fd = frm.split("/")
        best, bs = None, -1
        for c in cands:
            if c == frm:
                continue
            cd = c.split("/")
            n = 0
            for a, b in zip(fd[:-1], cd[:-1]):
                if a != b:
                    break
                n += 1
            if n > bs:
                best, bs = c, n
        return best

    def probe(base: str, exts: tuple) -> Optional[str]:
        if base in pset:
            return base
        for e in exts:
            if base + e in pset:
                return base + e
        for e in exts:
            if f"{base}/index{e}" in pset:
                return f"{base}/index{e}"
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            for e in (".ts", ".tsx", ".mts", ".cts"):
                if stem + e in pset:
                    return stem + e
        return None

    def resolve_import(frm: str, imp: langs.Imp) -> tuple[str, Any]:
        """-> ('file', path) | ('dir', dirpath) | ('pkg', name) | ('none', None)"""
        raw, L = imp.raw, imp.lang
        fdir = posixpath.dirname(frm)
        if L == "dep":
            return "pkg", raw
        if L == "python":
            if imp.level:
                base = fdir
                for _ in range(imp.level - 1):
                    base = posixpath.dirname(base)
                tgt = posixpath.join(base, raw.replace(".", "/")) if raw else base
                for c in (tgt + ".py", tgt + "/__init__.py", tgt + ".pyi"):
                    if c in pset:
                        return "file", c
                return "none", None
            parts = raw.split(".")
            for n in range(len(parts), 0, -1):
                c = near(pymods.get(".".join(parts[:n]), []), frm)
                if c:
                    return "file", c
            top = parts[0]
            if top and top not in STDLIB:
                return "pkg", top
            return "none", None
        if L == "js":
            if raw.startswith("."):
                r = probe(posixpath.normpath(posixpath.join(fdir, raw)), JS_EXT)
                return ("file", r) if r else ("none", None)
            for pre in ("@/", "~/", "#/"):
                if raw.startswith(pre):
                    for b in ("src/", "app/", ""):
                        r = probe(posixpath.normpath(b + raw[len(pre):]), JS_EXT)
                        if r:
                            return "file", r
                    return "none", None
            if raw.startswith("/"):
                return "none", None
            if "/" in raw and not raw.startswith("@"):
                for b in ("", "src/"):
                    r = probe(posixpath.normpath(b + raw), JS_EXT)
                    if r:
                        return "file", r
            name = raw.split("/")[0] if not raw.startswith("@") else "/".join(raw.split("/")[:2])
            if name.startswith("node:") or name in NODE_BUILTINS:
                return "none", None
            return "pkg", name
        if L == "go":
            if go_mod and (raw == go_mod or raw.startswith(go_mod + "/")):
                d = raw[len(go_mod):].lstrip("/")
                return ("dir", d) if d in dirs or d in dir_files else ("none", None)
            if "." in raw.split("/")[0]:
                return "pkg", "/".join(raw.split("/")[:3])
            return "none", None
        if L == "rust-mod":
            stem = posixpath.splitext(posixpath.basename(frm))[0]
            bases = [fdir] if stem in ("mod", "lib", "main") else [posixpath.join(fdir, stem), fdir]
            for b in bases:
                for c in (posixpath.join(b, raw + ".rs"), posixpath.join(b, raw, "mod.rs")):
                    if c in pset:
                        return "file", c
            return "none", None
        if L == "rust":
            segs = [x for x in raw.split("::") if x]
            if not segs:
                return "none", None
            if segs[0] in ("std", "core", "alloc"):
                return "none", None
            if segs[0] in ("crate", "self", "super"):
                segs = segs[1:]
                for n in range(len(segs), 0, -1):
                    key = "/".join(segs[:n])
                    c = near(suffix_idx.get(key, []) + suffix_idx.get(key + "/mod", []), frm)
                    if c:
                        return "file", c
                return "none", None
            return "pkg", segs[0]
        if L in ("java", "csharp"):
            parts = raw.replace(".", "/").split("/")
            for n in range(len(parts), 0, -1):
                c = near([x for x in suffix_idx.get("/".join(parts[:n]), [])
                          if x.endswith((".java", ".kt", ".scala", ".cs"))], frm)
                if c:
                    return "file", c
            if raw.startswith(("java.", "javax.", "kotlin.", "scala.", "System", "Microsoft")):
                return "none", None
            return "pkg", ".".join(raw.split(".")[:2])
        if L == "c":
            for c in (posixpath.normpath(posixpath.join(fdir, raw)), posixpath.normpath(raw)):
                if c in pset:
                    return "file", c
            c = near([p for p in suffix_idx.get(posixpath.splitext(raw)[0], []) if p.endswith(posixpath.splitext(raw)[1] or "")], frm)
            return ("file", c) if c else ("none", None)
        if L in ("ruby-rel", "path"):
            for c in (posixpath.normpath(posixpath.join(fdir, raw)), posixpath.normpath(raw)):
                for suf in ("", ".rb", ".php", ".md", ".sh"):
                    if c + suf in pset:
                        return "file", c + suf
            return "none", None
        if L == "ruby":
            c = near(suffix_idx.get(raw, []), frm)
            return ("file", c) if c else ("pkg", raw.split("/")[0])
        if L == "php":
            c = near(suffix_idx.get(raw.replace("\\", "/"), []) or suffix_idx.get(raw.split("\\")[-1], []), frm)
            return ("file", c) if c else ("none", None)
        if L == "dart":
            if raw.startswith("dart:"):
                return "none", None
            if raw.startswith("package:"):
                pk, _, rest = raw[8:].partition("/")
                if dart_pkg and pk == dart_pkg:
                    c = "lib/" + rest
                    return ("file", c) if c in pset else ("none", None)
                return "pkg", pk
            c = posixpath.normpath(posixpath.join(fdir, raw))
            return ("file", c) if c in pset else ("none", None)
        if L == "swift":
            return ("none", None) if raw in SWIFT_STD else ("pkg", raw)
        return "none", None

    # ---- per-file work
    file_alias: dict[str, dict[str, tuple[str, str]]] = {}   # path -> alias -> (target_path, orig|'*mod')
    file_imports: dict[str, set[str]] = {}
    for p in paths:
        fx, fid = facts[p], file_id[p]
        w_file = 0.35 if meta[p]["is_test"] else 1.0
        d = posixpath.dirname(p)
        put(dir_node(d), fid, "contains")
        alias: dict[str, tuple[str, str]] = {}
        imps: set[str] = set()
        for imp in fx.imports:
            kind, tgt = resolve_import(p, imp)
            if kind == "file":
                put(fid, file_id[tgt], "imports", w_file)
                imps.add(tgt)
                for orig, al in imp.names:
                    if orig == "*mod":
                        alias[al] = (tgt, "*mod")
                    elif imp.lang == "python":
                        sub = None
                        if not imp.level:
                            sub = near([c for c in pymods.get(f"{imp.raw}.{orig}", [])], p)
                        else:
                            base = posixpath.dirname(tgt) if tgt.endswith("__init__.py") else posixpath.dirname(tgt)
                            for c in (f"{base}/{orig}.py", f"{base}/{orig}/__init__.py"):
                                if c in pset:
                                    sub = c
                                    break
                        if sub:
                            put(fid, file_id[sub], "imports", w_file)
                            imps.add(sub)
                            alias[al] = (sub, "*mod")
                        else:
                            alias[al] = (tgt, orig)
                    elif orig == "*ns":
                        alias[al] = (tgt, "*mod")
                    else:
                        alias[al] = (tgt, orig)
            elif kind == "dir":
                for t in dir_files.get(tgt, [])[:40]:
                    if not t.endswith("_test.go"):
                        put(fid, file_id[t], "imports", 0.5 * w_file)
                        imps.add(t)
            elif kind == "pkg":
                put(fid, pkg_node(tgt), "depends" if imp.lang == "dep" else "imports", w_file)
        file_alias[p] = alias
        file_imports[p] = imps

    def class_chain(path: str, cq: str, depth: int = 0) -> list[tuple[str, str]]:
        """[(path, classqual)] for the class and its resolvable ancestors."""
        out = [(path, cq)]
        if depth >= 3:
            return out
        for b in facts[path].bases.get(cq, []):
            tgt = lookup_type(path, b)
            if tgt:
                out += class_chain(tgt[0], tgt[1], depth + 1)
        return out

    def lookup_type(path: str, raw: str) -> Optional[tuple[str, str]]:
        """Resolve a type name used in `path` to (path, qualname)."""
        name = raw.split(".")[-1]
        head = raw.split(".")[0]
        if (path, raw) in sym_id:
            return path, raw
        al = file_alias.get(path, {}).get(head)
        if al:
            tp, orig = al
            if orig == "*mod":
                if (tp, name) in sym_id:
                    return tp, name
            elif (tp, orig) in sym_id:
                return tp, orig
        cands = [c for c in by_name.get(name, []) if c[2] in ("class", "interface", "struct", "trait", "object",
                                                              "record", "protocol", "enum", "mixin")]
        imp_c = [c for c in cands if c[1] in file_imports.get(path, ())]
        pick = imp_c or cands
        if len(pick) == 1 or (imp_c and len(imp_c) <= 2):
            return pick[0][1], pick[0][3]
        return None

    def lookup_call(path: str, owner_q: str, raw: str) -> list[tuple[int, float]]:
        parts = raw.split(".")
        last = parts[-1]
        out: list[tuple[int, float]] = []
        alias = file_alias.get(path, {})
        owner_cls = owner_q.rsplit(".", 1)[0] if "." in owner_q else None
        if parts[0] in ("self", "cls", "this", "super") and len(parts) == 2 and owner_cls:
            for cp, cq in class_chain(path, owner_cls):
                if (cp, f"{cq}.{last}") in sym_id:
                    return [(sym_id[(cp, f"{cq}.{last}")], 1.0)]
        if len(parts) == 1:
            if (path, raw) in sym_id:
                return [(sym_id[(path, raw)], 1.0)]
            if owner_cls:
                for cp, cq in class_chain(path, owner_cls):
                    if (cp, f"{cq}.{raw}") in sym_id:
                        return [(sym_id[(cp, f"{cq}.{raw}")], 1.0)]
            al = alias.get(raw)
            if al and al[1] != "*mod":
                if (al[0], al[1]) in sym_id:
                    return [(sym_id[(al[0], al[1])], 1.0)]
                if al[1] == "default":
                    tops = top_syms.get(al[0], [])
                    named = [t for t in tops if t == raw]
                    tops = named or tops
                    if len(tops) == 1:
                        return [(sym_id[(al[0], tops[0])], 1.0)]
                return []
            if al:
                return []
            cands = [c for c in by_name.get(raw, []) if c[2] in STRUCT_KINDS]
        else:
            head = parts[0]
            rest = ".".join(parts[1:])
            al = alias.get(head)
            if al and al[1] == "*mod":
                for q in (rest, last):
                    if (al[0], q) in sym_id:
                        return [(sym_id[(al[0], q)], 1.0)]
                return []
            if al and al[1] != "*mod":
                if (al[0], f"{al[1]}.{rest}") in sym_id:
                    return [(sym_id[(al[0], f"{al[1]}.{rest}")], 1.0)]
            if (path, raw) in sym_id:
                return [(sym_id[(path, raw)], 1.0)]
            if head not in ("self", "cls", "this", "super") and len(parts) == 2:
                t = lookup_type(path, head)
                if t and (t[0], f"{t[1]}.{last}") in sym_id:
                    return [(sym_id[(t[0], f"{t[1]}.{last}")], 1.0)]
            cands = [c for c in by_name.get(last, []) if c[2] in ("method", "function")]
        if not cands:
            return out
        imp_set = file_imports.get(path, set())
        near_c = [c for c in cands if c[1] in imp_set or c[1] == path]
        pick = near_c or cands
        if len(pick) > 3:
            return out
        wt = (0.9 if near_c else 0.6) / len(pick)
        return [(c[0], wt) for c in pick]

    for p in paths:
        fx, fid = facts[p], file_id[p]
        tw = 0.35 if meta[p]["is_test"] else 1.0
        local_names = {sy.name for sy in fx.symbols}
        for sy in fx.symbols:
            sid = sym_id.get((p, sy.qualname))
            if sid is None:
                continue
            parent_id = sym_id.get((p, sy.parent)) if sy.parent else None
            put(parent_id if parent_id else fid, sid, "contains")
        for owner_q, raws in fx.calls.items():
            src = sym_id.get((p, owner_q)) if owner_q else fid
            if src is None:
                src = fid
            for raw in raws:
                for tid, w in lookup_call(p, owner_q, raw):
                    if tid != src:
                        put(src, tid, "calls", w * tw)
        for cq, bases in fx.bases.items():
            src = sym_id.get((p, cq))
            if src is None:
                continue
            for b in bases:
                t = lookup_type(p, b)
                if t:
                    put(src, sym_id[(t[0], t[1])], "inherits", 1.0 * tw)
        imps = file_imports.get(p, set())
        for r in fx.refs:
            if r in local_names:
                continue
            cands = [c for c in by_name.get(r, []) if c[2] in STRUCT_KINDS and c[1] != p]
            if not cands:
                continue
            imp_c = [c for c in cands if c[1] in imps]
            if imp_c and len(imp_c) <= 3:
                for c in imp_c:
                    put(fid, c[0], "references", 0.6 * tw / len(imp_c))
            elif len(cands) == 1 and len(r) > 4:
                put(fid, cands[0][0], "references", 0.3 * tw)
    # directory hierarchy
    for d in sorted(dirs):
        put(dir_node(posixpath.dirname(d)), dir_node(d), "contains")
    dir_node("")
    # ---- diff against active edges
    return _apply_diff(s, desired)

def _apply_diff(s: Store, desired: dict) -> dict:
    ph = ",".join("?" * len(MANAGED_EDGES))
    existing = {(r["src"], r["dst"], r["kind"]): (r["id"], r["weight"])
                for r in s.q(f"SELECT id, src, dst, kind, weight FROM edges WHERE valid_to IS NULL AND kind IN ({ph})",
                             MANAGED_EDGES)}
    ts = now_iso()
    to_close = [(ts, eid) for k, (eid, _w) in existing.items() if k not in desired]
    to_add = [(k[0], k[1], k[2], v[0], ts, ts) for k, v in desired.items() if k not in existing]
    to_upd = [(v[0], existing[k][0]) for k, v in desired.items()
              if k in existing and abs((existing[k][1] or 0) - v[0]) > 1e-6]
    with s.tx():
        s.many("UPDATE edges SET valid_to=? WHERE id=?", to_close)
        s.many("INSERT INTO edges(src,dst,kind,weight,valid_from,recorded_at) VALUES(?,?,?,?,?,?)", to_add)
        s.many("UPDATE edges SET weight=? WHERE id=?", to_upd)
        # retire orphaned packages / empty dirs
        s.x("UPDATE nodes SET deleted_at=? WHERE kind='package' AND deleted_at IS NULL AND id NOT IN "
            "(SELECT dst FROM edges WHERE valid_to IS NULL)", (ts,))
        s.x("UPDATE nodes SET deleted_at=? WHERE kind='dir' AND deleted_at IS NULL AND id NOT IN "
            "(SELECT dst FROM edges WHERE valid_to IS NULL AND kind='contains')", (ts,))
    return {"edges_added": len(to_add), "edges_closed": len(to_close), "edges_total": len(desired)}
