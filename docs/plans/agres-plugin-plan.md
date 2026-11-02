# Agres 1.1 plugin — plan

Spec: `docs/plans/agres-plugin-spec.md`. Branch: `feat/plugin-1.1` (one worktree; phases touch
shared files `cli.py`/`config.py`, so no parallel worktrees). TDD per phase: failing test → green.
Scoped test per phase, full suite + `compileall` before every commit. Conventional commits.

| # | Phase | Files | Test cmd | Commit |
|---|---|---|---|---|
| 1 | `gc --dry-run` (R6) | `agres/cli.py`, `tests/test_cli.py` | `python3 -m unittest tests.test_cli -v` | `fix(gc): honor --dry-run` |
| 2 | Turn forget (R7) | `agres/memory.py` (`delete_turns`), `agres/cli.py`, `tests/test_memory.py`, `tests/test_cli.py` | `tests.test_memory tests.test_cli` | `feat(forget): delete captured turns by id or session` |
| 3 | Settings: retention, redact_patterns, plugin-option env (R3, R8, R9) | `agres/config.py`, `agres/util.py`, `agres/hooks.py` (session-end prune), `tests/test_hooks.py`, new `tests/test_config.py` | `tests.test_config tests.test_hooks` | `feat(config): turn retention, extra redaction, plugin options` |
| 4 | Provenance framing (R10) | `agres/pack.py`, `agres/cli.py` (show), `agres/search.py`, `tests/test_pack.py`, `tests/test_search.py` | `tests.test_pack tests.test_search` | `feat(pack): frame memory as evidence, tag non-agent sources` |
| 5 | Index reconcile (R11) | `agres/hooks.py` (edit, Bash branch), `agres/code/engine.py` (`reconcile`), `agres/status.py`, `tests/test_hooks.py`, `tests/test_index.py` | `tests.test_hooks tests.test_index` | `feat(index): reconcile dirty files after each turn` |
| 6 | Capture policy in status/install (R9) | `agres/status.py`, `agres/install.py`, `tests/test_status.py` | `tests.test_status` | `feat(status): show capture, retention, redaction limits` |
| 6a | Fold store + schema 101 (R14) | `agres/db.py`, `agres/v0.py`, new `agres/fold.py`, `tests/test_fold.py` | `tests.test_fold tests.test_db` | `feat(fold): fold store, schema 101` |
| 6b | Tool fold hook + unfold (R15, R18) | `agres/fold.py`, `agres/hooks.py` (`fold` event), `agres/cli.py` (`fold`, `unfold`, `show F`, `forget F`) | `tests.test_fold tests.test_hooks` | `feat(fold): fold large Bash output in the live window, unfold by ID` |
| 6c | Branch + span folds, pack, search, retention (R16, R17, R19) | `agres/hooks.py`, `agres/pack.py`, `agres/search.py` | `tests.test_fold tests.test_pack tests.test_search` | `feat(fold): subagent and compaction folds in pack and search` |
| 7 | Plugin layout (R1, R2, R3) | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `hooks/hooks.json`, skill path, `package.json`, `README.md`, `tests/test_plugin.py` (hooks.json ≡ `install.hook_entries`) | `tests.test_plugin` + `claude plugin validate .` | `feat(plugin): ship agres as a Claude Code plugin` |
| 8 | Install `--plugin`, skill-root scan, doctor (R4, R5) | `agres/install.py`, `tests/test_install.py` | `tests.test_install` | `feat(install): plugin migration, all skill roots, double-wiring check` |
| 9 | Eval set (R12, R20) | `agres/evalrun.py`, `evals/memory/cases.json`, `tests/test_evals.py` | `tests.test_evals` + `python3 -m agres.evalrun` | `test(evals): recall, staleness, abstention, pack size, latency` |
| 10 | Skill + docs | `skill/agres/SKILL.md`, `REFERENCE.md`, `CLAUDE.md`, `README.md`, ADR `docs/adr/0002-plugin-and-origami.md` | doctor skill check | `docs: plugin install, forget, retention, trust` |
| 11 | Hercules (R13) | `~/Dev/Purpleclaw/Hercules`: remove `skills/agres`, add dependency | `claude plugin validate` there | separate repo commit |
| 12 | Live switch (needs your OK) | run `agres install --plugin --dry-run`, show it, then `--yes`; `claude plugin marketplace add ~/Dev/agres`; `claude plugin install agres@agres`; `agres doctor` | doctor all ✓ | — |

Dependency direction unchanged: `hooks` → `config/memory/pack`; `agres.code` only from the edit
path (and CLI). Phase 2 also updates CLAUDE.md invariant: hard delete allowed for captured turns only. Rollout: phases 1–10 are inert until phase 12. Rollback: `agres uninstall` restores the
manifest; `claude plugin uninstall agres`.

Verification gate before PR: full suite, `compileall`, `claude plugin validate . --strict`,
fresh-context review subagent on phases 2, 3, 8 (deletes data / touches user config).
