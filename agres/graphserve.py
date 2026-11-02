"""`agres graph`: a live, animated view of the code index and project memory in the browser.

A small read-only HTTP server (stdlib `http.server`) on 127.0.0.1 serves one page (`web/graph.html`)
plus three endpoints:

  GET  /api/graph?level=files|symbols&limit=N   nodes + edges as JSON
  GET  /api/events                              server-sent events: `changed` when the store moves
  POST /api/index                               build / re-sync the code index (same-origin only)

Files level rolls symbol edges (calls, imports, references, inherits) up to weighted file→file
links; symbols level shows the top-ranked functions/classes with their files. Memory items (pins,
decisions, tasks, errors, notes) and recent folds join the graph, linked to the files they name in
`--files` or in their text. Requests with a foreign Host header are refused (DNS rebinding), and the
one write (POST /api/index) needs a matching Origin.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import db
from .memory import fmt_id, live_clause, source_tag
from .util import truncate

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "graph.html")
DEFAULT_PORT = 7357
SYMBOL_KINDS = ("function", "method", "class")
LINK_KINDS = ("calls", "imports", "references", "inherits")
PATHISH = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,6}\b")


# --------------------------------------------------------------------- graph
def build_graph(s: db.Store, level: str = "files", limit: int = 400) -> dict:
    level = "symbols" if level == "symbols" else "files"
    limit = max(20, min(int(limit or 400), 2000))
    rows = s.q("SELECT id, key, kind, name, qualname, path, line, sig, rank, lang FROM nodes "
               "WHERE deleted_at IS NULL AND kind IN ('file','package','function','method','class')")
    by_id = {r["id"]: r for r in rows}
    file_of = {r["path"]: r for r in rows if r["kind"] == "file"}
    nodes: dict[str, dict] = {}

    def add_code(r) -> str:
        key = r["key"]
        if key not in nodes:
            kind = "symbol" if r["kind"] in SYMBOL_KINDS else r["kind"]
            nodes[key] = {"id": key, "kind": kind, "sub": r["kind"], "label": r["name"] if kind != "file" else r["path"],
                          "path": r["path"], "line": r["line"], "sig": r["sig"], "lang": r["lang"],
                          "rank": round(r["rank"] or 0, 6), "group": (r["path"] or "").rsplit("/", 1)[0] or "."}
        return key

    files = sorted((r for r in rows if r["kind"] == "file"), key=lambda r: -(r["rank"] or 0))
    for r in files[:limit]:
        add_code(r)
    weights: dict[tuple, dict] = {}
    edges_q = s.q("SELECT src, dst, kind FROM edges WHERE valid_to IS NULL AND kind IN (%s)"
                  % ",".join("?" * len(LINK_KINDS)), LINK_KINDS)

    if level == "files":
        for e in edges_q:
            a, b = by_id.get(e["src"]), by_id.get(e["dst"])
            if not a or not b:
                continue
            fa = file_of.get(a["path"]) if a["kind"] != "package" else a
            fb = file_of.get(b["path"]) if b["kind"] != "package" else b
            if not fa or not fb or fa["key"] == fb["key"]:
                continue
            if fa["key"] not in nodes:
                continue
            if fb["kind"] == "package":
                add_code(fb)
            if fb["key"] not in nodes:
                continue
            k = (fa["key"], fb["key"], "imports" if e["kind"] == "imports" else "calls")
            weights.setdefault(k, {"source": k[0], "target": k[1], "kind": k[2], "weight": 0})["weight"] += 1
    else:
        syms = sorted((r for r in rows if r["kind"] in SYMBOL_KINDS), key=lambda r: -(r["rank"] or 0))[:limit]
        keep = {r["id"] for r in syms}
        for r in syms:
            add_code(r)
            f = file_of.get(r["path"])
            if f:
                add_code(f)
                k = (f["key"], r["key"], "defines")
                weights[k] = {"source": k[0], "target": k[1], "kind": "defines", "weight": 1}
        for e in edges_q:
            if e["src"] in keep and e["dst"] in keep and e["src"] != e["dst"]:
                a, b = by_id[e["src"]], by_id[e["dst"]]
                k = (a["key"], b["key"], e["kind"] if e["kind"] in ("calls", "inherits") else "calls")
                weights.setdefault(k, {"source": k[0], "target": k[1], "kind": k[2], "weight": 0})["weight"] += 1

    # drop isolated code nodes beyond the top 40 so the picture is a network, not a cloud of dots
    linked = {e["source"] for e in weights.values()} | {e["target"] for e in weights.values()}
    keep_iso = {k for k, _ in sorted(((k, n["rank"]) for k, n in nodes.items()), key=lambda t: -t[1])[:40]}
    nodes = {k: n for k, n in nodes.items() if k in linked or k in keep_iso}
    edges = [e for e in weights.values() if e["source"] in nodes and e["target"] in nodes]

    mem_nodes, mem_edges = _memory(s, nodes)
    out_nodes = list(nodes.values()) + mem_nodes
    return {"level": level, "nodes": out_nodes, "edges": edges + mem_edges, "stats": _stats(s),
            "project": os.path.basename(s.root), "indexed": bool(files), "fingerprint": fingerprint(s)}


def _memory(s: db.Store, code_nodes: dict) -> tuple[list, list]:
    paths = {n["path"]: k for k, n in code_nodes.items() if n["kind"] == "file" and n["path"]}
    base: dict[str, list] = {}
    for p, k in paths.items():
        base.setdefault(os.path.basename(p), []).append(k)

    def targets(text: str, files_json: Optional[str]) -> list[str]:
        found: list[str] = []
        cands = (json.loads(files_json) if files_json else []) + PATHISH.findall(text or "")
        for c in cands:
            c = c.strip("./") if c.startswith("./") else c
            k = paths.get(c) or (base[os.path.basename(c)][0] if len(base.get(os.path.basename(c), [])) == 1 else None)
            if k and k not in found:
                found.append(k)
        return found[:8]

    nodes, edges = [], []
    for r in s.q("SELECT * FROM items WHERE kind!='handoff' AND " + live_clause() + " ORDER BY updated_at DESC LIMIT 200"):
        nid = fmt_id(r["kind"], r["id"])
        kind = "rejected" if r["kind"] == "decision" and r["status"] == "rejected" else r["kind"]
        nodes.append({"id": nid, "kind": "memory", "sub": kind, "label": f"{nid} {truncate(r['title'], 40)}",
                      "text": r["text"], "why": r["why"], "status": r["status"], "critical": bool(r["priority"]),
                      "tag": source_tag(r["source"]), "updated": r["updated_at"], "rank": 0.004 + 0.004 * bool(r["priority"])})
        for t in targets(f"{r['text']} {r['why'] or ''}", r["files"]):
            edges.append({"source": nid, "target": t, "kind": "mentions", "weight": 1})
    if s.one("SELECT 1 FROM sqlite_master WHERE name='folds'"):
        for f in s.q("SELECT id, kind, title, summary, meta, created_at, substr(body,1,4000) b FROM folds "
                     "ORDER BY id DESC LIMIT 30"):
            nid = f"F{f['id']}"
            meta = json.loads(f["meta"]) if f["meta"] else {}
            nodes.append({"id": nid, "kind": "memory", "sub": "fold", "label": f"{nid} {truncate(f['title'], 40)}",
                          "text": f["summary"], "status": f["kind"], "updated": f["created_at"], "rank": 0.003,
                          "lines": meta.get("lines")})
            for t in targets(f"{f['summary']} {meta.get('command') or ''} {f['b'] or ''}", None)[:4]:
                edges.append({"source": nid, "target": t, "kind": "mentions", "weight": 1})
    return nodes, edges


def _stats(s: db.Store) -> dict:
    one = lambda sql: s.one(sql)[0]  # noqa: E731
    return {"files": one("SELECT COUNT(*) FROM files"),
            "symbols": one("SELECT COUNT(*) FROM nodes WHERE deleted_at IS NULL AND kind IN ('function','method','class')"),
            "edges": one("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL"),
            "items": one("SELECT COUNT(*) FROM items WHERE kind!='handoff' AND " + live_clause()),
            "folds": one("SELECT COUNT(*) FROM folds") if s.one("SELECT 1 FROM sqlite_master WHERE name='folds'") else 0,
            "last_sync": s.get_meta("last_sync"), "last_change": s.get_meta("last_change")}


def fingerprint(s: db.Store) -> str:
    """Cheap change detector: moves when the index, memory, folds or turns change."""
    parts = [s.get_meta("last_change") or "", s.get_meta("last_reconcile") or "", s.get_meta("last_sync") or "",
             str(s.one("SELECT COALESCE(MAX(updated_at),'') || COUNT(*) FROM items")[0]),
             str(s.one("SELECT COALESCE(MAX(id),0) FROM turns")[0])]
    if s.one("SELECT 1 FROM sqlite_master WHERE name='folds'"):
        parts.append(str(s.one("SELECT COALESCE(MAX(id),0) FROM folds")[0]))
    return "|".join(parts)


def recent_changes(s: db.Store, since: str) -> list[str]:
    """Node ids touched since `since` (ISO), for the page's flash-and-pulse effect."""
    ids = [r[0] for r in s.q("SELECT key FROM nodes WHERE updated_at > ? AND kind='file' LIMIT 50", (since,))]
    ids += [fmt_id(r["kind"], r["id"]) for r in s.q("SELECT id, kind FROM items WHERE updated_at > ? LIMIT 50", (since,))]
    if s.one("SELECT 1 FROM sqlite_master WHERE name='folds'"):
        ids += [f"F{r[0]}" for r in s.q("SELECT id FROM folds WHERE created_at > ? LIMIT 20", (since,))]
    return ids


# -------------------------------------------------------------------- server
class _State:
    def __init__(self, root: str, host: str, port: int):
        self.root, self.host, self.port = root, host, port
        self.indexing = False
        self.lock = threading.Lock()


def _handler(state: _State):
    class H(BaseHTTPRequestHandler):
        server_version = "agres-graph"

        def log_message(self, *a):  # quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1", state.host)

        def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(PAGE, "rb") as fh:
                    page = fh.read()
                return self._send(200, page, "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if u.path == "/api/graph":
                q = parse_qs(u.query)
                with db.Store(state.root) as s:
                    g = build_graph(s, (q.get("level") or ["files"])[0], int((q.get("limit") or [400])[0]))
                g["indexing"] = state.indexing
                return self._json(g)
            if u.path == "/api/events":
                return self._events()
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            origin = self.headers.get("Origin") or ""
            ok_origins = {f"http://{h}:{state.port}" for h in ("127.0.0.1", "localhost", state.host)}
            if origin not in ok_origins:
                return self._send(403, b"cross-origin request refused", "text/plain")
            if urlparse(self.path).path != "/api/index":
                return self._send(404, b"not found", "text/plain")
            with state.lock:
                if state.indexing:
                    return self._json({"started": False, "indexing": True})
                state.indexing = True
            threading.Thread(target=_index, args=(state,), daemon=True).start()
            return self._json({"started": True, "indexing": True}, 202)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last, beat = None, 0.0
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                while True:
                    with db.Store(state.root) as s:
                        fp = fingerprint(s)
                        changed = recent_changes(s, stamp) if last is not None and fp != last else []
                    if fp != last:
                        msg = {"fingerprint": fp, "changed": changed, "indexing": state.indexing, "first": last is None}
                        self.wfile.write(f"event: changed\ndata: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2))
                        last = fp
                    elif time.time() - beat > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        beat = time.time()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return H


def _index(state: _State) -> None:
    try:
        from .code import engine
        with db.Store(state.root) as s:
            engine.sync(s)
            engine.ensure_resolved(s)
    finally:
        state.indexing = False


def serve(root: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = True,
          ready=None) -> None:
    state = _State(root, host, port)
    try:
        httpd = ThreadingHTTPServer((host, port), _handler(state))
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), _handler(state))  # port taken: any free one
    httpd.daemon_threads = True
    state.port = httpd.server_address[1]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{state.port}/"
    if ready:
        ready(url, httpd)
    print(f"agres graph for {os.path.basename(root)} → {url}  (Ctrl-C stops)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
