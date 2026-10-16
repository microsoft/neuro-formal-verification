"use strict";

const { spawn } = require("node:child_process");

if (!process.env.NFV_PYTHON) {
  throw new Error("Trusted setup must configure NFV_PYTHON");
}
const child = spawn(process.env.NFV_PYTHON, ["-u", "-m", "nfv_workflow.sandbox"], {
  stdio: "inherit",
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
