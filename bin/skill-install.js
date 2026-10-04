#!/usr/bin/env node
/**
 * Install agres skill to common agent locations.
 * Skills (SKILL.md + adapters + snippets) to:
 *  - ~/.config/opencode/skills/agres (opencode)
 *  - ~/.opencode/skills/agres (opencode legacy data dir)
 *  - ~/.agents/skills/agres (generic agents)
 *  - ~/.claude/skills/agres (claude code)
 *  - ~/.codex/skills/agres (codex cli)
 *  - ~/.gemini/skills/agres (gemini cli)
 *  - ./skills/agres (if in a project)
 * Global memory markers (agents read these every session):
 *  - ~/.claude/CLAUDE.md, ~/.config/opencode/AGENTS.md, ~/.gemini/GEMINI.md
 * Project rules (if in a project): AGENTS.md, CLAUDE.md,
 *  .github/copilot-instructions.md, .clinerules, .cursor/rules/agres.mdc
 * commandcode/cline register from GitHub after push:
 *  commandcode skills add purple-claw/agres | cline skill add purple-claw/agres
 * Lazy but smart: idempotent, marker appends never duplicate.
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

function ensureMarkerBlock(filePath, marker, block) {
  // Idempotent marker append: create if missing, skip if marker present, else append.
  // Returns 'created' | 'appended' | 'exists'.
  try {
    if (!fs.existsSync(filePath)) {
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      fs.writeFileSync(filePath, block + '\n');
      return 'created';
    }
    const cur = fs.readFileSync(filePath, 'utf8');
    if (cur.includes(marker)) return 'exists';
    fs.appendFileSync(filePath, (cur.endsWith('\n') ? '' : '\n') + '\n' + block + '\n');
    return 'appended';
  } catch (e) {
    console.error(`rules file failed ${filePath}: ${e.message}`);
    return 'failed';
  }
}

const AGRES_MARKER = '<!-- agres:auto -->';
const AGRES_RULE_BLOCK = [
  AGRES_MARKER,
  '# Agres (auto memory)',
  '',
  'Use Agres Origami Memory AUTOMATICALLY, without being asked, when writing,',
  'editing, or debugging code, on any repository task, or when earlier context,',
  'decisions, or file/symbol context is needed. Stop only if the user says',
  '"stop using Agres".',
  '',
  '- Full contract: skills/agres/SKILL.md (or `npx @ithica/agres`).',
  '- Capture turns verbatim: `agres capture --role user|assistant --text "..."`.',
  '- After EVERY file edit: `agres touch --files <edited-files>`.',
  '- Window filling: `agres fold`; need history: `agres unfold --query "..."` (exact text).',
  '<!-- /agres:auto -->',
].join('\n');

function installProjectRules() {
  // Project-level auto-read surfaces: agents read these without being asked.
  // Only inside a project (package.json or .git), skipped with --no-rules.
  if (process.argv.includes('--no-rules')) {
    console.log('rules → skipped (--no-rules)');
    return;
  }
  const cwd = process.cwd();
  const inProject = fs.existsSync(path.join(cwd, 'package.json')) || fs.existsSync(path.join(cwd, '.git'));
  if (!inProject) {
    console.log('rules → skipped (not a project: no package.json or .git)');
    return;
  }
  const pkgRoot = path.join(__dirname, '..');
  const results = [];
  results.push(['AGENTS.md', ensureMarkerBlock(path.join(cwd, 'AGENTS.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  results.push(['CLAUDE.md', ensureMarkerBlock(path.join(cwd, 'CLAUDE.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  results.push(['.github/copilot-instructions.md', ensureMarkerBlock(path.join(cwd, '.github', 'copilot-instructions.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  results.push(['.clinerules', ensureMarkerBlock(path.join(cwd, '.clinerules'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  try {
    const src = path.join(pkgRoot, 'adapters', 'cursor-rule.mdc');
    const dest = path.join(cwd, '.cursor', 'rules', 'agres.mdc');
    if (fs.existsSync(src)) {
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(src, dest);
      results.push(['.cursor/rules/agres.mdc', 'installed']);
    }
  } catch (e) { results.push(['.cursor/rules/agres.mdc', 'failed']); }
  for (const [name, st] of results) console.log(`rules → ${name}: ${st}`);
}

function installGlobalRules() {
  // Home-level memory files agents read for EVERY session (not just one project).
  // Idempotent marker append; never overwrites user content.
  const home = os.homedir();
  const results = [];
  results.push(['~/.claude/CLAUDE.md', ensureMarkerBlock(
    path.join(home, '.claude', 'CLAUDE.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  results.push(['~/.config/opencode/AGENTS.md', ensureMarkerBlock(
    path.join(home, '.config', 'opencode', 'AGENTS.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  results.push(['~/.gemini/GEMINI.md', ensureMarkerBlock(
    path.join(home, '.gemini', 'GEMINI.md'), AGRES_MARKER, AGRES_RULE_BLOCK)]);
  for (const [name, st] of results) console.log(`global → ${name}: ${st}`);
}

function install() {
  const pkgRoot = path.join(__dirname, '..');
  const skillSrc = path.join(pkgRoot, 'skills', 'agres');
  const adaptersSrc = path.join(pkgRoot, 'adapters');
  const snippetsSrc = path.join(pkgRoot, 'snippets');
  const runtimeSrc = path.join(pkgRoot, 'runtime', 'agres_runtime.py');

  const targets = [
    path.join(os.homedir(), '.config', 'opencode', 'skills', 'agres'),
    path.join(os.homedir(), '.opencode', 'skills', 'agres'),
    path.join(os.homedir(), '.agents', 'skills', 'agres'),
    path.join(os.homedir(), '.claude', 'skills', 'agres'),
    path.join(os.homedir(), '.codex', 'skills', 'agres'),
    path.join(os.homedir(), '.gemini', 'skills', 'agres'),
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
    const shim = `#!/usr/bin/env bash\nexec npx -y @ithica/agres "$@"\n`;
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
  installGlobalRules();
  installProjectRules();
  console.log('verify auto-activation with: npx agres doctor');
  console.log('docs: https://github.com/purple-claw/agres');
}

if (require.main === module) install();
module.exports = { install };
