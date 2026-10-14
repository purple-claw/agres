# Agres REFERENCE — full command catalog and internals

Companion to SKILL.md (injector + watchdog quickstart). Come here when a
core command is not enough, or for very long sessions where the window
itself fills (fold/unfold overflow tools, last resort — normal work never
needs them).

## The problem Agres solves

Limited windows truncate long conversations: old instructions drop,
decisions fade, exact wording is lost, behavior drifts, work is redone.
Agres folds overflow out (exact text + summary kept) and unfolds exact text
back on demand — nothing is ever lost.

## Storage backend

SQLite is the primary structured memory store:

- Structured state, indexes, files, symbols, tasks, decisions, errors,
  pins, checkpoints, turns, folds, window manifests.
- JSONL event logs (append-only session events).
- Content-addressed storage (large payloads, artifacts).
- Markdown files (human-readable session memory).
- SQLite FTS5 keyword search; LIKE fallback when FTS5 is unavailable.

Retention: bounded growth via `agres prune` + `agres gc` + per-file
transactions + CAS GC. WAL + busy_timeout for concurrent CLI.
`agres status` shows the plain-language dashboard in realtime.

Do not require: external vector DBs, embedding models, cloud, remote APIs.
Vector search only if the user explicitly enables it later.

## Full command list

```bash
agres init
agres start "session goal"
agres status
agres status --json          # JSON for CI (adds progress, graph, packs)
agres index
agres touch --files <edited-files>
agres sync                   # drift scan + prune deleted (or --since HEAD~1)
agres map --tokens 1024      # ranked skeleton: hubs first, fits budget
agres repo-map
agres graph --build          # files+symbols+tasks+decisions+git in one graph
agres graph --query "..."    # subgraph neighborhood (BFS)
agres graph --export json|dot
agres trace                  # whole-project trace (writes .agres/trace.md)
agres brain [--query "..."]  # one bounded pack carrying the whole project
agres context --query "..."
agres search --query "..."
agres decision --text "..." --accepted|--rejected
agres task --objective "..."
agres error --message "..."
agres pin --text "..."
agres checkpoint --reason "..."
agres resume
agres end
```

Fold/unfold (the context engine):

```bash
agres capture --role user --text "..."        # verbatim user turn
agres capture --role assistant --text "..."   # verbatim reply
agres budget                 # usage bar + confidence (auto-fold hints)
agres budget --json
agres window                 # priority breakdown manifest
agres window --json
agres fold --target 200000   # fold low-priority until <= 200k in window
agres fold --target 0        # fold everything foldable (keep only pinned)
agres unfold --query "..."   # restore exact matching text
agres unfold --fold-id fold_xxx
agres prune --keep-days 30 --dry-run
agres gc --dry-run
agres repair                 # ghost sessions, WAL, FTS, integrity
agres doctor                 # verify skill + marker wiring (skills + rules)
```

Low-context model budget:

```bash
export AGRES_WINDOW_BUDGET=262144   # 256k tokens (e.g. DeepSeek V4 Flash)
```

## Core principle (origami model)

Raw Context -> Verbatim turn (durable) -> Fold (out of window, exact text +
summary kept) -> Unfold (exact text restored) -> Active Window Manifest
(token-budgeted). Unfold only what is needed: exact passage, summary only,
or partial content. Never reload a whole conversation unless necessary.

## Brain — project memory as a graph

- `agres graph --build` materializes one property graph in SQLite:
  `file|symbol|task|decision|error|pin|checkpoint|session|commit` nodes;
  `contains|imports|uses|touches|mentions|owns|about|covers|child_of` edges.
  Code structure from the index (imports, tree-sitter refs, PageRank);
  progress links are deterministic text→file mentions (no embeddings).
- `agres trace` walks the whole project — file/language stats, PageRank
  hubs, tasks by status, decisions accepted/rejected, recent errors, pins,
  checkpoints, git log + churn, health (FTS, stale packs, graph freshness).
- `agres brain [--query]` fuses trace + graph neighborhood + pins /
  decisions / tasks / recent turns into one token-bounded injection pack
  (receipt + stale tracking like `context`). Read at session start and
  after resume; cheaper than re-exploring, never goes stale silently
  (reindex marks packs stale).

Retrieval addition: graph neighborhood (BFS around query seeds) ranks above
raw chunk search for architecture questions; file reads still win for
line-level depth.

## Fold types

14 logical types, one physical `folds` table (kind TEXT, indexed):

1. ConversationFold (kind=turn, priority 1)  2. DecisionFold (decision, pinned 3)
3. TaskFold (task)  4. FileFold (file, via files/file_chunks)
5. SymbolFold (symbol)  6. DiffFold (diff)  7. ErrorFold (error)
8. TestFold (test)  9. CommandFold (command)  10. SearchFold (search)
11. RepoMapFold (repo_map)  12. DependencyFold (dependency)
13. ArchitectureFold (architecture)  14. KnowledgeFold (knowledge)

## Token budget (example, 256k window)

system 10% | task objective 10% | pinned/decisions 10% | recent turns 35%
| unfolded 20% | repo/architecture 5% | reserved response 5% (8k min).

Over budget: fold oldest turns first (verbatim preserved); keep pins/
decisions unless unpinned; unfold only needed passages.

## Long-session preservation

Extract durable folds per message: DecisionFold, ConstraintFold, TaskFold,
PreferenceFold — plus always VerbatimTurn. Rolling summaries: turn, topic,
task, session, project. Pins persist until explicitly unpinned or session ends.

Checkpoint at: plan approval, edit approval/rejection, task completion,
test run, build run, major refactor step, pause, resume, pack creation,
after fold.

## Rejection handling

1. Store rejection as DecisionFold. 2. Mark affected task invalid.
2. Mark downstream work stale. 4. Keep unrelated context. 5. Preserve the
   rejected attempt. 6. Regenerate only the affected part. Never repeat a
   rejected approach unless the user reopens it.
