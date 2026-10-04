#!/usr/bin/env python3
"""Agres eval harness — deterministic gates, no LLM, no network.

Proves every phase on a seeded fixture repo:
  G1 incremental index (manifest fast path + touch)
  G2 bounded packs fit tiny budgets (10k-viable)
  G3 stale packs flagged on reindex
  G4 hybrid retrieval beats legacy where graph matters
  G5 ranked map: hubs first, within budget
  G6 fold/unfold lossless (byte-exact)
  G7 checkpoint validation covers all facts

Usage:
  python3 bin/eval.py            # human report, exit 0 iff all pass
  python3 bin/eval.py --json     # JSON report (CI)
  AGRES_RUNTIME=/path/agres_runtime.py python3 bin/eval.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("AGRES_RUNTIME", str(HERE.parent / "runtime" / "agres_runtime.py")))
FILLER_FILES = 120


def sh(env, *args):
    py = os.environ.get("AGRES_PYTHON", sys.executable)
    p = subprocess.run([py, str(RUNTIME), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return p


def must_json(p, what):
    try:
        return json.loads(p.stdout)
    except Exception:
        print(f"FAIL {what}: not JSON\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}")
        raise SystemExit(1)


class Gate:
    def __init__(self, quiet=False):
        self.rows = []
        self.quiet = quiet

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), str(detail)))
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}",
              file=sys.stderr if self.quiet else sys.stdout)

    def summary(self, as_json):
        passed = sum(1 for _, ok, _ in self.rows if ok)
        total = len(self.rows)
        if as_json:
            print(json.dumps({"passed": passed, "total": total,
                              "gates": [{"name": n, "ok": ok, "detail": d}
                                        for n, ok, d in self.rows]}, indent=2))
        else:
            print(f"\nEVAL {passed}/{total} {'PASS' if passed == total else 'FAIL'}")
        return 0 if passed == total else 1


def build_fixture(root: Path):
    src = root / "src"
    src.mkdir(parents=True)
    (src / "auth.py").write_text(
        'def getAuthToken():\n    """auth token refresh handler."""\n    return 1\n\n'
        'class Store:\n    def save(self, key, value):\n        return key\n')
    (src / "api.py").write_text(
        'from auth import getAuthToken, Store\n\n'
        'def handle_request(req):\n    return getAuthToken()\n')
    (src / "worker.py").write_text(
        'from auth import Store\n\ndef worker():\n    return Store()\n')
    (src / "session.py").write_text(
        'def refresh_session():\n    """auth token refresh flow."""\n    return "auth token refresh"\n')
    (src / "auth_only.py").write_text('AUTH_WORD = "auth"\n')
    for i in range(FILLER_FILES):
        (src / f"filler{i:03d}.py").write_text(f"VALUE_{i} = {i}\n# filler {i}\n")
    (root / "package.json").write_text('{"name": "eval-fixture"}\n')


def main():
    as_json = "--json" in sys.argv
    g = Gate(quiet=as_json)
    tmp = Path(tempfile.mkdtemp(prefix="agres-eval-"))
    build_fixture(tmp)
    env = dict(os.environ, AGRES_PROJECT_ROOT=str(tmp))
    n_src = 5 + FILLER_FILES

    # G1 incremental -----------------------------------------------
    t0 = time.time()
    out = sh(env, "index", "--path", str(tmp), "--limit", "5000").stdout
    cold_s = time.time() - t0
    t0 = time.time()
    out2 = sh(env, "index", "--path", str(tmp), "--limit", "5000").stdout
    warm_s = time.time() - t0
    import re as _re

    def _count(pat, text):
        m = _re.search(pat, text)
        return int(m.group(1)) if m else -1

    cold_n = _count(r"Indexed: (\d+)", out)
    warm_skip = _count(r"Skipped unchanged: (\d+)", out2)
    warm_idx = _count(r"Indexed: (\d+)", out2)
    g.check("G1 cold indexes all, warm skips all",
            cold_n > 0 and warm_idx == 0 and warm_skip == cold_n,
            f"cold={cold_n} ({cold_s:.2f}s) warm_skip={warm_skip} ({warm_s:.2f}s)")
    (tmp / "src" / "auth.py").write_text(
        (tmp / "src" / "auth.py").read_text() + "\n# eval touch\n")
    t = must_json(sh(env, "touch", "--files", "src/auth.py", "--path", str(tmp)), "touch")
    g.check("G1 touch reindexes one", t["indexed"] == 1 and t["skipped"] == 0, str(t))

    # Session with durable facts ------------------------------------
    sess = sh(env, "start", "eval session").stdout.strip().splitlines()[-1]
    S = ["--session", sess]
    sh(env, "pin", "--text", "eval pin omega", *S)
    sh(env, "decision", "--text", "eval decision omega", "--accepted", *S)
    sh(env, "task", "--objective", "eval task omega", *S)
    sh(env, "error", "--message", "eval error omega", *S)
    marker = f"EVALMARKER {uuid.uuid4().hex} verbatim payload"
    for i in range(6):
        sh(env, "capture", "--role", "user", "--text", f"eval filler turn {i}", *S)
    sh(env, "capture", "--role", "user", "--text", marker, *S)

    # G2 bounded packs ----------------------------------------------
    for budget in (2000, 10000):
        r = must_json(sh(env, "context", "--query", "auth", "--limit", "3",
                           "--budget", str(budget), *S, "--json"), f"pack@{budget}")
        g.check(f"G2 pack fits @{budget}",
                r["fits"] and r["total_tokens"] <= r["usable"], str(r["total_tokens"]))

    # G3 stale -------------------------------------------------------
    p1 = must_json(sh(env, "context", "--query", "auth", "--limit", "2",
                       "--budget", "4000", *S, "--json"), "pack1")["pack_id"]
    (tmp / "src" / "api.py").write_text(
        (tmp / "src" / "api.py").read_text() + "\n# eval edit\n")
    sh(env, "touch", "--files", "src/api.py", "--path", str(tmp))
    db = sqlite3.connect(str(tmp / ".agres" / "agres.db"))
    stale = db.execute("SELECT stale FROM context_packs WHERE id = ?", (p1,)).fetchone()[0]
    g.check("G3 reindex marks pack stale", stale == 1, f"stale={stale}")

    # G4 retrieval ---------------------------------------------------
    d = must_json(sh(env, "search", "--query", "auth token refresh", "--limit", "5"), "q1")
    top = (d["chunks"] or [{}])[0].get("file_path", "")
    g.check("G4 AND-precision ranks phrase file first", top == "src/session.py", top)
    d = must_json(sh(env, "search", "--query", "getAuthToken", "--limit", "5"), "q2")
    fused = {f["file_path"] for f in d.get("fused", [])}
    g.check("G4 hybrid fused has definer+importer",
            {"src/api.py", "src/auth.py"} <= fused, str(sorted(fused)))
    dleg = must_json(sh(env, "search", "--query", "getAuthToken",
                         "--limit", "5", "--mode", "legacy"), "q2legacy")
    g.check("G4 legacy shape has no fused", "fused" not in dleg and "graph" not in dleg, "")
    d = must_json(sh(env, "search", "--query", "Store save", "--limit", "5"), "q3")
    sym0 = (d["symbols"] or [{}])[0]
    g.check("G4 method leaf ranks first",
            sym0.get("file_path") == "src/auth.py" and sym0.get("name", "").split(".")[-1] == "save",
            str((sym0.get("file_path"), sym0.get("name"))))

    # G5 map ----------------------------------------------------------
    m = sh(env, "map", "--path", str(tmp), "--tokens", "600").stdout
    first = next((l.rstrip(":") for l in m.splitlines()
                  if l and not l.startswith(("#", "Repo:", "Generated:", "Budget:", "Files:", " "))
                  and l.endswith(":")), "")
    g.check("G5 hub ranks first", first == "src/auth.py", first)
    g.check("G5 map within budget", len(m) // 4 <= 600, f"~{len(m)//4} tok")

    # G6 lossless ------------------------------------------------------
    sh(env, "fold", "--target", "0", *S)
    u = must_json(sh(env, "unfold", "--query", "EVALMARKER", *S), "unfold")
    fid = (u.get("unfolded") or [{}])[0].get("fold_id", "")
    row = db.execute("SELECT original_text FROM folds WHERE id = ?", (fid,)).fetchone()
    g.check("G6 fold/unfold byte-exact", row is not None and row[0] == marker,
            f"fold_id={fid}")
    db.close()

    # G7 checkpoint ----------------------------------------------------
    out = sh(env, "checkpoint", "--reason", "eval audit", *S).stdout
    g.check("G7 checkpoint validates all facts", "validation: 4/4" in out,
            out.strip().splitlines()[-1] if out.strip() else "")

    # G8 code-to-graph ------------------------------------------------
    gb = must_json(sh(env, "graph", "--build", "--path", str(tmp), "--json"), "graph-build")
    g.check("G8 graph builds nodes+edges",
            gb.get("nodes", 0) > 10 and gb.get("edges", 0) > 5, str((gb.get("nodes"), gb.get("edges"))))
    gq = must_json(sh(env, "graph", "--query", "getAuthToken", "--limit", "5", "--json"), "graph-q")
    qfiles = {n.get("file_path") for n in gq.get("nodes", []) if n.get("file_path")}
    g.check("G8 graph query reaches definer+importer",
            {"src/api.py", "src/auth.py"} <= qfiles, str(sorted(qfiles)))
    g8p = must_json(sh(env, "graph", "--query", "omega", "--limit", "10", "--json"), "graph-qp")
    pkinds = {n.get("kind") for n in g8p.get("nodes", [])}
    g.check("G8 graph holds progress nodes",
            {"task", "pin", "decision", "error"} <= pkinds, str(sorted(pkinds)))

    # G9 trace + brain -------------------------------------------------
    tr = must_json(sh(env, "trace", "--json", *S), "trace")
    g.check("G9 trace covers codebase+progress+graph",
            tr.get("codebase", {}).get("files", 0) >= n_src
            and tr.get("progress", {}).get("turns", 0) >= 7
            and tr.get("health", {}).get("graph", {}).get("nodes", 0) > 10,
            str((tr.get("codebase", {}).get("files"), tr.get("health", {}).get("graph", {}).get("nodes"))))
    br = must_json(sh(env, "brain", "--query", "auth token", "--budget", "4000",
                       *S, "--json"), "brain")
    g.check("G9 brain fits budget with graph+progress",
            br["fits"] and br["total_tokens"] <= br["usable"]
            and br.get("graph_nodes", 0) > 10
            and any(s["name"] == "progress" and s["tokens"] > 0 for s in br["sections"]),
            str((br["total_tokens"], br["graph_nodes"])))

    return g.summary(as_json)


if __name__ == "__main__":
    raise SystemExit(main())
