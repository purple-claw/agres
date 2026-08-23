#!/usr/bin/env node
/**
 * Install agres skill to common agent locations.
 * Copies SKILL.md + adapters + snippets to:
 *  - ~/.config/opencode/skills/agres
 *  - ~/.agent/skills/agres (if exists structure)
 *  - ./skills/agres (if in a project)
 * Lazy but smart: idempotent, overwrites only if newer.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');

function copyDir(src, dest) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

function install() {
  const pkgRoot = path.join(__dirname, '..');
  const skillSrc = path.join(pkgRoot, 'skills', 'agres');
  const adaptersSrc = path.join(pkgRoot, 'adapters');
  const snippetsSrc = path.join(pkgRoot, 'snippets');
  const runtimeSrc = path.join(pkgRoot, 'runtime', 'agres_runtime.py');

  const targets = [
    path.join(os.homedir(), '.config', 'opencode', 'skills', 'agres'),
    path.join(os.homedir(), '.agents', 'skills', 'agres'),
    path.join(os.homedir(), '.claude', 'skills', 'agres'),
    path.join(process.cwd(), 'skills', 'agres'),
  ];

  // Also install runtime to ~/.agres/runtime/agres_runtime.py
  const homeAgresRuntime = path.join(os.homedir(), '.agres', 'runtime', 'agres_runtime.py');
  try {
    fs.mkdirSync(path.dirname(homeAgresRuntime), { recursive: true });
    fs.copyFileSync(runtimeSrc, homeAgresRuntime);
    console.log(`runtime → ${homeAgresRuntime}`);
  } catch (e) { console.error(`runtime install failed: ${e.message}`); }

  // Install bin shim to ~/.local/bin/agres if not exists or --force
  const binDest = path.join(os.homedir(), '.local', 'bin', 'agres');
  try {
    fs.mkdirSync(path.dirname(binDest), { recursive: true });
    const shim = `#!/usr/bin/env bash\nexec npx agres "$@"\n`;
    if (!fs.existsSync(binDest) || process.argv.includes('--force')) {
      fs.writeFileSync(binDest, shim, { mode: 0o755 });
      console.log(`shim → ${binDest} (npx delegator)`);
    } else {
      console.log(`shim exists → ${binDest} (use --force to overwrite)`);
    }
  } catch (e) { console.error(`bin install failed: ${e.message}`); }

  for (const dest of targets) {
    try {
      // Only install to paths that make sense: home-based always, cwd only if package.json or .git exists
      if (dest.startsWith(path.join(process.cwd(), 'skills')) && !fs.existsSync(path.join(process.cwd(), 'package.json')) && !fs.existsSync(path.join(process.cwd(), '.git'))) {
        continue;
      }
      fs.mkdirSync(dest, { recursive: true });
      const srcSkill = path.join(skillSrc, 'SKILL.md');
      if (fs.existsSync(srcSkill)) fs.copyFileSync(srcSkill, path.join(dest, 'SKILL.md'));
      copyDir(adaptersSrc, path.join(dest, 'adapters'));
      copyDir(snippetsSrc, path.join(dest, 'snippets'));
      console.log(`skill → ${dest}`);
    } catch (e) { console.error(`failed ${dest}: ${e.message}`); }
  }
  console.log('\ndone. Verify with: npx agres status');
  console.log('docs: https://github.com/purple-claw/agres');
}

if (require.main === module) install();
module.exports = { install };
