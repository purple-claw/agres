---
name: agres
description: "Agres Origami Memory: makes a limited context window behave like a much larger one (verbatim capture, fold overflow out, unfold exact text on demand). USE AUTOMATICALLY without being asked WHEN: writing, editing, or debugging code; any repository task; multi-step or long-running work; needing earlier context, decisions, or file/symbol context. Stop only if the user says stop using Agres."
default_enabled: true  # Injector + watchdog always on; capture is 1 cheap sqlite write per turn
storage_backend: "sqlite"
runtime_cli: "agres"
stop_phrases:
  - "stop using Agres"  # matched case-insensitive, runtime normalizes
  - "disable Agres"
  - "Agres off"
  - "agres off"
  - "stop agres"
enable_phrases:
  - "enable Agres"
  - "use Agres"
  - "Agres on"
  - "agres on"
---

# Agres — Project Memory Injector + Watchdog

Agres is the project's memory. Two jobs, entire development long:

1. **Injector** — feed the model low-token, high-context packs: what the
   project is, how it is structured, what was decided, what is in flight.
   One bounded `brain` pack (≈500 tokens) replaces re-exploring the repo.
2. **Watchdog** — guard that memory while work happens: capture every
   durable fact (decisions, rejections, tasks, errors, pins), keep the
   index fresh on every edit, checkpoint every milestone. Nothing silently
   lost, nothing stale overriding fresh.

This is NOT token-budget management. No budget loops in normal flow.
Fold/unfold exists only as an overflow tool for very long sessions —
see REFERENCE.md. Day to day: inject, work, guard, checkpoint.

Full command catalog and internals: **REFERENCE.md** (next to this file).

## When active

Always on for: writing, editing, or debugging code; any repository task;
multi-step work; needing earlier context, decisions, or file/symbol context.
Trivial one-shots (<5 turns): capture turns only, nothing else.

Until the user says a stop phrase, stay on:

- stop using Agres / disable Agres / Agres off

Re-enable only on: enable Agres / use Agres / Agres on.

## Inject — session / task start

```bash
agres brain [--query "<task>"]      # THE command: bounded pack (identity+structure+progress+graph)
agres trace                          # full depth when brain is not enough (writes .agres/trace.md)
agres unfold --query "..."           # exact earlier wording back into the window
agres context --query "..."          # keyword search over memory
```

Read `brain` before reading files. Rebuild the graph after structural
changes (`index`/`sync`/many-file `touch`): `agres graph --build`.
First time in a repo: `agres init` then `agres index` — an empty index
means an empty brain, and an empty brain gets ignored.

## Watch — during work

```bash
agres capture --role user --text "<exact words>"        # after each user turn
agres capture --role assistant --text "<exact reply>"   # after each reply
agres touch --files <edited-files>  # after EVERY file edit — index stays fresh
agres decision --text "..." --accepted|--rejected   # incl. REJECTIONS, verbatim
agres task --objective "..."  |  agres error --message "..."  |  agres pin --text "..."
agres checkpoint --reason "..."     # plan approval, task done, test run, pause
agres resume                        # restore goal, decisions, tasks, pins, errors
agres status                        # dashboard: memory, conversation, brain, storage, fixes
```

Hooks (where installed) already run `touch` on edit, `brain` on session
start, and `checkpoint` on stop/compact — the commands above are the
manual fallback. Never let hooks failing stop the work: they are fail-open.

## Retrieval priority (high-context first, few tokens)

Pinned > active task > approved/rejected decisions > graph neighborhood
(BFS around the task) > mentioned files > recently edited files >
failing tests/errors > exact lookup > fold/memory search.

For architecture questions the graph neighborhood outranks raw text search;
for line-level depth, file reads still win.

## What Agres must not do

- Re-explore the repo each session (inject `brain` first).
- Dump repos into prompt (bounded packs only).
- Treat transcript as only memory; forget rejections; lose context on restart.
- Let stale override fresh (touch on edit, rebuild graph, re-read brain).
- Require external services or embeddings.
- Paraphrase memory (verbatim captures, exact unfolds).
- Run budget/fold loops unasked (overflow tool only).

## Final rule

Through the whole project, the model works from injected memory, not from
re-discovery. **Low tokens in, high context out — and the watchdog keeps it
true: every decision kept, every rejection kept, every milestone checkpointed.**
