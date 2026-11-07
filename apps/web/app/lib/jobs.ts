export const JOB_OPPORTUNITY_STATUSES = [
  "active",
  "expired",
  "closed",
  "filled",
  "withdrawn"
] as const;

export type JobOpportunityStatus = (typeof JOB_OPPORTUNITY_STATUSES)[number];

const JOB_OPPORTUNITY_STATUS_LABELS: Record<JobOpportunityStatus, string> = {
  active: "Active",
  expired: "Expired",
  closed: "Closed",
  filled: "Filled",
  withdrawn: "Withdrawn"
};

export function formatJobOpportunityStatus(status: JobOpportunityStatus): string {
  return JOB_OPPORTUNITY_STATUS_LABELS[status];
}
