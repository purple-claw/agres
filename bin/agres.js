#!/usr/bin/env node
/**
 * agres npx shim - finds python and delegates to runtime/agres_runtime.py
 * Lazy dev's shim: if python not found, complain and die. No fallback magic.
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const runtime = path.join(__dirname, '..', 'runtime', 'agres_runtime.py');

// Find python
function findPython() {
  const candidates = ['python3', 'python'];
  // Could add more logic, but ponytail says: try python3 then give up and tell user
  return candidates[0];
}

const python = process.env.AGRES_PYTHON || findPython();
const args = [runtime, ...process.argv.slice(2)];

const child = spawn(python, args, { stdio: 'inherit' });

child.on('error', (err) => {
  console.error(`agres: failed to spawn ${python}: ${err.message}`);
  console.error('hint: set AGRES_PYTHON to your python binary, or install python3');
  process.exit(1);
});

child.on('close', (code) => process.exit(code ?? 0));
