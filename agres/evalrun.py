"""`python3 -m agres.evalrun [--json] [--cases PATH]`: memory + origami eval on a throwaway project.

Measures what the audit asked for before adding retrieval machinery: recall@3, stale (superseded)
results, abstention on unrelated queries, the rejection guard, pack size, origami exactness and
artifact retention, and hook latency. Never touches a real store: everything runs under a temp HOME.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_CASES = os.path.join(REPO, "evals", "memory", "cases.json")


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def run(cases_path: str = DEFAULT_CASES) -> dict:
    with open(cases_path, encoding="utf-8") as fh:
        cases = json.load(fh)
    tmp = tempfile.mkdtemp(prefix="agres-eval-")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = os.path.join(tmp, "home")
    try:
        return _run(cases, tmp)
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


def _run(cases: dict, tmp: str) -> dict:
    from . import db, fold, hooks, memory, pack, search

    root = os.path.join(tmp, "home", "work", "okas")
    os.makedirs(os.path.join(root, ".git"))
    s = db.Store(root, create=True)
    ids: dict[str, str] = {}
    for it in cases["items"]:
        iid = memory.add_item(s, it["kind"], it["text"], status=it.get("status"), why=it.get("why"),
                              priority=1 if it.get("critical") else 0)
        ids[it["key"]] = memory.fmt_id(it["kind"], iid)
    for sup in cases.get("supersede", []):
        old, new = ids[sup["old"]], ids[sup["new"]]
        s.x("UPDATE items SET status='superseded', superseded_by=? WHERE id=?", (int(new[1:]), int(old[1:])))
    out: dict = {"cases": {}}

    # retrieval
    hit = []
    for c in cases["recall"]:
        top = [h["id"] for h in search.search(s, c["query"], include_turns=False, include_code=False, limit=3)]
        hit.append(ids[c["expect"]] in top)
        if not hit[-1]:
            out["cases"].setdefault("recall_misses", []).append({"query": c["query"], "got": top})
    out["recall_at_3"] = round(sum(hit) / len(hit), 3)

    # selective forgetting / updates: a superseded item is not returned by default
    stale = 0
    for c in cases.get("stale", []):
        got = [h["id"] for h in search.search(s, c["query"], include_turns=False, include_code=False)]
        stale += ids[c["must_not"]] in got
    out["stale_errors"] = stale

    # abstention: unrelated questions get no memory items
    quiet = [not search.search(s, q, include_turns=False, include_code=False) for q in cases.get("abstain", [])]
    out["abstain_rate"] = round(sum(quiet) / max(1, len(quiet)), 3)

    # rejection guard
    g_ok = []
    for i, c in enumerate(cases.get("guard", [])):
        raw = io.StringIO()
        hooks.main("prompt", io.StringIO(json.dumps({"session_id": f"g{i}", "cwd": root, "prompt": c["prompt"]})), raw)
        ctx = json.loads(raw.getvalue())["hookSpecificOutput"]["additionalContext"] if raw.getvalue() else ""
        g_ok.append(ids[c["expect"]] in ctx)
    out["guard_rate"] = round(sum(g_ok) / max(1, len(g_ok)), 3)

    # pack
    budget = 6000
    text = pack.build(s, budget_chars=budget, git=False)
    out["pack_chars"] = len(text)
    out["pack_within_budget"] = len(text) <= budget
    out["critical_in_pack"] = all(ids[it["key"]] in text for it in cases["items"] if it.get("critical"))

    # origami
    fc = cases["fold"]
    lines = [f"[{i:05d}] cc -c module_{i}.c -o module_{i}.o" for i in range(1, fc["lines"] + 1)]
    for ln, txt in fc["salient"].items():
        lines[int(ln) - 1] = txt
    big = "\n".join(lines)
    payload = {"session_id": "f", "cwd": root, "tool_name": "Bash", "tool_input": {"command": "make -j8"},
               "tool_response": {"stdout": big, "stderr": "", "interrupted": False, "isImage": False}}
    raw = io.StringIO()
    hooks.main("fold", io.StringIO(json.dumps(payload)), raw)
    new = json.loads(raw.getvalue())["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if raw.getvalue() else big
    fid = int(new.rsplit("agres fold F", 1)[1].split(":", 1)[0]) if "agres fold F" in new else None
    out["fold_ratio"] = round(len(new) / len(big), 4)
    out["salient_kept"] = round(sum(t in new for t in fc["salient"].values()) / len(fc["salient"]), 3)
    out["unfold_exact"] = bool(fid) and fold.unfold(s, fid, all_=True) == big
    out["unfold_grep_finds_salient"] = bool(fid) and all(
        t in fold.unfold(s, fid, grep=t.split(":")[0][:20]) for t in fc["salient"].values())
    payload["tool_input"]["command"] = f"agres unfold F{fid} --all"
    raw = io.StringIO()
    hooks.main("fold", io.StringIO(json.dumps(payload)), raw)
    out["no_refold_of_unfold"] = raw.getvalue() == ""

    # latency (real subprocesses through the shim, like Claude Code runs them)
    shim = os.path.join(REPO, "bin", "agres")
    env = dict(os.environ)
    ss = json.dumps({"session_id": "lat", "cwd": root, "source": "startup"}).encode()
    small = json.dumps({"session_id": "lat", "cwd": root, "tool_name": "Bash", "tool_input": {"command": "ls"},
                        "tool_response": {"stdout": "a\nb\n", "stderr": ""}}).encode()
    t_ss, t_fold = [], []
    for _ in range(12):
        a = time.perf_counter()
        subprocess.run([shim, "hook", "session-start"], input=ss, capture_output=True, env=env)
        t_ss.append((time.perf_counter() - a) * 1000)
        a = time.perf_counter()
        subprocess.run([shim, "hook", "fold"], input=small, capture_output=True, env=env)
        t_fold.append((time.perf_counter() - a) * 1000)
    out["session_start_p95_ms"] = round(_p95(t_ss[2:]), 1)
    out["session_start_median_ms"] = round(statistics.median(t_ss[2:]), 1)
    out["fold_hook_small_p95_ms"] = round(_p95(t_fold[2:]), 1)
    s.close()

    th = cases["thresholds"]
    checks = {
        "recall_at_3": out["recall_at_3"] >= th["recall_at_3"],
        "stale_errors": out["stale_errors"] <= th["stale_errors"],
        "abstain_rate": out["abstain_rate"] >= th["abstain_rate"],
        "guard_rate": out["guard_rate"] >= th["guard_rate"],
        "pack_within_budget": out["pack_within_budget"] and out["critical_in_pack"],
        "unfold_exact": out["unfold_exact"] and out["unfold_grep_finds_salient"] and out["no_refold_of_unfold"],
        "salient_kept": out["salient_kept"] >= th["salient_kept"],
        "fold_ratio": out["fold_ratio"] <= th["fold_ratio_max"],
        "session_start_p95_ms": out["session_start_p95_ms"] <= th["session_start_p95_ms"],
        "fold_hook_small_p95_ms": out["fold_hook_small_p95_ms"] <= th["fold_hook_small_p95_ms"],
    }
    out["checks"] = checks
    out["passed"] = all(checks.values())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m agres.evalrun")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = run(a.cases)
    if a.json:
        print(json.dumps(r, indent=2, sort_keys=True))
    else:
        for k, ok in r["checks"].items():
            val = r.get(k, r.get(k.replace("_max", "")))
            print(f"{'✓' if ok else '✗'} {k:24} {val}")
        print(f"pack {r['pack_chars']} chars · fold ratio {r['fold_ratio']} · session-start median "
              f"{r['session_start_median_ms']} ms")
        for m in r["cases"].get("recall_misses", []):
            print(f"  miss: {m['query']!r} → {m['got']}")
        print("PASS" if r["passed"] else "FAIL")
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
