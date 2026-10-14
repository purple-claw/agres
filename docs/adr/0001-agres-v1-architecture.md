# ADR 0001 — Agres 1.0 architecture: hooks do mechanics, model does judgment

Date: 2026-10-14 · Status: proposed (accepted on plan approval)

## Context

v0.5 tried to make the agent behave like it had a bigger context window ("origami"
fold/unfold) and relied on the model to run memory commands every turn. The harness,
not Agres, owns the context window; the model did not reliably run the commands; and
store/session scoping leaked memory across projects and hid it across sessions.
Evidence and numbers: `docs/plans/agres-v1-spec.md` §1.

## Decision

1. **Drop the fold/budget/window engine and reposition** Agres as project memory
   (decisions, rejections, constraints, open work, handoffs + on-demand code index).
2. **Hooks own every mechanical step** — inject on SessionStart (incl. after compaction),
   capture prompts and final replies, re-index on edit, store compaction summaries,
   close sessions. The skill only teaches judgment calls.
3. **One store per project at the repo root**, no global store or session pointer;
   sessions come from the harness `session_id` and are provenance only.
4. **Typed items with lifecycles and supersession, never deletion** (Graphiti-style);
   writes surface similar items so the model replaces instead of duplicating (mem0-style,
   no LLM in the loop).
5. **Index-first injection pack** (claude-mem style): full text for the few items that
   must hold, one-line IDs for the rest, `agres show ID` for exact text.
6. **Code engine = the unreleased Agres 1.0 package**, not the v0.5 indexer; on demand,
   not injected by default.
7. **Single repo + `agres install`** rather than a Claude Code plugin; the skill dir is a
   symlink into the repo for Claude and Codex.
8. **Migrate existing stores in place** with v0 backups and a rollback path.
9. **Capture prompts + final replies** via hooks, with `<private>` stripping and
   secret redaction.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep fold engine as an optional module | cannot affect the harness context; ~1.2k LOC of upkeep; misleading positioning |
| Claude Code plugin packaging | one-step install, but other agents still need a second path; user rule: keep plugins minimal; double-firing risk with settings hooks |
| Fresh stores, import on demand | loses 44 decisions / 80 pins from the default view until someone remembers to import |
| Model-driven capture (status quo) | measured not to happen; doubles output tokens |
| Embeddings / vector search | non-goal: local-only, no model downloads; FTS5 + IDs proved sufficient in claude-mem |
| Inject the PageRank repo map every session | generated overviews didn't help localisation and cost ~20% more (arXiv 2602.11988); grep/agentic search is the harness default |
| Fix v0.5 in place | scoping, session model and pack math are structural; the 1.0 engine already solves root/discovery/imports |

## Consequences

- Zero model tokens spent on capture; memory survives `/compact` because SessionStart
  re-injects it.
- Zeus keeps working through compat aliases; its "global session pointer" workaround
  becomes unnecessary.
- npm tagline and README change (no "256k → 1M" claim).
- Agents without hooks (Codex, opencode, Cursor) get a documented manual fallback
  (`agres brain` at start, explicit writes) but no automatic capture.
