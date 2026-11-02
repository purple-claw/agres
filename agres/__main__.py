import sys

# Origami's fold hook runs synchronously after every Bash call. Most outputs are small: decide that
# with json alone and exit before importing the CLI, the store and the code-graph schema.
FAST_MIN_CHARS = 1000  # fold.tool_fold never folds below this, whatever fold_chars says
DEFAULT_FOLD_CHARS = 12000


def _cfg_file(path):
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fast_threshold(env, cwd):
    """Smallest fold_chars any config layer could apply (global file, plugin option, nearest project
    file), or None when folding is off in the layer that wins. Errs low: a lower threshold only means
    the full hook decides."""
    import os
    layers = [_cfg_file(env.get("AGRES_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "agres",
                                                                   "config.json"))]
    opt = {}
    raw = env.get("CLAUDE_PLUGIN_OPTION_FOLD_CHARS") or env.get("CLAUDE_PLUGIN_OPTION_fold_chars")
    if raw:
        try:
            opt["fold_chars"] = int(float(raw))
        except ValueError:
            pass
    raw = env.get("CLAUDE_PLUGIN_OPTION_FOLD") or env.get("CLAUDE_PLUGIN_OPTION_fold")
    if raw and raw.strip().lower() in ("0", "false", "off", "no"):
        opt["fold"] = False
    elif raw and raw.strip().lower() in ("1", "true", "on", "yes"):
        opt["fold"] = True
    layers.append(opt)
    d = cwd
    while d and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, ".agres")):
            layers.append(_cfg_file(os.path.join(d, ".agres", "config.json")))
            break
        if os.path.exists(os.path.join(d, ".git")):
            break
        d = os.path.dirname(d)
    on, chars = True, DEFAULT_FOLD_CHARS
    for layer in layers:
        if "fold" in layer:
            on = bool(layer["fold"])
        if isinstance(layer.get("fold_chars"), (int, float)):
            chars = int(layer["fold_chars"])
    return max(FAST_MIN_CHARS, chars) if on else None


def _fast_fold() -> int:
    import io
    import json
    import os
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else None
        resp = payload.get("tool_response") if isinstance(payload, dict) else None
        if not isinstance(resp, dict):
            return 0
        limit = fast_threshold(os.environ, payload.get("cwd") or os.getcwd())
        if limit is None or not any(isinstance(resp.get(k), str) and len(resp[k]) > limit
                                    for k in ("stdout", "stderr")):
            return 0
    except Exception:  # garbage in: never block or break the tool call
        return 0
    from .hooks import main as hook_main
    return hook_main("fold", io.StringIO(raw))


if __name__ == "__main__":
    if sys.argv[1:3] == ["hook", "fold"]:
        sys.exit(_fast_fold())
    from .cli import main
    sys.exit(main())
