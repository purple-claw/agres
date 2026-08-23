# Agres Agent Memory Contract — Fold/Unfold Context Engine

Agents working in this repository should use Agres Origami Memory by default.

Rules:

- Do not assume the model remembers.
- Capture every conversation turn verbatim (agres capture --role user|assistant --text "<exact words>").
- Restore active task and pinned context before acting.
- Preserve decisions, constraints, and rejected approaches.
- Capture meaningful events: edits, commands, tests, errors, decisions, checkpoints.
- Fold large files, logs, diffs, and conversations into compact local memory.
- When the active window approaches its budget, fold older context out (agres fold --target <tokens>); exact text is preserved.
- Unfold exact original wording when earlier context is needed (agres unfold --query "<what you need>").
- Never paraphrase when unfolding.
- Use SQLite as the primary structured memory store.
- Use JSONL event logs and content-addressed storage for durable state.
- Use SQLite FTS5 keyword search when available.
- Do not require embeddings or external vector services.
- Keep memory local under .agres unless the user explicitly asks otherwise.

If the local `agres` CLI exists, use it for memory operations.
