type JcQuestionKind = "text" | "textarea" | "email" | "tel" | "url" | "number" | "date" | "month" | "select" | "radio" | "checkbox" | "file" | "unsupported";
type JcOption = { value: string; label: string; disabled: boolean };
type JcQuestion = {
  id: string; document_token: string; snapshot_token: string; fingerprint: string;
  text: string; section: string; help_text: string; kind: JcQuestionKind;
  required: boolean; options: JcOption[]; current_value: string; max_length: number | null;
  minimum: string; maximum: string; step: string; autocomplete: string;
  purpose: "resume" | "cover_letter" | "transcript" | "unknown";
  fillable: boolean; unsupported_reason: string; frame_id: number; frame_url: string;
};
type JcPlan = { question: JcQuestion; answer: string; replace_existing: boolean; expires_at: number };
type JcResult = {
  question_id: string; status: "filled" | "unchanged" | "skipped" | "failed" | "stale" | "undone" | "attached";
  actual_value: string; message: string;
};
type JcSnapshot = { page_url: string; page_title: string; document_token: string; snapshot_token: string; form_signature: string; questions: JcQuestion[]; warnings: string[]; fill_supported: boolean };
type JcAnswer = {
  question_id: string; answer: string; status: string; source_ids: string[]; rationale: string;
  missing_information: string[]; approved: boolean; skipped: boolean; replace_existing: boolean;
  requires_confirmation: boolean; approval_expires_at?: number;
};
type JcSession = {
  session_id: string; job_id: string; revision: number; page_url: string; page_title: string;
  questions: JcQuestion[]; answers: JcAnswer[]; evidence_ids: string[];
  draft_status: string; draft_model: string | null; draft_error: string;
  receipts: { at: string; results: JcResult[] }[]; expires_at: string;
};
type JcJob = { job_id: string; company: string; title: string; source_url: string | null; employment_timing: Record<string, unknown>; status?: string };
type JcDocument = { artifact_id: string; file_name: string; sha256: string; size_bytes: number; document_type: string; approval: string };
type JcFactFields = { subject_key: string; value: string; statement: string; scope: "general" | "country" | "term" | "job"; country: string; term: "" | "Winter" | "Summer" | "Fall"; year: number | null; job_id: string | null; expires_at: string | null };
type JcObservation = { question_id: string; answer: string; source: "page" | "panel"; question?: JcQuestion; answer_label?: string };
type JcFact = JcFactFields & { fact_id: string; revision: number; expired: boolean; source: JcObservation & { job_id: string }; updated_at: string };
type JcFactProposal = Omit<JcFactFields, "job_id" | "expires_at"> & { observation_id: string; rationale: string; missing_information: string[] };
type JcLearning = { run_id: string; job_id: string; revision: number; status: string; observations: JcObservation[]; proposals: JcFactProposal[]; confirmed_observations: string[]; error: string; existing_facts: JcFact[] };
type JcContext = { job: JcJob; profile: Record<string, unknown>; personal_facts: JcFact[]; evidence: { id: string; claim: string; skills: string[] }[]; documents: JcDocument[]; ai: { provider: string; model: string } };
