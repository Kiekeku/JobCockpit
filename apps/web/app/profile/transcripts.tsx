"use client";

import { FormEvent, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "../lib/api";

type TranscriptState = {
  transcript_artifacts: Array<{
    artifact_id: string;
    original_file_name: string;
    size_bytes: number;
    created_at: string;
    is_latest: boolean;
    download_url: string;
  }>;
  latest_transcript_artifact_id: string | null;
};

function useTranscripts() {
  return useQuery({
    queryKey: ["transcripts"],
    queryFn: () => fetchJson<TranscriptState>("/api/profile/transcript")
  });
}

export function RequiredTranscript() {
  const query = useTranscripts();
  return (
    <p>
      {query.isPending ? "Checking transcript..." : query.isError ? (
        <a href="/profile#transcripts">Check transcript in Profile</a>
      ) : query.data.latest_transcript_artifact_id ? (
        <a href="/api/profile/transcript/latest/download">Download latest transcript</a>
      ) : (
        <a href="/profile#transcripts">Transcript missing — upload in Profile</a>
      )}
    </p>
  );
}

export function TranscriptUploads({ hasProfile }: { hasProfile: boolean }) {
  const query = useTranscripts();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [uploadError, setUploadError] = useState(false);

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setUploadError(false);
    setStatus("Uploading transcript...");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/profile/transcript", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Upload failed");
      queryClient.setQueryData(["transcripts"], payload);
      await queryClient.invalidateQueries({ queryKey: ["profile"] });
      setFile(null);
      if (input.current) input.current.value = "";
      setStatus("Transcript saved as the latest version.");
    } catch (error) {
      setUploadError(true);
      setStatus(error instanceof Error ? error.message : "Upload failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="transcripts">
      <h3>Transcripts</h3>
      <p>Upload a PDF, up to 5 MB. The most recently uploaded version is used by default. Previous versions stay available below.</p>
      {!hasProfile ? <p>Create your profile to upload a transcript.</p> : null}
      <form className="uploadForm" onSubmit={upload}>
        <input aria-label="Transcript PDF" ref={input} type="file" accept=".pdf,application/pdf"
          disabled={!hasProfile || busy} onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        <button type="submit" disabled={!hasProfile || !file || busy}>Upload transcript</button>
      </form>
      {status ? <p role={uploadError ? "alert" : "status"}>
        {uploadError ? <strong>Transcript upload failed: </strong> : null}{status}
      </p> : null}
      {query.isPending ? <p>Loading transcripts...</p> : query.isError ? <p role="alert">Could not load transcripts.</p> : (
        <>
          {query.data.latest_transcript_artifact_id ? (
            <p><a href="/api/profile/transcript/latest/download">Download latest transcript</a></p>
          ) : !uploadError && !busy ? <p>No transcript uploaded yet.</p> : null}
          <ul className="artifactList">
            {query.data.transcript_artifacts.map((artifact) => (
              <li key={artifact.artifact_id}>
                <strong>{artifact.original_file_name}{artifact.is_latest ? " (Latest)" : " (Previous version)"}</strong>
                <span>Uploaded {new Date(artifact.created_at).toLocaleString()} · {Math.ceil(artifact.size_bytes / 1024)} KB</span>
                <a href={artifact.download_url}>Download this version</a>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
