---
name: agres
description: "Agres Origami Memory: makes a limited context window behave like a much larger one (verbatim capture, fold overflow out, unfold exact text on demand). USE AUTOMATICALLY without being asked WHEN: writing, editing, or debugging code; any repository task; multi-step or long-running work; needing earlier context, decisions, or file/symbol context. Stop only if the user says stop using Agres."
default_enabled: true  # Tier 1 capture (1 sqlite write) is always on; full fold/unfold loop past 10 turns or 25% budget
storage_backend: "sqlite"
runtime_cli: "agres"
stop_phrases:
  - "stop using Agres"  # matched case-insensitive, runtime normalizes
  - "disable Agres"
  - "Agres off"
  - "agres off"
  - "stop agres"
enable_phrases:
  - "enable Agres"
  - "use Agres"
  - "Agres on"
  - "agres on"
---

# Agres Origami Memory — Fold/Unfold Context Engine

Agres exists for one reason: **make a limited context window behave like a much larger one.**

A model like DeepSeek V4 Flash has a 256k-token context window. A long conversation overflows it. Agres folds the overflow out of the window (keeping every word durably on disk) and unfolds the exact original text back in when the agent needs it — so a 256k window behaves like 1M+ tokens of conversation, **word for word, from the start of the conversation to the end**.

Agres is the default operating memory mode unless the user explicitly disables it.

Prime directive:

**Capture every turn verbatim. Fold context out of the window when it fills. Unfold the exact original wording when it is needed. Never lose a word.**

## Default activation contract

Agres is applied automatically when it helps (tiered):

- Always: capture verbatim turns to durable store (cheap, 1 sqlite write) for ALL sessions
- Tier 2 (after 10 turns or >25% budget or explicit task): full fold/unfold + budget monitoring + checkpoints
- Specifically: long repository tasks, debugging sessions, code-changing requests with persistence need, low-context models (~256k)

For trivial one-shot tasks (<5 turns, <50k tokens) Ponytail Tier 1 only — no budget loops, no checkpoints, minimal overhead.

Continue applying Agres throughout the entire conversation until the user explicitly says one of these stop phrases (case-insensitive):

- stop using Agres
- disable Agres
- Agres off

If the user says one of the stop phrases:

1. Stop applying Agres immediately.
2. Do not reintroduce Agres behavior in the current conversation.
3. Do not re-enable Agres in future sessions until the user explicitly says one of these enable phrases:
   - enable Agres
   - use Agres
   - Agres on

## The problem Agres solves

Limited context windows truncate long conversations. When the window fills:

- Old instructions are dropped from the model's view.
- Decisions, constraints, and rejected approaches fade.
- The model cannot recall exact wording of earlier turns.
- Behavior drifts; work is redone.

Agres prevents this with a **fold/unfold engine**:

- **Capture**: every conversation turn is stored verbatim in durable local storage.
- **Fold**: when the active window approaches its budget, older / lower-priority context is folded out of the window. Its exact text is preserved with a summary.
- **Unfold**: when the agent needs an earlier passage, it searches folded context and restores the **exact original text** back into the window.
- **Window budget**: the agent always knows how many tokens are in the active window and how close it is to the limit.

The effect: the model's 256k window behaves like a much larger window because nothing is ever lost — everything is one deterministic unfold away.

## Storage backend

This Agres installation uses SQLite as the primary structured memory store.

Use:

- SQLite for structured state, indexes, files, symbols, tasks, decisions, errors, pins, checkpoints, turns, folds, and window manifests.
- JSONL event logs for append-only session events.
- Content-addressed storage for large text payloads and artifacts.
- Markdown files for human-readable session memory.
- SQLite FTS5 for keyword search when available.
- LIKE fallback search when FTS5 is unavailable.

Retention:

- Bounded growth via `agres prune` + `agres gc` + per-file transactions + CAS GC
- WAL + busy_timeout for concurrent CLI
- Visual `agres status` shows a plain-language dashboard (memory, conversation, brain, storage, fixes) in realtime

Do not require:

- external vector databases
- embedding models
- cloud services
- remote APIs

Vector search may be added later only if the user explicitly enables it.

## Runtime CLI

If the local `agres` CLI exists, use it for memory operations.

### Core memory commands

agres init
agres start "session goal"
agres status
agres index
agres touch --files <edited-files>   # after EVERY file edit — keeps index fresh
agres sync                           # drift scan + prune deleted (or --since HEAD~1)
agres map --tokens 1024              # ranked skeleton: hubs first, fits budget
agres repo-map
agres graph --build                  # code-to-graph: files+symbols+tasks+decisions+git in one graph
agres graph --query "..."            # subgraph neighborhood (BFS) for a question
agres graph --export json|dot        # dump .agres/graph.json|dot
agres trace                          # whole-project trace: codebase, structure, progress, work done
agres brain [--query "..."]          # context injection: one bounded pack carrying the whole project
agres context --query "search query"
agres search --query "search query"
agres decision --text "decision" --accepted
agres decision --text "decision" --rejected
agres task --objective "task"
agres error --message "error"
agres pin --text "important context"
agres checkpoint --reason "reason"
agres resume
agres end

### Fold/unfold commands (the context engine)

agres capture --role user --text "verbatim user turn"
agres capture --role assistant --text "verbatim assistant turn"
agres status                 # plain-language dashboard: memory, conversation, brain, storage, fixes
agres status --json          # JSON for CI (adds progress, graph, packs)
agres budget                 # visual budget bar + confidence (auto-fold hints)
agres window                 # visual manifest (priority breakdown)
agres budget --json          # JSON
agres window --json          # JSON manifest
agres fold --target 200000   # fold low-priority items until <= 200k tokens in window
agres fold --target 0        # fold everything foldable (keep only pinned)
agres unfold --query "login timeout AuthService"   # restore exact matching text
agres unfold --fold-id fold_xxx                     # restore one specific fold
agres window                 # show the current window manifest
agres prune --keep-days 30 --dry-run  # prune old data + VACUUM (bounded growth)
agres gc --dry-run           # GC unreferenced CAS
agres repair                 # fix ghost sessions, WAL, FTS, integrity

Set the budget for a low-context model:

export AGRES_WINDOW_BUDGET=262144   # 256k tokens (e.g. DeepSeek V4 Flash)

## Core principle

Agres uses an origami-style memory model:

Raw Context
  -> Verbatim turn (stored durably)
  -> Fold (removed from active window, exact text + summary kept)
  -> Unfold (exact original text restored to active window)
  -> Active Window Manifest (token-budgeted)

When context is needed, unfold only what is needed:

- Exact verbatim passage (full original text)
- Summary only (when a pointer is enough)
- Partial content

Never reload an entire conversation into the window unless necessary.

## The agent's operating loop (follow this every session)

### 0. Brain first — understand the whole project before acting

On session start (or resume), inject project memory before reading files:

agres brain [--query "<task>"]       # bounded pack: identity + structure + progress + graph + turns
agres trace                          # full trace when you need depth (writes .agres/trace.md)
agres graph --build                  # rebuild after index/sync, so the graph matches the code

The brain pack is the model's working memory of the project. Rebuild the
graph after structural changes (`index`/`sync`/`touch` on many files);
re-read `brain` after checkpoints. Never re-derive architecture by
re-reading the whole repo — the graph already holds it.

### 1. Capture — every turn, verbatim

After each user turn and each assistant reply, capture the turn verbatim:

agres capture --role user --text "<the user's exact words>"
agres capture --role assistant --text "<your exact reply>"

Capture the full wording. Do not paraphrase, summarize, or truncate. The
verbatim store is the source of truth that makes later unfold exact.

Also fold durable facts into structured memory as you go:

agres decision --text "..." --accepted
agres decision --text "..." --rejected
agres task --objective "..."
agres error --message "..."
agres pin --text "context that must never fade"

### 2. Monitor the budget

Periodically (and before any long action), check window usage:

agres budget

The report shows:

- Budget (default 256k; set via AGRES_WINDOW_BUDGET).
- Active window usage in tokens.
- What is currently in the window.
- What is folded.

### 3. Fold when the window fills

When usage approaches the usable budget (budget minus reserve), fold the
oldest / lowest-priority context out of the active window:

agres fold --target 200000

This removes older turns from the active window while keeping their exact
text + summary in the folds store. Pinned items are never auto-folded.

### 4. Unfold when you need earlier context

When a task requires something from earlier in the conversation, unfold it
with its exact original wording:

agres unfold --query "what the user said about refresh tokens"

or by specific fold:

agres unfold --fold-id fold_xxx

Unfolded text re-enters the active window verbatim. The model then sees the
exact original words, not a paraphrase.

### 5. Checkpoint and resume

Before pausing, checkpoint:

agres checkpoint --reason "pausing mid-task"

On resume:

agres resume

The checkpoint restores the session goal, decisions, tasks, pins, errors,
and the window manifest.

## Brain — project memory as a graph

Agres is the model's memory and brain for the project, not just for the
conversation. Three commands:

- `agres graph --build` materializes one property graph in SQLite:
  `file|symbol|task|decision|error|pin|checkpoint|session|commit` nodes;
  `contains|imports|uses|touches|mentions|owns|about|covers|child_of` edges.
  Code structure comes from the index (imports, tree-sitter refs, PageRank);
  progress links are deterministic text→file mentions (no embeddings).
- `agres trace` walks the whole project — file/language stats, PageRank
  hubs, tasks by status, decisions accepted/rejected, recent errors, pins,
  checkpoints, git log + churn, health (FTS, stale packs, graph freshness).
- `agres brain [--query "..."]` fuses trace + graph neighborhood + pins /
  decisions / tasks / recent turns into one token-bounded injection pack
  (receipt + stale tracking like `context`). Read it at session start and
  after resume; it is cheaper than re-exploring and never goes stale
  silently (reindex marks packs stale).

Retrieval priority addition: graph neighborhood (BFS around query seeds)
ranks above raw chunk search for architecture questions; file reads still
win for line-level depth.

## Fold types
Use these fold kinds (stored as `folds.kind`; 14 logical types map to 4 physical + metadata):

1. ConversationFold — verbatim turn (kind=turn, priority 1)
2. DecisionFold — (kind=decision, priority 3 pinned)
3. TaskFold — (kind=task)
4. FileFold — (kind=file, via files/file_chunks table)
5. SymbolFold — (kind=symbol)
6. DiffFold — (kind=diff)
7. ErrorFold — (kind=error)
8. TestFold — (kind=test)
9. CommandFold — (kind=command)
10. SearchFold — (kind=search)
11. RepoMapFold — (kind=repo_map)
12. DependencyFold — (kind=dependency)
13. ArchitectureFold — (kind=architecture)
14. KnowledgeFold — (kind=knowledge)

All 14 share the same `folds` table (kind TEXT, indexed) — ponytail: one table, not 14

## Token budget

Use bounded context packs.

Example budget (256k window):

system instructions:        10%
task objective:             10%
pinned/decisions:           10%
recent turns:               35%
unfolded context:           20%
repo/architecture summary:   5%
reserved for response:       5% (8k tokens minimum)

If over budget:

- Fold the oldest turns first (they are preserved verbatim).
- Keep pinned and decision context unless explicitly unpinned.
- Unfold only the specific passages needed, not whole conversations.

## Retrieval priority

Use this priority:

1. Pinned items (never auto-fold).
2. Active task items.
3. Approved decisions and rejected approaches.
4. Explicitly mentioned files/symbols.
5. Recently edited files.
6. Failing tests/errors.
7. Exact SQLite lookup.
8. Fold search (FTS5 over folded original text).
9. SQLite FTS keyword search.
10. LIKE fallback search.
11. LLM-assisted retrieval.

## Long session preservation

Important things must not remain only in chat.

Extract durable folds:

- Chat message -> DecisionFold
- Chat message -> ConstraintFold
- Chat message -> TaskFold
- Chat message -> PreferenceFold
- Chat message -> VerbatimTurn (always)

Maintain rolling summaries:

- Turn summary.
- Topic summary.
- Task summary.
- Session summary.
- Project summary.

Pinned context must remain until explicitly unpinned or session ends.

Create checkpoints at:

- Plan approval.
- Edit approval.
- Edit rejection.
- Task completion.
- Test run.
- Build run.
- Major refactor step.
- Session pause.
- Session resume.
- Context pack creation.
- After a fold operation.

## Rejection handling

When the user rejects something:

1. Store rejection as DecisionFold.
2. Mark affected task node invalid.
3. Mark downstream dependent work stale.
4. Keep unrelated context.
5. Preserve rejected attempt.
6. Regenerate only affected part.

Never repeat a rejected approach unless the user explicitly reopens it.

## What Agres must not do

Agres must not:

- Dump entire repositories into prompt.
- Treat raw chat transcript as the only memory.
- Forget rejected approaches.
- Rebuild everything after a small edit.
- Lose context after restart.
- Let stale context override fresh context.
- Require external services.
- Require embedding models by default.
- Assume the model remembers.
- Paraphrase when unfolding — always restore exact original wording.
- Let the window truncate silently — fold instead, so nothing is lost.
- Re-explore the repo from scratch each session — read `brain` first.
- Let the graph drift from the code — rebuild after index/sync.

## Final rule

Agres exists so that the agent behaves as if it retains all relevant context until the user ends the session, without forcing the model to hold everything in raw tokens. **Every word of the conversation is preserved verbatim and is one unfold away — even when the model's context window is only 256k.**

And Agres is the project's brain: **the whole repo — code, structure, progress, work done — lives as one queryable graph, and one bounded `brain` pack injects it into any session, so no progress or context is ever lost.**
