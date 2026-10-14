"""File discovery, read helpers and the cross-process sync lock."""
from __future__ import annotations

import contextlib
import os
import subprocess
from typing import Iterator, Optional

from ..util import BINARY_EXT, DEFAULT_IGNORE_DIRS, IgnoreRules
from .store import Store

MAX_FILE_BYTES = 1_500_000
MAX_FILES = 40_000


@contextlib.contextmanager
def sync_lock(store: Store) -> Iterator[bool]:
    """Cross-process lock so concurrent syncs (hooks + agent) don't collide."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-posix
        yield True
        return
    path = os.path.join(store.dir, "sync.lock")
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


# ------------------------------------------------------------------ discovery


def discover(root: str) -> list[tuple[str, int, int]]:
    """Return [(relpath, size, mtime_ns)] for indexable text files."""
    ign = IgnoreRules(root)
    rels: list[str] = []
    if os.path.isdir(os.path.join(root, ".git")):
        try:
            out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                 capture_output=True, timeout=60, check=True).stdout
            rels = [p.decode("utf-8", "replace") for p in out.split(b"\0") if p]
        except (subprocess.SubprocessError, OSError):
            rels = []
    if not rels:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".git")]
            for fn in fns:
                rels.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
    out_files: list[tuple[str, int, int]] = []
    seen = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        parts = rel.split("/")
        if any(p in DEFAULT_IGNORE_DIRS for p in parts[:-1]) or ign.ignored(rel):
            continue
        low = rel.lower()
        if os.path.splitext(low)[1] in BINARY_EXT or low.endswith((".min.js", ".min.css", ".lock", "-lock.json")):
            continue
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        if not os.path.isfile(os.path.join(root, rel)) or st.st_size > MAX_FILE_BYTES or st.st_size == 0:
            continue
        out_files.append((rel, st.st_size, st.st_mtime_ns))
        if len(out_files) >= MAX_FILES:
            break
    out_files.sort()
    return out_files


def read_text(abs_path: str) -> tuple[Optional[bytes], Optional[str]]:
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, None
    if b"\0" in data[:4096]:
        return None, None
    return data, data.decode("utf-8", "replace")
