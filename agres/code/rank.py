"""Graph algorithms over the brain: PageRank (plain + personalized), Louvain
communities, impact (reverse reachability) and shortest paths.

All pure-Python/stdlib. Sizes of interest (<=100k nodes, <=1M edges) run in
seconds; PageRank uses sparse adjacency lists with early stopping.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Optional

from .store import Store

CODE_KINDS_EXCL = ("decision", "task", "error", "pin", "note", "journal", "goal")
MEMORY_KINDS = CODE_KINDS_EXCL
# weight multipliers used for ranking (direction: src -> dst means "src depends on dst")
KIND_WEIGHT = {"imports": 1.0, "calls": 1.0, "inherits": 1.5, "references": 0.6, "depends": 0.3,
               "contains": 0.5, "co_change": 0.4}
IMPACT_KINDS = ("imports", "calls", "inherits", "references")


class Graph:
    """In-memory snapshot of active nodes/edges for fast algorithms."""

    def __init__(self, store: Store, edge_kinds: Optional[Iterable[str]] = None, include_memory: bool = False):
        self.store = store
        self.nodes: dict[int, dict] = {}
        for r in store.q("SELECT id,key,kind,name,qualname,path,line,end_line,lang,sig,doc,rank,community,attrs "
                         "FROM nodes WHERE deleted_at IS NULL"):
            if not include_memory and r["kind"] in MEMORY_KINDS:
                continue
            self.nodes[r["id"]] = dict(r)
        self.out: dict[int, list[tuple[int, str, float]]] = {}
        self.inn: dict[int, list[tuple[int, str, float]]] = {}
        sql = "SELECT src,dst,kind,weight FROM edges WHERE valid_to IS NULL"
        args: list[Any] = []
        if edge_kinds:
            ek = list(edge_kinds)
            sql += " AND kind IN (%s)" % ",".join("?" * len(ek))
            args = ek
        for r in store.q(sql, args):
            if r["src"] in self.nodes and r["dst"] in self.nodes:
                w = r["weight"] if r["weight"] is not None else 1.0
                self.out.setdefault(r["src"], []).append((r["dst"], r["kind"], w))
                self.inn.setdefault(r["dst"], []).append((r["src"], r["kind"], w))

    def edges(self) -> Iterable[tuple[int, int, str, float]]:
        for s, lst in self.out.items():
            for d, k, w in lst:
                yield s, d, k, w


# --------------------------------------------------------------- PageRank


def pagerank(nodes: list[int], edges: Iterable[tuple[int, int, float]],
             personal: Optional[dict[int, float]] = None, d: float = 0.85,
             max_iter: int = 80, tol: float = 1e-8) -> dict[int, float]:
    """Weighted PageRank with (optionally personalized) teleport + dangling handling."""
    n = len(nodes)
    if n == 0:
        return {}
    idx = {v: i for i, v in enumerate(nodes)}
    out: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    outw = [0.0] * n
    for s, t, w in edges:
        if s in idx and t in idx and w > 0:
            i, j = idx[s], idx[t]
            out[i].append((j, w))
            outw[i] += w
    if personal:
        tot = sum(max(0.0, v) for k, v in personal.items() if k in idx)
        p = [0.0] * n
        if tot > 0:
            for k, v in personal.items():
                if k in idx:
                    p[idx[k]] = max(0.0, v) / tot
        else:
            p = [1.0 / n] * n
    else:
        p = [1.0 / n] * n
    r = p[:]
    for _ in range(max_iter):
        new = [0.0] * n
        dangling = 0.0
        for i in range(n):
            if outw[i] > 0:
                share = r[i] / outw[i]
                for j, w in out[i]:
                    new[j] += share * w
            else:
                dangling += r[i]
        err = 0.0
        for i in range(n):
            v = (1 - d) * p[i] + d * (new[i] + dangling * p[i])
            err += abs(v - r[i])
            new[i] = v
        r = new
        if err < tol:
            break
    return {nodes[i]: r[i] for i in range(n)}


def structural_edges(g: Graph, test_down: bool = True) -> list[tuple[int, int, float]]:
    """Edges for ranking: dependency direction + a weak reverse edge so that
    hubs are rewarded for being depended upon but isolated leaves still flow."""
    out: list[tuple[int, int, float]] = []
    for s, d, k, w in g.edges():
        km = KIND_WEIGHT.get(k)
        if km is None:
            continue
        if k == "contains":
            out.append((s, d, km * w))        # container hands rank to members
            out.append((d, s, 0.25 * w))      # members feed their container
        elif k == "co_change":
            out.append((s, d, km * w))
            out.append((d, s, km * w))
        else:
            out.append((s, d, km * w))
            out.append((d, s, 0.08 * km * w))
    return out


def compute_ranks(store: Store) -> dict[int, float]:
    g = Graph(store)
    ids = [i for i, n in g.nodes.items() if n["kind"] != "dir" or True]
    pr = pagerank(ids, structural_edges(g))
    store.many("UPDATE nodes SET rank=? WHERE id=?", [(v, k) for k, v in pr.items()])
    return pr


def personalized_rank(g: Graph, focus: dict[int, float]) -> dict[int, float]:
    """Rank biased toward ``focus`` nodes (Aider-style), blended with global rank."""
    ids = list(g.nodes)
    pr = pagerank(ids, structural_edges(g), personal=focus, d=0.85, max_iter=50)
    # blend: relevance to focus dominates but global hubs keep a floor
    glob = {i: (g.nodes[i].get("rank") or 0.0) for i in ids}
    gmax = max(glob.values()) or 1.0
    pmax = max(pr.values()) or 1.0
    return {i: 0.75 * pr[i] / pmax + 0.25 * glob[i] / gmax for i in ids}


# ---------------------------------------------------------------- Louvain


def _one_level(g: dict[int, dict[int, float]], gamma: float) -> tuple[dict[int, int], bool]:
    nodes = sorted(g)
    comm = {u: u for u in nodes}
    k = {u: sum(g[u].values()) for u in nodes}
    m2 = sum(k.values())
    if m2 == 0:
        return comm, False
    tot = dict(k)
    moved_any = False
    for _ in range(25):
        moved = False
        for u in nodes:
            cu = comm[u]
            w_to: dict[int, float] = {}
            for v, w in g[u].items():
                if v != u:
                    w_to[comm[v]] = w_to.get(comm[v], 0.0) + w
            tot[cu] -= k[u]
            best_c, best_gain = cu, w_to.get(cu, 0.0) - gamma * tot[cu] * k[u] / m2
            for c in sorted(w_to):
                gain = w_to[c] - gamma * tot[c] * k[u] / m2
                if gain > best_gain + 1e-12:
                    best_c, best_gain = c, gain
            tot[best_c] += k[u]
            if best_c != cu:
                comm[u] = best_c
                moved = moved_any = True
        if not moved:
            break
    return comm, moved_any


def louvain(adj: dict[int, dict[int, float]], resolution: float = 1.0, max_levels: int = 6) -> dict[int, int]:
    """Deterministic Louvain modularity optimisation. ``adj`` must be symmetric."""
    g = {u: dict(vs) for u, vs in adj.items()}
    mapping = {u: u for u in g}
    for _ in range(max_levels):
        part, moved = _one_level(g, resolution)
        if not moved:
            break
        labels = {c: i for i, c in enumerate(sorted(set(part.values())))}
        mapping = {n: labels[part[mapping[n]]] for n in mapping}
        ng: dict[int, dict[int, float]] = {i: {} for i in labels.values()}
        for u, vs in g.items():
            cu = labels[part[u]]
            for v, w in vs.items():
                cv = labels[part[v]]
                ng[cu][cv] = ng[cu].get(cv, 0.0) + w
        if len(ng) == len(g):
            break
        g = ng
    return mapping


def compute_communities(store: Store) -> dict[int, int]:
    """Cluster *files* by coupling (imports/calls/refs/co-change), then label symbols
    with their file's community. Isolated files fall back to their directory."""
    g = Graph(store)
    files = {i: n for i, n in g.nodes.items() if n["kind"] == "file"}
    owner: dict[int, int] = {}
    for i, n in g.nodes.items():
        if n["kind"] == "file":
            owner[i] = i
    for s, d, k, w in g.edges():
        if k == "contains" and g.nodes[s]["kind"] in ("file", "class", "struct", "interface", "trait", "object", "module") \
                and g.nodes[d]["kind"] not in ("file", "dir"):
            owner[d] = owner.get(s, s)
    # resolve nested owners to their file
    for i in list(owner):
        seen = 0
        while owner[i] not in files and owner.get(owner[i], owner[i]) != owner[i] and seen < 5:
            owner[i] = owner[owner[i]]
            seen += 1
    adj: dict[int, dict[int, float]] = {f: {} for f in files}
    for s, d, k, w in g.edges():
        if k in ("contains",) or k not in KIND_WEIGHT:
            continue
        a, b = owner.get(s), owner.get(d)
        if a is None or b is None or a == b or a not in files or b not in files:
            continue
        ww = w * KIND_WEIGHT[k]
        adj[a][b] = adj[a].get(b, 0.0) + ww
        adj[b][a] = adj[b].get(a, 0.0) + ww
    part = louvain(adj) if any(adj.values()) else {f: i for i, f in enumerate(files)}
    store.many("UPDATE nodes SET community=? WHERE id=?",
               [(part.get(owner.get(i, i), -1), i) for i in g.nodes if owner.get(i, i) in part])
    return part


# ----------------------------------------------------------- traversal tools


def impact(g: Graph, start: Iterable[int], depth: int = 3, kinds: Iterable[str] = IMPACT_KINDS) -> list[list[tuple[int, str]]]:
    """Reverse reachability: who depends (transitively) on ``start``. Returns levels of (node, via_kind)."""
    ks = set(kinds)
    seen = set(start)
    frontier = list(seen)
    levels: list[list[tuple[int, str]]] = []
    for _ in range(depth):
        nxt: list[tuple[int, str]] = []
        for n in frontier:
            for s, k, _w in g.inn.get(n, []):
                if k in ks and s not in seen:
                    seen.add(s)
                    nxt.append((s, k))
        if not nxt:
            break
        levels.append(nxt)
        frontier = [n for n, _ in nxt]
    return levels


def shortest_path(g: Graph, a: int, b: int, kinds: Optional[Iterable[str]] = None,
                  max_nodes: int = 200000) -> Optional[list[tuple[int, str, int]]]:
    """BFS over undirected structural edges. Returns [(from, kind, to)] hops (kind may end with '^' if traversed backwards)."""
    ks = set(kinds) if kinds else {k for k in KIND_WEIGHT if k != "contains"} | {"contains"}
    prev: dict[int, tuple[int, str]] = {a: (a, "")}
    q = deque([a])
    while q and len(prev) < max_nodes:
        u = q.popleft()
        if u == b:
            break
        for v, k, _w in g.out.get(u, []):
            if k in ks and v not in prev:
                prev[v] = (u, k)
                q.append(v)
        for v, k, _w in g.inn.get(u, []):
            if k in ks and v not in prev:
                prev[v] = (u, k + "^")
                q.append(v)
    if b not in prev:
        return None
    hops: list[tuple[int, str, int]] = []
    cur = b
    while cur != a:
        p, k = prev[cur]
        hops.append((p, k, cur))
        cur = p
    hops.reverse()
    return hops


def neighborhood(g: Graph, seeds: Iterable[int], depth: int = 1, kinds: Optional[Iterable[str]] = None,
                 limit: int = 400) -> set[int]:
    ks = set(kinds) if kinds else None
    seen = set(seeds)
    frontier = list(seen)
    for _ in range(depth):
        nxt: list[int] = []
        for u in frontier:
            for v, k, _w in g.out.get(u, []) + g.inn.get(u, []):
                if (ks is None or k in ks) and v not in seen:
                    seen.add(v)
                    nxt.append(v)
                    if len(seen) >= limit:
                        return seen
        frontier = nxt
    return seen
