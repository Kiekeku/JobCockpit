#!/usr/bin/env node

import { existsSync, mkdirSync, statSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { availableParallelism } from "node:os";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { loadDotEnv } from "./env.mjs";

// Select the prepared runtime without leaking local service settings into tests.
loadDotEnv(".env", ["UV_PROJECT_ENVIRONMENT", "UV_PYTHON"]);

const args = new Set(process.argv.slice(2).filter((arg) => arg !== "--"));
const options = new Set(["--fast", "--slow", "--serial"]);
const selectedTests = [...args].filter((arg) => !options.has(arg));
const testsRoot = resolve("tests");
function isTestSelector(arg) {
  if (!/^tests[\\/][^:]+\.py(?:::.+)?$/.test(arg)) return false;
  const file = resolve(arg.split("::")[0]);
  const withinTests = relative(testsRoot, file);
  return !isAbsolute(withinTests) && withinTests !== ".."
    && !withinTests.startsWith(`..${sep}`) && existsSync(file) && statSync(file).isFile();
}
const unknownArgs = selectedTests.filter((arg) => !isTestSelector(arg));
if (unknownArgs.length > 0 || (args.has("--fast") && args.has("--slow"))) {
  console.error("Usage: node scripts/test.mjs [--fast|--slow] [--serial] [tests/file.py[::test_id] ...]");
  process.exit(2);
}

mkdirSync(".pytest-tmp", { recursive: true });
mkdirSync(".tmp", { recursive: true });
const baseTemp = `.tmp/pytest-basetemp-${process.pid}-${Date.now()}`;

// Browser integration must exercise current compiled extension code on fresh checkouts.
if (!args.has("--fast")) {
  const built = spawnSync(process.execPath, ["scripts/build-extension.mjs"], {
    stdio: "inherit", windowsHide: true
  });
  if (built.status !== 0) process.exit(built.status ?? 1);
}

const pytestArgs = [
  "--cache-dir",
  ".tmp/uv-run-cache",
  "run",
  "--no-sync",
  "python", "-m", "pytest",
  "--basetemp",
  baseTemp,
  "-p",
  "no:cacheprovider"
];

if (!args.has("--serial")) {
  const defaultWorkerCount = String(Math.min(16, availableParallelism()));
  pytestArgs.push("-n", process.env.PYTEST_XDIST_WORKERS || defaultWorkerCount);
}

if (args.has("--fast")) {
  pytestArgs.push("-m", "not slow");
} else if (args.has("--slow")) {
  pytestArgs.push("-m", "slow");
}
pytestArgs.push(...selectedTests);

const uvCommand = process.platform === "win32" ? "uv.exe" : "uv";
const child = spawn(uvCommand, pytestArgs, { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) {
    console.error(`pytest terminated by ${signal}`);
    process.exit(1);
  }
  process.exit(code ?? 1);
});
