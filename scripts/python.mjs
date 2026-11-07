#!/usr/bin/env node
import { spawn } from "node:child_process";
import { preparedPython, repositoryRoot } from "./python-runtime.mjs";

try {
  const child = spawn(preparedPython(), process.argv.slice(2), {
    cwd: repositoryRoot, stdio: "inherit", windowsHide: true,
  });
  child.on("error", (error) => {
    console.error(`Cannot start Python: ${error.message}`);
    process.exitCode = 1;
  });
  child.on("exit", (code, signal) => {
    process.exitCode = code ?? (signal === "SIGINT" ? 130 : 1);
  });
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
