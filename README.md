# agres

**Project memory for coding agents.** Decisions, rejected ideas, constraints, open work, handoffs. Survives new sessions and `/compact`. SQLite, local, stdlib Python.

## Problem

New session, agent forgets everything. Re-explores the repo, re-asks, re-proposes the idea you killed yesterday.

0.x tried to fix that by "folding" the context window. Doesn't work — the harness owns the window, not a CLI. And it asked the model to run `agres capture` every turn. Model didn't. 20 turns captured across 4 real sessions.

## 1.0: hooks do mechanics, model does judgment

- **Hooks** (zero model tokens, always exit 0):
  - session start + after `/compact` → inject `# Agres memory` block (~1.5k tokens)
  - every prompt + final reply → stored verbatim (deduped, `<private>` stripped, secrets redacted)
  - every edit → file re-indexed
  - prompt drifts back to a rejected idea → one line reminder
  - session end → auto-handoff if the agent wrote none
- **Model** only records what needs judgment: `decide`, `reject`, `pin`, `task` / `done`, `handoff`.
- **One store per repo**: `<git root>/.agres/agres.db`. Never `/`, never `$HOME`. No global store, no global session pointer.

Memory block looks like:

```text
# Agres memory — okas (homekit-v1.2_stable@06c0bf7)

## Last handoff (H217, 4h ago)
Stage 1 runbook merged. KNX keepalive deployed, waiting for soak. Next: stage 2 rollback test.

## Constraints (must hold)
- P199 Secure boot dev: NO sacrificial board. Zero brick tolerance…

## Rejected — don't re-propose
- D214 Blue/teal centre dot in the app icon: user wants the ring only.

## Open work
- T200 Secure boot runbook stage 2: rollback test on the dev board · 1d
…
## More (`agres show <ID>`)
P185 CHECKPOINT 2026-10-13… · P174 Wireless adb… · D90 Suffix-always hostname… · … +104 more
```

Full text for what must hold, one line per everything else, `agres show <ID>` for exact text.

## Install

```bash
git clone https://github.com/purple-claw/agres ~/Dev/agres
~/Dev/agres/bin/agres install --dry-run   # shows every change first
~/Dev/agres/bin/agres install --yes       # shim, skill links, hooks; backups in ~/.agres-archive/
agres doctor
```

or `npx @ithica/agres install --dry-run`. Undo: `agres uninstall`.

Install touches: `~/.local/bin/agres`, skill links (`~/.claude/skills`, `~/.codex/skills`, `~/.agent-skills`), six `agres hook …` entries in `~/.claude/settings.json` (other hooks untouched), the Agres section of `~/.claude/CLAUDE.md`. Nothing deleted — replaced files move to the archive.

Needs Python ≥ 3.9. FTS5 if your sqlite has it, LIKE fallback if not. tree-sitter optional.

## Use

Agent side (the skill teaches this):

```bash
agres decide "MQTT on :1884 for app<->board; HTTP only for auth" --why "LAN auth"
agres reject "burn eFuses on a spare board" --why "zero brick tolerance"
agres pin --critical "no sacrificial board"
agres task "stage 2 rollback test"      # -> T12
agres done T12 --note "slot A fallback verified"
agres handoff "Done: … State: … Next: … Watch out: …"
agres search "efuse rollback"           # items, past turns, code
agres show D41                          # exact text + what it replaced
agres map --query "mqtt auth"           # PageRank code skeleton
```

Every write prints similar live items. Duplicate? `--replaces <ID>`. Nothing hard-deleted — old item goes `superseded`, still searchable.

You side:

- "agres off" / "agres on" in a prompt → pause/resume for this repo.
- `agres status`, `agres config`, `agres gc`.

## Upgrading from 0.x

0.x stores are left alone until you migrate. Session start says so in one line.

```bash
agres migrate --dry-run     # from anywhere inside the repo
agres migrate               # old files -> .agres/v0/, nested stray stores merged + archived
agres migrate --rollback    # changed your mind
agres migrate --all --dry-run
agres migrate --global      # old ~/.agres junk drawer -> proposal file, you review, then --routes
```

Pinned "CHECKPOINT …" / "Phase 5 COMPLETE …" lines become notes. They were history, not rules.

## Other agents

Codex, opencode, Cursor: no hooks there. Run `agres brain` at task start, same write commands. Snippet for `AGENTS.md` / cursor rules: `skill/agres/adapters/AGENTS-snippet.md`.

## Not doing

- embeddings / vector DB
- LLM calls inside hooks
- touching the context window (harness owns it)
- user preferences — Claude Code's own memory + CLAUDE.md do that

## Dev

`python3 -m unittest discover -s tests`. Spec, plan, ADR in `docs/`. See `CLAUDE.md`.

MIT.
