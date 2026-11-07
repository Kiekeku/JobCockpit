import { basename, dirname, isAbsolute, resolve } from "node:path";

export function isRepositoryCliWorker(commandLine, launcherPath) {
  const tokens = (commandLine ?? "").match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
  const args = tokens.map((token) => token.replace(/^(?:"(.*)"|'(.*)')$/, "$1$2"));
  const normalize = (path) => process.platform === "win32" ? resolve(path).toLowerCase() : resolve(path);
  // Scheduled launchers pass an absolute script path. Do not infer another
  // process's working directory from a relative command line.
  const nodeWorker = args.length >= 3 && normalize(args[1]) === normalize(launcherPath)
    && args[2] === "worker" && isAbsolute(args[1]);
  const marker = args[2]?.startsWith("jobcockpit_root=") ? args[2].slice("jobcockpit_root=".length) : "";
  const pythonWorker = /^python(?:3(?:\.\d+)?)?(?:\.exe)?$/i.test(basename(args[0] ?? ""))
    && args[1] === "-X" && marker && isAbsolute(marker)
    && normalize(marker) === normalize(dirname(dirname(launcherPath)))
    && args[3] === "-m" && args[4] === "jobcockpit.cli" && args[5] === "worker";
  return Boolean(nodeWorker || pythonWorker);
}
