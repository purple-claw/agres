"""agres command line. Memory commands first; admin and compatibility aliases after.

Write commands print the new ID plus up to 3 similar live items, so the agent
can pass `--replaces <ID>` instead of piling up near-duplicates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Optional

from . import __version__, config, db, memory, paths
from .memory import fmt_id, parse_id

DEPRECATED = {
    "fold": "context folding was removed in agres 1.0: Claude Code owns the context window. "
            "Memory is re-injected after /compact; use `agres search` / `agres show` for exact recall.",
    "budget": "window budgets were removed in agres 1.0 (the pack is sized by `config budget_chars`).",
    "window": "the window manifest was removed in agres 1.0; see `agres status`.",
    "graph": "use `agres map --query ...` (code skeleton) or `agres search ...` instead of `graph`.",
    "event": "generic events were removed in agres 1.0; use `agres note \"...\"`.",
}


class UserError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def _open(args, create: Optional[bool] = None) -> db.Store:
    start = getattr(args, "path", None) or os.getcwd()
    root = paths.find_root(start)
    if root is None:
        raise UserError("no agres project here (no .agres/ or git checkout above this directory); "
                        "run `agres init` to create one")
    if create is None:
        create = bool(config.load(root).get("auto_init")) and os.path.exists(os.path.join(root, ".git"))
    try:
        return db.Store(root, create=create)
    except db.NoStore:
        raise UserError(f"no agres store at {root}; run `agres init`")
    except db.V0Store:
        raise UserError(f"{root} has a v0 agres store; run `agres migrate` first")
    except db.NewerSchema as e:
        raise UserError(str(e))


def _session(args, s: db.Store) -> Optional[str]:
    return memory.resolve_session(s, getattr(args, "session", None))


def _item_id(ref: str) -> int:
    kind, n = parse_id(ref)
    if kind == "turn":
        raise UserError(f"{ref} is a conversation turn, not a memory item")
    return n


def _text(args) -> str:
    parts = getattr(args, "text", None) or []
    text = " ".join(parts) if isinstance(parts, list) else (parts or "")
    for alt in ("text_opt", "objective", "message", "reason", "query_opt"):
        text = text or (getattr(args, alt, None) or "")
    if not text.strip():
        raise UserError("nothing to record: give the text as an argument")
    return text


def _record(args, kind: str, status: Optional[str] = None, priority: int = 0, why: Optional[str] = None,
            source: str = "agent") -> None:
    with _open(args) as s:
        text = _text(args)
        replaces = _item_id(args.replaces) if getattr(args, "replaces", None) else None
        if kind == "handoff":
            iid = memory.add_handoff(s, text, source=source, session_id=_session(args, s))
        else:
            iid = memory.add_item(s, kind, text, status=status, priority=priority,
                                  why=why if why is not None else getattr(args, "why", None),
                                  files=getattr(args, "files", None), session_id=_session(args, s),
                                  source=source, replaces=replaces)
        label = {"rejected": " (rejected)"}.get(status or "", "") + (" (critical)" if priority else "")
        print(f"{fmt_id(kind, iid)} recorded{label}" + (f", replaces {args.replaces.upper()}" if replaces else ""))
        if kind != "handoff":
            sims = memory.similar(s, text, kind=kind, exclude_id=iid)
            sims = [r for r in sims if r["id"] != replaces]
            if sims:
                print("similar live items (if the new one supersedes one, re-run with --replaces <ID> "
                      "or `agres drop <ID>`):")
                for r in sims:
                    print(f"  {fmt_id(r['kind'], r['id'])} {r['title']}")


# ----------------------------------------------------------------- commands
def cmd_decide(a):
    _record(a, "decision")


def cmd_reject(a):
    _record(a, "decision", status="rejected")


def cmd_pin(a):
    _record(a, "pin", priority=1 if a.critical else 0)


def cmd_task(a):
    status = None
    st = (getattr(a, "status", None) or "").lower()
    if st in ("done", "complete", "completed", "finished"):
        status = "done"
    elif st in ("dropped", "cancelled", "canceled", "abandoned"):
        status = "dropped"
    _record(a, "task", status=status)


def cmd_error(a):
    _record(a, "error", why=getattr(a, "context", None))


def cmd_note(a):
    _record(a, "note")


def cmd_handoff(a):
    _record(a, "handoff", source="checkpoint" if a.cmd == "checkpoint" else "agent")


def _close(a, verb: str):
    with _open(a) as s:
        for ref in a.ids:
            try:
                old, new = memory.close_item(s, _item_id(ref), verb, note=a.note)
            except ValueError as e:
                raise UserError(str(e))
            it = memory.get_item(s, _item_id(ref))
            print(f"{fmt_id(it['kind'], it['id'])} {old} → {new}")


def cmd_done(a):
    _close(a, "done")


def cmd_drop(a):
    _close(a, "drop")


def cmd_forget(a):
    with _open(a) as s:
        try:
            memory.forget(s, _item_id(a.id))
        except ValueError as e:
            raise UserError(str(e))
        print(f"{a.id.upper()} forgotten (text redacted, tombstone kept)")


def cmd_search(a):
    from . import search
    query = " ".join(a.query or []) or a.query_opt or ""
    if not query.strip():
        raise UserError("search needs words")
    kinds = [k.rstrip("s") if k.rstrip("s") in memory.KINDS else k for k in (a.kind or [])]
    with _open(a) as s:
        hits = search.search(s, query, kinds=kinds, include_closed=a.all, include_code=not a.no_code,
                             limit=a.limit)
        print(json.dumps(hits, indent=1) if a.json else search.format_hits(hits))


def cmd_show(a):
    from . import search
    with _open(a) as s:
        try:
            print(search.show(s, a.id, around=a.around))
        except ValueError as e:
            raise UserError(str(e))


def cmd_brain(a):
    from . import pack
    chars = a.chars or (a.budget * 4 if a.budget else 0)
    with _open(a) as s:
        if a.session:
            memory.touch_session(s, a.session)
        cfg = config.load(s.root)
        print(pack.build(s, budget_chars=chars or int(cfg.get("budget_chars") or pack.DEFAULT_CHARS),
                         query=a.query or None))


def cmd_map(a):
    from .code import engine
    with _open(a) as s:
        if not s.one("SELECT 1 FROM files LIMIT 1"):
            print("indexing (first run)…", file=sys.stderr)
            engine.sync(s)
        engine.ensure_resolved(s)
        print(engine.repo_map(s, query=a.query or None, focus_files=a.focus or (), tokens=a.tokens))


def cmd_index(a):
    from .code import engine
    with _open(a) as s:
        r = engine.sync(s, full=a.full)
        c = r.get("counts", {})
        print(f"indexed {c.get('files', '?')} files, {c.get('symbols', '?')} symbols, {c.get('edges', '?')} edges "
              f"(+{r.get('added', 0)} ~{r.get('modified', 0)} -{r.get('deleted', 0)}) in {r.get('seconds', '?')}s")


def cmd_touch(a):
    from .code import engine
    files = [f for chunk in (a.files or []) + (a.file_opt or []) for f in chunk.split(",") if f.strip()]
    with _open(a) as s:
        print(f"touched {engine.touch(s, files)} file(s)")


def cmd_init(a):
    target = a.dir or os.getcwd()
    if not a.dir and not a.here:
        root = paths.find_root(target)
        if root and os.path.exists(os.path.join(root, ".git")):
            target = root
    target = os.path.realpath(target)
    if paths.is_forbidden_root(target):
        raise UserError(f"refusing to create a store in {target} (filesystem root or home directory)")
    try:
        with db.Store(target, create=True) as s:
            print(f"agres store ready at {s.dir}")
    except db.V0Store:
        raise UserError(f"{target} has a v0 store; run `agres migrate`")


def cmd_start(a):
    with _open(a, create=True) as s:
        sid = a.session or f"sess_{uuid.uuid4().hex[:18]}"
        memory.touch_session(s, sid, agent="cli", title=" ".join(a.title) or None, cwd=os.getcwd())
        print(sid)


def cmd_end(a):
    with _open(a) as s:
        sid = _session(a, s)
        if not sid:
            raise UserError("no recent session to end")
        memory.end_session(s, sid, "cli")
        print(f"ended {sid}")


def cmd_capture(a):
    with _open(a) as s:
        tid = memory.add_turn(s, _session(a, s), a.role, _text(a))
        print(fmt_id("turn", tid) if tid else "duplicate or empty turn skipped")


def cmd_onoff(a):
    root = paths.find_root(a.path or os.getcwd())
    if root is None:
        raise UserError("no agres project here")
    config.set_off(root, a.cmd == "off")
    print(f"agres {a.cmd} for {root}")


def cmd_config(a):
    root = paths.find_root(a.path or os.getcwd())
    if a.key is None:
        print(json.dumps(config.load(root), indent=2, sort_keys=True))
        return
    if a.value is None:
        print(json.dumps(config.load(root).get(a.key)))
        return
    try:
        val = config.coerce(a.key, a.value)
    except ValueError as e:
        raise UserError(str(e))
    if a.project:
        if root is None:
            raise UserError("no agres project here")
        config.save_project(root, {a.key: val})
    else:
        config.save_global({a.key: val})
    print(f"{a.key} = {json.dumps(val)}" + (" (project)" if a.project else " (global)"))


def cmd_status(a):
    from . import status
    with _open(a, create=False) as s:
        print(status.render(s, deep=a.deep, as_json=a.json))


def cmd_gc(a):
    with _open(a, create=False) as s:
        n = 0
        if a.turns_older_than:
            import datetime as _dt
            cut = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=a.turns_older_than)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            with s.tx():
                ids = [r[0] for r in s.q("SELECT id FROM turns WHERE created_at < ?", (cut,))]
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    ph = ",".join("?" * len(chunk))
                    s.x(f"DELETE FROM fts_turns WHERE rowid IN ({ph})", chunk)
                    s.x(f"DELETE FROM turns WHERE id IN ({ph})", chunk)
                n = len(ids)
        if s.fts:
            for t in db.FTS_TABLES:
                s.x(f"INSERT INTO {t}({t}) VALUES('optimize')")
        s.x("PRAGMA optimize")
        s.x("VACUUM")
        print(f"gc done; {n} old turn(s) removed")


def cmd_hook(a):
    from . import hooks
    return hooks.main(a.event)


def cmd_migrate(a):
    from . import migrate
    return migrate.cli(a)


def cmd_install(a):
    from . import install
    return install.cli(a)


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", help="session id to stamp on writes (provenance only)")
    common.add_argument("-C", "--path", help="run as if started in this directory")

    p = argparse.ArgumentParser(prog="agres", description="Project memory for coding agents.")
    p.add_argument("--version", action="version", version=f"agres {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def add(name, fn, help_=None, aliases=()):
        hidden = help_ is argparse.SUPPRESS  # no help kwarg => argparse keeps it out of --help
        sp = sub.add_parser(name, parents=[common], aliases=list(aliases), **({} if hidden else {"help": help_}))
        sp.set_defaults(fn=fn)
        return sp

    def text_cmd(name, fn, help_, *extra, aliases=()):
        sp = add(name, fn, help_, aliases)
        sp.add_argument("text", nargs="*", help="the text to record")
        sp.add_argument("--text", dest="text_opt", help=argparse.SUPPRESS)
        sp.add_argument("--why", help="reason / context")
        sp.add_argument("--files", nargs="+", help="repo files this is about")
        sp.add_argument("--replaces", metavar="ID", help="item this supersedes")
        for flag, kw in extra:
            sp.add_argument(*flag, **kw)
        return sp

    # memory: recall
    sp = add("search", cmd_search, "ranked search over memory, conversation and code")
    sp.add_argument("query", nargs="*")
    sp.add_argument("--query", dest="query_opt", help=argparse.SUPPRESS)
    sp.add_argument("--kind", action="append", help="pin|decision|task|error|note|handoff|turn|code")
    sp.add_argument("--all", action="store_true", help="include closed / superseded items")
    sp.add_argument("--no-code", action="store_true")
    sp.add_argument("--limit", type=int, default=12)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--mode", help=argparse.SUPPRESS)
    sp = add("show", cmd_show, "exact text of an item (D12) or turn (M40)")
    sp.add_argument("id")
    sp.add_argument("--around", type=int, default=0, help="turns before/after (for M ids)")
    sp = add("brain", cmd_brain, "print the injection pack", aliases=["resume"])
    sp.add_argument("--query", default="")
    sp.add_argument("--budget", type=int, default=0, help="size in tokens (≈4 chars each)")
    sp.add_argument("--chars", type=int, default=0)
    sp.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    # memory: record
    text_cmd("decide", cmd_decide, "record an accepted decision")
    text_cmd("reject", cmd_reject, "record a rejected idea (won't be re-proposed)")
    text_cmd("pin", cmd_pin, "record a constraint/fact that must hold",
             (("--critical",), {"action": "store_true", "help": "always injected in full"}))
    text_cmd("task", cmd_task, "open a task",
             (("--objective",), {"help": argparse.SUPPRESS}), (("--status",), {"help": argparse.SUPPRESS}),
             (("--task-id",), {"help": argparse.SUPPRESS}))
    text_cmd("error", cmd_error, "record an unresolved error",
             (("--message",), {"help": argparse.SUPPRESS}), (("--context",), {"help": argparse.SUPPRESS}),
             (("--task",), {"help": argparse.SUPPRESS}))
    text_cmd("note", cmd_note, "record searchable knowledge (not injected in full)")
    text_cmd("handoff", cmd_handoff, "where things stand + next steps, for the next session")
    for name, fn, h in (("done", cmd_done, "close a task (done) or error (fixed)"),
                        ("drop", cmd_drop, "drop a task, retire a pin/note, retract a decision")):
        sp = add(name, fn, h)
        sp.add_argument("ids", nargs="+", metavar="ID")
        sp.add_argument("--note")
    sp = add("forget", cmd_forget, "redact an item for good (e.g. a leaked secret)")
    sp.add_argument("id")

    # code
    sp = add("map", cmd_map, "PageRank code skeleton within a token budget", aliases=["repo-map"])
    sp.add_argument("--query", default="")
    sp.add_argument("--tokens", type=int, default=1024)
    sp.add_argument("--focus", action="append")
    sp = add("index", cmd_index, "index / re-sync the code graph", aliases=["sync"])
    sp.add_argument("--full", "--force", action="store_true", dest="full")
    sp.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    sp.add_argument("--since", help=argparse.SUPPRESS)
    sp.add_argument("--no-prune", action="store_true", help=argparse.SUPPRESS)
    sp = add("touch", cmd_touch, "re-index specific files")
    sp.add_argument("files", nargs="*")
    sp.add_argument("--files", dest="file_opt", action="append")
    sp.add_argument("--force", action="store_true", help=argparse.SUPPRESS)

    # admin
    sp = add("init", cmd_init, "create the store (git root by default)")
    sp.add_argument("dir", nargs="?")
    sp.add_argument("--here", action="store_true", help="create in the current directory, not the git root")
    sp = add("status", cmd_status, "what's stored, index health, settings", aliases=["trace"])
    sp.add_argument("--deep", action="store_true")
    sp.add_argument("--json", action="store_true")
    add("on", cmd_onoff, "re-enable capture and injection for this project")
    add("off", cmd_onoff, "pause capture and injection for this project")
    sp = add("config", cmd_config, "show or set settings")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--project", action="store_true", help="write to .agres/config.json")
    sp = add("gc", cmd_gc, "optimize the store; optionally prune old turns", aliases=["prune", "repair"])
    sp.add_argument("--turns-older-than", type=int, metavar="DAYS")
    sp.add_argument("--keep-days", type=int, dest="turns_older_than", help=argparse.SUPPRESS)
    sp.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    sp = add("hook", cmd_hook, "internal: run a Claude Code hook (reads JSON on stdin)")
    sp.add_argument("event")
    sp = add("migrate", cmd_migrate, "upgrade v0 stores to 1.0")
    sp.add_argument("target", nargs="?", help="project root (default: current project)")
    sp.add_argument("--all", action="store_true", help="every v0 store under ~ (or TARGET)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--rollback", action="store_true")
    sp.add_argument("--global", dest="global_", action="store_true", help="route the old ~/.agres store")
    sp.add_argument("--routes", help="apply an edited routes JSON file")
    for name, h in (("install", "wire agres into Claude Code / Codex (hooks, skill, shim)"),
                    ("uninstall", "remove agres wiring, restoring backups"),
                    ("doctor", "check wiring and store health")):
        sp = add(name, cmd_install, h)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--yes", action="store_true")

    # compatibility (zeus, old habits)
    sp = add("start", cmd_start, argparse.SUPPRESS)
    sp.add_argument("title", nargs="*")
    add("end", cmd_end, argparse.SUPPRESS)
    sp = add("capture", cmd_capture, argparse.SUPPRESS)
    sp.add_argument("--role", choices=["user", "assistant", "system"], required=True)
    sp.add_argument("--text", dest="text_opt", required=True)
    sp = text_cmd("decision", None, argparse.SUPPRESS,
                  (("--accepted",), {"action": "store_true"}), (("--rejected",), {"action": "store_true"}),
                  (("--task",), {"help": argparse.SUPPRESS}))
    sp.set_defaults(fn=lambda a: cmd_reject(a) if a.rejected else cmd_decide(a))
    sp = text_cmd("checkpoint", cmd_handoff, argparse.SUPPRESS, (("--reason",), {"help": argparse.SUPPRESS}))
    for name in ("context", "unfold"):
        sp = add(name, cmd_search, argparse.SUPPRESS)
        sp.add_argument("query", nargs="*")
        sp.add_argument("--query", dest="query_opt")
        sp.add_argument("--fold-id", help=argparse.SUPPRESS)
        sp.add_argument("--limit", type=int, default=12)
        sp.add_argument("--budget", type=int, help=argparse.SUPPRESS)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(kind=None, all=False, no_code=False)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in DEPRECATED:  # old flags (e.g. fold --target) must not trip argparse
        print(f"agres {argv[0]}: {DEPRECATED[argv[0]]}")
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        rc = args.fn(args)
        return int(rc or 0)
    except UserError as e:
        print(f"agres: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
