#!/usr/bin/env node
// npm/npx shim: run the bundled Python package (stdlib only) with python3.
const { spawn } = require("child_process");
const path = require("path");

const root = path.join(__dirname, "..");
const python = process.env.AGRES_PYTHON || "python3";
const env = {
  ...process.env,
  PYTHONPATH:
    root +
    (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ""),
};

const child = spawn(python, ["-m", "agres", ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
});
child.on("error", (err) => {
  console.error(
    `agres: could not start ${python}: ${err.message} (set AGRES_PYTHON to a Python >= 3.9)`,
  );
  process.exit(1);
});
child.on("close", (code) => process.exit(code ?? 0));
