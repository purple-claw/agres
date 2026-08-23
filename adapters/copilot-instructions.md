# Agres Copilot Instructions — Fold/Unfold Context Engine

For long sessions and large repositories, use Agres memory discipline by default.

- Do not assume prior context is remembered.
- Capture every conversation turn verbatim.
- Preserve decisions, constraints, and rejected approaches.
- Prefer local project memory under .agres.
- Restore active task before acting.
- Capture meaningful events and checkpoints.
- Use SQLite as the primary structured memory store.
- Use SQLite FTS5 keyword search when available.
- Do not require embeddings or external vector services.
- Fold large files/logs/diffs into summaries instead of dumping them.
- Unfold exact original wording when earlier context is needed — never paraphrase.
- Build bounded context packs.
