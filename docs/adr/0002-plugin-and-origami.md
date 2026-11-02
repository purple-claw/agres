# ADR 0002 — Plugin distribution and Origami fold/unfold

Date: 2026-11-02 · Status: accepted (user approved the plan and asked for Origami as the backbone)
Supersedes in part: ADR 0001 decision 1 ("drop the fold engine"); agres memory D2.

## Context

ADR 0001 dropped v0.5's fold engine because it flipped rows in its own tables and never touched what
the model saw. Two things changed:

1. Claude Code plugins bundle hooks, a skill and a `bin/` on the Bash PATH in one installable unit,
   replacing hand-edited `~/.claude/settings.json` hooks and skill symlinks (audit finding: stale
   v0.5 skill copy left behind by the old install path).
2. A `PostToolUse` hook can replace a built-in tool's result before the model reads it
   (`hookSpecificOutput.updatedToolOutput`, value shaped like `tool_response`). Probed on Claude Code
   2.1.316: Bash `seq 1 3` → the model reported the replacement stub. A plain-string value is ignored.

Research (Context-Folding arXiv 2510.11967, AgentFold 2510.24699, ACON 2510.00615, Memex 2603.04257,
LCM 2605.04050, Anthropic context-engineering / context-editing docs, Factory.ai compression probes)
agrees on: fold at work boundaries, keep raw history complete and fold only the view, anchored
summaries, verbatim artifacts, exact unfold by pointer, evaluate with probes not compression ratio.

## Decision

- Ship agres as a Claude Code plugin; the repo is also its own directory marketplace.
- Origami returns with four fold kinds over one `folds` table (schema 101):
  **tool** (live window: big Bash output → head + salient lines + tail + `unfold` stub),
  **agent** (subagent result = branch/return), **span** (compaction summary + exact turn range),
  **manual** (agent-written anchored summary). `agres unfold F<id>` returns exact, bounded text.
- Fold summaries are deterministic extracts or come from the agent / Claude Code's own compaction; no
  LLM call inside hooks.

## Consequences

- Real context savings on long command output; the model can always get the exact text back.
- Depends on an output-replacement field whose non-MCP behaviour was verified by probe, not docs.
  If it stops working, output passes through unfolded (safe failure); an eval guards the shape.
- Folding hides lines; salient-line extraction and `--grep` unfold reduce, not remove, that risk.
- Turns and tool/agent folds are raw capture and may be hard-deleted (forget, gc, retention).

## Alternatives

| Option | Why not |
|---|---|
| Keep 1.0 (no folding) | user wants Origami; the window lever now exists |
| v0.5 fold engine back as-is | still doesn't touch the window |
| LLM summarizer in the hook | latency, cost, model-dependent drift; Factory data shows regenerated summaries drift |
| Fold `Read` output | Edit requires the real Read result |
