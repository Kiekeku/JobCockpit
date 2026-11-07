const SUCCESSFUL_BROWSER_HEALTH_ACCESS =
  /^INFO:\s+.+ - "GET \/api\/browser-health HTTP\/\d(?:\.\d)?" 200 OK$/;

export function shouldDisplayServiceLine(name, line) {
  return name !== "api" || !SUCCESSFUL_BROWSER_HEALTH_ACCESS.test(line);
}
