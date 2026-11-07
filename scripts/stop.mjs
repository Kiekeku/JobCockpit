#!/usr/bin/env node

import { execFile } from "node:child_process";
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";
import { loadDotEnv } from "./env.mjs";
import { isRepositoryCliWorker } from "./cli-processes.mjs";

loadDotEnv(".env");

const args = new Set(process.argv.slice(2));

if (args.has("--help") || args.has("-h")) {
  console.log(`Usage: pnpm stop [options]

Stops local JobCockpit development services and repository CLI workers.

Options:
  --dry-run          Show matching listeners and CLI workers without stopping them

Environment:
  Reads JOBCOCKPIT_API_PORT, JOBCOCKPIT_WEB_PORT, and
  JOBCOCKPIT_ATS_MOCK_PORT from .env with the same defaults as pnpm dev.
`);
  process.exit(0);
}

const dryRun = args.has("--dry-run");
const isWindows = process.platform === "win32";
const host = process.env.JOBCOCKPIT_DEV_HOST ?? "127.0.0.1";
const PID_FILE = "var/dev-pids.json";
const configuredServices = [
  { name: "api", port: envPort("JOBCOCKPIT_API_PORT", 8000) },
  { name: "web", port: envPort("JOBCOCKPIT_WEB_PORT", 3000) },
  { name: "ats", port: envPort("JOBCOCKPIT_ATS_MOCK_PORT", 8001) }
];

const pidFile = readPidFile();
const pidFilePids = pidFile.services.map((service) => service.pid);
const repoCliPath = repositoryCliPath();
const repoCliProcesses = await findRepoCliProcesses(repoCliPath);
const repoCliPids = repoCliProcesses.map((candidate) => candidate.pid);
const configuredListeners = await findListeners(configuredServices.map((service) => service.port));
const verifiedConfiguredListeners = await verifyConfiguredListeners(configuredListeners, configuredServices);
const verifiedConfiguredListenerPidSet = new Set(
  verifiedConfiguredListeners.map((listener) => listener.pid)
);
const verifiedPidFileServices = await verifyPidFileServices(pidFile, verifiedConfiguredListenerPidSet);
const verifiedPidFilePids = verifiedPidFileServices.map((service) => service.pid);
const targetPids = new Set([
  ...verifiedPidFilePids,
  ...verifiedConfiguredListeners.map((listener) => listener.pid),
  ...repoCliPids
]);
const pids = [...targetPids].sort((left, right) => left - right);
const verifiedPidFilePortServices = verifiedPidFileServices.filter((service) => service.port !== null);
const verifiedPidFileListeners = (
  await findListeners(verifiedPidFilePortServices.map((service) => service.port))
).filter((listener) => targetPids.has(listener.pid));
const verifiedListenerServices = verifiedPidFilePortServices.filter((service) =>
  verifiedPidFileListeners.some((listener) => listener.port === service.port && listener.pid === service.pid)
);
const serviceCatalog = [...configuredServices, ...verifiedListenerServices];
const listeners = mergeListeners(
  verifiedConfiguredListeners,
  verifiedPidFileListeners
);
const services = servicesForListeners(listeners, serviceCatalog);
const listenerPidSet = new Set(listeners.map((listener) => listener.pid));

if (pids.length === 0) {
  console.log("[stop] no JobCockpit dev listeners or CLI workers found");
  removePidFile();
  process.exit(0);
}

for (const pid of pidFilePids) {
  const verified = verifiedPidFilePids.includes(pid) || listenerPidSet.has(pid);
  console.log(`[stop] pid file ${PID_FILE} pid ${pid}${verified ? "" : " (not verified; skipping)"}`);
}
for (const listener of listeners) {
  const service = services.find((candidate) => candidate.port === listener.port);
  console.log(`[stop] ${service?.name ?? "unknown"} ${host}:${listener.port} pid ${listener.pid}`);
}
for (const candidate of repoCliProcesses) {
  console.log(`[stop] cli worker ${candidate.path} pid ${candidate.pid}`);
}

if (dryRun) {
  console.log("[stop] dry run only; no processes stopped");
  process.exit(0);
}

const stopFailures = [];
for (const pid of pids) {
  try {
    await stopPid(pid);
  } catch (error) {
    if (isProcessNotFound(error)) {
      console.log(`[stop] pid ${pid} already exited`);
      continue;
    }
    stopFailures.push({ pid, message: error.message });
    console.error(`[stop] failed to stop pid ${pid}: ${error.message}`);
  }
}
await sleep(500);
const remaining = await findListeners(services.map((service) => service.port));
const remainingRepoCliProcesses = await findRepoCliProcesses(repoCliPath);
if (remaining.length > 0 || remainingRepoCliProcesses.length > 0) {
  for (const listener of remaining) {
    const service = services.find((candidate) => candidate.port === listener.port);
    console.error(
      `[stop] still listening: ${service?.name ?? "unknown"} ${host}:${listener.port} pid ${listener.pid}`
    );
  }
  for (const candidate of remainingRepoCliProcesses) {
    console.error(`[stop] still running: cli worker ${candidate.path} pid ${candidate.pid}`);
  }
  if (isWindows && stopFailures.some((failure) => failure.message.includes("not found"))) {
    console.error(
      "[stop] Windows reports a listener whose owning process no longer exists or is not visible."
    );
    console.error(
      "[stop] Run `pnpm dev -- --next-ports` to start on free nearby ports, or reboot Windows to clear the stale listener."
    );
  }
  console.error("[stop] one or more JobCockpit dev services are still active");
  process.exit(1);
}
removePidFile();
console.log(`[stop] stopped ${pids.length} process${pids.length === 1 ? "" : "es"}`);

async function findListeners(ports) {
  if (ports.length === 0) {
    return [];
  }
  return isWindows ? findWindowsListeners(ports) : findPosixListeners(ports);
}

function repositoryCliPath() {
  return isWindows
    ? resolve(process.cwd(), ".venv", "Scripts", "jobcockpit.exe")
    : resolve(process.cwd(), ".venv", "bin", "jobcockpit");
}

async function findRepoCliProcesses(cliPath) {
  const launcherWorkers = await findRepoLauncherWorkers();
  if (!existsSync(cliPath)) {
    return launcherWorkers;
  }
  if (isWindows) {
    const output = await execText(
      "powershell.exe",
      [
        "-NoProfile",
        "-Command",
        [
          "$target = [IO.Path]::GetFullPath($env:JOBCOCKPIT_STOP_CLI_PATH)",
          "Get-Process -Name jobcockpit -ErrorAction SilentlyContinue | ForEach-Object {",
          "  try {",
          "    if ([IO.Path]::GetFullPath($_.Path) -ieq $target) { [string]$_.Id }",
          "  } catch {}",
          "}",
          "exit 0"
        ].join("\n")
      ],
      {
        env: { ...process.env, JOBCOCKPIT_STOP_CLI_PATH: cliPath }
      }
    );
    return [...launcherWorkers, ...processIdsFromLines(output).map((pid) => ({ pid, path: cliPath }))];
  }
  const output = await execText("ps", ["-eo", "pid=,args="]);
  const candidates = [...launcherWorkers];
  for (const line of output.split(/\r?\n/)) {
    const match = line.trim().match(/^(\d+)\s+(.+)$/);
    if (!match || !commandLineHasExactToken(match[2], cliPath)) {
      continue;
    }
    const pid = Number.parseInt(match[1], 10);
    if (Number.isInteger(pid) && pid > 0) {
      candidates.push({ pid, path: cliPath });
    }
  }
  return candidates;
}

async function findRepoLauncherWorkers() {
  const launcherPath = resolve(process.cwd(), "scripts", "cli.mjs");
  if (isWindows) {
    const output = await execText("powershell.exe", ["-NoProfile", "-Command",
      "@(Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='python.exe'\" | Select-Object ProcessId,ParentProcessId,CommandLine) | ConvertTo-Json -Compress"
    ]);
    const rows = output.trim() ? JSON.parse(output) : [];
    const workers = (Array.isArray(rows) ? rows : [rows])
      .filter((row) => isRepositoryCliWorker(row.CommandLine, launcherPath));
    const workerPids = new Set(workers.map((row) => row.ProcessId));
    return workers.filter((row) => !workerPids.has(row.ParentProcessId))
      .map((row) => ({ pid: row.ProcessId, path: launcherPath }));
  }
  const output = await execText("ps", ["-eo", "pid=,args="]);
  return output.split(/\r?\n/).flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(.+)$/);
    return match && isRepositoryCliWorker(match[2], launcherPath)
      ? [{ pid: Number(match[1]), path: launcherPath }] : [];
  });
}

function processIdsFromLines(output) {
  return output
    .split(/\r?\n/)
    .map((line) => Number.parseInt(line.trim(), 10))
    .filter((pid) => Number.isInteger(pid) && pid > 0);
}

function commandLineHasExactToken(commandLine, expectedToken) {
  const tokens = commandLine.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
  return tokens.some((token) => {
    const unquoted = token.replace(/^(?:"(.*)"|'(.*)')$/, "$1$2");
    return resolve(unquoted) === expectedToken;
  });
}

async function findWindowsListeners(ports) {
  const output = await execText("netstat", ["-ano"]);
  const wanted = new Set(ports.map(String));
  const listeners = [];
  for (const line of output.split(/\r?\n/)) {
    const parts = line.trim().split(/\s+/);
    if (parts.length < 5 || parts[0] !== "TCP" || parts[3] !== "LISTENING") {
      continue;
    }
    const port = portFromAddress(parts[1]);
    const pid = Number.parseInt(parts[4], 10);
    if (port !== null && wanted.has(String(port)) && Number.isInteger(pid) && pid > 0) {
      listeners.push({ port, pid });
    }
  }
  return listeners;
}

async function findPosixListeners(ports) {
  const listeners = [];
  for (const port of ports) {
    try {
      const output = await execText("lsof", [`-tiTCP:${port}`, "-sTCP:LISTEN"]);
      for (const line of output.split(/\r?\n/)) {
        const pid = Number.parseInt(line.trim(), 10);
        if (Number.isInteger(pid) && pid > 0) {
          listeners.push({ port, pid });
        }
      }
    } catch (error) {
      if (!String(error.message).includes("exit 1")) {
        throw error;
      }
    }
  }
  return listeners;
}

async function stopPid(pid) {
  if (isWindows) {
    await execText("taskkill", ["/PID", String(pid), "/T", "/F"]);
    return;
  }
  await execText("kill", ["-TERM", String(pid)]);
}

function isProcessNotFound(error) {
  return error.message.includes("not found") || error.message.includes("not found.");
}

async function verifyConfiguredListeners(listeners, services) {
  const verified = [];
  for (const listener of listeners) {
    const service = services.find((candidate) => candidate.port === listener.port);
    if (!service) {
      continue;
    }
    if (await processLooksLikeJobCockpitService(listener.pid, service)) {
      verified.push(listener);
    }
  }
  return verified;
}

async function verifyPidFileServices(pidFile, currentListenerPids) {
  const verified = [];
  for (const service of pidFile.services) {
    if (currentListenerPids.has(service.pid)) {
      verified.push(service);
      continue;
    }
    if (pidFile.parentPid) {
      try {
        const parentPid = await parentProcessId(service.pid);
        if (parentPid === pidFile.parentPid) {
          verified.push(service);
          continue;
        }
      } catch {
        // Fall through to command-line verification.
      }
    }
    if (await processLooksLikeJobCockpitService(service.pid, service)) {
      verified.push(service);
    }
  }
  return verified;
}

function mergeListeners(...listenerGroups) {
  const merged = new Map();
  for (const listener of listenerGroups.flat()) {
    merged.set(`${listener.port}:${listener.pid}`, listener);
  }
  return [...merged.values()];
}

function servicesForListeners(listeners, services) {
  const listenerPorts = new Set(listeners.map((listener) => listener.port));
  const selected = new Map();
  for (const service of services) {
    if (service.port !== null && listenerPorts.has(service.port)) {
      selected.set(`${service.name}:${service.port}`, service);
    }
  }
  return [...selected.values()];
}

async function processLooksLikeJobCockpitService(pid, service) {
  try {
    const commandLine = (await processCommandLine(pid)).toLowerCase();
    if (!commandLine) {
      return false;
    }
    if (commandLine.includes("jobcockpit.api.main:app")) {
      return service.name === "api";
    }
    if (commandLine.includes("mock_site:app") || commandLine.includes("apps/ats-mock-site")) {
      return service.name === "ats";
    }
    if (
      commandLine.includes("@jobcockpit/web") ||
      commandLine.includes("apps/web") ||
      (service.name === "web" && commandLine.includes("next") && commandLine.includes("jobcockpit"))
    ) {
      return service.name === "web";
    }
    if (commandLine.includes("jobcockpit worker reprocess")) {
      return service.name === "worker";
    }
    if (commandLine.includes("@jobcockpit/chrome-extension")) {
      return service.name === "extension";
    }
    return false;
  } catch {
    return false;
  }
}

async function processCommandLine(pid) {
  if (isWindows) {
    return execText("powershell.exe", [
      "-NoProfile",
      "-Command",
      `$process = Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}"; if ($process) { [string]$process.CommandLine }`
    ]);
  }
  return execText("ps", ["-o", "args=", "-p", String(pid)]);
}

async function parentProcessId(pid) {
  if (isWindows) {
    const output = await execText("powershell.exe", [
      "-NoProfile",
      "-Command",
      `$process = Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}"; if ($process) { [string]$process.ParentProcessId }`
    ]);
    const parentPid = Number.parseInt(output.trim(), 10);
    if (Number.isInteger(parentPid) && parentPid > 0) {
      return parentPid;
    }
    throw new Error("process_not_found");
  }
  const output = await execText("ps", ["-o", "ppid=", "-p", String(pid)]);
  const parentPid = Number.parseInt(output.trim(), 10);
  if (Number.isInteger(parentPid) && parentPid > 0) {
    return parentPid;
  }
  throw new Error("process_not_found");
}

function portFromAddress(address) {
  const match = address.match(/:(\d+)$/);
  if (!match) {
    return null;
  }
  return Number.parseInt(match[1], 10);
}

function execText(command, commandArgs, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(command, commandArgs, { windowsHide: true, ...options }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`${command} failed with exit ${error.code}: ${stderr || stdout}`));
        return;
      }
      resolve(stdout);
    });
  });
}

function readPidFile() {
  if (!existsSync(PID_FILE)) {
    return { parentPid: null, services: [] };
  }
  try {
    const payload = JSON.parse(readFileSync(PID_FILE, "utf8"));
    if (!Array.isArray(payload.services)) {
      return { parentPid: null, services: [] };
    }
    const parentPid = Number.parseInt(String(payload.parentPid), 10);
    return {
      parentPid: Number.isInteger(parentPid) && parentPid > 0 ? parentPid : null,
      services: payload.services
        .map((service) => ({
          name: typeof service.name === "string" ? service.name : "unknown",
          pid: Number.parseInt(String(service.pid), 10),
          port: service.port === undefined ? null : Number.parseInt(String(service.port), 10)
        }))
        .filter(
          (service) =>
            Number.isInteger(service.pid) &&
            service.pid > 0 &&
            (service.port === null || (Number.isInteger(service.port) && service.port > 0))
        )
    };
  } catch (error) {
    console.error(`[stop] ignoring unreadable ${PID_FILE}: ${error.message}`);
    return { parentPid: null, services: [] };
  }
}

function removePidFile() {
  try {
    unlinkSync(PID_FILE);
  } catch {
    // The pid file may have already been removed.
  }
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
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
