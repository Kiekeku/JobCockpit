#!/usr/bin/env node

import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, unlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { resolve } from "node:path";
import process from "node:process";
import { shouldDisplayServiceLine } from "./dev-output.mjs";
import { loadDotEnv } from "./env.mjs";

loadDotEnv(".env");

const args = new Set(process.argv.slice(2));

if (args.has("--help") || args.has("-h")) {
  console.log(`Usage: pnpm dev [options]

Starts the local JobCockpit development stack.

Options:
  --mock             Also start the local ATS mock site
  --no-worker        Disable the API background processing worker
  --extension        Also start the Chrome extension TypeScript watcher
  --next-ports       Use the next available ports when configured ports are busy
  --skip-migrations  Do not run Alembic migrations before starting servers

Environment:
  Copy .env.example to .env and set JOBCOCKPIT_API_PORT,
  JOBCOCKPIT_WEB_PORT, or JOBCOCKPIT_ATS_MOCK_PORT to override ports.
`);
  process.exit(0);
}

const includeMock = args.has("--mock");
const includeWorker = !args.has("--no-worker");
const includeExtension = args.has("--extension");
const skipMigrations = args.has("--skip-migrations");
const useNextPorts = args.has("--next-ports");
const isWindows = process.platform === "win32";
const uvRunArgs = ["run", "--no-sync"];
const uvSyncArgs = ["sync"];
// Keep the unset-provider default aligned with Settings in core/config.py.
const renderedProvider = process.env.JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER
  ?? "invisible_playwright";
if (["invisible_playwright", "invisible_playwright_first"].includes(renderedProvider)) {
  uvSyncArgs.push("--extra", "invisible-browser");
}
const host = process.env.JOBCOCKPIT_DEV_HOST ?? "127.0.0.1";
// Match uv sync/run when a separately validated runtime is selected locally.
const venvRoot = resolve(process.cwd(), process.env.UV_PROJECT_ENVIRONMENT ?? ".venv");
const venvPython = isWindows
  ? resolve(venvRoot, "Scripts", "python.exe")
  : resolve(venvRoot, "bin", "python");
let apiPort = envPort("JOBCOCKPIT_API_PORT", 8000);
let webPort = envPort("JOBCOCKPIT_WEB_PORT", 3000);
let atsMockPort = envPort("JOBCOCKPIT_ATS_MOCK_PORT", 8001);

if (useNextPorts) {
  const reservedPorts = new Set();
  apiPort = await chooseAvailablePort("api", "JOBCOCKPIT_API_PORT", apiPort, reservedPorts);
  webPort = await chooseAvailablePort("web", "JOBCOCKPIT_WEB_PORT", webPort, reservedPorts);
  if (includeMock) {
    atsMockPort = await chooseAvailablePort(
      "ats",
      "JOBCOCKPIT_ATS_MOCK_PORT",
      atsMockPort,
      reservedPorts
    );
  }
}

const apiBaseUrl = process.env.JOBCOCKPIT_API_BASE_URL ?? `http://${host}:${apiPort}`;
const PID_FILE = "var/dev-pids.json";
const DEV_LOG_DIR = resolve(process.cwd(), "var", "logs", "dev");
const sessionId = `${new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}-${process.pid}`;
const DEV_LOG_PATH = resolve(DEV_LOG_DIR, `dev-${sessionId}.log`);
const API_DIAGNOSTIC_LOG_PATH = resolve(DEV_LOG_DIR, `dev-${sessionId}-api.jsonl`);
const API_CRASH_LOG_PATH = resolve(DEV_LOG_DIR, `dev-${sessionId}-api-crash.log`);
const ATS_DIAGNOSTIC_LOG_PATH = resolve(DEV_LOG_DIR, `dev-${sessionId}-ats.jsonl`);
const ATS_CRASH_LOG_PATH = resolve(DEV_LOG_DIR, `dev-${sessionId}-ats-crash.log`);
const apiRestartLimit = envRestartLimit("JOBCOCKPIT_DEV_API_RESTART_LIMIT", -1);
const children = [];
let shuttingDown = false;
let sessionLogFailed = false;

mkdirSync(DEV_LOG_DIR, { recursive: true });
writeFileSync(
  DEV_LOG_PATH,
  `${new Date().toISOString()} [dev] session_started pid=${process.pid} cwd=${process.cwd()}\n`
);

function appendSessionLog(name, line, channel = "INFO") {
  try {
    appendFileSync(
      DEV_LOG_PATH,
      `${new Date().toISOString()} [${name}] [${channel}] ${line}\n`,
      { encoding: "utf8" }
    );
  } catch (error) {
    if (!sessionLogFailed) {
      sessionLogFailed = true;
      console.error(
        `[dev] failed to append session log ${DEV_LOG_PATH}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
}

function logDev(message) {
  console.log(`[dev] ${message}`);
  appendSessionLog("dev", message);
}

function warnDev(message) {
  console.warn(`[dev] ${message}`);
  appendSessionLog("dev", message, "WARN");
}

function errorDev(message) {
  console.error(`[dev] ${message}`);
  appendSessionLog("dev", message, "ERROR");
}

const services = [
  {
    name: "api",
    command: venvPython,
    args: [
      "scripts/run_uvicorn_dev.py",
      "jobcockpit.api.main:app",
      "--app-dir",
      "apps/api",
      "--reload-dir",
      "apps/api",
      "--host",
      host,
      "--port",
      String(apiPort),
      "--diagnostic-log",
      API_DIAGNOSTIC_LOG_PATH,
      "--crash-log",
      API_CRASH_LOG_PATH,
      "--restart-limit",
      String(apiRestartLimit)
    ],
    url: `${apiBaseUrl}/api/health`,
    port: apiPort,
    portEnv: "JOBCOCKPIT_API_PORT"
  },
  {
    name: "web",
    command: "pnpm",
    args: [
      "--filter",
      "@jobcockpit/web",
      "dev",
      "--hostname",
      host,
      "--port",
      String(webPort)
    ],
    url: `http://${host}:${webPort}`,
    port: webPort,
    portEnv: "JOBCOCKPIT_WEB_PORT"
  }
];

if (includeMock) {
  services.push({
    name: "ats",
    command: venvPython,
    args: [
      "scripts/run_uvicorn_dev.py",
      "mock_site:app",
      "--app-dir",
      "apps/ats-mock-site",
      "--reload-dir",
      "apps/ats-mock-site",
      "--host",
      host,
      "--port",
      String(atsMockPort),
      "--diagnostic-log",
      ATS_DIAGNOSTIC_LOG_PATH,
      "--crash-log",
      ATS_CRASH_LOG_PATH,
      "--restart-limit",
      String(apiRestartLimit)
    ],
    url: `http://${host}:${atsMockPort}/ats/generic/acme`,
    port: atsMockPort,
    portEnv: "JOBCOCKPIT_ATS_MOCK_PORT"
  });
}

if (includeExtension) {
  services.push({
    name: "extension",
    command: "pnpm",
    args: ["--filter", "@jobcockpit/chrome-extension", "dev"],
    url: "apps/chrome-extension/dist"
  });
}

function prefixStream(name, stream, target) {
  let buffer = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    buffer += chunk;
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.length > 0) {
        emitServiceLine(name, line, target);
      }
    }
  });
  stream.on("end", () => {
    if (buffer.length > 0) {
      emitServiceLine(name, buffer, target);
    }
  });
}

function emitServiceLine(name, line, target) {
  appendSessionLog(name, line, target === process.stderr ? "STDERR" : "STDOUT");
  if (shouldDisplayServiceLine(name, line)) {
    target.write(`[${name}] ${line}\n`);
  }
}

function spawnProcess(service) {
  const needsShell = isWindows && service.command === "pnpm";
  const child = spawn(service.command, service.args, {
    cwd: process.cwd(),
    env: childEnv(),
    shell: needsShell,
    windowsHide: isWindows
  });
  children.push({ child, service });
  appendSessionLog(
    "dev",
    `service_spawned name=${service.name} pid=${child.pid ?? "unknown"} command=${service.command} args=${JSON.stringify(service.args)}`
  );
  writePidFile();
  prefixStream(service.name, child.stdout, process.stdout);
  prefixStream(service.name, child.stderr, process.stderr);
  child.on("exit", (code, signal) => {
    if (shuttingDown) {
      return;
    }
    const reason = describeProcessExit(code, signal);
    errorDev(`${service.name} stopped unexpectedly with ${reason}`);
    shutdown(code && code > 0 ? code : 1);
  });
}

function runOnce(name, command, commandArgs) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, commandArgs, {
      cwd: process.cwd(),
      env: childEnv(),
      shell: false,
      windowsHide: isWindows
    });
    prefixStream(name, child.stdout, process.stdout);
    prefixStream(name, child.stderr, process.stderr);
    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`${name} failed with exit code ${code}`));
      }
    });
  });
}

async function shutdown(exitCode = 0) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  appendSessionLog("dev", `shutdown_started exit_code=${exitCode}`);
  await Promise.all(children.map(({ child }) => stopProcess(child)));
  removePidFile();
  appendSessionLog("dev", `shutdown_complete exit_code=${exitCode}`);
  setTimeout(() => process.exit(exitCode), 100);
}

function stopProcess(child) {
  if (child.killed || child.exitCode !== null || child.pid === undefined) {
    return Promise.resolve();
  }
  if (isWindows) {
    return new Promise((resolve) => {
      const killer = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
        stdio: "ignore",
        windowsHide: true
      });
      const timeout = setTimeout(resolve, 3000);
      killer.on("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
      killer.on("error", () => {
        clearTimeout(timeout);
        try {
          child.kill();
        } catch {
          // The process may have already exited between the state check and kill.
        }
        resolve();
      });
    });
  } else {
    try {
      child.kill();
    } catch {
      // The process may have already exited between the state check and kill.
    }
    return Promise.resolve();
  }
}

function childEnv() {
  return {
    ...process.env,
    JOBCOCKPIT_API_BASE_URL: apiBaseUrl,
    JOBCOCKPIT_ATS_MOCK_PORT: String(atsMockPort),
    JOBCOCKPIT_BACKGROUND_WORKERS_ENABLED: includeWorker ? "true" : "false"
  };
}

function writePidFile() {
  mkdirSync("var", { recursive: true });
  const payload = {
    parentPid: process.pid,
    cwd: process.cwd(),
    host,
    sessionLog: DEV_LOG_PATH,
    apiDiagnosticLog: API_DIAGNOSTIC_LOG_PATH,
    apiCrashLog: API_CRASH_LOG_PATH,
    services: children
      .filter(({ child }) => child.pid !== undefined)
      .map(({ child, service }) => ({
        name: service.name,
        pid: child.pid,
        port: service.port
      }))
  };
  writeFileSync(PID_FILE, `${JSON.stringify(payload, null, 2)}\n`);
}

function removePidFile() {
  try {
    unlinkSync(PID_FILE);
  } catch {
    // The pid file may have been removed manually or never written.
  }
}

function envPort(name, fallback) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const port = Number.parseInt(raw, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535 || String(port) !== raw.trim()) {
    throw new Error(`${name} must be an integer port between 1 and 65535`);
  }
  return port;
}

function envRestartLimit(name, fallback) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < -1 || String(value) !== raw.trim()) {
    throw new Error(`${name} must be -1 (unlimited), zero, or a positive integer`);
  }
  return value;
}

function describeProcessExit(code, signal) {
  if (signal) {
    return `signal ${signal}`;
  }
  if (!isWindows || code === null) {
    return `exit code ${code}`;
  }
  const unsigned = Number(code) >>> 0;
  const signed = unsigned < 0x80000000 ? unsigned : unsigned - 0x100000000;
  return `exit code ${code} (0x${unsigned.toString(16).toUpperCase().padStart(8, "0")}; signed ${signed})`;
}

async function assertPortsAvailable(candidateServices) {
  const portServices = candidateServices.filter((service) => typeof service.port === "number");
  const duplicatePorts = findDuplicatePorts(portServices);
  if (duplicatePorts.length > 0) {
    throw new Error(
      [
        "Configured development services share the same port:",
        ...duplicatePorts.map(({ key, names }) => `  - ${key}: ${names.join(", ")}`),
        "Use .env to assign distinct JOBCOCKPIT_*_PORT values."
      ].join("\n")
    );
  }

  const results = await Promise.all(portServices.map((service) => checkPortAvailable(service)));
  const failures = results.filter((result) => result !== null);
  if (failures.length === 0) {
    return;
  }

  const lines = ["Cannot start JobCockpit because one or more dev ports are unavailable:"];
  for (const failure of failures) {
    lines.push(
      `  - ${failure.name} ${host}:${failure.port} (${failure.portEnv}): ${failure.reason}`
    );
  }
  lines.push("Stop the process using the port or choose another port in .env.");
  if (isWindows) {
    lines.push("Windows checks:");
    for (const failure of failures) {
      lines.push(`  netstat -ano | findstr ":${failure.port}"`);
    }
  }
  throw new Error(lines.join("\n"));
}

function findDuplicatePorts(portServices) {
  const ports = new Map();
  for (const service of portServices) {
    const key = `${host}:${service.port}`;
    const names = ports.get(key) ?? [];
    names.push(service.name);
    ports.set(key, names);
  }
  return [...ports.entries()]
    .filter(([, names]) => names.length > 1)
    .map(([key, names]) => ({ key, names }));
}

function checkPortAvailable(service) {
  return checkRawPortAvailable(service.port).then((result) => {
    if (result.available) {
      return null;
    }
    return {
      name: service.name,
      port: service.port,
      portEnv: service.portEnv,
      reason: result.reason
    };
  });
}

function checkRawPortAvailable(port) {
  return new Promise((resolve) => {
    const server = createServer();
    server.once("error", (error) => {
      resolve({ available: false, reason: portErrorReason(error) });
    });
    server.once("listening", () => {
      server.close(() => resolve({ available: true }));
    });
    server.listen({ host, port, exclusive: true });
  });
}

function portErrorReason(error) {
  switch (error.code) {
    case "EADDRINUSE":
      return "already in use";
    case "EACCES":
      return "blocked by Windows permissions or reserved by another service";
    default:
      return error.message;
  }
}

async function chooseAvailablePort(serviceName, envName, preferredPort, reservedPorts) {
  for (let port = preferredPort; port <= 65535; port += 1) {
    if (reservedPorts.has(port)) {
      continue;
    }
    const result = await checkRawPortAvailable(port);
    if (result.available) {
      reservedPorts.add(port);
      if (port !== preferredPort) {
        warnDev(
          `${serviceName} ${host}:${preferredPort} (${envName}) is unavailable; using ${host}:${port}`
        );
      }
      return port;
    }
  }
  throw new Error(`No available port found for ${serviceName} starting at ${preferredPort}`);
}


process.on("SIGINT", () => {
  logDev("stopping services...");
  shutdown(0);
});
process.on("SIGTERM", () => shutdown(0));

try {
  await assertPortsAvailable(services);
  try {
    await runOnce("sync", "uv", uvSyncArgs);
  } catch (error) {
    if (isWindows) {
      errorDev("If uv sync reports a locked runtime file, preview workers with `pnpm stop -- --dry-run`, stop them, and retry.");
    }
    throw error;
  }
  if (!skipMigrations) {
    await runOnce("migrate", "uv", [...uvRunArgs, "python", "scripts/migrate.py"]);
  }
  logDev(`session log: ${DEV_LOG_PATH}`);
  logDev(`API diagnostics: ${API_DIAGNOSTIC_LOG_PATH}`);
  logDev(`API crash log: ${API_CRASH_LOG_PATH}`);
  logDev("starting JobCockpit services");
  for (const service of services) {
    logDev(`${service.name}: ${service.url}`);
    spawnProcess(service);
  }
} catch (error) {
  errorDev(error instanceof Error ? (error.stack ?? error.message) : String(error));
  shutdown(1);
}
