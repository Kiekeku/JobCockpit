#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, resolve } from "node:path";
import process from "node:process";
import { loadDotEnv } from "./env.mjs";

loadDotEnv(".env");

const args = parseArgs(process.argv.slice(2));

if (args.help) {
  console.log(`Usage: pnpm extension:dev [options]

Builds the Chrome extension and launches a dedicated Chrome/Chromium dev profile with the unpacked extension loaded.

Options:
  --no-build              Launch Chrome without rebuilding the extension
  --allow-other-extensions
                          Do not pass --disable-extensions-except, so Web Store
                          extensions such as Codex can be installed in the dev profile
  --chrome PATH           Chrome/Chromium executable path
  --profile-dir PATH      Chrome user data directory for this dev profile
  --url URL               URL to open after launching Chrome
  -h, --help              Show this help

Environment:
  JOBCOCKPIT_CHROME_PATH                  Chrome/Chromium executable path
  CHROME_PATH                             Chrome/Chromium executable path fallback
  JOBCOCKPIT_EXTENSION_CHROME_PROFILE_DIR Chrome user data directory
  JOBCOCKPIT_EXTENSION_DEV_URL            URL to open after launching
  JOBCOCKPIT_EXTENSION_ALLOW_OTHER_EXTENSIONS
                                          Set to 1/true/yes/on to allow other extensions
  JOBCOCKPIT_DEV_HOST                     Dev server host for the default URL
  JOBCOCKPIT_WEB_PORT                     Dev server web port for the default URL
`);
  process.exit(0);
}

const repoRoot = process.cwd();
const extensionDir = resolve(repoRoot, "apps/chrome-extension");
const profileDir = resolve(
  repoRoot,
  args.profileDir ?? process.env.JOBCOCKPIT_EXTENSION_CHROME_PROFILE_DIR ?? "var/chrome-extension-dev-profile"
);
const startupUrl = args.url ?? process.env.JOBCOCKPIT_EXTENSION_DEV_URL ?? defaultStartupUrl();
const allowOtherExtensions = args.allowOtherExtensions || envFlag("JOBCOCKPIT_EXTENSION_ALLOW_OTHER_EXTENSIONS");

if (!existsSync(resolve(extensionDir, "manifest.json"))) {
  fail(`Could not find extension manifest at ${resolve(extensionDir, "manifest.json")}`);
}

if (!args.noBuild) {
  run("pnpm", ["--filter", "@jobcockpit/chrome-extension", "build"]);
}

mkdirSync(profileDir, { recursive: true });

const chromePath = args.chrome ?? process.env.JOBCOCKPIT_CHROME_PATH ?? process.env.CHROME_PATH ?? findChromeExecutable();
if (!chromePath) {
  fail(
    [
      "Could not find Google Chrome, Chrome for Testing, or Chromium.",
      "Install Chrome/Chromium, or pass --chrome PATH, or set JOBCOCKPIT_CHROME_PATH."
    ].join("\n")
  );
}

const chromeArgs = [
  `--user-data-dir=${profileDir}`,
  `--load-extension=${extensionDir}`,
  "--no-first-run",
  "--no-default-browser-check",
  startupUrl
];

if (!allowOtherExtensions) {
  chromeArgs.splice(1, 0, `--disable-extensions-except=${extensionDir}`);
}

console.log(`[extension-dev] Chrome/Chromium: ${chromePath}`);
console.log(`[extension-dev] Extension: ${extensionDir}`);
console.log(`[extension-dev] Profile: ${profileDir}`);
console.log(`[extension-dev] Other extensions: ${allowOtherExtensions ? "allowed" : "disabled except JobCockpit"}`);
console.log(`[extension-dev] Opening: ${startupUrl}`);

const child = spawn(chromePath, chromeArgs, {
  cwd: repoRoot,
  detached: true,
  stdio: "ignore",
  windowsHide: false
});
child.unref();
console.log("[extension-dev] Chrome launched. If the extension was already open, reload it from chrome://extensions after rebuilding.");

function parseArgs(argv) {
  const parsed = {
    help: false,
    noBuild: false,
    allowOtherExtensions: false,
    chrome: null,
    profileDir: null,
    url: null
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    switch (arg) {
      case "-h":
      case "--help":
        parsed.help = true;
        break;
      case "--no-build":
        parsed.noBuild = true;
        break;
      case "--allow-other-extensions":
        parsed.allowOtherExtensions = true;
        break;
      case "--chrome":
        parsed.chrome = requireValue(argv, ++index, arg);
        break;
      case "--profile-dir":
        parsed.profileDir = requireValue(argv, ++index, arg);
        break;
      case "--url":
        parsed.url = requireValue(argv, ++index, arg);
        break;
      default:
        fail(`Unknown option: ${arg}`);
    }
  }
  return parsed;
}

function requireValue(argv, index, option) {
  const value = argv[index];
  if (!value || value.startsWith("--")) {
    fail(`${option} requires a value`);
  }
  return value;
}

function run(command, commandArgs) {
  console.log(`[extension-dev] ${command} ${commandArgs.join(" ")}`);
  const result = spawnSync(command, commandArgs, {
    cwd: repoRoot,
    env: process.env,
    shell: process.platform === "win32",
    stdio: "inherit",
    windowsHide: false
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function findChromeExecutable() {
  const candidates = chromeExecutableCandidates();
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) {
      return candidate;
    }
  }
  return findChromeOnPath();
}

function chromeExecutableCandidates() {
  if (process.platform === "win32") {
    const programFiles = process.env.ProgramFiles;
    const programFilesX86 = process.env["ProgramFiles(x86)"];
    const localAppData = process.env.LOCALAPPDATA;
    return [
      programFiles ? `${programFiles}\\Google\\Chrome for Testing\\Application\\chrome.exe` : null,
      localAppData ? `${localAppData}\\Google\\Chrome for Testing\\Application\\chrome.exe` : null,
      programFiles ? `${programFiles}\\Chromium\\Application\\chrome.exe` : null,
      programFilesX86 ? `${programFilesX86}\\Chromium\\Application\\chrome.exe` : null,
      ...playwrightChromiumExecutableCandidates(localAppData),
      programFiles ? `${programFiles}\\Google\\Chrome\\Application\\chrome.exe` : null,
      programFilesX86 ? `${programFilesX86}\\Google\\Chrome\\Application\\chrome.exe` : null,
      localAppData ? `${localAppData}\\Google\\Chrome\\Application\\chrome.exe` : null
    ];
  }
  if (process.platform === "darwin") {
    return [
      "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
      `${homedir()}/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`,
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      `${homedir()}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
    ];
  }
  return [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-for-testing",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable"
  ];
}

function playwrightChromiumExecutableCandidates(localAppData) {
  if (!localAppData) {
    return [];
  }
  const browsersRoot = resolve(localAppData, "ms-playwright");
  if (!existsSync(browsersRoot)) {
    return [];
  }
  return readdirSync(browsersRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && /^chromium-\d+$/.test(entry.name))
    .map((entry) => ({
      revision: Number.parseInt(entry.name.slice("chromium-".length), 10),
      executable: resolve(browsersRoot, entry.name, "chrome-win64", "chrome.exe")
    }))
    .filter((candidate) => Number.isInteger(candidate.revision) && existsSync(candidate.executable))
    .sort((left, right) => right.revision - left.revision)
    .map((candidate) => candidate.executable);
}

function findChromeOnPath() {
  const names = process.platform === "win32" ? ["chrome.exe", "chromium.exe"] : ["chromium", "chromium-browser", "google-chrome-for-testing", "google-chrome", "google-chrome-stable"];
  const pathEntries = (process.env.PATH ?? "").split(delimiter).filter(Boolean);
  for (const pathEntry of pathEntries) {
    for (const name of names) {
      const candidate = resolve(pathEntry, name);
      if (existsSync(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

function defaultStartupUrl() {
  const host = process.env.JOBCOCKPIT_DEV_HOST && process.env.JOBCOCKPIT_DEV_HOST !== "0.0.0.0"
    ? process.env.JOBCOCKPIT_DEV_HOST
    : "localhost";
  return `http://${host}:${envPort("JOBCOCKPIT_WEB_PORT", 3000)}`;
}

function envPort(name, fallback) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const port = Number.parseInt(raw, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535 || String(port) !== raw.trim()) {
    fail(`${name} must be an integer port between 1 and 65535`);
  }
  return port;
}

function envFlag(name) {
  const raw = process.env[name];
  if (!raw) {
    return false;
  }
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

function fail(message) {
  console.error(`[extension-dev] ${message}`);
  process.exit(1);
}
