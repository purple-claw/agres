"""`agres hook <event>`: the mechanical half of Agres, driven by Claude Code hooks.

Reads the hook's JSON on stdin. Always exits 0: a memory tool must never block
or break a session. Failures go to `.agres/hook.log`. Output is printed only
where it reaches the model (SessionStart, UserPromptSubmit); Stop must stay
silent — any context there would make the conversation continue.

| event         | Claude Code event | does                                                    |
|---------------|-------------------|---------------------------------------------------------|
| session-start | SessionStart      | upsert session, inject the pack (also after /compact)   |
| prompt        | UserPromptSubmit  | on/off phrases, capture prompt, rejection guard         |
| stop          | Stop              | capture the final reply (deduped)                       |
| edit          | PostToolUse       | re-index edited files                                   |
| post-compact  | PostCompact       | keep the compaction summary as the handoff              |
| session-end   | SessionEnd        | close session, auto-handoff if the agent wrote none     |
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import traceback
from typing import Any, Optional, TextIO

from . import config, db, memory, pack, paths
from .util import truncate

ALIASES = {"SessionStart": "session-start", "UserPromptSubmit": "prompt", "Stop": "stop",
           "PostToolUse": "edit", "PostCompact": "post-compact", "SessionEnd": "session-end"}
CLAUDE_NAME = {v: k for k, v in ALIASES.items()}
SYNC_EVENTS = {"session-start", "prompt", "session-end"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

OFF_PHRASES = ("stop using agres", "disable agres", "agres off", "stop agres", "turn off agres", "turn agres off")
ON_PHRASES = ("enable agres", "use agres", "agres on", "turn on agres", "turn agres on", "start using agres")
TOGGLE_MAX = 120
GUARD_MAX = 3
GUARD_CHARS = 600
LOG_MAX = 256 * 1024

OFF_NOTICE = 'Agres is off for this project: no memory injection or capture until the user says "agres on".'
ON_NOTICE = "Agres is on again for this project; memory capture and injection resume."
V0_NOTICE = ("This project has an Agres v0 store (.agres/agres.db). Its memory is not injected until it is "
             "upgraded with `agres migrate`.")
NEWER_NOTICE = "This project's Agres store was written by a newer agres; upgrade agres to use it."


class _Ctx:
    def __init__(self, event: str, payload: dict):
        self.event = event
        self.p = payload
        self.sid = payload.get("session_id") or ""
        self.cwd = payload.get("cwd") or os.getcwd()
        self.root = paths.find_root(self.cwd)
        self.cfg = config.load(self.root)
        self.state: Optional[str] = None  # "v0" | "newer" when the store can't be used
        self._store: Optional[db.Store] = None

    def store(self, create_ok: bool) -> Optional[db.Store]:
        if self._store or self.root is None:
            return self._store
        busy = 2000 if self.event in SYNC_EVENTS else 10000
        try:
            self._store = db.Store(self.root, create=create_ok and bool(self.cfg.get("auto_init")), busy_ms=busy)
        except db.NoStore:
            return None
        except db.V0Store:
            self.state = "v0"
        except db.NewerSchema:
            self.state = "newer"
        return self._store

    def close(self) -> None:
        if self._store:
            self._store.close()


# ------------------------------------------------------------------ handlers
def on_session_start(c: _Ctx) -> Optional[str]:
    if c.root is None:
        return None
    if config.is_off(c.root):
        return OFF_NOTICE
    s = c.store(create_ok=True)
    if s is None:
        return {"v0": V0_NOTICE, "newer": NEWER_NOTICE}.get(c.state or "")
    if c.sid:
        memory.touch_session(s, c.sid, agent="claude-code", cwd=c.cwd, title=c.p.get("session_title"))
    notice = "(re-injected after context compaction)" if c.p.get("source") == "compact" else None
    return pack.build(s, budget_chars=int(c.cfg.get("budget_chars") or pack.DEFAULT_CHARS), notice=notice)


def on_prompt(c: _Ctx) -> Optional[str]:
    text = c.p.get("prompt") or ""
    if c.root is None or not text.strip() or _is_harness(text, c.cfg):
        return None
    toggle = _toggle(text)
    if toggle == "off":
        config.set_off(c.root, True)
        return OFF_NOTICE
    if toggle == "on" and config.is_off(c.root):
        config.set_off(c.root, False)
        return ON_NOTICE
    if config.is_off(c.root):
        return None
    s = c.store(create_ok=True)
    if s is None:
        return None
    new = True
    if c.cfg.get("capture") in ("all", "prompts"):
        new = memory.add_turn(s, c.sid, "user", text, prompt_id=c.p.get("prompt_id")) is not None
    if c.sid:
        memory.touch_session(s, c.sid, agent="claude-code", cwd=c.cwd, prompt=new)
    return _guard(s, c.sid, text) if c.cfg.get("guard") else None


def on_stop(c: _Ctx) -> None:
    if c.root is None or config.is_off(c.root) or c.cfg.get("capture") != "all":
        return None
    s = c.store(create_ok=False)
    if s is not None:
        memory.add_turn(s, c.sid, "assistant", c.p.get("last_assistant_message") or "",
                        prompt_id=c.p.get("prompt_id"))
    return None


def on_edit(c: _Ctx) -> None:
    files = _edited_files(c.p, c.cwd)
    if not files or c.root is None or config.is_off(c.root):
        return None
    s = c.store(create_ok=False)
    if s is None:
        return None
    _touch_files(s, files)
    if c.sid:
        key = f"files:{c.sid}"
        seen = json.loads(s.get_meta(key) or "[]")
        for f in files:
            rel = os.path.relpath(f, c.root)
            if not rel.startswith("..") and rel not in seen:
                seen.append(rel)
        s.set_meta(key, json.dumps(seen[-100:]))
    return None


def on_post_compact(c: _Ctx) -> None:
    summary = (c.p.get("compact_summary") or "").strip()
    if not summary or c.root is None or config.is_off(c.root):
        return None
    s = c.store(create_ok=False)
    if s is not None:
        memory.add_handoff(s, summary, source="compact", session_id=c.sid or None)
    return None


def on_session_end(c: _Ctx) -> None:
    if c.root is None or not c.sid:
        return None
    s = c.store(create_ok=False)
    if s is None:
        return None
    memory.end_session(s, c.sid, c.p.get("reason"))
    meta_keys = (f"files:{c.sid}", f"guard:{c.sid}")
    files = json.loads(s.get_meta(meta_keys[0]) or "[]")
    s.x("DELETE FROM meta WHERE key IN (?,?)", meta_keys)
    if config.is_off(c.root) or s.one("SELECT 1 FROM items WHERE kind='handoff' AND session_id=?", (c.sid,)):
        return None
    prompts = s.q("SELECT text FROM turns WHERE session_id=? AND role='user' ORDER BY id", (c.sid,))
    if not prompts:
        return None
    recorded = [memory.fmt_id(r["kind"], r["id"]) for r in
                s.q("SELECT id, kind FROM items WHERE session_id=? AND kind!='handoff' ORDER BY id", (c.sid,))]
    parts = [f"Auto-handoff (session ended: {c.p.get('reason') or 'unknown'}; no handoff was written). "
             f"{len(prompts)} prompt(s). Last request: \"{truncate(prompts[-1]['text'], 200)}\"."]
    if files:
        parts.append("Files edited: " + ", ".join(files[:8]) + (f" (+{len(files) - 8} more)." if len(files) > 8 else "."))
    if recorded:
        parts.append("Recorded: " + ", ".join(recorded[:12]) + ".")
    memory.add_handoff(s, " ".join(parts), source="auto", session_id=c.sid)
    return None


HANDLERS = {"session-start": on_session_start, "prompt": on_prompt, "stop": on_stop, "edit": on_edit,
            "post-compact": on_post_compact, "session-end": on_session_end}


# ------------------------------------------------------------------- helpers
def _is_harness(text: str, cfg: dict) -> bool:
    head = text.lstrip()[:200]
    return any(head.startswith(p) for p in cfg.get("skip_prefixes") or ())


def _toggle(text: str) -> Optional[str]:
    norm = " ".join(text.lower().split()).strip(" .!?\"'`")
    for verdict, phrases in (("off", OFF_PHRASES), ("on", ON_PHRASES)):
        for ph in phrases:
            if norm.startswith(ph) or (ph in norm and len(norm) <= TOGGLE_MAX):
                return verdict
    return None


def _guard(s: db.Store, sid: str, text: str) -> Optional[str]:
    hits = memory.similar(s, text, kinds=["decision", "pin"], limit=8, min_overlap=2, min_ratio=0.5, basis="item")
    hits = [h for h in hits if (h["kind"] == "decision" and h["status"] == "rejected")
            or (h["kind"] == "pin" and h["priority"] > 0)]
    key = f"guard:{sid}"
    seen = set(json.loads(s.get_meta(key) or "[]")) if sid else set()
    hits = [h for h in hits if h["id"] not in seen][:GUARD_MAX]
    if not hits:
        return None
    lines = ["Agres memory relevant to this request:"]
    for h in hits:
        if h["kind"] == "decision":
            body = h["text"] + (f" (why: {h['why']})" if h["why"] else "")
            line = f"- D{h['id']} was rejected earlier: {truncate(body, 170)}"
        else:
            line = f"- P{h['id']} constraint: {truncate(h['text'], 170)}"
        if sum(len(x) + 1 for x in lines) + len(line) > GUARD_CHARS:
            break
        lines.append(line)
    if sid:
        s.set_meta(key, json.dumps(sorted(seen | {h["id"] for h in hits})))
    return "\n".join(lines) if len(lines) > 1 else None


def _edited_files(p: dict, cwd: str) -> list[str]:
    tool = p.get("tool_name") or ""
    ti = p.get("tool_input") or {}
    found: list[str] = []
    if tool in EDIT_TOOLS:
        found = [ti.get("file_path") or ti.get("notebook_path") or ""]
    elif tool == "Bash":
        diff = (p.get("tool_response") or {}).get("bashEditDiff") if isinstance(p.get("tool_response"), dict) else None
        found = _paths_in(diff)
    out = []
    for f in found:
        if isinstance(f, str) and f.strip():
            f = f if os.path.isabs(f) else os.path.join(cwd, f)
            if f not in out:
                out.append(os.path.normpath(f))
    return out


def _paths_in(obj: Any) -> list[str]:
    """bashEditDiff's shape isn't documented: accept lists/dicts with file_path/filePath/path keys,
    or a dict keyed by path."""
    out: list[str] = []
    if isinstance(obj, list):
        for x in obj:
            out += _paths_in(x)
    elif isinstance(obj, dict):
        for k in ("file_path", "filePath", "path"):
            if isinstance(obj.get(k), str):
                out.append(obj[k])
        if not out:
            out += [k for k in obj if isinstance(k, str) and ("/" in k or "." in k)]
    return out


def _touch_files(store: db.Store, files: list[str]) -> int:
    try:
        from .code import engine  # lazy: the prompt/stop path never pays for the code engine
    except ImportError:
        return 0
    return engine.touch(store, files)


def _log(root: Optional[str], event: str, err: str) -> None:
    sys.stderr.write(f"agres hook {event}: {err.strip().splitlines()[-1] if err.strip() else err}\n")
    if not root or not os.path.isdir(paths.store_dir(root)):
        return
    path = os.path.join(paths.store_dir(root), "hook.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {ts} {event}\n{err}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------- main
def main(event: str, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    ctx: Optional[_Ctx] = None
    name = event
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        if event == "auto":
            name = payload.get("hook_event_name") or ""
        name = ALIASES.get(name, name)
        handler = HANDLERS.get(name)
        if handler is None:
            return 0
        ctx = _Ctx(name, payload)
        text = handler(ctx)
        if text and name in ("session-start", "prompt"):
            stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": CLAUDE_NAME[name],
                                                            "additionalContext": text}}))
    except Exception:
        _log(ctx.root if ctx else None, name, traceback.format_exc())
    finally:
        if ctx:
            ctx.close()
    return 0
