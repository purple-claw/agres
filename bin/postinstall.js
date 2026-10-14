// postinstall: never touches your files. Wiring is an explicit, reversible step.
if (!process.env.CI && process.env.AGRES_SILENT_POSTINSTALL !== "1") {
  console.log(
    "\nagres installed. Wire it into Claude Code / Codex (shows a dry run first):",
  );
  console.log("  npx @ithica/agres install --dry-run");
  console.log("  npx @ithica/agres doctor\n");
}
