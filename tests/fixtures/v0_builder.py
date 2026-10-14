"""Build REAL v0 stores by driving the old v0.5 runtime as a subprocess.

The runtime is taken from git history (pinned commit, so it keeps working after the
file is deleted from the tree in P9) and falls back to `runtime/agres_runtime.py`.
It is run exactly the way the old shim ran it: `python3 agres_runtime.py <cmd>` with
`HOME` / `AGRES_HOME` pointing into a throwaway home and cwd = the project dir.

Quirks reproduced on purpose: the old CLI makes `./.agres` wherever it runs (so a
command in a subdirectory creates a nested stray store) and the global store is simply
the one created with cwd == $HOME.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OLD_RUNTIME_COMMIT = "7874d2a"  # last commit with the v0.5 runtime as shipped
_cached: Optional[str] = None


def old_runtime() -> str:
    """Path to a copy of the v0.5 runtime; skips the test if it cannot be found."""
    global _cached
    if _cached and os.path.isfile(_cached):
        return _cached
    src = None
    try:
        src = subprocess.run(["git", "-C", REPO, "show", f"{OLD_RUNTIME_COMMIT}:runtime/agres_runtime.py"],
                             capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        local = os.path.join(REPO, "runtime", "agres_runtime.py")
        if os.path.isfile(local):
            with open(local, "rb") as fh:
                src = fh.read()
    if not src:
        raise unittest.SkipTest("v0.5 runtime not available (git history and runtime/ both missing)")
    fd, path = tempfile.mkstemp(prefix="agres_runtime_v0_", suffix=".py")
    with os.fdopen(fd, "wb") as fh:
        fh.write(src)
    _cached = path
    return path


class V0Builder:
    def __init__(self, home: str):
        self.home = home
        self.environment = dict(os.environ, HOME=home, AGRES_HOME=os.path.join(home, ".agres"))
        for k in ("AGRES_ROOT", "AGRES_SESSION", "AGRES_PROJECT_ROOT"):
            self.environment.pop(k, None)

    # ------------------------------------------------------------------ plumbing
    def run(self, cwd: str, *args: str) -> str:
        os.makedirs(cwd, exist_ok=True)
        r = subprocess.run([sys.executable, old_runtime(), *args], cwd=cwd, env=self.environment,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"v0 runtime {args} failed ({r.returncode}): {r.stderr or r.stdout}")
        return r.stdout

    def db(self, cwd: str) -> str:
        return os.path.join(cwd, ".agres", "agres.db")

    def sql(self, cwd: str, query: str, args=()):
        """Run SQL directly on a v0 store (used to backdate rows / read originals)."""
        conn = sqlite3.connect(self.db(cwd))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    # ---------------------------------------------------------------- commands
    def start(self, cwd: str, goal: str) -> str:
        return self.run(cwd, "start", goal).strip().splitlines()[-1]

    def end(self, cwd: str, sid: str) -> None:
        self.run(cwd, "end", "--session", sid)

    def pin(self, cwd: str, sid: str, text: str) -> None:
        self.run(cwd, "pin", "--text", text, "--session", sid)

    def decision(self, cwd: str, sid: str, text: str, rejected: bool = False, flags: bool = True) -> None:
        a = ["decision", "--text", text, "--session", sid]
        if flags:
            a.append("--rejected" if rejected else "--accepted")
        self.run(cwd, *a)

    def task(self, cwd: str, sid: str, objective: str, status: str = "active") -> str:
        return self.run(cwd, "task", "--objective", objective, "--status", status, "--session", sid).strip()

    def error(self, cwd: str, sid: str, message: str, context: Optional[str] = None) -> None:
        a = ["error", "--message", message, "--session", sid]
        if context:
            a += ["--context", context]
        self.run(cwd, *a)

    def checkpoint(self, cwd: str, sid: str, reason: Optional[str] = None) -> None:
        a = ["checkpoint", "--session", sid]
        if reason is not None:
            a += ["--reason", reason]
        self.run(cwd, *a)

    def capture(self, cwd: str, sid: str, role: str, text: str) -> None:
        self.run(cwd, "capture", "--role", role, "--text", text, "--session", sid)

    def backdate(self, cwd: str, table: str, where: str, ts: str, col: str = "created_at", args=()) -> None:
        self.sql(cwd, f"UPDATE {table} SET {col}=? WHERE {where}", (ts, *args))

    # ------------------------------------------------------------------ fixtures
    def standard(self, cwd: str, goal: str = "Secure boot runbook") -> dict:
        """A small project history that exercises every mapping rule. Returns the session ids."""
        s1 = self.start(cwd, goal)
        self.pin(cwd, s1, "Never use a sacrificial board for the eFuse tests.")
        self.pin(cwd, s1, "Board root password lives in the vault, not in the repo.")
        self.decision(cwd, s1, "Use MQTT on port 1884 for the status bus.")
        self.decision(cwd, s1, "Hades serves the web console on port 80.")
        self.decision(cwd, s1, "Use a sacrificial board for fuse tests.", rejected=True)
        self.decision(cwd, s1, "Keep the contract package dependency free.")
        self.task(cwd, s1, "Write the secure boot runbook for the fresh dev board")
        self.task(cwd, s1, "Wire argus watchdog into the deploy script")
        self.task(cwd, s1, "Phase 5 complete: hardening verified", status="active")
        self.task(cwd, s1, "Port the converter to python", status="done")
        self.task(cwd, s1, "Drop legacy alexa bridge", status="completed")
        self.error(cwd, s1, "xknx tunnel drops after 20 min", context="seen on callisto only")
        self.error(cwd, s1, "AuthData wipe on redeploy is fixed and resolved")
        self.checkpoint(cwd, s1, "session end")
        self.checkpoint(cwd, s1, "callisto KNX outage fix verified on board; next: pulsar ETS categories")
        self.checkpoint(cwd, s1, "pre-compact")
        self.checkpoint(cwd, s1, "secure boot runbook drafted; awaiting fresh board")
        self.checkpoint(cwd, s1, "")
        self.capture(cwd, s1, "user", "please draft the secure boot runbook")
        self.capture(cwd, s1, "assistant", "Drafting the runbook now: step 1 is to read the OTP map.")
        self.capture(cwd, s1, "user", "no sacrificial board, find another way")
        self.end(cwd, s1)
        s2 = self.start(cwd, "pulsar ETS categorisation fix")
        self.pin(cwd, s2, "Pulsar converter output must stay byte-identical to v1.1 fixtures.")
        self.checkpoint(cwd, s2, "ETS categories fixed, 52 tests green")
        return {"sessions": [s1, s2]}
