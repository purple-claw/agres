---
name: agres
description: "Agres Origami Memory: makes a limited context window (e.g. 256k tokens on DeepSeek V4 Flash) behave like a 1M+ window by capturing every turn verbatim, folding older context out of the active window, and unfolding exact original text on demand. Use by default in every coding conversation until the user explicitly says stop using Agres."
default_enabled: true
storage_backend: "sqlite"
runtime_cli: "agres"
stop_phrases:
  - "stop using Agres"
  - "disable Agres"
  - "Agres off"
enable_phrases:
  - "enable Agres"
  - "use Agres"
  - "Agres on"
---

# Agres Origami Memory — Fold/Unfold Context Engine

Agres exists for one reason: **make a limited context window behave like a much larger one.**

A model like DeepSeek V4 Flash has a 256k-token context window. A long conversation overflows it. Agres folds the overflow out of the window (keeping every word durably on disk) and unfolds the exact original text back in when the agent needs it — so a 256k window behaves like 1M+ tokens of conversation, **word for word, from the start of the conversation to the end**.

Agres is the default operating memory mode unless the user explicitly disables it.

Prime directive:

**Capture every turn verbatim. Fold context out of the window when it fills. Unfold the exact original wording when it is needed. Never lose a word.**

## Default activation contract

Agres is applied automatically to:

- every coding conversation
- every repository task
- every debugging session
- every code-changing request
- every long-running session
- every low-context-window model session (e.g. 256k models)

Continue applying Agres throughout the entire conversation until the user explicitly says one of these stop phrases:

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
agres repo-map
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
agres budget                 # show active window usage vs. the budget
agres fold --target 200000   # fold low-priority items until <= 200k tokens in window
agres fold --target 0        # fold everything foldable (keep only pinned)
agres unfold --query "login timeout AuthService"   # restore exact matching text
agres unfold --fold-id fold_xxx                     # restore one specific fold
agres window                 # show the current window manifest

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

## Fold types

Use these fold types:

1. ConversationFold — verbatim turn, folded when old.
2. DecisionFold
3. TaskFold
4. FileFold
5. SymbolFold
6. DiffFold
7. ErrorFold
8. TestFold
9. CommandFold
10. SearchFold
11. RepoMapFold
12. DependencyFold
13. ArchitectureFold
14. KnowledgeFold

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

## Final rule

Agres exists so that the agent behaves as if it retains all relevant context until the user ends the session, without forcing the model to hold everything in raw tokens. **Every word of the conversation is preserved verbatim and is one unfold away — even when the model's context window is only 256k.**
