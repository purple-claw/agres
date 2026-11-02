"""Settings: built-in defaults ← ~/.config/agres/config.json ← plugin options ← <root>/.agres/config.json.

Plugin options reach hooks as `CLAUDE_PLUGIN_OPTION_<KEY>` env vars (Claude Code `userConfig`); an
unset, empty or invalid one is ignored.

The per-project off switch is a flag file (`.agres/off`) so hooks can check it
without opening the database.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from . import paths

DEFAULTS: dict[str, Any] = {
    "auto_init": True,      # hooks create .agres/ at a git root on first use
    "budget_chars": 6000,   # injection pack size (hard max 9000)
    "guard": True,          # flag rejected ideas / critical pins that a prompt touches
    "capture": "all",       # all | prompts | none
    "turn_retention_days": 0,  # >0: session end deletes captured turns + tool folds older than this
    "redact_patterns": [],  # extra regexes redacted before storing (after the built-in shapes)
    "fold": True,           # origami: fold big tool output in the live window (PostToolUse)
    "fold_chars": 12000,    # fold a stdout/stderr longer than this
    "fold_head": 40,        # lines kept verbatim from the top / bottom of a folded output
    "fold_tail": 20,
    "fold_tools": ["Bash"],
    "skip_prefixes": [      # harness-generated prompts that aren't human turns
        "<task-notification", "<system-reminder", "<agent-message", "[SYSTEM NOTIFICATION",
        "<local-command-", "<command-name>", "Caveat: The messages below were generated",
    ],
}


def global_path() -> str:
    return os.environ.get("AGRES_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "agres", "config.json")


def project_path(root: str) -> str:
    return os.path.join(paths.store_dir(root), "config.json")


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


PLUGIN_OPTIONS = ("capture", "turn_retention_days", "budget_chars", "fold", "fold_chars")


def plugin_options(env: Optional[dict] = None) -> dict:
    env = os.environ if env is None else env
    out = {}
    for key in PLUGIN_OPTIONS:
        raw = env.get(f"CLAUDE_PLUGIN_OPTION_{key.upper()}") or env.get(f"CLAUDE_PLUGIN_OPTION_{key}")
        if raw is None or not str(raw).strip():
            continue
        try:
            out[key] = coerce(key, str(raw).strip())
        except ValueError:
            continue
    return out


def load(root: Optional[str] = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(_read(global_path()))
    cfg.update(plugin_options())
    if root:
        cfg.update(_read(project_path(root)))
    from .util import set_extra_patterns
    set_extra_patterns(cfg.get("redact_patterns") or [])
    return cfg


def save_global(values: dict) -> None:
    _write(global_path(), {**_read(global_path()), **values})


def save_project(root: str, values: dict) -> None:
    _write(project_path(root), {**_read(project_path(root)), **values})


def coerce(key: str, raw: str) -> Any:
    """CLI string → typed value matching the default's type."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r} (one of: {', '.join(DEFAULTS)})")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if raw.lower() in ("1", "true", "on", "yes"):
            return True
        if raw.lower() in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"{key} expects on/off")
    if isinstance(default, int):
        val = int(raw)
        if val < 0:
            raise ValueError(f"{key} must be ≥ 0")
        return val
    if isinstance(default, list):
        vals = [x.strip() for x in raw.split(",") if x.strip()]
        if key == "redact_patterns":
            import re
            for v in vals:
                try:
                    re.compile(v)
                except re.error as e:
                    raise ValueError(f"redact_patterns: {v!r} is not a valid regex ({e})")
        return vals
    if key == "capture" and raw not in ("all", "prompts", "none"):
        raise ValueError("capture expects all | prompts | none")
    return raw


def off_flag(root: str) -> str:
    return os.path.join(paths.store_dir(root), "off")


def is_off(root: str) -> bool:
    return os.path.exists(off_flag(root))


def set_off(root: str, off: bool) -> None:
    flag = off_flag(root)
    if off:
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as fh:
            fh.write("agres is off for this project; `agres on` re-enables it\n")
    elif os.path.exists(flag):
        os.remove(flag)
