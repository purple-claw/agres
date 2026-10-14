"""On-demand code views: aider-style repo map (personalized PageRank) and symbol search.

Both read the graph the indexer built; callers (engine.py) make sure edges are resolved first
where that matters. Output is plain text / plain dicts, never JSON blobs.
"""
from __future__ import annotations

import bisect
import json
import re
from typing import Iterable, Optional

from . import rank
from .store import Store
from ..util import split_identifier, terms

CHARS_PER_TOKEN = 3.5
TIERS = ((10, 10), (40, 6), (10 ** 9, 3))   # (files up to this position, symbols shown per file)
MAX_SIG = 100
NON_CODE_LANGS = {"json", "yaml", "toml", "markdown", "manifest", "xml", "html", "css", "text", "gradle"}
HIDE_KINDS = ("file", "dir", "package")


# ------------------------------------------------------------------ search


def _words(query: str) -> list[str]:
    out: list[str] = []
    for t in re.findall(r"[A-Za-z0-9_]+", query):
        out.append(t.lower())
        out.extend(split_identifier(t))
    return list(dict.fromkeys(w for w in out if len(w) > 1 or w.isdigit()))


def search(store: Store, query: str, limit: int = 10) -> list[dict]:
    """Symbols/files whose name, path, signature or doc match `query`, best first."""
    words = _words(query or "")
    if not words or limit <= 0:
        return []
    cand = store.search_ids(query, limit=max(limit * 5, 40))
    if not cand:
        return []
    score = dict(cand)
    ids = list(score)
    rows = store.q("SELECT id,kind,name,path,line,sig,doc,rank FROM nodes WHERE deleted_at IS NULL AND kind NOT IN "
                   "('dir','package') AND id IN (%s)" % ",".join("?" * len(ids)), ids)
    if not rows:
        return []
    top = max(score[r["id"]] for r in rows) or 1.0
    rmax = max((r["rank"] or 0.0) for r in rows) or 1.0
    compact = "".join(split_identifier(re.sub(r"[^A-Za-z0-9_]", "", query)))
    out = []
    for r in rows:
        sub = split_identifier(r["name"])
        bonus = 0.0
        if compact and "".join(sub) == compact:
            bonus = 1.0                                        # exact symbol / file name
        elif all(w in sub or w in r["name"].lower() for w in words):
            bonus = 0.5                                        # every query word is in the name
        elif any(r["name"].lower().startswith(w) for w in words):
            bonus = 0.2
        if r["kind"] == "file":
            bonus -= 0.05
        s = score[r["id"]] / top + bonus + 0.15 * (r["rank"] or 0.0) / rmax
        out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "path": r["path"], "line": r["line"],
                    "sig": r["sig"] or r["doc"] or "", "score": round(float(s), 4)})
    out.sort(key=lambda d: (-d["score"], d["path"] or "", d["line"] or 0))
    return out[:limit]


# ---------------------------------------------------------------- repo map


def _tier_symbols(pos: int) -> int:
    for upto, n in TIERS:
        if pos < upto:
            return n
        pos -= upto
    return TIERS[-1][1]


def _seed_weights(store: Store, g: rank.Graph, query: Optional[str],
                  focus: Iterable[str]) -> tuple[dict[int, float], set[int]]:
    """PageRank teleport weights from focus files and query terms, plus the focus file ids."""
    seeds: dict[int, float] = {}
    focus_ids: set[int] = set()
    by_key = {n["key"]: i for i, n in g.nodes.items() if n["kind"] == "file"}
    by_path = {n["path"]: i for i, n in g.nodes.items() if n["kind"] == "file"}
    for rel in focus:
        fid = by_key.get(f"f:{rel}")
        if fid is not None:
            seeds[fid] = seeds.get(fid, 0.0) + 1.0
            focus_ids.add(fid)
    if query:
        cleaned = " ".join(sorted(terms(query)))
        hits = store.search_ids(cleaned, limit=40) if cleaned else []
        top = max((s for _i, s in hits), default=0.0) or 1.0
        for nid, sc in hits:
            n = g.nodes.get(nid)
            if n is None or n["kind"] in ("dir", "package"):
                continue
            w = 0.6 * sc / top
            fid = nid if n["kind"] == "file" else by_path.get(n["path"])
            if fid is not None:
                seeds[fid] = seeds.get(fid, 0.0) + w
            if n["kind"] != "file":
                seeds[nid] = seeds.get(nid, 0.0) + 0.4 * w
    return seeds, focus_ids


def _file_block(path: str, syms: list[dict], limit: int, indent: int = 2) -> list[str]:
    """Header + up to `limit` symbol lines (ranked pick, shown in source order, parents included)."""
    by_q = {s["qualname"]: s for s in syms}
    picked: dict[str, dict] = {}
    for s in sorted(syms, key=lambda x: (-(x["rank"] or 0.0), x["line"] or 0)):
        if len(picked) >= limit:
            break
        par = by_q.get(s["parent"]) if s["parent"] else None
        if par is not None and par["qualname"] not in picked:
            picked[par["qualname"]] = par
        picked[s["qualname"]] = s
    lines = [f"{path}:"]
    for s in sorted(picked.values(), key=lambda x: (x["line"] or 0, x["qualname"])):
        depth = min(s["qualname"].count("."), 3) if s["parent"] else 0
        text = re.sub(r"\s*(?:=>|\{)$", "", " ".join((s["sig"] or f"{s['kind']} {s['name']}").split()))
        if len(text) > MAX_SIG:
            text = text[:MAX_SIG - 1] + "…"
        lines.append(" " * (indent * (depth + 1)) + text)
    return lines


def build_map(store: Store, query: Optional[str] = None, focus_files: Iterable[str] = (),
              tokens: int = 1024) -> str:
    """Files in personalized-rank order, each with its top symbol signatures, fitted to ~tokens*3.5 chars.

    `focus_files` must already be repo-relative. The block list is a pure function of the file order, so
    the number of whole files that fit is found by binary search over prefix sums; leftover space is
    filled with the next file's highest-ranked lines.
    """
    g = rank.Graph(store)
    files = {i: n for i, n in g.nodes.items() if n["kind"] == "file"}
    if not files:
        return ""
    seeds, focus_ids = _seed_weights(store, g, query, focus_files)
    score = rank.personalized_rank(g, seeds) if seeds else {i: (n["rank"] or 0.0) for i, n in g.nodes.items()}
    sym_by_path: dict[str, list[dict]] = {}
    for i, n in g.nodes.items():
        if n["kind"] not in HIDE_KINDS and n["path"]:
            d = dict(n)
            d["rank"] = score.get(i, 0.0)
            d["parent"] = None
            q = n["qualname"] or n["name"]
            if "." in q:
                d["parent"] = q.rsplit(".", 1)[0]
            d["qualname"] = q
            sym_by_path.setdefault(n["path"], []).append(d)

    def file_score(fid: int) -> float:
        n = files[fid]
        s = score.get(fid, 0.0)
        if fid in focus_ids:      # the file being worked on leads; its neighbours follow by rank
            return 2.0 + s
        if fid in seeds:
            return s
        attrs = json.loads(n["attrs"]) if n.get("attrs") else {}
        if attrs.get("test"):
            s *= 0.5
        if n["lang"] in NON_CODE_LANGS or not sym_by_path.get(n["path"]):
            s *= 0.35
        return s

    order = sorted(files, key=lambda f: (-file_score(f), files[f]["path"]))
    blocks = [_file_block(files[f]["path"], sym_by_path.get(files[f]["path"], []), _tier_symbols(pos))
              for pos, f in enumerate(order)]
    texts = ["\n".join(b) + "\n" for b in blocks]
    budget = int(tokens * CHARS_PER_TOKEN)
    prefix = [0]
    for t in texts:
        prefix.append(prefix[-1] + len(t))
    k = bisect.bisect_right(prefix, budget) - 1          # most whole files that fit
    out = "".join(texts[:k])
    if k < len(blocks):                                   # top up with part of the next file
        room = budget - len(out)
        part, used = [], 0
        for ln in blocks[k]:
            if used + len(ln) + 1 > room and (part or k > 0):
                break
            part.append(ln)
            used += len(ln) + 1
        if part:
            out += "\n".join(part) + "\n"
    return out
