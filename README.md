# agres

**Origami Memory for agents that run out of context before they run out of work.**

You have a 256k window. Your conversation is 1M. Your model forgets the first half and confidently invents the rest. `agres` folds the old stuff out of the window and unfolds the exact original text when you need it. No paraphrase, no embedding, no cloud, no vector database that costs more than the model.

It is SQLite. It is a single Python file. It works.

## Why this exists

Because every other memory thing wants you to install an embedding model, a vector store, a cloud API key, and then re-index your entire repo while explaining why you need RAG to remember what you said 10 minutes ago.

You do not. You need `capture` and `unfold` and a budget bar that tells you when you are about to be truncated. That is what this does.

If you expected a distributed knowledge graph, you are in the wrong repo. Close this tab, it will save us both time.

## Install

No global install needed. Use `npx`.

```bash
npx @ithica/agres --help
npx @ithica/agres status
```

If you insist on installing:

```bash
npm i -g @ithica/agres
agres status
```

Note: `agres` unscoped is blocked by npm (too similar to `dagre`), hence `@ithica/agres`. Binary is still `agres`, so after install `agres status` works. `npx` form is `npx @ithica/agres`.

### Install skill for your agent

Makes every supported agent use Agres AUTOMATICALLY — no invocation needed.
Auto-activation works two ways: skill routers match the `SKILL.md` description
triggers ("USE AUTOMATICALLY WHEN: writing/editing/debugging code, repo tasks, ..."),
and project rule files (`AGENTS.md`, `CLAUDE.md`, cursor rules, copilot instructions)
are read by agents without being asked.

```bash
npx @ithica/agres skill-install
# or forced:
npx @ithica/agres skill-install --force
```

This copies to:

* `~/.config/opencode/skills/agres/` (opencode)
* `~/.opencode/skills/agres/` (opencode legacy data dir)
* `~/.agents/skills/agres/` (generic agents)
* `~/.claude/skills/agres/` (claude code)
* `~/.codex/skills/agres/` (codex cli)
* `~/.gemini/skills/agres/` (gemini cli)
* `./skills/agres/` (project-local, if `package.json` or `.git` exists)
* global memory, read every session (appended once, never duplicated):
  `~/.claude/CLAUDE.md`, `~/.config/opencode/AGENTS.md`, `~/.gemini/GEMINI.md`
* project rules (same condition, skip with `--no-rules`): `AGENTS.md` + `CLAUDE.md`
  marker block (appended once, never duplicated), `.github/copilot-instructions.md`
  marker block, `.clinerules` (cline) marker block, `.cursor/rules/agres.mdc`
  (`alwaysApply: true`)
* runtime → `~/.agres/runtime/agres_runtime.py`
* shim → `~/.local/bin/agres` (delegates to npx)

commandcode and cline register skills from GitHub after push:

```bash
commandcode skills add purple-claw/agres
cline skill add purple-claw/agres
```

Verify auto-activation wiring any time with:

```bash
npx @ithica/agres doctor          # human report
npx @ithica/agres doctor --json   # CI-friendly
```

No hidden postinstall that clobbers your filesystem. The `postinstall` script literally does nothing except print a hint, because side effects in postinstall are how you get paged at 3am.

If you run a managed env where you cannot write to home, set `AGRES_HOME` and `AGRES_PROJECT_ROOT` yourself.

## Quick start

```bash
# 1. check that it sees your model and budget
npx @ithica/agres status
# shows: model, budget bar, confidence, storage, counts, health

# 2. start a session with a goal (so you do not end up with a zombie session)
npx @ithica/agres start "refactor auth, keep verbatim context for 1M conversation"

# 3. capture every turn verbatim (your agent should do this automatically via the skill)
npx @ithica/agres capture --role user --text "we decided to keep the old auth flow, do not touch it"
npx @ithica/agres capture --role assistant --text "understood, auth flow pinned"

# 4. pin what must never fade
npx @ithica/agres pin --text "auth flow is frozen, do not invent a new one"

# 5. watch the budget
npx @ithica/agres budget
npx @ithica/agres window

# 6. when it fills, fold
npx @ithica/agres fold --target 200000

# 7. when you need old exact wording, unfold
npx @ithica/agres unfold --query "auth flow"
# or by id
npx @ithica/agres unfold --fold-id fold_abc123

# 8. checkpoint before you pause, resume later
npx @ithica/agres checkpoint --reason "pausing mid-refactor"
npx @ithica/agres resume

# 9. project brain: trace everything, graph it, inject it next session
npx @ithica/agres trace                       # whole-project trace → .agres/trace.md
npx @ithica/agres graph --build               # code+progress+git → one queryable graph
npx @ithica/agres graph --query "auth flow"   # subgraph neighborhood for a question
npx @ithica/agres brain --query "auth flow"   # one bounded pack carrying the whole project

# 10. when done
npx @ithica/agres end
```

## How it works without the marketing

```
turn (verbatim) -> window_items (budgeted, priority ordered) -> fold -> folds (exact text + summary + FTS) -> unfold -> window_items
turns table keeps the full transcript forever (never truncated)
window_items is what counts against your budget (priority 1 recent, 2 unfolded, 3+ pinned never auto-folded)
folds is the disk backup with FTS5 so you can grep the exact phrasing later

brain: index + sessions + git -> graph_nodes/graph_edges (one property graph)
       trace = codebase + PageRank hubs + tasks/decisions/errors + git log + health
       brain = trace + graph neighborhood + pins/decisions/turns, token-bounded with receipt
read `brain` at session start: the model gets the whole project without re-exploring it
```

Priority:

* `p0` low, folded first
* `p1` recent turns
* `p2` unfolded (you asked for it, so it stays)
* `p3+` pinned, never auto-folded

If you add a 20000 char turn, it gets truncated to 12000 for the window (so one chat message does not OOM your budget), but `turns.content` keeps the full 20000 forever. That is on purpose. You can thank the edge case tests later.

## Visual analytics you actually wanted in `agres status`

`npx @ithica/agres status` (or `--json` for CI) shows a plain-language
dashboard — labeled sections, gauges and sparklines, every issue paired
with its fix. Non-technical readers get words ("Instant memory 12% used,
223k free"); technical readers keep exact numbers and `--json`:

```
◆ Agres Memory  your project's brain and conversation memory
● Session sess_f5f4555f0f08… · "refactor auth flow"
Health  ✓ Excellent — everything is saved and within budget
── Instant memory ──  what is remembered right now
Used ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 12% · 31k of 254k tokens · 223k free
── Conversation ──  this session
Messages 48 total · last 24h ▁▂▅▇
Tucked away 12 · kept in total 60 · bring any back with unfold
── Project brain ──  code and progress, always queryable
Code 126 files · 1,204 symbols indexed
Knowledge graph 138 nodes · 14 links (refreshed 2h ago)
Progress 3 active tasks · 1 done task · 5 decisions · 2 pinned notes · 0 errors
── Storage ──  on this machine
Database 9.6MB + saved memories 87KB (619 items) · Fast search ✓ on
── Needs attention ──
✓ Nothing — all good.
Next step: agres brain --query "…"   · --json for scripts
```

`agres budget` and `agres window` keep the detailed gauge views. If you do not like words, use `--json` and parse it yourself.

## CLI reference

No surprise flags. All commands take `--help`.

| command | what it does |
|---|---|
| `agres status` | 7-line visual dashboard (gauge, stacked bar, sparkline). Add `--json` |
| `agres budget` | `agres status` but only the budget block, with auto-fold hints |
| `agres window` | what is currently in the active window (priority, tokens) |
| `agres start "goal"` | create session, returns `sess_xxx` |
| `agres capture --role user/assistant --text "..."` | store turn verbatim, auto-fold if over usable |
| `agres fold --target 200000` | fold oldest low priority until under target |
| `agres fold --target 0` | fold everything foldable (keep pinned) |
| `agres unfold --query "..."` | FTS search folds, re-inject exact text |
| `agres unfold --fold-id fold_xxx` | unfold specific fold |
| `agres pin --text "..."` | never auto-fold |
| `agres decision --text "..." --accepted/--rejected` | durable decision log |
| `agres task --objective "..."` | task tracker |
| `agres error --message "..."` | error log |
| `agres checkpoint --reason "..."` | snapshot + validation (facts covered + audit artifact) |
| `agres context --query "..." [--budget N] [--json]` | bounded pack (fits budget, receipt + stale tracking) |
| `agres search --query "..."` | hybrid RRF (fts+symbols+import-graph, AND-first BM25) — `--mode legacy` for old shape |
| `agres index --path . --limit 1000` | index files/symbols/chunks per-project (incremental via manifest) |
| `agres map [--tokens 1024] [--query ...] [--focus a.py]` | ranked PageRank skeleton (hubs first, fits budget) |
| `agres graph --build` | code-to-graph: files+symbols+progress+git → one SQLite property graph |
| `agres graph --query "..." [--hops 2]` | BFS subgraph neighborhood for a question |
| `agres graph --export json\|dot` | dump `.agres/graph.json` / `graph.dot` |
| `agres trace [--json]` | whole-project trace → `.agres/trace.md` (codebase, hubs, progress, git, health) |
| `agres brain [--query ...] [--budget N]` | context injection: one bounded pack carrying the whole project → `.agres/brain.md` |
| `agres touch --files a.py,b.py` | re-index only listed files — call after EVERY edit |
| `agres sync [--since HEAD~1]` | drift scan + prune deleted, git-scoped if --since |
| `agres prune --keep-days 30 --dry-run` | bounded growth, VACUUM |
| `agres gc --dry-run` | GC unreferenced CAS objects |
| `agres repair` | fix ghost session, WAL, integrity |
| `agres skill-install [--no-rules]` | install skill files to agent paths + project auto-read rules |
| `agres doctor [--json]` | verify auto-activation wiring (skills + project rules) |
| `agres end` | close session |

Env:

* `AGRES_HOME` (default `~/.agres`)
* `AGRES_PROJECT_ROOT` (default `cwd`)
* `AGRES_WINDOW_BUDGET` (default `262144`)
* `AGRES_MODEL` (override model id for status)
* `AGRES_PYTHON` (override python binary)
* `AGRES_MAX_WINDOW_ITEM_CHARS` (default 12000)
* `AGRES_MAX_WINDOW_ITEMS` (default 500)

## Python requirement

Single file `runtime/agres_runtime.py`, Python 3.8+. No deps. If you do not have `python3` on PATH, set `AGRES_PYTHON`. We are not bundling a Python runtime because that would be the opposite of lazy.

Optional precision upgrade (never required): `pip install tree_sitter tree_sitter_python`
(+ `tree_sitter_javascript`, `tree_sitter_go`, ...) for class-scoped methods
(`Store.save`), string-safe imports, and usage edges in the map graph.
Without it, regex extraction is used. Check with `agres doctor` (`parser=` line).

SQLite FTS5 if available, `LIKE ESCAPE` fallback if not. You do not need to care.

## Model details in status

We tried to be clever and infer your model so you do not have to set it. Order:

1. `AGRES_MODEL` env (you set it, we trust it)
2. latest opencode session from `~/.local/share/opencode/opencode.db` (includes tokens and cost)
3. fallback to `budget` size

If you run a 1M model with `AGRES_WINDOW_BUDGET=262144` to test low-context behavior, status will say `model ctx 1,048,576 → throttled to budget 262,144`. That is not a bug, that is you testing throttling. Set `AGRES_WINDOW_BUDGET=1048576` if you want the full window.

New sessions store `model`/`model_provider` in `sessions` table for history.

## Architecture for people who ask

* No embeddings. Keyword search via FTS5. If you need semantic, add it yourself and open a PR, but do not require it by default.
* WAL + `busy_timeout=5000` + `foreign_keys=ON` + `synchronous=NORMAL`. So concurrent `capture` does not lock.
* `events.seq AUTOINCREMENT` (not `MAX+1`), so concurrent writes do not duplicate PK.
* `file_id = hash(path+hash)` not hash-only, so two empty `__init__.py` files do not collide.
* One transaction per file in index, not one per 20 files.
* Token estimate is `len//4` deterministic, not `tiktoken` half the time. Budget bars need determinism more than 3 percent accuracy.
* `window_items.text` capped, `turns.content` never truncated. So you do not lose verbatim even if window is capped.

If you want the full list of 14 fixes and 29 edge case tests, read the commit history. It is long, it is boring, and it is why this thing does not corrupt your DB when you hit it with 20 parallel captures.

## Development

```bash
git clone https://github.com/purple-claw/agres
cd agres
python3 -m py_compile runtime/agres_runtime.py
npm run eval          # 13 deterministic gates, no LLM (exits non-zero on fail)
# with tree-sitter backend: AGRES_PYTHON=/path/to/venv/python npm run eval
npx @ithica/agres status --json | jq .window_pct
```

## Publishing this package

This repo *is* the npm package. The `package.json` name `agres` is free on npm at time of this writing.

```bash
npm login
npm publish --access public
# or dry run first
npm pack --dry-run
```

Github is just `git push`. No build step. No bundler. We keep it that way.

## FAQ you will ask anyway

**Do I need to use the skill?** No. You can call `npx @ithica/agres` directly from your agent. The skill just tells the agent to call it at the right time.

**Will it slow my agent?** Tiered activation. For trivial tasks (<5 turns, <50k tokens) it is just one SQLite write per capture. For long sessions it does budget checks and auto-fold. If you think that is slow, measure it before opening an issue.

**Does it require a vector DB?** No. And it never will by default. If you want embeddings, fork it.

**Why SQLite and not markdown only?** Because markdown is not queryable and you will eventually want `SELECT * FROM turns WHERE session_id=?`. We keep markdown files as human-readable mirrors, not source of truth.

**Why not just use the model's context?** Because it truncates silently and then hallucinates the truncated part. This folds explicitly so you control what is lost from the window, and you can unfold the exact wording.

## License

MIT. Do what you want, but do not blame us when you `agres fold --target 0` and wonder where your context went. It is in `folds`, use `agres unfold`.

## Credits

Lazy yet super smart dev who got paged one too many times for an over-engineered memory service and decided to replace it with one Python file and a bar chart.

Issues go to https://github.com/purple-claw/agres/issues. Provide `agres status --json` and `AGRES_WINDOW_BUDGET` and we will actually look.
