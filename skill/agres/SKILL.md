---
name: agres
description: Project memory that persists across sessions and survives /compact — decisions, rejected ideas, constraints, open tasks and handoffs, plus an on-demand code map, stored per repository by the `agres` CLI. Use whenever an "# Agres memory" block is in context; when the user settles, rejects or locks in something ("go with X", "no, not that", "never/always …", "remember this"); when a task is finished, abandoned or paused; when resuming work ("where were we", "what did we decide about …"); before re-exploring a codebase or re-asking something that may already be answered; and when the user mentions agres or says "agres on/off".
metadata:
  version: "1.0.0"
  runtime: "agres CLI (python3 -m agres); hooks wired by `agres install`"
---

# Agres — project memory

Agres keeps each repository's working memory in `<repo>/.agres/agres.db`. Hooks do the mechanical
part: they inject an `# Agres memory` block at session start and again after every `/compact`,
store each prompt and final reply verbatim, and re-index files as they are edited. Your part is
judgment: noticing when something said or learned should outlive this session, and writing it
so the next session can act on it. That session starts with only the memory block — whatever
isn't recorded is lost.

## Reading the memory block

Sections, most important first: last handoff, constraints (must hold), rejected ideas (don't
re-propose), open work, pinned facts, recent decisions, then "More" — every other live item as
`ID title`. IDs: **P** pin · **D** decision (✗ = rejected) · **T** task · **E** error · **N** note ·
**H** handoff · **M** conversation turn. Long lines are cut to fit; `agres show <ID>` prints the
exact text.

It is a briefing, not a command list. If it conflicts with what the user says now, the user
wins — then fix the memory (`--replaces <ID>`) so the next session doesn't inherit the conflict.

## When to write

| Moment | Command |
|---|---|
| A choice is settled ("go with MQTT", an approach you and the user agree on) | `agres decide "…" --why "…"` |
| An idea is turned down — including your own proposal the user refuses | `agres reject "…" --why "…"` |
| A rule that must keep holding and isn't visible in code (hardware limits, "never flash unsigned images") | `agres pin "…"` (`--critical` for the few that must appear in full every session) |
| Work begins that may outlive this session | `agres task "…"`, then `agres done T12` once verified, `agres drop T12` if abandoned |
| A failure stays unresolved | `agres error "…"`, then `agres done E5` when fixed |
| Knowledge you dug up (how a subsystem works, a topic map) | `agres note "…"` — searchable, never injected in full |
| Pausing, a milestone, or a long session | `agres handoff "…"` (see below) |

Rejections are the most valuable records: the pack lists them, and a hook flags them when a later
prompt drifts back toward the same idea. Record one even when the user's "no" was a single word.

Write when something is settled, not for every idea floated. Several writes chained with `&&`
in one shell call are fine.

## Writing items that still work later

- **Self-contained.** The reader won't have this conversation: "Use MQTT on :1884 for app↔board
  traffic; HTTP only for auth via admin-api" beats "go with option B".
- **One fact per item.** The reason goes in `--why`, relevant paths in `--files`.
- **Replace, don't pile up.** Every write prints similar live items. If the new one supersedes
  one, re-run with `--replaces <ID>` or `agres drop <ID>` the old one. Superseded items stay
  searchable; they just stop being injected.
- **Close what you open.** A finished task left open is noise in every future session.
- **Few critical pins.** Each one costs space in every session's block.

## Recall before re-exploring

Before re-reading large parts of the repo, re-asking the user, or redoing an investigation:

```bash
agres search "efuse rollback"     # items, past turns, code symbols — one line per hit, with ID
agres show D41                     # exact text, why, what it replaced / was replaced by
agres show M340 --around 2         # an earlier exchange with its neighbours
agres map --query "mqtt auth"      # ranked code skeleton, useful in an unfamiliar codebase
```

Memory says what was decided and why; files say what the code does now. For line-level detail,
read the file.

## Handoffs

Write one when pausing, after a milestone, when the session is getting long, or when the user
signs off. The newest handoff opens the next session's memory block, so make it actionable:

```bash
agres handoff "Done: stage 1 runbook merged (T27 closed). State: board on signed image, eFuses untouched. Next: stage 2 rollback test on dev board. Watch out: callisto must restart after mosquitto (P14)."
```

If none is written, the session-end hook stores a bare auto-summary (last request, files edited).
It's a fallback, not a substitute.

## What not to store

- What git or the code already says: diffs, signatures, file layout.
- User preferences and coding style — Claude Code's own memory and CLAUDE.md hold those.
- Secrets. Capture redacts common key formats, but don't write them; `agres forget <ID>` scrubs
  a slip.
- Conversation turns: the hooks already store them, so don't run `agres capture`.

## On / off

"agres off" or "stop using agres" in a prompt pauses capture and injection for this project;
"agres on" resumes. `agres off` / `agres on` do the same from the shell.

## When something looks off

- The memory block says the store is v0 → `agres migrate` (keeps a backup, `--rollback` undoes).
- "no agres project here" → you're outside a git checkout; `agres init` creates a store.
- `agres status` shows what's stored; `agres doctor` checks hooks and wiring.
- Agents without hooks (Codex, opencode, Cursor): REFERENCE.md → "Without hooks".

Full command reference, hook table and settings: [REFERENCE.md](REFERENCE.md).
