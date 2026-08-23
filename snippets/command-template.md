Use the Agres Origami Memory fold/unfold protocol for the following request.

Agres makes a limited context window (e.g. 256k) behave like a much larger one by capturing every turn verbatim, folding old context out of the active window, and unfolding exact original text when needed.

Before acting:

- If .agres exists, read .agres/session.md, .agres/decisions.md, .agres/tasks.md, .agres/pinned.md, .agres/errors.md, and .agres/context-pack.md.
- If the local agres CLI exists, use it for memory operations.
- Check the window budget: agres budget
- Identify active task, constraints, pinned context, relevant files/symbols, recent edits, errors, and failing tests.
- If earlier conversation context is needed, unfold it with its exact wording: agres unfold --query "<what you need>"
- Use SQLite lookup and keyword search before broad reasoning.
- Build the smallest useful context pack.

During work:

- Capture every conversation turn verbatim: agres capture --role user --text "<exact words>" and agres capture --role assistant --text "<exact reply>".
- Store decisions, tasks, errors, pins, and checkpoints.
- When the window approaches its budget, fold older context: agres fold --target <tokens>
- Never paraphrase when unfolding; always restore exact original text.

After acting:

- Capture the final turn verbatim.
- Update decisions, tasks, errors, and session summary.
- Store rejected approaches explicitly.
- Create a checkpoint if the state changed materially.

Request:

$ARGUMENTS
