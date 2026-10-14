# Agres 1.0 — implementation plan

Status: **DRAFT, awaiting approval** · Spec: `agres-v1-spec.md` · ADR: `../adr/0001-agres-v1-architecture.md`

## Ground rules

- Work in a worktree: `git -C ~/Dev/agres worktree add ~/Dev/agres-v1 -b feat/agres-v1`.
  Your uncommitted edits in `~/Dev/agres` (SKILL.md, bin/skill-install.js, REFERENCE.md)
  stay untouched there.
- TDD per phase: failing test → confirm red for the right reason → minimal green →
  refactor. Tests are stdlib `unittest` (pytest isn't installed; stays dependency-free).
- Phase gate command (scoped): `python3 -m unittest discover -s tests -p 'test_<area>*.py' -v`.
  Regression gate before every commit: `python3 -m compileall -q agres && python3 -m unittest discover -s tests -v`.
- One concern per commit, conventional commits. Nothing touches `~/.claude`, `~/.agres`,
  `~/.agent-skills` or any real project store until **P6/P8**, and those start with a
  dry-run you confirm.

## Phases

| Phase | Builds | Owns (files) | Depends | Gate |
|---|---|---|---|---|
| **P0** setup | worktree; copy the 1.0 package from `~/.agent-skills/agres/agres/` into `agres/code/` (it is not in git anywhere yet); test skeleton; docs | `agres/__init__.py`, `agres/code/**` (verbatim import), `tests/__init__.py`, `docs/**` | — | `python3 -m compileall -q agres` |
| **P1** core store | root resolution, schema v1, items/sessions/turns API, title derivation, similar-item lookup, session-for-CLI resolution, redaction | `agres/paths.py`, `agres/db.py`, `agres/memory.py`, `agres/util.py`, `tests/test_paths.py`, `tests/test_db.py`, `tests/test_memory.py` | P0 | `-p 'test_[pdm]*.py'` |
| **P2** pack + search | brain renderer (R3 order, budgets, boundary truncation, footer), unified search (items, turns; code hits via a P4 hook-in point) | `agres/pack.py`, `agres/search.py`, `tests/test_pack.py`, `tests/test_search.py` | P1 | `-p 'test_[ps][ae]*.py'` |
| **P3** hooks | `agres hook <event>` for the 6 events; capture, dedupe, `<private>`, harness-prompt filter, on/off, rejection guard, auto-handoff, fail-open + hook.log | `agres/hooks.py`, `tests/test_hooks.py`, `tests/fixtures/hook-payloads/*.json` | P1, P2 | `-p 'test_hooks.py'` + latency test |
| **P4** code engine | put the 1.0 engine on the shared DB; fixes: same-stem stdlib resolution, secret denylist, Yocto/DT extensions, per-file touch + deferred resolve; `map` and code hits for search | `agres/code/**`, `tests/test_index.py` | P1 (db API only) — **parallel worktree** | `-p 'test_index.py'` + perf smoke on an Okas clone in /tmp |
| **P5** CLI | argparse surface (R9), compat aliases, deprecation notices, `bin/agres` shim, npm `bin/agres.js` → `python3 -m agres` | `agres/cli.py`, `agres/__main__.py`, `bin/agres`, `bin/agres.js`, `tests/test_cli.py`, `tests/test_compat_zeus.py` | P1–P4 | `-p 'test_c*.py'` (zeus flow runs `zeus_mem.py` against the new CLI via PATH) |
| **P6** migration | v0→v1 in place, nested-store merge, global routing report, `--rollback` | `agres/migrate.py`, `tests/test_migrate.py`, `tests/fixtures/v0_builder.py` (drives the **old** runtime from `git show HEAD:runtime/agres_runtime.py`) | P1 | `-p 'test_migrate.py'`, then **dry-run on all 13 real stores → report → you confirm → apply** |
| **P7** skill + evals | SKILL.md (≤120 lines), REFERENCE.md, one adapter snippet; skill-creator evals (3 scenarios, new vs old-skill snapshot), viewer, iterate; optional description optimisation | `skill/agres/**`, eval workspace `~/Dev/agres-workspace/` (outside the repo) | P5 (CLI frozen) | eval benchmark: new ≥ old, zero model `capture` calls |
| **P8** install + cutover | `agres install/uninstall/doctor`; settings.json hook swap (backup first); symlinks for `~/.agent-skills/agres` and `~/.codex/skills/agres`; archive old copies to `~/.agres-archive/`; `/agres` command → skill pointer; proposed `~/.claude/CLAUDE.md` Agres block (diff shown, you approve) | `agres/install.py`, `tests/test_install.py` (temp HOME) | P5 | `--dry-run` diff → you confirm → apply → `agres doctor` → live session check in Okas |
| **P9** cleanup + ship | delete `runtime/agres_runtime.py`, `skills/agres/` copy, old `snippets/`, `adapters/`, `bin/eval.py` (stay in git history); README rewrite; package.json 1.0.0 + new description | repo root | all | full suite, `/verify-review` (fresh-context reviewer), then `/ship` (PR; push only after you say so) |

Critical path: P0 → P1 → P2 → P3 → P5 → P7/P8. P4 and P6 run beside P2/P3.

## Eval scenarios for P7 (seeded fixture repo with migrated-style memory)

1. **resume** — "pick up where we left off on the secure boot runbook" → uses the pack or
   `agres search`, honours the critical pin, doesn't re-explore the whole repo.
2. **reject** — "no, never use a sacrificial board for the eFuse tests, find another way" →
   records `agres reject` (or `pin --critical`), proposes an alternative, no `capture`.
3. **wrap-up** — "KNX outage fix is verified on the board, wrap up" → `agres done T…`,
   `agres handoff "…"`, no duplicate decision (uses `--replaces` when a similar one exists).

Baseline = snapshot of the current skill (`old_skill`). Assertions are drafted while runs
execute; results go to the skill-creator viewer for your review before any rewrite.

## Rollback

- Code: branch-only until `/ship`; the old runtime stays in git history and at
  `~/.agres/runtime/agres_runtime.py`.
- Wiring: `agres uninstall` restores the settings.json backup and the old shim.
- Data: every migrated store keeps `.agres/v0/agres.db`; `agres migrate --rollback PATH`
  puts it back. The global store is archived, never deleted.

## Risks

| Risk | Mitigation |
|---|---|
| `async: true` hooks unsupported on the local CLI (v2.1.287) | `doctor` checks; fall back to sync hooks with 5 s timeouts (all still fail-open) |
| `last_assistant_message` misses text between tool calls | accepted: the final reply is what matters; transcript is still on disk |
| Harness-generated prompts logged as human turns | prefix filter list (configurable) + fixture tests |
| 1.0 engine is untested | P4 starts with failing tests for the audit's known bugs; never run full index inside sync hooks |
| Migration misclassifies tasks/pins | dry-run report first; `--rollback`; migrated tasks flagged `origin=v0` |
| Global CLAUDE.md block still says "capture turns verbatim" | P8 proposes the replacement; you approve the diff |

## Agres watchdog note

The global CLAUDE.md asks for `agres decision`/`checkpoint` during work. Running the
**v0** CLI now would rewrite the global active-session pointer and hide Okas's memory
(finding F2), so the decisions from this session are recorded in the ADR instead and
get imported into the new store after P6.
