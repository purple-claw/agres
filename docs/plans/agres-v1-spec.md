# Agres 1.0 — spec

Status: **DRAFT, awaiting approval** · Date: 2026-10-14 · Owner: Iris
Plan: `agres-v1-plan.md` · ADR: `../adr/0001-agres-v1-architecture.md`

## 1. Why

Agres is meant to be project memory for coding agents. Two jobs: inject a small,
high-signal pack at session start, and keep that memory true while work happens.
An audit of v0.5 (runtime 5,903 LOC) on real stores showed it does neither:

| # | Finding (evidence) | Effect |
|---|---|---|
| F1 | Store = `./.agres` of the *cwd*, else global `~/.agres` (`agres_runtime.py:35-55`). `touch` mkdirs `.agres` wherever it runs (`:4307`). | 8 stray nested stores (Okas `packages/`, `app/`, `app/lib/`…). `/` and `$HOME` sessions write into the global DB: other repos' pins, test fixtures `a.py`, `subdir/b.py`, goal "Test model per session" injected into unrelated sessions. |
| F2 | One global `active_session_id` pointer for all projects (`runtime_state.json`); pins/decisions/tasks filtered by it (`:3036-3064`, `:3808-3829`). | Starting any new session hides every earlier decision and rejection from `brain`/`resume`. Zeus needed a workaround. |
| F3 | `pack_reserve()` keeps 1,024 of the hook's 1,500 tokens as "reply headroom" (`:3016`); sections char-sliced mid-word; JSON receipt inside the pack. | Okas brain = 206 tokens: 1 of 80 pins, 0 of 44 decisions, 0 of 27 tasks; 57% header+receipt. |
| F4 | SKILL.md asks the model to run `agres capture --text "<exact reply>"` after every turn. | Doubles output tokens; not followed in practice: 20 turns captured across 4 real sessions, assistant avg 153 chars (summaries, not verbatim). |
| F5 | Fold/unfold/budget/window engine (~1.2k LOC) only flips rows in `window_items`/`folds`. | Cannot change what is in the model's context — the harness owns it. Model detection reads opencode files (Claude sessions labelled "muse-spark"). |
| F6 | No lifecycle: `cmd_task` is INSERT-only; pins/decisions can't be superseded; sessions never end. | 27 "active" tasks incl. "Phase 5 complete"; 80 pins incl. transient status. Memory rots. |
| F7 | Import graph mostly blind: Python relative imports never captured; `import json` resolves to any local `json.py`; `uses` edges never built (swallowed ValueError, `:3306`). | PageRank "hubs" alphabetical or wrong. |
| F8 | Sprawl: v0.5 runtime + unused "Agres 1.0" package (3k LOC) + `legacy/` v0 (4.9k LOC) in the skill dir; docs copied to 4 places; `/agres` command still teaches fold protocol; frontmatter keys `default_enabled`, `stop_phrases` are ignored by Claude Code and rejected by `package_skill.py`. | Diverging behavior per agent; skill can't be packaged. |

What *is* used and valuable: decisions incl. rejections (44 in Okas), pins (80),
tasks, checkpoints, incremental manifest indexing, FTS5 search, PageRank map idea.

## 2. Principle

**Hooks do mechanics. The model does judgment. The CLI is the single source of truth.**

- Anything that must happen every time (inject, capture, re-index, session close)
  is a hook — deterministic, zero model tokens, fail-open.
- The model is asked only for what needs judgment: is this a decision, a
  rejection, a constraint? Is the task done? What should the next session know?
- Inject only what can't be re-derived from the code (constraints, decisions,
  rejections, open work, handoff). Code structure is on demand (evidence: generated
  repo overviews don't improve file localisation and add ~20% cost — arXiv 2602.11988).

## 3. Requirements (frozen)

**R1 Project scoping.** Root = nearest ancestor containing `.agres/agres.db` →
else `git rev-parse --show-toplevel` → else *no project*. `/` and `$HOME` are never
roots. Only `agres init [DIR]` creates a store outside a git repo (zeus uses this).
No global project store, no global active-session pointer. Hooks with no project
exit 0 silently.

**R2 Memory model.** One `items` table, kinds and lifecycles:

| kind | ID | statuses | meaning |
|---|---|---|---|
| pin | P | active → retired | constraint/fact that must hold (`--critical` = always injected) |
| decision | D | accepted / rejected → superseded | choice made; rejected = "don't propose again" |
| task | T | open → done / dropped (stale = derived, >21 d untouched) | work in flight |
| error | E | open → fixed | unresolved bug/failure worth remembering |
| note | N | active → retired | searchable knowledge; never injected in full |
| handoff | H | (append-only) | "where we are + next steps" for the next session |

Never hard-delete. Replacement = new item + `superseded_by` on the old one
(Graphiti-style invalidation). `agres forget ID` redacts text (tombstone kept) for
secrets captured by mistake.

**R3 Injection pack (`agres brain`, SessionStart).** Plain text, no receipt.
Default 6,000 chars (~1.5k tokens), hard max 9,000 (Claude Code caps hook context at
10,000 chars). Project-scoped, never session-scoped. Truncation only at item
boundaries; per-item caps end in `…` + ID. Empty sections omitted. Order:

1. Header: project name, branch@sha, dirty-file count.
2. Latest handoff (≤700 chars; model-written preferred over auto/compact).
3. Critical pins (all).
4. Rejected decisions (newest 10) — "do not re-propose".
5. Open tasks (newest 8) and open errors (newest 5).
6. Other active pins, ranked by overlap with dirty/recently-touched files, then recency.
7. Recent accepted decisions (newest 8).
8. Index of every remaining active item: `ID title` joined by ` · ` until budget.
9. Footer (always): how to recall (`agres search`, `agres show ID`) and record
   (`agres decide|reject|pin|task|done|handoff`).

**R4 Capture (hooks).** UserPromptSubmit stores `prompt`; Stop stores
`last_assistant_message`. Dedupe `UNIQUE(session_id, role, sha1(text))` (Stop
re-fires when other Stop hooks block). Strip `<private>…</private>`; redact common
secret shapes (PEM blocks, `AKIA…`, `ghp_…`, `sk-…`, `xox…-`). Skip harness-generated
prompts (task notifications, agent messages, system reminders). Per-turn cap 64 KB
(truncation marked). Stop hook writes **nothing** to stdout.

**R5 Freshness.** PostToolUse on `Edit|Write|MultiEdit|NotebookEdit` (and Bash
`tool_response.bashEditDiff` when present) re-indexes touched files, async. Global
edge resolution is deferred (dirty flag) to the next query command or `agres index`.

**R6 Latency & safety.** p95 on an Okas-size repo (≈460 files): SessionStart
< 300 ms, UserPromptSubmit < 100 ms; Stop/PostToolUse/PostCompact async. Every hook:
exit 0 on any failure, errors to `.agres/hook.log` (rotated at 256 KB), DB
`busy_timeout` 2 s (sync hooks) / 10 s (async). `.agres/.gitignore` = `*`.

**R7 On/off.** "stop using agres" / "disable agres" / "agres off" in a prompt →
`.agres/off` flag (per project) and a one-line confirmation; "agres on" / "enable
agres" / "use agres" removes it. While off: no capture, no injection except one line
saying it is off. `agres off|on` CLI does the same.

**R8 Rejection guard.** UserPromptSubmit runs an FTS match of the prompt against
rejected decisions and critical pins. Strong matches only (≥2 shared non-stopword
terms and bm25 under threshold) → ≤3 lines, ≤600 chars, via `additionalContext`.
Configurable off (`agres config guard off`).

**R9 CLI surface** (model-facing first; every write prints the new ID plus up to 3
similar active items so the model can `--replaces` instead of duplicating):

```
agres search QUERY [--kind K] [--all]     ranked one-line hits across items, turns, code
agres show ID [--around N]                exact text; turns with N neighbours
agres pin TEXT [--critical] [--files F…] [--replaces ID]
agres decide TEXT [--why W] [--files F…] [--replaces ID]
agres reject TEXT [--why W] [--replaces ID]
agres task TEXT [--files F…]              open a task
agres done ID [--note N]                  task→done, error→fixed
agres drop ID [--note N]                  task→dropped, pin/note→retired
agres error TEXT | agres note TEXT | agres handoff TEXT
agres brain [--query Q] [--budget TOKENS] print the pack
agres map [--query Q] [--tokens N]        PageRank code skeleton, on demand
```

Admin: `init`, `index`, `status`, `doctor`, `install`, `uninstall`, `migrate`,
`on|off`, `forget ID`, `gc`, `config`, `hook EVENT` (internal).

Compat aliases (zeus + old habits): `start GOAL` (prints session id), `end`,
`resume` (=brain), `capture --role --text`, `decision --text --accepted|--rejected`,
`task --objective`, `error --message`, `pin --text`, `checkpoint --reason`
(=handoff), `context|unfold --query` (=search), `touch`, `sync` (=index),
`repo-map` (=map), `trace` (=status --deep), global `--session ID` / `AGRES_SESSION`.
`fold|budget|window` print a one-line deprecation notice and exit 0 (one release).

**R10 Code index.** Built on the Agres 1.0 engine (ancestor/git root, `git ls-files`
discovery, pruned walk, `.agresignore`, two-phase incremental parse→resolve,
bi-temporal edges, relative imports, Dart, PageRank + communities). Add: Yocto/DT
text indexing (`.bb .bbappend .bbclass .inc .conf .dts .dtsi`); secret denylist
(never read `.env*`, `*.pem`, `*.key`, `id_rsa*`, `secrets/**`, `*credentials*`);
stdlib/third-party imports must not resolve to same-stem local files.
Not injected at SessionStart by default (`agres config brain.map on` to opt in).

**R11 Install.** `agres install [--dry-run]`: one `agres` shim on PATH
(`~/.local/bin/agres` → `python3 -m agres` from `~/Dev/agres`); symlink
`~/.agent-skills/agres` → `~/Dev/agres/skill/agres` (Claude via existing
`~/.claude/skills/agres` link, Codex via `~/.codex/skills/agres` link); replace the
three `agres-*.sh` hook entries in `~/.claude/settings.json` with `agres hook <event>`
entries (settings backed up first; non-agres hooks untouched); replace
`~/.claude/commands/agres.md` with a pointer to the skill. `agres doctor` verifies
all of it. `agres uninstall` reverses it.

**R12 Migration.** `agres migrate [PATH|--all] [--dry-run]` converts v0 stores in
place: old DB → `.agres/v0/agres.db` (with its md/jsonl/cas dirs), new v1
`agres.db`. Mapping: pins→pin (active), decisions→decision (accepted/rejected),
tasks→task (`/complete|done|finished|shipped/i` → done; others open), errors→error,
checkpoints→handoff (reason text), turns→turns, sessions→sessions (ended).
Nested stray stores are merged into their repo-root store (items tagged
`origin=<relpath>`), then archived. The global `~/.agres` gets a routing report
(each item → suggested project by path/term overlap); user confirms; unmatched and
test-fixture rows are archived, not deleted. Hooks seeing a v0 store do nothing
except one SessionStart line: "run `agres migrate`".

**R13 Skill.** `skill/agres/SKILL.md` ≤ 120 lines, spec-valid frontmatter
(`name`, `description` ≤ 1,024 chars third-person, `metadata` for custom keys).
Body covers judgment only: when to record (decision vs rejection vs pin vs note),
replace instead of duplicate, close tasks, write handoffs, search before
re-exploring, what not to store (anything derivable from code/git, secrets, user
preferences — Claude Code auto-memory owns those). No capture instructions.
`REFERENCE.md` = CLI catalog + hook table + no-hooks fallback protocol for agents
without hooks (Codex/opencode/Cursor). One shared adapter snippet replaces the 5
near-duplicate adapters.

## 4. Interfaces

### 4.1 Hooks (Claude Code) — `agres hook <event>` reads the event JSON on stdin

| Event (matcher) | Uses | Does | Output | Mode / timeout |
|---|---|---|---|---|
| SessionStart (`startup\|resume\|clear\|compact`) | `session_id, cwd, source` | upsert session; build pack | `hookSpecificOutput.additionalContext` = pack | sync / 10 s |
| UserPromptSubmit | `session_id, cwd, prompt, prompt_id` | on/off phrases; capture; rejection guard | `additionalContext` only on guard hit / toggle | sync / 5 s |
| Stop | `session_id, cwd, last_assistant_message` | capture reply (dedupe) | none (stdout must stay empty) | async |
| PostToolUse (`Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) | `tool_input.file_path`, `tool_response.bashEditDiff` | touch files | none | async |
| PostCompact | `compact_summary, trigger` | store as handoff (source=compact) | none | async |
| SessionEnd | `session_id, reason` | close session; auto-handoff if none this session (prompts count, files edited, items recorded, last prompt ≤200 chars) | none | sync / 5 s (<1.5 s budget) |

### 4.2 Storage — `<root>/.agres/agres.db`, `PRAGMA user_version = 100`

```sql
meta(key TEXT PRIMARY KEY, value TEXT)
sessions(id TEXT PRIMARY KEY, agent TEXT, title TEXT, cwd TEXT,
         started_at TEXT, last_seen_at TEXT, ended_at TEXT, end_reason TEXT,
         prompts INTEGER DEFAULT 0)
items(id INTEGER PRIMARY KEY,
      kind TEXT NOT NULL CHECK(kind IN ('pin','decision','task','error','note','handoff')),
      status TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, why TEXT,
      files TEXT,            -- JSON array of repo-relative paths
      priority INTEGER NOT NULL DEFAULT 0,   -- 1 = critical
      superseded_by INTEGER REFERENCES items(id),
      session_id TEXT, source TEXT NOT NULL, origin TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)
turns(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT CHECK(role IN ('user','assistant','system')),
      text TEXT NOT NULL, hash TEXT NOT NULL, prompt_id TEXT, created_at TEXT NOT NULL,
      UNIQUE(session_id, role, hash))
fts_items(title, text, why)   -- FTS5 external-content + triggers; LIKE fallback
fts_turns(text)
-- code tables from the 1.0 engine: nodes, edges, files, scans, changes, commits, commit_files, fts_nodes
```

`title` = first sentence of `text`, ≤ 80 chars (or explicit). Session id for CLI
writes: `--session` → `AGRES_SESSION` → most recently seen session of this project
within 12 h → none. Provenance only; nothing is filtered by session.

### 4.3 Module layout (package `agres/`, Python ≥ 3.9, stdlib only; tree-sitter optional)

```
agres/__main__.py  cli.py  paths.py  db.py  memory.py  pack.py  search.py
      hooks.py  migrate.py  install.py  util.py
agres/code/        indexer.py langs.py ts_backend.py rank.py gitwalk.py  (from 1.0)
skill/agres/       SKILL.md REFERENCE.md adapters/AGENTS-snippet.md
tests/             unittest; fixtures/hook-payloads/*.json, fixtures/v0-store builder
bin/agres          shim · bin/agres.js (npm shim, kept working)
```

`hooks.py` must not import `agres.code` on the prompt/stop paths (latency).

## 5. Non-goals

Embeddings/vector search · LLM calls inside hooks (no API cost) · controlling the
harness context window (fold engine dropped) · cross-project/global memory (CLAUDE.md
and Claude Code auto-memory own user-level prefs) · npm publish (repo stays
publishable; publishing is a separate `/ship`) · changing zeus (compat aliases only).

## 6. Failure modes

| Failure | Behaviour |
|---|---|
| Hook exception / timeout | exit 0, no stdout, line in `.agres/hook.log` |
| DB locked | sync hooks give up after 2 s (no injection > wrong injection); async wait 10 s |
| No project root (`/`, `$HOME`, non-git) | hooks no-op; CLI write commands error "no agres project here — `agres init`" |
| v0 store found | read nothing, write nothing; SessionStart emits the migrate line |
| Store schema newer than runtime | read-only; writes refused with upgrade notice |
| Stop fires N times per turn | one turn row (UNIQUE) |
| Pack over budget | drop lowest-priority whole items; footer always kept; never > 9,000 chars |
| Secret pasted in prompt | pattern-redacted on capture; `agres forget ID` for the rest |
| Huge repo | index never runs inside sync hooks; touch is per-file |

## 7. Acceptance criteria

- **A1** Okas after migration: pack has latest handoff, all critical pins, both
  rejections, open tasks, ≥ 40 IDs in the index, ≤ 6,000 chars, p95 < 300 ms (10 runs).
- **A2** From `Okas-Homekit-v1.2/packages/pulsar` every command resolves to the
  Okas root store; no new `.agres` dir is created anywhere (test).
- **A3** cwd `/` or `$HOME`: every hook exits 0, prints nothing, creates nothing.
- **A4** One user + one assistant turn stored when Stop fires 3×; Stop stdout empty.
- **A5** Zeus smoke flow (`topic new` → `log` → `open` → `close`) passes unchanged.
- **A6** `agres search "sacrificial board"` ranks the constraint and the rejection top-2.
- **A7** Skill evals (skill-creator): new ≥ old pass rate on 3 scenarios, fewer
  tokens/tool calls, zero model-issued `agres capture` calls.
- **A8** `python3 -m unittest` green; `agres doctor` green after install.
- **A9** New non-engine code ≤ 2.5k LOC; total incl. the code engine ≤ 5.5k (today
  three runtimes: 5.9k + 3k + 4.9k legacy = 13.8k); no module > 700 lines except the
  `langs.py` data tables.
