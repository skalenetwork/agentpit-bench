#!/usr/bin/env node
// Launcher for `npx agentpitbench-mcp`: the server itself is the Python package agentpitbench-mcp (no
// dependencies), run through uvx, or pipx if uv is not installed. One implementation, no logic to keep in sync.
"use strict";
const { spawn } = require("node:child_process");

const args = process.argv.slice(2);
const runners = [
  ["uvx", ["agentpitbench-mcp", ...args]],
  ["pipx", ["run", "agentpitbench-mcp", ...args]],
];

function run(i) {
  if (i >= runners.length) {
    process.stderr.write("agentpitbench-mcp needs uv (https://docs.astral.sh/uv/) or pipx to run the Python server.\n");
    process.exit(1);
  }
  const [cmd, argv] = runners[i];
  const child = spawn(cmd, argv, { stdio: "inherit", env: process.env });
  child.on("error", (e) => {
    if (e.code === "ENOENT") run(i + 1);
    else { process.stderr.write(`agentpitbench-mcp: ${e.message}\n`); process.exit(1); }
  });
  child.on("exit", (code, signal) => process.exit(code === null ? 1 : code));
  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
}

run(0);
