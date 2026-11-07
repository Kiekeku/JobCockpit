"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { fetchJson } from "../../lib/api";

type DiscoveryState = {
  status: string;
  eligible: boolean;
  reason_code?: string | null;
  attempts?: number;
  next_attempt_at?: string | null;
  history: { at: string; outcome: string; reason_code: string }[];
};

const messages: Record<string, string> = {
  pending: "Original company source discovery is queued.",
  running: "Searching employer sites and checking alternative postings.",
  retry_scheduled: "No verified replacement yet. Source discovery will try again.",
  resolved: "The original company posting was verified.",
  unavailable: "No verified company source was found after the scheduled attempts.",
  attempted: "Candidates were found, but the exact posting could not be confirmed.",
  blocked: "Source discovery needs review before another attempt.",
  cancelled: "Discovery stopped because this job or its source changed.",
  not_attempted: "Original company source discovery has not run yet."
};

export function SourceReconciliation({ jobId }: { jobId: string }) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["source-reconciliation", jobId],
    queryFn: () => fetchJson<DiscoveryState>(`/api/jobs/${jobId}/source-reconciliation`),
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.eligible && ["not_attempted", "pending", "running", "retry_scheduled"].includes(state.status)
        ? 10000 : false;
    }
  });
  const retry = useMutation({
    mutationFn: () => fetchJson(`/api/jobs/${jobId}/source-reconciliation/retry`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["source-reconciliation", jobId] })
  });
  const state = query.data;
  useEffect(() => {
    if (state?.status === "resolved") {
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  }, [state?.status, jobId, queryClient]);
  if (query.isError) return <p className="mutedText">Source discovery status could not be loaded.</p>;
  if (!state || (state.status === "not_attempted" && !state.eligible)) return null;
  const waiting = ["pending", "running", "retry_scheduled"].includes(state.status);
  return (
    <div className="sourceReconciliation">
      <p>{state.status === "resolved" && state.reason_code === "source_description_enriched"
        ? "The full posting description was recovered and this job was updated."
        : messages[state.status] ?? "Source discovery needs review."}</p>
      {state.next_attempt_at ? <p className="mutedText">Next attempt: {new Date(state.next_attempt_at).toLocaleString()}</p> : null}
      {state.history.length ? (
        <details>
          <summary>Source discovery history ({state.history.length})</summary>
          <ul>{state.history.map((attempt, index) => (
            <li key={`${attempt.at}-${index}`}>
              {new Date(attempt.at).toLocaleString()} — {attempt.outcome.replaceAll("_", " ")}: {attempt.reason_code.replaceAll("_", " ")}
            </li>
          ))}</ul>
        </details>
      ) : null}
      {state.eligible && !waiting ? (
        <button type="button" disabled={retry.isPending} onClick={() => retry.mutate()}>
          {retry.isPending ? "Queueing..." : "Retry source discovery"}
        </button>
      ) : null}
      {retry.isError ? <p role="alert">Could not queue discovery. Refresh the job and try again.</p> : null}
    </div>
  );
}
