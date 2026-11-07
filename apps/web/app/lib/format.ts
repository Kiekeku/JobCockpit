export function formatDurationMonths(value: unknown, explicitLabel?: unknown): string {
  if (typeof explicitLabel === "string" && explicitLabel.trim()) {
    return explicitLabel.trim();
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return "Unknown";
  }
  return `${value} mo`;
}
