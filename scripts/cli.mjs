#!/usr/bin/env node

import { spawn } from "node:child_process";
import { preparedPython, repositoryRoot } from "./python-runtime.mjs";

// Use the same prepared environment as pnpm dev. Never sync packages while
// scheduled workers may have their executables or native modules open.
let python;
try {
  python = preparedPython();
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
const cliArgs = process.argv.slice(2);
// Python preserves unknown -X options without changing application arguments.
// This makes relative/manual Node launches attributable to this repository.
const workerMarker = cliArgs[0] === "worker" ? ["-X", `jobcockpit_root=${repositoryRoot}`] : [];
const child = spawn(python, [...workerMarker, "-m", "jobcockpit.cli", ...cliArgs], {
  cwd: repositoryRoot,
  stdio: "inherit",
  windowsHide: true,
});
child.on("error", (error) => {
  console.error(`Cannot start JobCockpit CLI: ${error.message}`);
  process.exitCode = 1;
});
child.on("exit", (code, signal) => {
  process.exitCode = code ?? (signal === "SIGINT" ? 130 : 1);
});
