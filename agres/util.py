"""Small shared helpers: time, hashing, token estimates, identifier splitting."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import os
import re

_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z0-9]+")

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".idea",
    ".vscode", "build", "dist", "target", "out", ".next", ".nuxt", ".dart_tool",
    ".pub-cache", "Pods", ".cache", "coverage", ".agres", "vendor", "bower_components",
    ".terraform", ".serverless", "site-packages", ".eggs", ".svelte-kit", ".turbo",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".svgz",
    ".pdf", ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".class", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".obj", ".bin", ".dat",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi",
    ".mkv", ".wav", ".ogg", ".flac", ".apk", ".aab", ".ipa", ".pyc", ".pyo",
    ".db", ".sqlite", ".sqlite3", ".lock", ".wasm", ".psd", ".ai", ".sketch",
    ".map", ".min.js", ".min.css",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> _dt.datetime:
    return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def epoch_to_iso(ts: int | float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(ts: str | None) -> str:
    """Human relative time for an ISO timestamp ('3h ago')."""
    if not ts:
        return "never"
    try:
        delta = (_dt.datetime.now(_dt.timezone.utc) - parse_iso(ts)).total_seconds()
    except ValueError:
        return ts
    if delta < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit} ago"
    return "just now"


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


_tiktoken = None
if os.environ.get("AGRES_TOKENIZER") == "tiktoken":  # optional, never required
    try:  # pragma: no cover - optional dependency
        import tiktoken

        _tiktoken = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tiktoken = None


def est_tokens(text: str) -> int:
    """Token estimate. ~3.5 chars/token is a conservative blend for code+prose.

    Deliberately slightly pessimistic so injected packs stay inside budgets.
    """
    if not text:
        return 0
    if _tiktoken is not None:  # pragma: no cover
        return len(_tiktoken.encode(text, disallowed_special=()))
    return int(len(text) / 3.5) + 1


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def split_identifier(name: str) -> list[str]:
    """getUserToken / get_user_token / HTTPServer -> lowercase words."""
    s = _CAMEL_2.sub(r"\1 \2", _CAMEL_1.sub(r"\1 \2", name))
    return [w.lower() for w in _WORD.findall(s)]


def search_text(*parts: str | None) -> str:
    """Text for the FTS index: originals plus identifier sub-words."""
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        out.append(p)
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", p):
            words = split_identifier(tok)
            if len(words) > 1:
                out.append(" ".join(words))
    return " ".join(out)


class IgnoreRules:
    """Minimal .agresignore support (gitignore-lite: globs, dir names, comments)."""

    def __init__(self, root: str):
        self.patterns: list[str] = []
        p = os.path.join(root, ".agresignore")
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.patterns.append(line.rstrip("/"))

    def ignored(self, rel: str) -> bool:
        base = os.path.basename(rel)
        parts = rel.split("/")
        for pat in self.patterns:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat):
                return True
            if any(fnmatch.fnmatch(part, pat) for part in parts[:-1]):
                return True
        return False


def is_test_path(path: str) -> bool:
    p = path.lower()
    name = os.path.basename(p)
    if re.search(r"(^|/)(tests?|__tests__|spec|specs|e2e|testing)(/|$)", p):
        return True
    return bool(
        re.match(r"(test_.*|.*_test\.\w+|.*\.test\.\w+|.*\.spec\.\w+|.*_spec\.\w+|.*tests?\.\w+)$", name)
    )


# ------------------------------------------------------------------ privacy
_PRIVATE = re.compile(r"<private>.*?(?:</private>|\Z)", re.S | re.I)
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
]


def redact(text: str) -> str:
    """Strip <private>…</private> spans and well-known secret shapes before storing."""
    if not text:
        return text
    text = _PRIVATE.sub("[private]", text)
    for pat in _SECRETS:
        text = pat.sub("[redacted]", text)
    return text


_STOP = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it its
just let me more my no not of on only or our out over so some than that the their them then there these
they this to too up us use used using was we were what when where which while who why will with would you
your also any all after before about should must need want get got make made new now via per yes""".split())


def terms(text: str) -> set[str]:
    """Significant lowercase terms: words of 3+ chars minus stopwords, plus numbers."""
    out = set()
    for w in re.findall(r"[A-Za-z0-9_]+", text or ""):
        lw = w.lower()
        if lw.isdigit():
            if len(lw) >= 2:
                out.add(lw)
        elif len(lw) >= 3 and lw not in _STOP:
            out.add(lw)
            for sub in split_identifier(w):
                if len(sub) >= 3 and sub not in _STOP:
                    out.add(sub)
    return out
