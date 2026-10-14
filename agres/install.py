"""`agres install | uninstall | doctor`: wire agres into Claude Code and Codex.

Install is a list of reversible actions recorded in a manifest under
~/.agres-archive/; nothing is deleted — replaced files and directories are moved
into the archive and `uninstall` moves them back.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from typing import Optional

from . import __version__, config, db, paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_SRC = os.path.join(REPO, "skill", "agres")
SHIM_SRC = os.path.join(REPO, "bin", "agres")
LEGACY_HOOKS = ("agres-touch.sh", "agres-session.sh", "agres-checkpoint.sh")
SPEC_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

CLAUDE_MD_BLOCK = """# Agres — project memory (hooks capture and inject; you record judgment)
- Hooks inject `# Agres memory` at session start and after /compact, and capture prompts and replies:
  never run `agres capture`.
- Record as you work: `agres decide|reject|pin [--critical]|task|note|handoff "..."`, close with
  `agres done|drop <ID>`; when a write lists a similar item, use `--replaces <ID>` instead of duplicating.
- Before re-exploring or re-asking: `agres search "<words>"`, `agres show <ID>`. Don't re-propose a
  rejected idea unless the user reopens it.
- "agres off" / "agres on" pauses or resumes it for the project. Details: skill `agres`.
"""


def H(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def hook_entries(agres_cmd: str) -> dict:
    def cmd(event: str, timeout: int, run_async: bool = False) -> dict:
        h = {"type": "command", "command": f"{agres_cmd} hook {event}", "timeout": timeout}
        if run_async:
            h["async"] = True
        return h

    return {
        "SessionStart": [{"matcher": "", "hooks": [cmd("session-start", 10)]}],
        "UserPromptSubmit": [{"hooks": [cmd("prompt", 5)]}],
        "Stop": [{"hooks": [cmd("stop", 30, True)]}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [cmd("edit", 60, True)]}],
        "PostCompact": [{"hooks": [cmd("post-compact", 30, True)]}],
        "SessionEnd": [{"matcher": "", "hooks": [cmd("session-end", 5)]}],
    }


def _is_agres_hook(h: dict) -> bool:
    c = h.get("command") or ""
    return bool(re.search(r"(^|/)agres hook ", c)) or any(x in c for x in LEGACY_HOOKS)


def strip_agres_hooks(settings: dict) -> dict:
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = []
        for g in hooks[event] or []:
            kept = [h for h in g.get("hooks", []) if not _is_agres_hook(h)]
            if kept:
                groups.append(dict(g, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def with_agres_hooks(settings: dict, agres_cmd: str) -> dict:
    settings = strip_agres_hooks(json.loads(json.dumps(settings)))
    hooks = settings.setdefault("hooks", {})
    for event, groups in hook_entries(agres_cmd).items():
        hooks.setdefault(event, []).extend(groups)
    return settings


def replace_claude_md_block(text: str) -> str:
    """Swap the `# Agres…` top-level section (to the next `# ` heading) for the 1.0 block."""
    return swap_section(text, CLAUDE_MD_BLOCK)


# --------------------------------------------------------------------- plan
class Plan:
    def __init__(self):
        self.ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.archive = H(".agres-archive", f"install-{self.ts}")
        self.steps: list[tuple[str, str, dict]] = []  # (kind, description, data)

    def add(self, kind: str, desc: str, **data) -> None:
        self.steps.append((kind, desc, data))


def _points_at(link: str, other: str) -> bool:
    """True if `link` is a symlink whose own target (one hop) is `other`."""
    if not os.path.islink(link) or link == other:
        return False
    t = os.readlink(link)
    t = t if os.path.isabs(t) else os.path.join(os.path.dirname(link), t)
    return os.path.normpath(t) == os.path.normpath(other)


def _link_target(path: str) -> Optional[str]:
    return os.path.realpath(path) if os.path.lexists(path) else None


def build_plan() -> Plan:
    p = Plan()
    shim = H(".local", "bin", "agres")
    if _link_target(shim) != os.path.realpath(SHIM_SRC):
        p.add("link", f"{shim} -> {SHIM_SRC}", path=shim, target=SHIM_SRC)
    for link in (H(".agent-skills", "agres"), H(".claude", "skills", "agres"), H(".codex", "skills", "agres")):
        if not os.path.isdir(os.path.dirname(link)):
            continue
        if _link_target(link) == os.path.realpath(SKILL_SRC) or _points_at(link, H(".agent-skills", "agres")):
            continue  # already ours, or chains through ~/.agent-skills/agres (re-linked above)
        p.add("link", f"{link} -> {SKILL_SRC}", path=link, target=SKILL_SRC)
    settings = H(".claude", "settings.json")
    if os.path.isdir(H(".claude")):
        cur = _read_json_strict(settings)
        new = with_agres_hooks(cur, shim)
        if new != cur:
            p.add("settings", f"{settings}: agres hooks for SessionStart, UserPromptSubmit, Stop, PostToolUse, "
                              "PostCompact, SessionEnd (legacy agres-*.sh entries removed)", path=settings, data=new)
        for name in LEGACY_HOOKS:
            f = H(".claude", "hooks", name)
            if os.path.exists(f):
                p.add("archive", f"archive legacy hook script {f}", path=f)
        cmd = H(".claude", "commands", "agres.md")
        if os.path.exists(cmd):
            p.add("archive", f"archive {cmd} (old fold protocol; /agres now loads the skill)", path=cmd)
        md = H(".claude", "CLAUDE.md")
        if os.path.exists(md):
            with open(md, encoding="utf-8") as fh:
                text = fh.read()
            new_text = replace_claude_md_block(text)
            if new_text != text:
                p.add("write", f"{md}: replace the Agres section with the 1.0 block", path=md, text=new_text)
    return p


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _read_json_strict(path: str) -> dict:
    """Missing file -> {}; unparseable -> refuse, never overwrite a file we can't read."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        raise ValueError(f"refusing to touch {path}: not valid JSON ({e})") from None
    if not isinstance(data, dict):
        raise ValueError(f"refusing to touch {path}: top level is not an object")
    return data


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: str, body: str) -> None:
    """Write through symlinks, keep the original file mode (settings.json may hold tokens)."""
    real = os.path.realpath(path)
    mode = stat.S_IMODE(os.stat(real).st_mode) if os.path.exists(real) else 0o600
    tmp = f"{real}.agres-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def _archive(path: str, archive: str, manifest: list) -> None:
    dest = os.path.join(archive, os.path.relpath(path, H()).replace(os.sep, "__"))
    os.makedirs(archive, exist_ok=True)
    shutil.move(path, dest)
    manifest.append({"op": "moved", "from": path, "to": dest})


def _save_manifest(p: Plan, manifest: list) -> None:
    os.makedirs(p.archive, exist_ok=True)
    with open(os.path.join(p.archive, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": __version__, "ts": p.ts, "actions": manifest}, fh, indent=2)


def apply(p: Plan) -> list:
    """Run the plan; the manifest is rewritten after every step so a half-done install can be undone."""
    manifest: list = []
    for kind, _, d in p.steps:
        path = d["path"]
        if kind == "link":
            if os.path.lexists(path):
                if os.path.islink(path):
                    manifest.append({"op": "unlinked", "path": path, "was": os.readlink(path)})
                    os.unlink(path)
                else:
                    _archive(path, p.archive, manifest)
                _save_manifest(p, manifest)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(d["target"], path)
            manifest.append({"op": "linked", "path": path})
        elif kind in ("settings", "write"):
            body = json.dumps(d["data"], indent=2) + "\n" if kind == "settings" else d["text"]
            entry = {"op": "replaced", "kind": kind, "path": path, "backup": None, "sha": _sha(body)}
            if os.path.exists(path):
                os.makedirs(p.archive, exist_ok=True)
                entry["backup"] = os.path.join(p.archive, os.path.basename(path))
                shutil.copy2(os.path.realpath(path), entry["backup"])
            manifest.append(entry)
            _save_manifest(p, manifest)
            _atomic_write(path, body)
        elif kind == "archive":
            _archive(path, p.archive, manifest)
        _save_manifest(p, manifest)
    return manifest


def swap_section(text: str, block: str) -> str:
    """Replace the `# Agres…` top-level section of `text` with `block` (appending if absent)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(r"#\s+Agres\b", l)), None)
    if start is None:
        return text.rstrip("\n") + "\n\n" + block if block else text
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"#\s", lines[i])), len(lines))
    tail = "".join(lines[end:])
    return "".join(lines[:start]) + block + ("\n" + tail if tail and block else tail)


def agres_section(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(r"#\s+Agres\b", l)), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"#\s", lines[i])), len(lines))
    return "".join(lines[start:end]).rstrip("\n") + "\n"


def _undo_replaced(a: dict, dry_run: bool) -> str:
    """Never copy a stale backup over a live file: re-apply only agres's own change in reverse."""
    path, backup = a["path"], a.get("backup")
    if a.get("kind") == "settings" or path.endswith("settings.json"):
        cur = _read_json_strict(path)
        new = strip_agres_hooks(json.loads(json.dumps(cur)))
        old = _read_json(backup) if backup else {}
        for event, groups in (old.get("hooks") or {}).items():  # put back the legacy hooks install removed
            legacy = [g for g in groups if any(_is_agres_hook(h) for h in g.get("hooks", []))]
            if legacy:
                new.setdefault("hooks", {}).setdefault(event, []).extend(legacy)
        if not dry_run:
            _atomic_write(path, json.dumps(new, indent=2) + "\n")
        return f"{path}: agres 1.0 hooks removed, legacy hooks restored, everything else kept"
    with open(path, encoding="utf-8") as fh:
        cur = fh.read()
    old = open(backup, encoding="utf-8").read() if backup and os.path.exists(backup) else ""
    if _sha(cur) == a.get("sha"):
        if not dry_run:
            _atomic_write(path, old)
        return f"{path}: restored (unchanged since install)"
    if not dry_run:
        _atomic_write(path, swap_section(cur, agres_section(old)))
    return f"{path}: changed since install — only the Agres section reverted, your edits kept"


def uninstall(dry_run: bool = False) -> list[str]:
    """Undo the most recent install manifest, newest action first; also strip any agres hooks left."""
    base = H(".agres-archive")
    runs = sorted(d for d in os.listdir(base) if d.startswith("install-")
                  and os.path.exists(os.path.join(base, d, "manifest.json"))) if os.path.isdir(base) else []
    done: list[str] = []
    if runs:
        with open(os.path.join(base, runs[-1], "manifest.json"), encoding="utf-8") as fh:
            actions = json.load(fh)["actions"]
        for a in reversed(actions):
            if a["op"] == "linked" and os.path.islink(a["path"]):
                done.append(f"remove link {a['path']}")
                if not dry_run:
                    os.unlink(a["path"])
            elif a["op"] == "unlinked":
                done.append(f"restore link {a['path']} -> {a['was']}")
                if not dry_run and not os.path.lexists(a["path"]):
                    os.symlink(a["was"], a["path"])
            elif a["op"] == "moved":
                done.append(f"restore {a['from']}")
                if not dry_run and not os.path.lexists(a["from"]):
                    shutil.move(a["to"], a["from"])
            elif a["op"] == "replaced" and os.path.exists(a["path"]):
                done.append(_undo_replaced(a, dry_run))
        if not dry_run:
            os.rename(os.path.join(base, runs[-1]), os.path.join(base, "undone-" + runs[-1]))
    settings = H(".claude", "settings.json")
    cur = _read_json(settings)
    if not runs and cur and strip_agres_hooks(json.loads(json.dumps(cur))) != cur:
        done.append(f"remove agres hooks from {settings}")
        if not dry_run:
            _atomic_write(settings, json.dumps(strip_agres_hooks(cur), indent=2) + "\n")
    return done


# ------------------------------------------------------------------- doctor
def doctor(cwd: Optional[str] = None) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    out.append((sys.version_info >= (3, 9), f"python {sys.version.split()[0]} (needs ≥ 3.9)"))
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        out.append((True, "sqlite FTS5 available"))
    except sqlite3.OperationalError:
        out.append((True, "sqlite without FTS5 — search falls back to LIKE (slower, still works)"))
    which = shutil.which("agres")
    out.append((bool(which) and os.path.realpath(which) == os.path.realpath(SHIM_SRC),
                f"`agres` on PATH → {os.path.realpath(which) if which else 'missing'} (want {SHIM_SRC})"))
    skill_md = os.path.join(SKILL_SRC, "SKILL.md")
    out.append(_check_skill(skill_md))
    for link in (H(".claude", "skills", "agres"), H(".codex", "skills", "agres"), H(".agent-skills", "agres")):
        if os.path.lexists(link) or os.path.isdir(os.path.dirname(link)):
            ok = _link_target(link) == os.path.realpath(SKILL_SRC)
            out.append((ok, f"skill {link} → {_link_target(link) or 'missing'}"))
    settings = _read_json(H(".claude", "settings.json"))
    cmds = [h.get("command", "") for gs in (settings.get("hooks") or {}).values() for g in gs for h in g.get("hooks", [])]
    for event, groups in hook_entries("agres").items():
        want = groups[0]["hooks"][0]["command"].split(" ", 1)[1]
        n = sum(1 for c in cmds if c.endswith(want) and _is_agres_hook({"command": c}))
        out.append((n == 1, f"hook {event}: {n} × `{want}`" + ("" if n == 1 else " (want exactly 1)")))
    legacy = [c for c in cmds if any(x in c for x in LEGACY_HOOKS)]
    out.append((not legacy, "no legacy agres-*.sh hooks" if not legacy else f"legacy hooks still wired: {legacy}"))
    root = paths.find_root(cwd or os.getcwd())
    if root:
        try:
            with db.Store(root) as s:
                out.append((True, f"project {root}: v1 store, {s.one('SELECT COUNT(*) FROM items')[0]} items"
                                  + (" (OFF)" if config.is_off(root) else "")))
        except db.V0Store:
            out.append((False, f"project {root}: v0 store — run `agres migrate`"))
        except db.NoStore:
            out.append((True, f"project {root}: no store yet (created on first session)"))
        except db.NewerSchema as e:
            out.append((False, str(e)))
    return out


def _check_skill(skill_md: str) -> tuple[bool, str]:
    try:
        with open(skill_md, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False, f"skill file missing: {skill_md}"
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return False, "SKILL.md has no frontmatter"
    keys = re.findall(r"^([A-Za-z][\w-]*):", m.group(1), re.M)
    desc = re.search(r"^description:\s*(.+?)(?=^\S|\Z)", m.group(1), re.M | re.S)
    desc_len = len(" ".join(desc.group(1).split()).strip("\"'")) if desc else 0
    bad = sorted(set(keys) - SPEC_FRONTMATTER)
    ok = not bad and "name" in keys and 0 < desc_len <= 1024
    return ok, f"SKILL.md frontmatter: keys {keys}, description {desc_len} chars" + (f", unsupported {bad}" if bad else "")


# ---------------------------------------------------------------------- cli
def cli(a) -> int:
    if a.cmd == "doctor":
        bad = 0
        for ok, msg in doctor():
            print(("✓ " if ok else "✗ ") + msg)
            bad += not ok
        return 1 if bad else 0
    if a.cmd == "uninstall":
        steps = uninstall(dry_run=a.dry_run)
        print("\n".join(steps) if steps else "nothing to undo")
        return 0
    try:
        plan = build_plan()
    except ValueError as e:
        print(f"agres install: {e}")
        return 1
    if not plan.steps:
        print("already installed; `agres doctor` checks the wiring")
        return 0
    for _, desc, _ in plan.steps:
        print(f"- {desc}")
    if a.dry_run or not a.yes:
        print("\n(dry run — re-run with --yes to apply; replaced files go to ~/.agres-archive/)")
        return 0
    apply(plan)
    print(f"\ninstalled. backups and manifest: {plan.archive}")
    return 0
