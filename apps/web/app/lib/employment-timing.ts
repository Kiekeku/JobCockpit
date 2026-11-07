export type EmploymentTimingInput = {
  raw_label?: unknown;
  timing_type?: unknown;
  start_date?: unknown;
};

const MONTH_ABBREVIATIONS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec"
] as const;

function trimmedString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function isoDateParts(value: unknown): { year: number; month: number; day: number } | null {
  const text = trimmedString(value);
  const match = text?.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) {
    return null;
  }

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null;
  }
  return { year, month, day };
}

function academicTermForStartDate(
  timing: EmploymentTimingInput
): string | null {
  if (timing.timing_type !== "fixed_term") {
    return null;
  }
  const parts = isoDateParts(timing.start_date);
  if (!parts) {
    return null;
  }
  if (parts.month <= 4) {
    return `Winter ${parts.year}`;
  }
  if (parts.month <= 8) {
    return `Summer ${parts.year}`;
  }
  return `Fall ${parts.year}`;
}

export function formatJobTimingEvidence(timing: EmploymentTimingInput): string | null {
  return trimmedString(timing.raw_label);
}

export function formatJobStartTerm(timing: EmploymentTimingInput): string {
  const academicTerm = academicTermForStartDate(timing);
  if (academicTerm) {
    return academicTerm;
  }

  const evidence = formatJobTimingEvidence(timing);
  if (evidence) {
    return evidence;
  }

  const startDate = trimmedString(timing.start_date);
  const parts = isoDateParts(startDate);
  if (parts) {
    return `${MONTH_ABBREVIATIONS[parts.month - 1]} ${parts.year}`;
  }
  return startDate ?? "Unknown";
}

export function isMissingActualTerm(timing: EmploymentTimingInput): boolean {
  if (timing.timing_type === "posted_date_fallback") {
    return true;
  }
  if (isoDateParts(timing.start_date)) {
    return false;
  }
  // A duration describes how long the job lasts, not when it starts.
  const label = formatJobTimingEvidence(timing);
  const durationOnly = /^\d+\s*(?:[-–—/]\s*\d+\s*)?[- ]?\s*(?:months?|weeks?)(?:\s+(?:co-?op|internship|work term))?$/i;
  if (label && durationOnly.test(label)) {
    return true;
  }
  return formatJobStartTerm(timing).toLowerCase() === "unknown";
}

export function jobTermFilterText(timing: EmploymentTimingInput): string {
  return [formatJobTimingEvidence(timing), trimmedString(timing.start_date), formatJobStartTerm(timing)]
    .map((part) => part?.toLowerCase())
    .filter(Boolean)
    .join(" ");
}
