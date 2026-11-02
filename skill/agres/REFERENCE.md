# Agres reference

Contents: [Commands](#commands) · [Origami](#origami) · [The memory block](#the-memory-block) ·
[Hooks](#hooks) · [Settings](#settings) · [Storage](#storage) · [Install](#install) ·
[Without hooks](#without-hooks) · [Migrating from 0.x](#migrating-from-0x) ·
[Evaluation](#evaluation) · [Compatibility aliases](#compatibility-aliases)

## Commands

Every command accepts `--session ID` (provenance stamp) and `-C DIR` (run as if in DIR).

### Recall

| Command | Does |
|---|---|
| `agres search WORDS… [--kind K]… [--all] [--no-code] [--limit N] [--json]` | Ranked hits across memory items, folds, conversation turns and code symbols (weighted reciprocal-rank fusion; AND-matches before OR-matches). `--all` includes closed and superseded items. Kinds: pin, decision, task, error, note, handoff, fold, turn, code. |
| `agres show ID [--around N]` | Exact text of an item (`D12`) with why, files, source tag, and the replaces / superseded-by chain; for a turn (`M40`), `--around N` adds N turns either side from the same session; for a fold (`F3`), same as `unfold F3`. |
| `agres unfold F<id> [--lines A-B] [--grep TEXT]… [--regex RE] [--context N] [--all]` | Exact text of a fold. Lines are counted on `\n` only, like a shell. Bounded to 200 lines / 16,000 chars unless `--all` (a longer single line is shown cut, never dropped). `--grep` is a literal, case-insensitive substring, repeatable (any matches); `--regex` is an opt-in Python regex checked against the first 4,000 chars of each line. |
| `agres brain [--query Q] [--budget TOKENS \| --chars N]` | Prints the memory block (same as the SessionStart hook). `--query` adds a "Related" section. |
| `agres map [--query Q] [--focus FILE]… [--tokens N]` | Aider-style code skeleton: files ranked by personalised PageRank with their top signatures, fitted to ~N tokens. Indexes on first use. |

### Record

| Command | Creates | Closes with |
|---|---|---|
| `agres decide TEXT [--why W] [--files F…] [--replaces ID]` | D (accepted) | `drop` → retracted, or a newer item `--replaces` it |
| `agres reject TEXT [--why W] [--replaces ID]` | D (rejected) | — |
| `agres pin TEXT [--critical] [--files F…] [--replaces ID]` | P | `drop` → retired |
| `agres task TEXT [--files F…]` | T (open) | `done` → done, `drop` → dropped |
| `agres error TEXT [--why W]` | E (open) | `done` → fixed, `drop` → dropped |
| `agres note TEXT` | N | `drop` → retired |
| `agres handoff TEXT` | H — supersedes the previous handoff | — |
| `agres fold SUMMARY (--turns M40-M88 \| --stdin) [--title T]` | F (manual fold) | `forget F<id>` |
| `agres done ID… [--note N]` / `agres drop ID… [--note N]` | — | — |
| `agres forget ID… [--session SID] [--dry-run]` | items: redacts text permanently (tombstone row kept, status kept if already closed); turns (`M…`) and folds (`F…`): deleted; `--session SID`: that session's turns and tool/agent folds deleted (its handoffs and span summaries stay). The ID prefix must match the item's kind. | — |

Every write prints up to 3 similar live items so near-duplicates can be replaced instead of
added. Items are never hard-deleted; replaced items get status `superseded` and a link to their
successor. Captured turns and tool/agent folds are raw capture: `forget`, `gc` and retention
delete them.

### Admin

| Command | Does |
|---|---|
| `agres init [DIR] [--here]` | Create a store — at the git root by default, in the current directory with `--here`, never in `/` or `$HOME`. Hooks create one automatically in git checkouts (`auto_init`). |
| `agres status [--deep] [--json]` | One screen: live memory counts, what the next session sees (pack size, items reachable / in full), **attention** items each with the command that fixes them (no critical pins, stale tasks, hook errors, silent capture, unwired hooks, leftover 0.x stores), 7-day activity, hook health, code index, store. `--deep` adds the hook log tail. |
| `agres dash [--watch SECS] [--json] [--width N] [--no-color]` | Full-screen dashboard: attention, memory by kind and status, the next session's block section by section, constraints and rejections, open work, origami folds, 14-day activity sparklines, recent sessions, hook health, code index by language, privacy and store. Two columns from 120 columns; `--watch` redraws live. Alias `dashboard`. |
| `agres graph [--port 7357] [--host 127.0.0.1] [--no-open] [--index]` | Local web server + browser page: an animated, interactive graph of the code index (files level: symbol links rolled up to weighted file→file links; symbols level: top-ranked functions/classes) with memory items and folds linked to the files they name. Signals pulse along links, live updates stream in (server-sent events) and flash what changed. Drag, pan, zoom, search (`/`), lens Code/Memory/Both, inspector with neighbours and the matching CLI command. Read-only except "Build index"; refuses foreign Host/Origin headers. Alias `serve`. |
| `agres index [--full]` / `agres touch FILE…` | Sync the code graph / re-parse specific files (global edge resolution is deferred to the next query). |
| `agres on` / `agres off` | Resume / pause capture and injection for this project (`.agres/off`). |
| `agres config [KEY [VALUE]] [--project]` | Show or set settings (global by default). |
| `agres gc [--turns-older-than DAYS] [--dry-run]` | Optimise FTS and VACUUM; optionally prune turns older than DAYS (> 0; items are never pruned). `--dry-run` prints the cutoff and count and deletes nothing. |
| `agres migrate [ROOT] [--all] [--dry-run] [--rollback] [--global] [--routes FILE]` | Upgrade 0.x stores. |
| `agres install [--plugin] [--dry-run] [--yes]` / `agres uninstall` / `agres doctor` | Wire hooks, skill links and the shim (or, with `--plugin`, hand hooks and the Claude skill to the plugin); undo; verify every skill folder and flag double wiring. |
| `agres hook EVENT` | Internal: run a Claude Code hook with its JSON on stdin. |

## Origami

Fold big context out of the window, unfold the exact text by ID. The raw store stays complete;
only the model's view is folded. Previews are deterministic, never an LLM summary.

| Kind | Trigger | Body |
|---|---|---|
| tool | PostToolUse (sync) on Bash: stdout or stderr over `fold_chars` (default 12,000) | full output; the model gets the first `fold_head` (40) lines, ≤ 30 numbered salient lines (every previewed line cut at 300 chars, the whole preview ≤ 6,000 chars) (error, fail, exception, traceback, warning, panic, fatal, assert, denied, undefined, not found), the last `fold_tail` (20) lines and a stub `[agres fold F12: N lines, K KB folded …]` |
| agent | PostToolUse (async) on `Agent`/`Task` | subagent brief + result |
| span | PostCompact | none; points at turns `M<a>–M<b>` since the previous span fold; summary = `compact_summary` |
| manual | `agres fold` | `--turns` range and/or `--stdin` text |

Never folded: output of a command that runs `agres` (in command position: start, after `;`/`&&`/`|`/`(`/quotes/backticks/`$(`, as a path ending in `/agres`, or `python -m agres` — so no unfold → refold loop; `cd ~/Dev/agres && make` still folds), interrupted calls, images,
`Read` results (Edit needs them), anything with `fold off` or the project off.

How it reaches the window: a PostToolUse hook may return
`{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {...}}}`; the value
must be an object shaped like `tool_response` (`stdout`, `stderr`, …) — a plain string is ignored.
Over ~30,000 chars Claude Code itself keeps the full output in a file and hands the hook only the first 30,000 chars plus `persistedOutputPath`; the fold stores that whole file, so unfold stays exact. Verified on Claude Code 2.1.316 by probe (a 92 KB build log reached the model as ~2.5k chars with its error line); if a future version ignores it, output passes through
unfolded (safe), and `python3 -m agres.evalrun` will still pass while the live probe would not.

Research behind the design (2024–2026): Context-Folding branch/return (arXiv 2510.11967),
AgentFold granular vs deep folds (2510.24699), ACON compression guidelines (2510.00615), Memex
indexed archive + dereference (2603.04257), LCM lossless pointers (2605.04050), Anthropic's
context-engineering and context-editing docs (keep full history, clear the view; subagents return
1–2k-token results), and Factory.ai's compression probes (anchored, incremental summaries drift less;
artifact tracking — paths, errors — is what compression loses first). Principles taken: fold at work
boundaries, keep the raw record, fixed-schema summaries (Goal · Done · Decisions · Artifacts · Open ·
Next), verbatim artifacts, exact bounded unfold, a small re-injected index, measure probes not ratio.

## The memory block

Built by `agres brain` and the SessionStart hook. Default 6,000 characters, hard maximum 9,000
(Claude Code replaces hook context over 10,000 characters with a file path). Sections in priority
order; whole items are dropped when space runs out, never cut mid-word:

1. Header — project, branch@sha, dirty-file count; optional notice (off, v0, after compaction);
   one line framing the block as evidence, not instructions. Rows not written through the agent CLI
   carry a tag: `[imported]` (migration), `[auto]`, `[compact]`.
2. Last handoff (≤ 700 chars).
3. Constraints — critical pins, in full.
4. Rejected — newest 10 rejected decisions with their why.
5. Open work — open tasks touched in the last 21 days (older ones become "(stale)" in the index) and open errors.
5b. Folded — up to 8 recent folds, summaries before raw tool folds, `F<id> [range or size] gist`.
6. Related — only with `--query`.
7. Pinned — other pins, ranked by overlap with dirty files, then recency (up to 12).
8. Recent decisions — newest 8 accepted.
9. More — every remaining live item as `ID title`, then `+N more`.
10. Footer — recall and record commands.

About 20% of the budget is held back for "More" whenever there are more items than sections 7–8
can show, so IDs stay discoverable.

## Hooks

Shipped in the plugin's `hooks/hooks.json` (or written to `~/.claude/settings.json` by
`agres install` without `--plugin`; both come from `install.hook_entries`). Every hook exits 0;
failures are written to `.agres/hook.log`.

| Event (matcher) | Command | Mode | Does |
|---|---|---|---|
| SessionStart (all sources) | `agres hook session-start` | sync, 10 s | Registers the session, injects the memory block via `additionalContext` (also after `/compact`). Outside a git checkout: nothing. |
| UserPromptSubmit | `agres hook prompt` | sync, 5 s | "agres off/on" phrases; stores the prompt (skips harness-generated prompts); rejection guard — if the prompt overlaps a rejected decision or critical pin, injects ≤ 3 lines, once per item per session. |
| Stop | `agres hook stop` | async | Stores `last_assistant_message`; deduped per (session, prompt_id, text). Prints nothing — output on Stop would continue the conversation. |
| PostToolUse (`Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) | `agres hook edit` | async | Re-indexes edited files; after Bash also re-touches what `git status` reports changed (≤ 200 files, needs an index) since `bashEditDiff` is rarely present; remembers edits for the auto-handoff. |
| PostToolUse (`Bash`) | `agres hook fold` | sync, 10 s | Origami tool fold (see above). Small outputs exit before the CLI loads (~40 ms). |
| PostToolUse (`Agent\|Task`) | `agres hook agent-fold` | async | Stores the subagent's brief + result as an agent fold. |
| PostCompact | `agres hook post-compact` | async | Stores `compact_summary` as the current handoff and as a span fold over the compacted turns. |
| SessionEnd | `agres hook session-end` | sync, 5 s | Applies turn retention (once a day); closes the session; writes an auto-handoff if the session had prompts but no handoff. |

## Settings

`~/.config/agres/config.json` (global) ← plugin options (`/plugin` settings; reach hooks as
`CLAUDE_PLUGIN_OPTION_<KEY>`) ← `<root>/.agres/config.json` (`--project`).

| Key | Default | Meaning |
|---|---|---|
| `auto_init` | `true` | Hooks/CLI create `.agres/` at a git root on first use |
| `budget_chars` | `6000` | Memory block size (max 9000) |
| `guard` | `true` | Rejection guard on prompts |
| `capture` | `all` | `all` (prompts + replies), `prompts`, or `none` |
| `skip_prefixes` | harness markers | Prompts starting with these are not stored as human turns |
| `turn_retention_days` | `0` | > 0: session end deletes captured turns and tool/agent folds older than this (once a day). Items are never pruned. |
| `redact_patterns` | `[]` | Extra regexes replaced by `[redacted]` before anything is stored. CLI: one regex (`agres config redact_patterns 'ACME-[0-9]{1,6}'`) or a JSON list (`'["a\\d+", "b"]'`). Best effort. |
| `fold` | `true` | Origami tool/agent folds |
| `fold_chars` | `12000` | Fold a Bash stdout/stderr longer than this (floor 1000) |
| `fold_head` / `fold_tail` | `40` / `20` | Lines kept verbatim at the top / bottom of a folded output |
| `fold_tools` | `["Bash"]` | Tools whose output may be folded |

## Storage

One SQLite file per project, `<root>/.agres/agres.db` (`PRAGMA user_version = 101`; 100 stores
upgrade in place by adding the fold tables), WAL mode.
Root = nearest ancestor with `.agres/agres.db` or `.git`; `/` and `$HOME` are never roots.
`.agres/.gitignore` contains `*`, so nothing is committed.

- `items` — pin / decision / task / error / note / handoff with status, why, files, priority,
  `superseded_by`, session, source, timestamps. FTS index includes identifier sub-words.
- `turns` — verbatim prompts and replies (`<private>…</private>` stripped, common secret formats
  redacted, 64 KB cap per turn).
- `sessions` — provenance only; nothing is filtered by session.
- `folds` — origami folds (`kind`, title, summary, body, meta JSON with command / turn range).
- `nodes`, `edges`, `files`, `commits` … — the code graph (bi-temporal edges: closed, never deleted).

## Install

```bash
claude plugin marketplace add ~/Dev/agres     # or purple-claw/agres
claude plugin install agres@agres
agres install --plugin --dry-run              # what moves out of settings.json / ~/.claude/skills
agres install --plugin --yes                  # backups + manifest in ~/.agres-archive/
agres doctor
```

`agres uninstall` undoes the last install manifest; `claude plugin uninstall agres` removes the
plugin.

## Without hooks

Codex, opencode, Cursor and other agents without Claude Code hooks use the same store manually:

1. At task start run `agres brain` and read it (it is what the hook would have injected).
2. Record judgment with the same commands (`decide`, `reject`, `pin`, `task`, `done`, `handoff`).
3. Optionally store important exchanges with `agres capture --role user|assistant --text "…"`;
   skip it for routine turns — it costs output tokens.
4. Run `agres touch FILE…` after large edits if you rely on `agres map`.

The project-rules snippet in `adapters/AGENTS-snippet.md` says this in five lines.

## Migrating from 0.x

0.x stores (`.agres/agres.db` with fold/window tables) are left untouched by 1.0 until migrated;
the SessionStart hook says so in one line.

```bash
agres migrate --dry-run          # report for the current project (and nested 0.x stores under it)
agres migrate                    # convert; the 0.x files move to .agres/v0/
agres migrate --rollback         # put the 0.x store back
agres migrate --all --dry-run    # every 0.x store under ~
agres migrate --global           # route items from the old global ~/.agres store to projects
```

Mapping: pins → pin; decisions → accepted/rejected; tasks → done when the text says
complete/done/finished/shipped/resolved, else open; errors → error; meaningful checkpoints →
handoffs (newest active); turns and sessions copied. Nested stray stores inside a project are
merged into the root store (`origin` = their path) and archived.

## Evaluation

`python3 -m agres.evalrun [--json]` seeds a throwaway project from `evals/memory/cases.json` and
reports recall@3, superseded results returned (should be 0), abstention on unrelated queries, the
rejection guard, pack size, fold ratio, salient-line retention, byte-exact unfold, no refold of
unfold output, and SessionStart / fold hook p95 latency. Categories follow MemoryAgentBench and
LongMemEval, adapted to coding-agent memory; it is a small regression set, not a benchmark.

## Compatibility aliases

Kept for zeus and 0.x habits; hidden from `--help`:
`start GOAL` (prints a `sess_…` id) · `end` · `resume` (= brain) · `capture --role --text` ·
`decision --text … --accepted|--rejected` · `task --objective … [--status done]` ·
`error --message … [--context]` · `pin --text …` · `checkpoint --reason …` (= handoff) ·
`context --query …` and `unfold --query …` without an `F` id (= search) · `sync` (= index) ·
`repo-map` (= map) · `trace` (= status) · `prune|repair` (= gc). `fold --target …` (v0 window folding),
`budget`, `window`, `graph`, `event` print a notice.
