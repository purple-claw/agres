# Agres 1.1 — Claude Code plugin + audit fixes (spec)

Status: approved 2026-11-02 (user: "proceed with implementation"), Origami section added the same day
at the user's request. Source: the Agres audit report; scope: plugin conversion + audit items 1–5 +
Origami fold/unfold. Reopens rejected D2 — see ADR 0002.

## Goals

Ship Agres as a Claude Code **plugin** (hooks, skill and CLI in one installable unit) instead of
hand-wired `~/.claude/settings.json` hooks + skill symlinks, and fix the audit's high/medium items.

## Requirements (frozen)

### Plugin

- **R1 Plugin layout.** The agres repo is the plugin and its own directory marketplace:
  `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `hooks/hooks.json`, the skill,
  and `bin/agres` (plugin `bin/` is put on the Bash tool's PATH while the plugin is on).
  `claude plugin validate .` passes.
- **R2 Hooks from the plugin.** `hooks/hooks.json` wires the same six events as `install.py`
  (`SessionStart`, `UserPromptSubmit`, `Stop` async, `PostToolUse` async, `PostCompact` async,
  `SessionEnd`) through `"${CLAUDE_PLUGIN_ROOT}/bin/agres" hook <event>`. Same timeouts, same
  invariants (always exit 0, only SessionStart/UserPromptSubmit print).
- **R3 Plugin options.** `userConfig` exposes `capture` (all | prompts | none),
  `turn_retention_days` (number, 0 = keep forever) and `budget_chars`. Hooks read them from
  `CLAUDE_PLUGIN_OPTION_<KEY>` env. Precedence: defaults ← `~/.config/agres/config.json` ←
  plugin option ← `<root>/.agres/config.json`.
- **R4 Wiring migration.** `agres install --plugin [--dry-run|--yes]` moves an existing
  settings-hook install to the plugin: strips agres hooks from `~/.claude/settings.json`, archives
  `~/.claude/skills/agres` (the plugin provides it), relinks the other skill roots
  (`~/.agents/skills`, `~/.agent-skills`, `~/.codex/skills`) to this checkout, keeps the shim for
  terminal use, updates the CLAUDE.md block. Reversible through the existing manifest/`uninstall`.

### Audit fixes

- **R5 Stale-skill detection (audit #1).** Installer and `doctor` cover every skill root,
  including `~/.agents/skills`. `doctor` prints, per root, what is there (link to us / other copy +
  detected version, "v0 Origami" marker). A non-link stale copy is archived by install, not deleted.
  `doctor` flags double wiring: plugin enabled **and** agres hooks still in `settings.json`, or
  plugin enabled **and** `~/.claude/skills/agres` present.
- **R6 `gc --dry-run` (audit #2).** Prints cutoff + count of turns that would go; deletes nothing,
  no VACUUM. Flag becomes visible in `--help`.
- **R7 Exact-turn deletion (audit #3).** `agres forget M<id> [M<id> …]` hard-deletes those turns
  and their FTS rows. `agres forget --session <sid>` deletes every turn of a session.
  `--dry-run` previews. Item forget (`P/D/T/E/N/H`) keeps its tombstone behaviour.
  Turns are raw capture, so hard delete is the point; items still never hard-delete.
  **Override of the repo invariant "nothing is hard-deleted"**: CLAUDE.md gets the exception
  "except captured turns, on explicit `forget`/`gc`/retention".
- **R8 Retention (audit #4).** Setting `turn_retention_days` (default 0 = off, so nothing changes
  silently). When > 0, the `session-end` hook prunes older turns at most once a day (meta key
  `last_retention`). Durable items are never touched by retention.
- **R9 Capture policy visible (audit #5, #6).** `agres status` shows capture mode, retention and
  "redaction is best effort". `install` output states the capture policy and how to change it.
  New setting `redact_patterns` (list of extra regexes, applied after built-ins; bad regex is
  skipped and logged, never crashes a hook).
- **R10 Provenance + trust framing (audit #7).** Provenance comes from the code path that wrote the
  row (`items.source`: `agent`, `auto`, `compact`, `checkpoint`, `migrate`, …), never from text.
  The pack opens with one line saying the block is recorded notes — evidence, not instructions;
  the user's current request wins. Items not written by the agent CLI are tagged in the pack and in
  `show`/`search` (`[auto]`, `[compact]`, `[imported]`). No CLI flag can set `source`.
- **R11 Index freshness (audit #4 medium).** The async `PostToolUse` edit hook, on a `Bash` call,
  reconciles even without `bashEditDiff`: when the code index exists, git-dirty + untracked files
  whose mtime is newer than the last reconcile are re-touched (bounded: ≤ 200 files, skipped if no
  index or no git). `status` shows last full sync and last reconcile time. The prompt/stop path
  still never imports `agres.code` (repo invariant kept).
- **R12 Eval set (audit #10).** `evals/memory/cases.json` + a fixture builder: recall@k for known
  items, superseded items excluded by default, rejected ideas surfaced by the guard, abstention
  (unrelated query → no item hits), pack size ≤ budget, SessionStart latency. Run by
  `python3 -m agres.evalrun` (prints metrics) and asserted by `tests/test_evals.py`.

### Origami — fold / unfold (the user reopened D2)

Why it can work now: v0.5 folded rows in its own tables, which never touched the model's window.
A plugin has three real levers (verified 2026-11-02 against Claude Code 2.1.316):

1. **PostToolUse `updatedToolOutput`** replaces a built-in tool's result *before the model sees it*.
   Probe: a Bash `seq 1 3` result replaced by a stub; the model reported the stub. A plain string is
   ignored — the value must be an object shaped like `tool_response` (`{stdout, stderr, …}`).
2. **SessionStart `compact`** re-injects after compaction (already used by the pack).
3. **Subagents** are branch/return folds (Context-Folding, arXiv 2510.11967): the parent keeps only the
   returned result; PostToolUse on `Agent` sees it.

Design principles taken from the research (AgentFold 2510.24699, Context-Folding, ACON 2510.00615,
Memex 2603.04257, LCM 2605.04050, Anthropic context-engineering + context-editing docs, Factory.ai
compression probes): fold at work boundaries; keep the raw store complete and fold only the view;
anchored fixed-schema summaries; artifacts (paths, errors, SHAs) verbatim; unfold exact text by ID with
a bounded excerpt; small re-injected index; measure probe accuracy and re-reads, not compression ratio.

- **R14 Fold store.** New table `folds` (+ `fts_folds`), id prefix **F**: `kind` (`tool` | `span` |
  `agent` | `manual`), `title`, `summary`, `body` (exact text, redacted), `meta` JSON (tool, command,
  turn range, line/char counts), `session_id`, `source`, `created_at`. Schema 100 → 101, upgraded in
  place on open (additive tables only); 100 stores keep working.
- **R15 Tool fold (granular, live window).** Sync `PostToolUse` hook on `Bash` (tools list
  configurable): when stdout or stderr exceeds `fold_chars` (default 12 000), store the full text as a
  fold and return `updatedToolOutput` with: first `fold_head` lines (40), salient lines (error / fail /
  exception / traceback / warning / `FAILED` / `panic`, ≤ 30, verbatim), last `fold_tail` lines (20),
  and a stub `[agres fold F12: 4 210 lines, 312 KB folded · unfold: agres unfold F12 --lines 300-420 |
  --grep PATTERN]`. Never folds output of an `agres` command (no unfold→refold loop), images, or
  interrupted calls. Deterministic, no LLM, < 100 ms for 1 MB. `fold off` keeps outputs untouched.
- **R16 Branch fold.** Async `PostToolUse` on `Agent`: store the subagent's prompt + returned result as
  an `agent` fold (output is not replaced — it is already condensed). Survives compaction via the pack.
- **R17 Span fold (deep).** On `PostCompact`, the compacted conversation span (turns since the previous
  span fold) becomes a `span` fold: summary = `compact_summary`, body = pointer to turns `M<a>–M<b>`;
  unfold prints those exact turns. Manual: `agres fold "<anchored summary>" --turns M40-M88` (or `--stdin`
  for any text) — the agent's own deep consolidation.
- **R18 Unfold.** `agres unfold F12 [--lines A-B] [--grep PAT] [--context N] [--all]`: exact text,
  bounded to 200 lines / 16 000 chars unless `--all`, with a "showing x–y of N" footer. `show F12` is
  an alias. `forget F12` hard-deletes a tool/agent fold (raw capture, like turns); span folds drop only
  the summary.
- **R19 Pack + search.** The pack lists this project's recent folds (`## Folded — unfold by ID`, one line
  each, newest first, span/agent folds before tool folds, ≤ 8). `search` covers folds (kind `fold`).
  Retention (`turn_retention_days`) also prunes tool folds.
- **R20 Origami evals.** Unfold is byte-exact; salient lines survive a fold; folded output size ≤ budget;
  no refold of `agres` output; fold hook latency on a 1 MB output.

### Hercules

- **R13** The Hercules plugin drops its copy of `skills/agres` and declares a dependency on the
  agres plugin (`dependencies` in its `plugin.json`), so only one agres skill is listed.

## Non-goals (v1.2 spec, later)

Temporal validity fields, reviewable consolidation, MCP adapter, team export/import, semantic
search, cloud anything. LLM-written summaries inside hooks (folds use deterministic extracts; the
anchored summaries come from the agent or from Claude Code's compaction). Folding `Read` output
(Edit needs the real Read result).

## Interfaces

```text
agres install --plugin [--dry-run] [--yes]
agres doctor                       # + skill-root scan, double-wiring check, plugin state
agres gc [--turns-older-than N] [--dry-run]
agres forget <ID>... [--session SID] [--dry-run]     # M ids hard-delete, item ids tombstone
agres config turn_retention_days 30 | redact_patterns 'ACME-[0-9]{6}' | fold off | fold_chars 20000
agres fold "<summary>" --turns M40-M88 | --stdin [--title T]
agres unfold F12 [--lines A-B] [--grep PAT] [--context N] [--all]
python3 -m agres.evalrun [--json]
```

## Failure modes

| Case | Behaviour |
|---|---|
| Plugin on + old settings hooks still there | every event fires twice; doctor ✗ with fix (`agres install --plugin`) |
| `CLAUDE_PLUGIN_OPTION_*` names differ from guess | unknown env ignored; file config still works; doctor prints effective config |
| Bad `redact_patterns` regex | skipped, written to hook.log |
| Retention prune hits a locked DB | busy timeout; skipped this time, retried next session end |
| Reconcile with no git | no-op |
| Fold hook fails / times out | original output passes through untouched (hook exits 0, no JSON) |
| `updatedToolOutput` shape changes in a future Claude Code | stub ignored → model sees full output; eval + doctor probe catch it |
| Folded output hid the one line the model needed | stub tells it how to `--grep`/`--lines`; salient lines kept verbatim |

## Open risk (stated, not solved)

Provenance labels can't stop a poisoned record that came in through the agent CLI itself (the
agent can be talked into writing it). 2026 sources (OWASP ASI06, FARMA write-up) say labels help
but are not enough alone; this spec does framing + labels only.
