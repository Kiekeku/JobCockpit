import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadDotEnv } from "./env.mjs";

export const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export function preparedPython() {
  process.chdir(repositoryRoot);
  loadDotEnv(".env");
  const environment = resolve(repositoryRoot, process.env.UV_PROJECT_ENVIRONMENT ?? ".venv");
  const python = resolve(environment, process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (!existsSync(python)) {
    throw new Error(`Prepared Python environment not found: ${python}. Run pnpm dev to prepare it first.`);
  }
  return python;
}
