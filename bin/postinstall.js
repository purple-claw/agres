// postinstall: do nothing, be lazy. Don't touch user's filesystem without asking.
// If user wants skill installed to opencode, they run: npx agres skill-install
// We only print a hint, not mutate.
if (process.env.AGRES_SILENT_POSTINSTALL !== '1') {
  // silent in CI
  if (!process.env.CI) {
    console.log('\nagres installed. Quick start:');
    console.log('  npx agres --help');
    console.log('  npx agres status');
    console.log('  npx agres skill-install  # installs to ~/.config/opencode/skills/agres\n');
  }
}
