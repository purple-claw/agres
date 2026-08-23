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
npx agres --help
npx agres status
```

If you insist on installing:

```bash
npm i -g agres
agres status
```

### Install skill for your agent

Puts `SKILL.md` where your agent will actually find it:

```bash
npx agres skill-install
# or forced:
npx agres skill-install --force
```

This copies to:

* `~/.config/opencode/skills/agres/` (opencode)
* `~/.agents/skills/agres/` (generic agents)
* `~/.claude/skills/agres/` (claude)
* `./skills/agres/` (project-local, if `package.json` or `.git` exists)
* runtime → `~/.agres/runtime/agres_runtime.py`
* shim → `~/.local/bin/agres` (delegates to npx)

No hidden postinstall that clobbers your filesystem. The `postinstall` script literally does nothing except print a hint, because side effects in postinstall are how you get paged at 3am.

If you run a managed env where you cannot write to home, set `AGRES_HOME` and `AGRES_PROJECT_ROOT` yourself.

## Quick start

```bash
# 1. check that it sees your model and budget
npx agres status
# shows: model, budget bar, confidence, storage, counts, health

# 2. start a session with a goal (so you do not end up with a zombie session)
npx agres start "refactor auth, keep verbatim context for 1M conversation"

# 3. capture every turn verbatim (your agent should do this automatically via the skill)
npx agres capture --role user --text "we decided to keep the old auth flow, do not touch it"
npx agres capture --role assistant --text "understood, auth flow pinned"

# 4. pin what must never fade
npx agres pin --text "auth flow is frozen, do not invent a new one"

# 5. watch the budget
npx agres budget
npx agres window

# 6. when it fills, fold
npx agres fold --target 200000

# 7. when you need old exact wording, unfold
npx agres unfold --query "auth flow"
# or by id
npx agres unfold --fold-id fold_abc123

# 8. checkpoint before you pause, resume later
npx agres checkpoint --reason "pausing mid-refactor"
npx agres resume

# 9. when done
npx agres end
```

## How it works without the marketing

```
turn (verbatim) -> window_items (budgeted, priority ordered) -> fold -> folds (exact text + summary + FTS) -> unfold -> window_items
turns table keeps the full transcript forever (never truncated)
window_items is what counts against your budget (priority 1 recent, 2 unfolded, 3+ pinned never auto-folded)
folds is the disk backup with FTS5 so you can grep the exact phrasing later
```

Priority:

* `p0` low, folded first
* `p1` recent turns
* `p2` unfolded (you asked for it, so it stays)
* `p3+` pinned, never auto-folded

If you add a 20000 char turn, it gets truncated to 12000 for the window (so one chat message does not OOM your budget), but `turns.content` keeps the full 20000 forever. That is on purpose. You can thank the edge case tests later.

## Visual analytics you actually wanted in `agres status`

`npx agres status` (or `--json` for CI) shows:

* model detection (via `AGRES_MODEL`, `OPENCODE_MODEL`, or `~/.local/share/opencode/opencode.db` latest session, plus limit from `~/.cache/opencode/models.json`)
* window budget bar with `high/medium/low/critical` confidence
* `budget <-> model` line (throttled 1M → 256k when you test low-context)
* storage: `DB + WAL + SHM + CAS + total` and object counts
* counts for every table (sessions, events, turns, folds, window_items, etc.)
* health warnings (ghost session, DB too large, FTS fallback)

Example:

```
┌─ Agres Origami Memory - Status
│ session     sess_abc  ● active
│ model     muse-spark-1.2-contributor-free via opencode ctx 1,048,576 (1M)
│ budget↔model model ctx 1,048,576 → throttled to budget 262,144
├─ Window Budget - 262,144 tokens
│  ████████████████░░░░░░░░░░░░░░  53.7%  136,400 used
│  confidence  ~ MEDIUM
...
├─ Storage  DB: 9.6MB  CAS: 85KB (611 objects)
└─
```

`agres budget` and `agres window` also include model in header/footer now. If you do not like it, use `--json` and parse it yourself.

## CLI reference

No surprise flags. All commands take `--help`.

| command | what it does |
|---|---|
| `agres status` | visual analytics (budget bar, confidence, model, storage, health). Add `--json` |
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
| `agres checkpoint --reason "..."` | snapshot |
| `agres context --query "..."` | build context pack |
| `agres search --query "..."` | FTS5 or LIKE fallback |
| `agres index --path . --limit 1000` | index files/symbols/chunks per-project |
| `agres prune --keep-days 30 --dry-run` | bounded growth, VACUUM |
| `agres gc --dry-run` | GC unreferenced CAS objects |
| `agres repair` | fix ghost session, WAL, integrity |
| `agres skill-install` | install skill files to agent paths |
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
npx agres status --json | jq .window_pct
# run edge tests (if you have them locally):
python3 /tmp/test_edgecases3.py
python3 /tmp/test_deepseek_realtime.py
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

**Do I need to use the skill?** No. You can call `npx agres` directly from your agent. The skill just tells the agent to call it at the right time.

**Will it slow my agent?** Tiered activation. For trivial tasks (<5 turns, <50k tokens) it is just one SQLite write per capture. For long sessions it does budget checks and auto-fold. If you think that is slow, measure it before opening an issue.

**Does it require a vector DB?** No. And it never will by default. If you want embeddings, fork it.

**Why SQLite and not markdown only?** Because markdown is not queryable and you will eventually want `SELECT * FROM turns WHERE session_id=?`. We keep markdown files as human-readable mirrors, not source of truth.

**Why not just use the model's context?** Because it truncates silently and then hallucinates the truncated part. This folds explicitly so you control what is lost from the window, and you can unfold the exact wording.

## License

MIT. Do what you want, but do not blame us when you `agres fold --target 0` and wonder where your context went. It is in `folds`, use `agres unfold`.

## Credits

Lazy yet super smart dev who got paged one too many times for an over-engineered memory service and decided to replace it with one Python file and a bar chart.

Issues go to https://github.com/purple-claw/agres/issues. Provide `agres status --json` and `AGRES_WINDOW_BUDGET` and we will actually look.
