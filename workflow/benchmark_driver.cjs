// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

"use strict";

const { spawn } = require("node:child_process");

if (!process.env.NFV_PYTHON || !process.env.NFV_BENCH_PAYLOAD || !process.env.NFV_BENCH_OUT) {
  throw new Error("Trusted benchmark setup must configure Python, payload and output");
}
const environment = { ...process.env };
for (const name of ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]) {
  delete environment[name];
}
const child = spawn(process.env.NFV_PYTHON, [
  "-u", "-m", "nfv_workflow.benchmark", "worker",
  "--payload", process.env.NFV_BENCH_PAYLOAD, "--out", process.env.NFV_BENCH_OUT,
], { stdio: "inherit", env: environment });
const heartbeat = setInterval(() => {
  console.log(JSON.stringify({ event: "nfv_benchmark_running" }));
}, 60000);
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.once("error", (error) => {
  clearInterval(heartbeat);
  console.error(error.message);
  process.exitCode = 1;
});
child.once("close", (code) => {
  clearInterval(heartbeat);
  process.exitCode = code ?? 1;
});
