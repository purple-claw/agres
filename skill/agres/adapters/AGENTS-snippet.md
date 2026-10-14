<!-- agres:begin — paste into AGENTS.md / .cursor/rules / .clinerules / copilot-instructions -->
## Project memory (agres)
- At task start run `agres brain` and read it: last handoff, constraints, rejected ideas, open work.
- Record judgment as you go: `agres decide|reject|pin|task|note|handoff "..."`; close with `agres done|drop <ID>`;
  when a write lists a similar item, use `--replaces <ID>` instead of adding a duplicate.
- Before re-exploring or re-asking: `agres search "<words>"`, then `agres show <ID>`.
- Never re-propose an idea listed as rejected unless the user reopens it.
<!-- agres:end -->
