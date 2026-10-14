"""Where a project's memory lives.

Root = nearest ancestor holding `.agres/agres.db` or a `.git` entry (dir, or file
for worktrees/submodules). `/`, $HOME and $HOME's ancestors are never roots, so
a session started in $HOME can't grow a global junk-drawer store.
"""
from __future__ import annotations

import os
from typing import Optional

STORE_DIR = ".agres"
DB_NAME = "agres.db"


def home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def is_forbidden_root(path: str) -> bool:
    p = os.path.realpath(path)
    h = home()
    if p == os.path.dirname(p):  # filesystem root
        return True
    return p == h or h.startswith(p.rstrip(os.sep) + os.sep)


def store_dir(root: str) -> str:
    return os.path.join(root, STORE_DIR)


def db_path(root: str) -> str:
    return os.path.join(root, STORE_DIR, DB_NAME)


def find_root(start: Optional[str] = None) -> Optional[str]:
    env = os.environ.get("AGRES_ROOT")
    if env:
        return os.path.realpath(env)
    cur = os.path.realpath(start or os.getcwd())
    while not is_forbidden_root(cur):
        if os.path.isfile(db_path(cur)) or os.path.exists(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return None


def git_root(start: Optional[str] = None) -> Optional[str]:
    """Nearest ancestor with a `.git` entry (ignoring any .agres stores on the way)."""
    cur = os.path.realpath(start or os.getcwd())
    while not is_forbidden_root(cur):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return None
