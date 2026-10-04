# Agres Origami Memory — Fold/Unfold Context Engine

Agres makes a limited context window (e.g. 256k tokens) behave like a much larger one. Every conversation turn is captured verbatim; old context is folded out of the active window (exact text preserved); exact wording is unfolded back in when needed.

Agres is the default memory operating mode for coding sessions unless explicitly disabled.

Before acting:

- Inject project memory first: agres brain [--query "<task>"] (whole project in one bounded pack).
- Restore active task.
- Restore pinned decisions and constraints.
- Restore rejected approaches.
- Restore relevant files/symbols (agres graph --query "<what you need>" for structure, then read files for depth).
- Restore recent errors/tests.
- Check the window budget: agres budget
- Unfold earlier context verbatim when needed: agres unfold --query "<what you need>"
- Build a bounded context pack.

During work:

- Capture every turn verbatim: agres capture --role user|assistant --text "<exact words>"
- When the window fills, fold older context: agres fold --target <tokens>
- Never paraphrase when unfolding — always restore exact original text.
- After structural changes, rebuild the brain: agres index/sync, then agres graph --build.

Memory backend:

- Use SQLite as the primary structured memory store.
- Use JSONL event logs and content-addressed storage for durable state.
- Use SQLite FTS5 keyword search when available.
- Do not require embeddings or external vector services.

If the local `agres` CLI exists, use it for memory operations.
