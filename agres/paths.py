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


def _git_owner(cur: str) -> Optional[str]:
    """Root that owns the `.git` entry in `cur`: `cur` itself, or for a linked worktree
    (`.git` file whose gitdir has a `commondir`) the main checkout — worktrees get removed,
    and memory must not go with them. Submodules (no commondir) stay their own root."""
    g = os.path.join(cur, ".git")
    if os.path.isdir(g):
        return cur
    if not os.path.isfile(g):
        return None
    try:
        with open(g, encoding="utf-8") as fh:
            line = fh.readline().strip()
        if not line.startswith("gitdir:"):
            return cur
        gitdir = line.split(":", 1)[1].strip()
        gitdir = os.path.realpath(gitdir if os.path.isabs(gitdir) else os.path.join(cur, gitdir))
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            common = fh.read().strip()
    except OSError:
        return cur
    common = os.path.realpath(common if os.path.isabs(common) else os.path.join(gitdir, common))
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else None
    return main if main and os.path.isdir(main) and not is_forbidden_root(main) else cur


def find_root(start: Optional[str] = None) -> Optional[str]:
    env = os.environ.get("AGRES_ROOT")
    if env:
        return os.path.realpath(env)
    cur = os.path.realpath(start or os.getcwd())
    while not is_forbidden_root(cur):
        if os.path.isfile(db_path(cur)):
            return cur
        owner = _git_owner(cur)
        if owner:
            return owner
        cur = os.path.dirname(cur)
    return None


def git_root(start: Optional[str] = None) -> Optional[str]:
    """Nearest ancestor with a `.git` entry (ignoring any .agres stores on the way)."""
    cur = os.path.realpath(start or os.getcwd())
    while not is_forbidden_root(cur):
        owner = _git_owner(cur)
        if owner:
            return owner
        cur = os.path.dirname(cur)
    return None
