"""Git history -> brain: commits, per-file churn, and temporal-coupling (co-change) edges.

Co-change edges ("files that historically change together") capture hidden
coupling that static analysis cannot see; they are a well-studied signal for
change impact (Zimmermann et al., "Mining Version Histories to Guide Software
Changes"). Hotspots (churn x centrality) follow Tornhill's "Your Code as a Crime Scene".
"""
from __future__ import annotations

import math
import subprocess
from collections import Counter
from typing import Optional

from .store import Store
from ..util import epoch_to_iso, now_iso

MAX_COMMITS = 1500
MAX_FILES_PER_COMMIT = 30
MIN_PAIR_COUNT = 2
MIN_COUPLING = 0.25
TOP_PARTNERS = 5


def git(root: str, *args: str, timeout: int = 60) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def is_repo(root: str) -> bool:
    return git(root, "rev-parse", "--is-inside-work-tree") is not None


def head_info(root: str) -> dict:
    return {
        "head": (git(root, "rev-parse", "HEAD") or "").strip(),
        "branch": (git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip(),
    }


def dirty_files(root: str) -> list[tuple[str, str]]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if not out:
        return []
    res: list[tuple[str, str]] = []
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        e = parts[i]
        if len(e) > 3:
            code, path = e[:2].strip() or "?", e[3:]
            if "R" in e[:2] or "C" in e[:2]:
                i += 1  # skip the original path of a rename/copy
            res.append((code, path))
        i += 1
    return res


def ingest(store: Store, limit: int = MAX_COMMITS) -> dict:
    """Incrementally ingest commits; recompute co-change edges. Returns stats."""
    root = store.root
    if not is_repo(root):
        return {"commits": 0, "git": False}
    last = store.get_meta("git_last_sha")
    rng = []
    if last and git(root, "cat-file", "-e", f"{last}^{{commit}}") is not None:
        rng = [f"{last}..HEAD"]
    fmt = "%x1e%H%x1f%at%x1f%an%x1f%s"
    out = git(root, "log", f"--max-count={limit}", "--no-merges", "--no-renames", "--numstat",
              f"--format={fmt}", *rng, timeout=120)
    if out is None:
        return {"commits": 0, "git": True}
    new = 0
    with store.tx():
        for block in out.split("\x1e"):
            block = block.strip("\n")
            if not block:
                continue
            head, _, body = block.partition("\n")
            f = head.split("\x1f")
            if len(f) < 4:
                continue
            sha, ts, author, subject = f[0], int(f[1] or 0), f[2], f[3]
            files = []
            for ln in body.splitlines():
                p = ln.split("\t")
                if len(p) == 3:
                    a = int(p[0]) if p[0].isdigit() else 0
                    d = int(p[1]) if p[1].isdigit() else 0
                    files.append((p[2], a, d))
            if store.one("SELECT 1 FROM commits WHERE sha=?", (sha,)):
                continue
            store.x("INSERT INTO commits(sha,ts,author,subject,nfiles) VALUES(?,?,?,?,?)",
                    (sha, epoch_to_iso(ts), author, subject, len(files)))
            store.many("INSERT OR IGNORE INTO commit_files(sha,path,added,deleted) VALUES(?,?,?,?)",
                       [(sha, p, a, d) for p, a, d in files])
            new += 1
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    if head:
        store.set_meta("git_last_sha", head)
    edges = _co_change(store) if new or not store.one("SELECT 1 FROM edges WHERE kind='co_change' LIMIT 1") else None
    return {"commits": new, "git": True, "co_change": edges}


def _co_change(store: Store) -> int:
    live = {r["path"]: r["id"] for r in store.q("SELECT path, id FROM nodes WHERE kind='file' AND deleted_at IS NULL")}
    rows = store.q(
        "SELECT cf.sha, cf.path FROM commit_files cf JOIN commits c ON c.sha=cf.sha "
        "WHERE c.nfiles <= ? ORDER BY cf.sha", (MAX_FILES_PER_COMMIT,))
    by_commit: dict[str, list[str]] = {}
    for r in rows:
        if r["path"] in live:
            by_commit.setdefault(r["sha"], []).append(r["path"])
    single: Counter = Counter()
    pair: Counter = Counter()
    for files in by_commit.values():
        if len(files) < 2:
            for f in files:
                single[f] += 1
            continue
        fs = sorted(set(files))
        for f in fs:
            single[f] += 1
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pair[(fs[i], fs[j])] += 1
    cand: dict[str, list[tuple[float, str, int]]] = {}
    for (a, b), n in pair.items():
        if n < MIN_PAIR_COUNT:
            continue
        score = n / math.sqrt(single[a] * single[b])  # cosine-style coupling in [0,1]
        if score >= MIN_COUPLING:
            cand.setdefault(a, []).append((score, b, n))
            cand.setdefault(b, []).append((score, a, n))
    keep: dict[tuple[str, str], float] = {}
    for a, lst in cand.items():
        lst.sort(reverse=True)
        for score, b, n in lst[:TOP_PARTNERS]:
            k = (a, b) if a < b else (b, a)
            keep[k] = max(keep.get(k, 0.0), round(score, 3))
    desired = {(live[a], live[b]): w for (a, b), w in keep.items()}
    existing = {(r["src"], r["dst"]): (r["id"], r["weight"]) for r in
                store.q("SELECT id,src,dst,weight FROM edges WHERE kind='co_change' AND valid_to IS NULL")}
    ts = now_iso()
    with store.tx():
        store.many("UPDATE edges SET valid_to=? WHERE id=?",
                   [(ts, eid) for k, (eid, _w) in existing.items() if k not in desired])
        store.many("INSERT INTO edges(src,dst,kind,weight,valid_from,recorded_at) VALUES(?,?,'co_change',?,?,?)",
                   [(k[0], k[1], w, ts, ts) for k, w in desired.items() if k not in existing])
        store.many("UPDATE edges SET weight=? WHERE id=?",
                   [(w, existing[k][0]) for k, w in desired.items() if k in existing and abs(existing[k][1] - w) > 1e-6])
    return len(desired)


def recent_commits(store: Store, n: int = 10) -> list[dict]:
    return [dict(r) for r in store.q("SELECT sha,ts,author,subject,nfiles FROM commits ORDER BY ts DESC, rowid DESC LIMIT ?", (n,))]


def commit_files(store: Store, sha: str, limit: int = 8) -> list[str]:
    return [r["path"] for r in store.q("SELECT path FROM commit_files WHERE sha=? ORDER BY added+deleted DESC LIMIT ?", (sha, limit))]


def hotspots(store: Store, days: int = 90, limit: int = 10) -> list[dict]:
    """Churn in the last ``days`` days weighted by structural importance."""
    import datetime as dt
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = store.q(
        "SELECT cf.path AS path, COUNT(*) AS commits, SUM(cf.added+cf.deleted) AS churn "
        "FROM commit_files cf JOIN commits c ON c.sha=cf.sha WHERE c.ts >= ? GROUP BY cf.path", (cutoff,))
    rk = {r["path"]: (r["rank"] or 0.0, r["id"]) for r in
          store.q("SELECT path, rank, id FROM nodes WHERE kind='file' AND deleted_at IS NULL")}
    if not rk:
        return []
    rmax = max(v[0] for v in rk.values()) or 1.0
    out = []
    for r in rows:
        if r["path"] not in rk or r["commits"] < 2:
            continue
        centrality = rk[r["path"]][0] / rmax
        score = math.log1p(r["commits"]) * math.log1p(r["churn"] or 0) * (0.3 + centrality)
        out.append({"path": r["path"], "commits": r["commits"], "churn": r["churn"] or 0,
                    "score": score, "centrality": centrality})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]
