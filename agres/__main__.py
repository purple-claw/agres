import sys

# Origami's fold hook runs synchronously after every Bash call. Most outputs are small: decide that
# with json alone and exit before importing the CLI, the store and the code-graph schema.
FAST_MIN_CHARS = 1000  # fold.tool_fold never folds below this, whatever fold_chars says

if sys.argv[1:3] == ["hook", "fold"]:
    import io
    import json

    raw = sys.stdin.read()
    try:
        resp = json.loads(raw).get("tool_response") if raw.strip() else None
    except (ValueError, AttributeError):
        resp = None
    if not isinstance(resp, dict) or not any(isinstance(resp.get(k), str) and len(resp[k]) > FAST_MIN_CHARS
                                             for k in ("stdout", "stderr")):
        sys.exit(0)
    from .hooks import main as hook_main
    sys.exit(hook_main("fold", io.StringIO(raw)))

from .cli import main  # noqa: E402

sys.exit(main())
