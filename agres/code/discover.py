"""File discovery, read helpers and the cross-process sync lock.

Discovery never leaves the store root: `git ls-files` run inside it (so a store
at a sub-directory of a repo only sees that sub-tree), else a pruned walk that
refuses to descend into ignored, vendored or secret directories.
"""
from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from typing import Iterator, Optional

from ..util import BINARY_EXT, DEFAULT_IGNORE_DIRS, SECRET_DIRS, IgnoreRules, is_secret_path
from .store import Store

MAX_FILE_BYTES = 1_500_000
MAX_FILES = 40_000
NOISE_SUFFIXES = (".min.js", ".min.css", ".lock", "-lock.json")
EMPTY_OK = frozenset({"__init__.py", "__init__.pyi"})


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


def _in_ignored_dir(parts: list[str]) -> bool:
    """True if a directory component is vendored/generated, except Yocto's `build/conf/<file>`."""
    for i, p in enumerate(parts[:-1]):
        if p in DEFAULT_IGNORE_DIRS and not (p == "build" and len(parts) == i + 3 and parts[i + 1] == "conf"):
            return True
    return False


def indexable_name(rel: str, ign: IgnoreRules) -> bool:
    """Name-only filter shared by discover() and touch(): no stat, no read.

    Rejects ignored/vendored directories, the secret denylist, `.agresignore`
    matches, binaries, minified bundles and lockfiles.
    """
    if _in_ignored_dir(rel.split("/")):
        return False
    if is_secret_path(rel) or ign.ignored(rel):
        return False
    low = rel.lower()
    return os.path.splitext(low)[1] not in BINARY_EXT and not low.endswith(NOISE_SUFFIXES)


def git_listing(root: str) -> Optional[list[str]]:
    """Tracked + untracked-not-ignored files under `root`, or None when git can't answer."""
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return [p.decode("utf-8", "replace") for p in r.stdout.split(b"\0") if p and not p.endswith(b"/")]


def walk_listing(root: str, ign: IgnoreRules) -> Iterator[str]:
    """Pruned walk below `root`: ignored directories are never entered."""
    for dp, dns, fns in os.walk(root):
        rel_dir = os.path.relpath(dp, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        pre = f"{rel_dir}/" if rel_dir else ""
        if "build" in dns and os.path.isdir(os.path.join(dp, "build", "conf")):   # Yocto: build/conf/*.conf
            for fn in sorted(os.listdir(os.path.join(dp, "build", "conf"))):
                yield f"{pre}build/conf/{fn}"
        dns[:] = sorted(d for d in dns if d not in DEFAULT_IGNORE_DIRS and d.lower() not in SECRET_DIRS
                        and not ign.ignored(pre + d))
        for fn in sorted(fns):
            yield pre + fn


def file_state(root: str, rel: str) -> Optional[tuple[int, int]]:
    """(size, mtime_ns) of a regular, size-capped, non-empty file that really lives under `root`."""
    p = os.path.join(root, rel)
    try:
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            real = os.path.realpath(p)
            if not (real == root or real.startswith(root + os.sep)):
                return None
            st = os.stat(p)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
        return None
    if st.st_size == 0 and os.path.basename(rel) not in EMPTY_OK:  # empty `__init__.py` marks a package
        return None
    return st.st_size, st.st_mtime_ns


def discover(root: str) -> list[tuple[str, int, int]]:
    """Return [(relpath, size, mtime_ns)] for indexable text files under `root`."""
    ign = IgnoreRules(root)
    rels = git_listing(root)
    if rels is None:
        rels = list(walk_listing(root, ign))
    out_files: list[tuple[str, int, int]] = []
    seen = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        if not indexable_name(rel, ign):
            continue
        state = file_state(root, rel)
        if state is None:
            continue
        out_files.append((rel, state[0], state[1]))
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
