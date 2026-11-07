#!/usr/bin/env node
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync, watch, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(repo, "apps/chrome-extension");
const require = createRequire(join(repo, "package.json"));
const runtimeFiles = ["manifest.json", "sidepanel.html", "sidepanel.css", "dist/content.js", "dist/sidepanel.js", "dist/background.js", "dist/build-info.json"];

function build() {
  const compiled = spawnSync(process.execPath, [require.resolve("typescript/bin/tsc"), "-p", join(source, "tsconfig.json")], { cwd: repo, stdio: "inherit", windowsHide: true });
  if (compiled.status !== 0) return false;
  const manifest = JSON.parse(readFileSync(join(source, "manifest.json"), "utf8"));
  const pkg = JSON.parse(readFileSync(join(source, "package.json"), "utf8"));
  if (manifest.version !== pkg.version) throw new Error("Extension manifest and package versions differ.");
  // Remove only obsolete known compiler outputs, never source or unrelated files.
  for (const stale of ["popup.js", "popup.js.map"]) {
    const target = resolve(source, "dist", stale);
    if (dirname(target) !== resolve(source, "dist")) throw new Error("Invalid compiler output path.");
    if (existsSync(target)) unlinkSync(target);
  }
  const hash = createHash("sha256");
  for (const name of runtimeFiles.filter(name => name !== "dist/build-info.json")) hash.update(name).update(readFileSync(join(source, name)));
  const buildId = hash.digest("hex").slice(0, 12);
  const info = { version: manifest.version, build_id: buildId, runtime_files: runtimeFiles };
  writeFileSync(join(source, "dist/build-info.json"), JSON.stringify(info, null, 2) + "\n");
  const stage = join(repo, "var/extension/builds", buildId);
  mkdirSync(join(stage, "dist"), { recursive: true });
  for (const name of runtimeFiles) copyFileSync(join(source, name), join(stage, name));
  const present = readdirSync(stage, { recursive: true, withFileTypes: true }).filter(entry => entry.isFile());
  if (present.length !== runtimeFiles.length) throw new Error("The staged extension contains unexpected files. Use a fresh build directory.");
  writeFileSync(join(repo, "var/extension/latest-build.json"), JSON.stringify({ ...info, directory: stage }, null, 2) + "\n");
  console.log(`[extension] v${manifest.version} · ${buildId} · ${runtimeFiles.length} runtime files`);
  return true;
}

if (!build() && !process.argv.includes("--watch")) process.exit(1);
if (process.argv.includes("--watch")) {
  let timer;
  const rebuild = () => { clearTimeout(timer); timer = setTimeout(() => { try { build(); } catch (error) { console.error(error.message); } }, 180); };
  watch(join(source, "src"), { recursive: true }, rebuild);
  for (const name of ["manifest.json", "sidepanel.html", "sidepanel.css", "tsconfig.json", "package.json"]) watch(join(source, name), rebuild);
  console.log("[extension] Watching source. Reload the extension after a rebuild.");
}
