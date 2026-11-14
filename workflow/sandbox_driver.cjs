// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

"use strict";

const { spawn } = require("node:child_process");

if (!process.env.NFV_PYTHON) {
  throw new Error("Trusted setup must configure NFV_PYTHON");
}
const environment = { ...process.env };
// NFV only needs the sidecar's per-run connection token, not platform auth.
for (const name of ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]) {
  delete environment[name];
}
const child = spawn(process.env.NFV_PYTHON, ["-u", "-m", "nfv_workflow.sandbox"], {
  stdio: "inherit",
  env: environment,
});
const heartbeat = setInterval(() => {
  console.log(JSON.stringify({ event: "nfv_running" }));
}, 60000);
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("error", (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
child.on("close", (code) => {
  clearInterval(heartbeat);
  process.exitCode = code ?? 1;
});
