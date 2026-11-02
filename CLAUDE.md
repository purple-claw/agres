# agres — developing this repo

Project memory CLI for coding agents. Python ≥ 3.9, **stdlib only** (tree-sitter optional), one SQLite
file per project. Spec/plan/ADR: `docs/plans/agres-v1-*.md`, `docs/plans/agres-plugin-*.md` (1.1: plugin + Origami), `docs/adr/`.
Ships as a Claude Code plugin: `.claude-plugin/`, `hooks/hooks.json` (generated from `install.hook_entries`, test-enforced), `bin/agres` (on PATH).

- Run: `python3 -m agres …` (or `bin/agres`). Tests: `python3 -m unittest discover -s tests` (no pytest).
  Scoped: `python3 -m unittest tests.test_hooks -v`. Before a commit: `python3 -m compileall -q agres` + full suite.
- Layout: `agres/` (paths, db, memory, fold, pack, search, hooks, cli, config, status, install, evalrun, migrate*, v0),
  `agres/code/` (index engine: discover, indexer, resolve, rank, repomap, gitwalk, langs, engine),
  `skill/agres/` (the shipped skill), `tests/` (unittest; `tests/helpers.py` fakes `$HOME`).
- Invariants: hooks always exit 0 and print only on SessionStart/UserPromptSubmit and the `fold` PostToolUse hook (updatedToolOutput); nothing is hard-deleted
  (supersede/retire) — **except captured turns** (`forget M<id>`/`--session`, `gc`, retention); `/` and `$HOME` are never project roots; the prompt/stop hook path must not import
  `agres.code` (latency: SessionStart p95 < 300 ms, prompt < 100 ms, fold hook on small output < 60 ms — keep its fast path in `__main__.py` import-free).
- Evals: `python3 -m agres.evalrun` (also in the suite as `tests.test_evals`).
- Tests never touch the real `~/.agres`, `~/.claude` or project stores — use `TempHome`.
- Conventional commits; one concern per commit.
