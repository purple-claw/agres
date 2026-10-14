# Agres reference

Contents: [Commands](#commands) · [The memory block](#the-memory-block) · [Hooks](#hooks) ·
[Settings](#settings) · [Storage](#storage) · [Without hooks](#without-hooks) ·
[Migrating from 0.x](#migrating-from-0x) · [Compatibility aliases](#compatibility-aliases)

## Commands

Every command accepts `--session ID` (provenance stamp) and `-C DIR` (run as if in DIR).

### Recall

| Command | Does |
|---|---|
| `agres search WORDS… [--kind K]… [--all] [--no-code] [--limit N] [--json]` | Ranked hits across memory items, conversation turns and code symbols (weighted reciprocal-rank fusion; AND-matches before OR-matches). `--all` includes closed and superseded items. Kinds: pin, decision, task, error, note, handoff, turn, code. |
| `agres show ID [--around N]` | Exact text of an item (`D12`) with why, files, and the replaces / superseded-by chain; for a turn (`M40`), `--around N` adds N turns either side from the same session. |
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
| `agres done ID… [--note N]` / `agres drop ID… [--note N]` | — | — |
| `agres forget ID` | redacts text permanently (tombstone row kept) | — |

Every write prints up to 3 similar live items so near-duplicates can be replaced instead of
added. Nothing is hard-deleted; replaced items get status `superseded` and a link to their
successor.

### Admin

| Command | Does |
|---|---|
| `agres init [DIR] [--here]` | Create a store — at the git root by default, in the current directory with `--here`, never in `/` or `$HOME`. Hooks create one automatically in git checkouts (`auto_init`). |
| `agres status [--deep] [--json]` | One screen: live memory counts, what the next session sees (pack size, items reachable / in full), **attention** items each with the command that fixes them (no critical pins, stale tasks, hook errors, silent capture, unwired hooks, leftover 0.x stores), 7-day activity, hook health, code index, store. `--deep` adds the hook log tail. |
| `agres index [--full]` / `agres touch FILE…` | Sync the code graph / re-parse specific files (global edge resolution is deferred to the next query). |
| `agres on` / `agres off` | Resume / pause capture and injection for this project (`.agres/off`). |
| `agres config [KEY [VALUE]] [--project]` | Show or set settings (global by default). |
| `agres gc [--turns-older-than DAYS]` | Optimise FTS and VACUUM; optionally prune old turns (items are never pruned). |
| `agres migrate [ROOT] [--all] [--dry-run] [--rollback] [--global] [--routes FILE]` | Upgrade 0.x stores. |
| `agres install [--dry-run] [--yes]` / `agres uninstall` / `agres doctor` | Wire hooks, skill links and the shim; undo; verify. |
| `agres hook EVENT` | Internal: run a Claude Code hook with its JSON on stdin. |

## The memory block

Built by `agres brain` and the SessionStart hook. Default 6,000 characters, hard maximum 9,000
(Claude Code replaces hook context over 10,000 characters with a file path). Sections in priority
order; whole items are dropped when space runs out, never cut mid-word:

1. Header — project, branch@sha, dirty-file count; optional notice (off, v0, after compaction).
2. Last handoff (≤ 700 chars).
3. Constraints — critical pins, in full.
4. Rejected — newest 10 rejected decisions with their why.
5. Open work — open tasks touched in the last 21 days (older ones become "(stale)" in the index) and open errors.
6. Related — only with `--query`.
7. Pinned — other pins, ranked by overlap with dirty files, then recency (up to 12).
8. Recent decisions — newest 8 accepted.
9. More — every remaining live item as `ID title`, then `+N more`.
10. Footer — recall and record commands.

About 20% of the budget is held back for "More" whenever there are more items than sections 7–8
can show, so IDs stay discoverable.

## Hooks

Installed into `~/.claude/settings.json` by `agres install`. Every hook exits 0; failures are
written to `.agres/hook.log`.

| Event (matcher) | Command | Mode | Does |
|---|---|---|---|
| SessionStart (all sources) | `agres hook session-start` | sync, 10 s | Registers the session, injects the memory block via `additionalContext` (also after `/compact`). Outside a git checkout: nothing. |
| UserPromptSubmit | `agres hook prompt` | sync, 5 s | "agres off/on" phrases; stores the prompt (skips harness-generated prompts); rejection guard — if the prompt overlaps a rejected decision or critical pin, injects ≤ 3 lines, once per item per session. |
| Stop | `agres hook stop` | async | Stores `last_assistant_message`; deduped per (session, prompt_id, text). Prints nothing — output on Stop would continue the conversation. |
| PostToolUse (`Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) | `agres hook edit` | async | Re-indexes edited files (Bash via `tool_response.bashEditDiff`); remembers them for the auto-handoff. |
| PostCompact | `agres hook post-compact` | async | Stores `compact_summary` as the current handoff. |
| SessionEnd | `agres hook session-end` | sync, 5 s | Closes the session; writes an auto-handoff if the session had prompts but no handoff. |

## Settings

`~/.config/agres/config.json` (global) overlaid by `<root>/.agres/config.json` (`--project`).

| Key | Default | Meaning |
|---|---|---|
| `auto_init` | `true` | Hooks/CLI create `.agres/` at a git root on first use |
| `budget_chars` | `6000` | Memory block size (max 9000) |
| `guard` | `true` | Rejection guard on prompts |
| `capture` | `all` | `all` (prompts + replies), `prompts`, or `none` |
| `skip_prefixes` | harness markers | Prompts starting with these are not stored as human turns |

## Storage

One SQLite file per project, `<root>/.agres/agres.db` (`PRAGMA user_version = 100`), WAL mode.
Root = nearest ancestor with `.agres/agres.db` or `.git`; `/` and `$HOME` are never roots.
`.agres/.gitignore` contains `*`, so nothing is committed.

- `items` — pin / decision / task / error / note / handoff with status, why, files, priority,
  `superseded_by`, session, source, timestamps. FTS index includes identifier sub-words.
- `turns` — verbatim prompts and replies (`<private>…</private>` stripped, common secret formats
  redacted, 64 KB cap per turn).
- `sessions` — provenance only; nothing is filtered by session.
- `nodes`, `edges`, `files`, `commits` … — the code graph (bi-temporal edges: closed, never deleted).

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

## Compatibility aliases

Kept for zeus and 0.x habits; hidden from `--help`:
`start GOAL` (prints a `sess_…` id) · `end` · `resume` (= brain) · `capture --role --text` ·
`decision --text … --accepted|--rejected` · `task --objective … [--status done]` ·
`error --message … [--context]` · `pin --text …` · `checkpoint --reason …` (= handoff) ·
`context|unfold --query …` (= search) · `sync` (= index) · `repo-map` (= map) · `trace` (= status) ·
`prune|repair` (= gc). `fold`, `budget`, `window`, `graph`, `event` print a removal notice.
