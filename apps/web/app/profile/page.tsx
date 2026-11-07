"use client";

import { FormEvent, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "../lib/api";
import { TranscriptUploads } from "./transcripts";

type LanguageProficiency = "none" | "basic" | "intermediate" | "professional" | "fluent" | "native";

type ApiProfileLanguage = {
  language: string;
  proficiency: LanguageProficiency;
};

type ApiProfile = {
  candidate_id: string;
  first_name: string;
  last_name: string;
  full_name: string;
  email: string;
  phone: string;
  street_address: string;
  address_line_2: string;
  city: string;
  province_state: string;
  postal_code: string;
  country: string;
  current_year: string;
  graduation_date: string;
  graduation_month: string;
  graduation_year: string;
  gpa: string;
  co_op_program: boolean | null;
  willing_to_relocate: boolean | null;
  work_authorized_countries: string[];
  requires_sponsorship: boolean | null;
  linkedin_url: string;
  github_url: string;
  links: string[];
  summary: string;
  cover_letter_custom_instructions: string;
  target_role_include_keywords: string[];
  target_role_exclude_keywords: string[];
  language_filter_enabled: boolean;
  languages: ApiProfileLanguage[];
  skills: string[];
};

type ApiArtifact = {
  artifact_id: string;
  original_file_name: string;
  content_type: string;
  size_bytes: number;
};

type ApiEvidence = {
  evidence_id: string;
  claim: string;
  skills: string[];
  private: boolean;
  allowed_for_resume: boolean;
  allowed_for_cover_letter: boolean;
};

type ApiProfileState = {
  profile: ApiProfile | null;
  resume_artifacts: ApiArtifact[];
  cover_letter_sample_artifacts: ApiArtifact[];
  evidence_items: ApiEvidence[];
};

type ProfileForm = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  street_address: string;
  address_line_2: string;
  city: string;
  province_state: string;
  postal_code: string;
  country: string;
  current_year: string;
  graduation_date: string;
  graduation_month: string;
  graduation_year: string;
  gpa: string;
  co_op_program: boolean | null;
  willing_to_relocate: boolean | null;
  work_authorized_countries: string;
  requires_sponsorship: boolean | null;
  linkedin_url: string;
  github_url: string;
  links: string;
  summary: string;
  cover_letter_custom_instructions: string;
  target_role_include_keywords: string;
  target_role_exclude_keywords: string;
  language_filter_enabled: boolean;
  english_proficiency: LanguageProficiency;
  french_proficiency: LanguageProficiency;
  skills: string;
};

type EvidenceEditForm = {
  claim: string;
  skills: string;
  private: boolean;
  allowed_for_resume: boolean;
  allowed_for_cover_letter: boolean;
};

const EMPTY_PROFILE_FORM: ProfileForm = {
  first_name: "",
  last_name: "",
  email: "",
  phone: "",
  street_address: "",
  address_line_2: "",
  city: "",
  province_state: "",
  postal_code: "",
  country: "",
  current_year: "",
  graduation_date: "",
  graduation_month: "",
  graduation_year: "",
  gpa: "",
  co_op_program: null,
  willing_to_relocate: null,
  work_authorized_countries: "",
  requires_sponsorship: null,
  linkedin_url: "",
  github_url: "",
  links: "",
  summary: "",
  cover_letter_custom_instructions: "",
  target_role_include_keywords: "",
  target_role_exclude_keywords: "",
  language_filter_enabled: false,
  english_proficiency: "fluent",
  french_proficiency: "none",
  skills: ""
};


function linesToList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

const LANGUAGE_PROFICIENCIES: Array<{ value: LanguageProficiency; label: string }> = [
  { value: "none", label: "None" },
  { value: "basic", label: "Basic" },
  { value: "intermediate", label: "Intermediate" },
  { value: "professional", label: "Professional" },
  { value: "fluent", label: "Fluent" },
  { value: "native", label: "Native" }
];

const GRADUATION_MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December"
];

function graduationDateFromParts(month: string, year: string): string {
  return [month, year].map((part) => part.trim()).filter(Boolean).join(" ");
}

function hasRecognizedGraduationMonth(value: string): boolean {
  const normalizedValue = value.toLocaleLowerCase();
  return GRADUATION_MONTHS.some((month) => normalizedValue.includes(month.toLocaleLowerCase()));
}

function graduationDateForPayload(form: ProfileForm, graduationFieldsDirty: boolean): string {
  const legacyGraduationDate = form.graduation_date.trim();
  if (
    !graduationFieldsDirty &&
    !form.graduation_month.trim() &&
    legacyGraduationDate &&
    !hasRecognizedGraduationMonth(legacyGraduationDate)
  ) {
    return legacyGraduationDate;
  }
  return graduationDateFromParts(form.graduation_month, form.graduation_year);
}

function languageKey(value: string): string {
  return value.trim().toLocaleLowerCase();
}

function profileLanguageProficiency(
  profile: ApiProfile | null | undefined,
  language: string,
  fallback: LanguageProficiency
): LanguageProficiency {
  return profile?.languages?.find((item) => languageKey(item.language) === languageKey(language))?.proficiency ?? fallback;
}

function profileToForm(profile: ApiProfile | null | undefined): ProfileForm {
  if (!profile) {
    return EMPTY_PROFILE_FORM;
  }
  return {
    first_name: profile.first_name ?? "",
    last_name: profile.last_name ?? "",
    email: profile.email ?? "",
    phone: profile.phone ?? "",
    street_address: profile.street_address ?? "",
    address_line_2: profile.address_line_2 ?? "",
    city: profile.city ?? "",
    province_state: profile.province_state ?? "",
    postal_code: profile.postal_code ?? "",
    country: profile.country ?? "",
    current_year: profile.current_year ?? "",
    graduation_date: profile.graduation_date ?? "",
    graduation_month: profile.graduation_month ?? "",
    graduation_year: profile.graduation_year ?? "",
    gpa: profile.gpa ?? "",
    co_op_program: profile.co_op_program ?? null,
    willing_to_relocate: profile.willing_to_relocate ?? null,
    work_authorized_countries: (profile.work_authorized_countries ?? []).join("\n"),
    requires_sponsorship: profile.requires_sponsorship ?? null,
    linkedin_url: profile.linkedin_url ?? "",
    github_url: profile.github_url ?? "",
    links: (profile.links ?? []).join("\n"),
    summary: profile.summary ?? "",
    cover_letter_custom_instructions: profile.cover_letter_custom_instructions ?? "",
    target_role_include_keywords: (profile.target_role_include_keywords ?? []).join("\n"),
    target_role_exclude_keywords: (profile.target_role_exclude_keywords ?? []).join("\n"),
    language_filter_enabled: profile.language_filter_enabled ?? false,
    english_proficiency: profileLanguageProficiency(profile, "English", "fluent"),
    french_proficiency: profileLanguageProficiency(profile, "French", "none"),
    skills: (profile.skills ?? []).join(", ")
  };
}

function evidenceToForm(item: ApiEvidence): EvidenceEditForm {
  return {
    claim: item.claim,
    skills: item.skills.join(", "),
    private: item.private,
    allowed_for_resume: item.allowed_for_resume,
    allowed_for_cover_letter: item.allowed_for_cover_letter
  };
}

export default function ProfilePage() {
  const queryClient = useQueryClient();
  const profileQuery = useQuery({
    queryKey: ["profile"],
    queryFn: () => fetchJson<ApiProfileState>("/api/profile"),
    refetchOnWindowFocus: false
  });
  const [profileForm, setProfileForm] = useState<ProfileForm>(EMPTY_PROFILE_FORM);
  const [profileFormDirty, setProfileFormDirty] = useState(false);
  const [graduationFieldsDirty, setGraduationFieldsDirty] = useState(false);
  const [profileStatus, setProfileStatus] = useState("");
  const [uploadStatus, setUploadStatus] = useState("");
  const [coverLetterSampleStatus, setCoverLetterSampleStatus] = useState("");
  const [evidenceStatus, setEvidenceStatus] = useState("");
  const [selectedResume, setSelectedResume] = useState<File | null>(null);
  const [selectedCoverLetterSample, setSelectedCoverLetterSample] = useState<File | null>(null);
  const [evidenceEdits, setEvidenceEdits] = useState<Record<string, EvidenceEditForm>>({});
  const [dirtyEvidenceIds, setDirtyEvidenceIds] = useState<Record<string, boolean>>({});

  const hasProfile = Boolean(profileQuery.data?.profile);

  useEffect(() => {
    if (!profileFormDirty) {
      setProfileForm(profileToForm(profileQuery.data?.profile));
      setGraduationFieldsDirty(false);
    }
  }, [profileFormDirty, profileQuery.data?.profile]);

  useEffect(() => {
    setEvidenceEdits((current) => {
      const nextEdits: Record<string, EvidenceEditForm> = {};
      for (const item of profileQuery.data?.evidence_items ?? []) {
        nextEdits[item.evidence_id] =
          dirtyEvidenceIds[item.evidence_id] && current[item.evidence_id]
            ? current[item.evidence_id]
            : evidenceToForm(item);
      }
      return nextEdits;
    });
  }, [dirtyEvidenceIds, profileQuery.data?.evidence_items]);

  function setProfileField<Field extends keyof ProfileForm>(field: Field, value: ProfileForm[Field]) {
    setProfileForm((current) => ({ ...current, [field]: value }));
    setProfileFormDirty(true);
    if (field === "graduation_month" || field === "graduation_year") {
      setGraduationFieldsDirty(true);
    }
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProfileStatus("Saving profile...");
    const preservedLanguages = (profileQuery.data?.profile?.languages ?? []).filter(
      (item) => !["english", "french"].includes(languageKey(item.language))
    );
    const response = await fetch("/api/profile", {
      method: hasProfile ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: profileForm.first_name,
        last_name: profileForm.last_name,
        email: profileForm.email,
        phone: profileForm.phone,
        street_address: profileForm.street_address,
        address_line_2: profileForm.address_line_2,
        city: profileForm.city,
        province_state: profileForm.province_state,
        postal_code: profileForm.postal_code,
        country: profileForm.country,
        current_year: profileForm.current_year,
        graduation_date: graduationDateForPayload(profileForm, graduationFieldsDirty),
        graduation_month: profileForm.graduation_month,
        graduation_year: profileForm.graduation_year,
        gpa: profileForm.gpa,
        co_op_program: profileForm.co_op_program,
        willing_to_relocate: profileForm.willing_to_relocate,
        work_authorized_countries: linesToList(profileForm.work_authorized_countries),
        requires_sponsorship: profileForm.requires_sponsorship,
        linkedin_url: profileForm.linkedin_url,
        github_url: profileForm.github_url,
        links: linesToList(profileForm.links),
        summary: profileForm.summary,
        cover_letter_custom_instructions: profileForm.cover_letter_custom_instructions,
        target_role_include_keywords: linesToList(profileForm.target_role_include_keywords),
        target_role_exclude_keywords: linesToList(profileForm.target_role_exclude_keywords),
        language_filter_enabled: profileForm.language_filter_enabled,
        languages: [
          { language: "English", proficiency: profileForm.english_proficiency },
          { language: "French", proficiency: profileForm.french_proficiency },
          ...preservedLanguages
        ],
        skills: linesToList(profileForm.skills)
      })
    });
    if (response.ok) {
      const payload = (await response.json()) as ApiProfileState;
      setProfileForm(profileToForm(payload.profile));
      queryClient.setQueryData(["profile"], payload);
      setGraduationFieldsDirty(false);
      setProfileFormDirty(false);
      setProfileStatus("Profile saved.");
      await profileQuery.refetch();
    } else {
      setProfileStatus(`Profile save failed: ${response.status}`);
    }
  }

  async function uploadResume(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!hasProfile) {
      setUploadStatus("Create a profile before uploading a resume.");
      return;
    }
    if (!selectedResume) {
      setUploadStatus("Choose a resume first.");
      return;
    }
    setUploadStatus("Uploading resume...");
    const formData = new FormData();
    formData.append("file", selectedResume);
    const response = await fetch("/api/profile/resume", {
      method: "POST",
      body: formData
    });
    if (response.ok) {
      const payload = await response.json();
      setUploadStatus(`${payload.parse_status}; ${payload.evidence_created_count} evidence items created.`);
      setSelectedResume(null);
      await profileQuery.refetch();
    } else {
      setUploadStatus(`Resume upload failed: ${response.status}`);
    }
  }

  async function uploadCoverLetterSample(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!hasProfile) {
      setCoverLetterSampleStatus("Create a profile before uploading a sample.");
      return;
    }
    if (!selectedCoverLetterSample) {
      setCoverLetterSampleStatus("Choose a sample first.");
      return;
    }
    setCoverLetterSampleStatus("Uploading sample...");
    const formData = new FormData();
    formData.append("file", selectedCoverLetterSample);
    const response = await fetch("/api/profile/cover-letter-sample", {
      method: "POST",
      body: formData
    });
    if (response.ok) {
      const payload = await response.json();
      setCoverLetterSampleStatus(payload.parse_status);
      setSelectedCoverLetterSample(null);
      await profileQuery.refetch();
    } else {
      setCoverLetterSampleStatus(`Sample upload failed: ${response.status}`);
    }
  }

  async function deleteResumeArtifact(artifactId: string) {
    setUploadStatus("Removing resume...");
    const response = await fetch(`/api/profile/resume/${artifactId}`, { method: "DELETE" });
    if (response.ok) {
      setUploadStatus("Resume removed.");
      await profileQuery.refetch();
    } else {
      setUploadStatus(`Resume removal failed: ${response.status}`);
    }
  }

  async function deleteCoverLetterSampleArtifact(artifactId: string) {
    setCoverLetterSampleStatus("Removing sample...");
    const response = await fetch(`/api/profile/cover-letter-sample/${artifactId}`, { method: "DELETE" });
    if (response.ok) {
      setCoverLetterSampleStatus("Sample removed.");
      await profileQuery.refetch();
    } else {
      setCoverLetterSampleStatus(`Sample removal failed: ${response.status}`);
    }
  }

  function setEvidenceEditField<Field extends keyof EvidenceEditForm>(
    evidenceId: string,
    field: Field,
    value: EvidenceEditForm[Field]
  ) {
    setEvidenceEdits((current) => ({
      ...current,
      [evidenceId]: {
        ...current[evidenceId],
        [field]: value
      }
    }));
    setDirtyEvidenceIds((current) => ({ ...current, [evidenceId]: true }));
  }

  async function saveEvidence(evidenceId: string) {
    const edit = evidenceEdits[evidenceId];
    if (!edit) {
      return;
    }
    setEvidenceStatus("Saving evidence...");
    const response = await fetch(`/api/profile/evidence/${evidenceId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        claim: edit.claim,
        skills: linesToList(edit.skills),
        private: edit.private,
        allowed_for_resume: edit.allowed_for_resume,
        allowed_for_cover_letter: edit.allowed_for_cover_letter
      })
    });
    if (response.ok) {
      const payload = (await response.json()) as ApiEvidence;
      setEvidenceEdits((current) => ({ ...current, [evidenceId]: evidenceToForm(payload) }));
      setDirtyEvidenceIds((current) => {
        const next = { ...current };
        delete next[evidenceId];
        return next;
      });
      setEvidenceStatus("Evidence saved.");
      await profileQuery.refetch();
    } else {
      setEvidenceStatus(`Evidence save failed: ${response.status}`);
    }
  }

  async function deleteEvidence(evidenceId: string) {
    const response = await fetch(`/api/profile/evidence/${evidenceId}`, { method: "DELETE" });
    if (response.ok) {
      setDirtyEvidenceIds((current) => {
        const next = { ...current };
        delete next[evidenceId];
        return next;
      });
      setEvidenceStatus("Evidence deleted.");
      await profileQuery.refetch();
    }
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">JobCockpit</div>
        <nav>
          <a href="/">Dashboard</a>
          <a className="active" href="/profile">Profile</a>
          <a href="/sources">Sources</a>
        </nav>
      </aside>
      <section className="workspace">
        <header className="topbar">
          <div>
            <h1>Profile</h1>
            <p>Candidate details, resume upload, and evidence review.</p>
          </div>
          <a className="secondaryAction" href="/">Dashboard</a>
        </header>

        <section className="profileGrid" id="profile">
          <div className="panel">
            <div className="panelHeader">
              <h2>{hasProfile ? "Edit profile" : "Create profile"}</h2>
              <span>{hasProfile ? "Saved locally" : "Not configured"}</span>
            </div>
            <form className="profileForm" onSubmit={saveProfile}>
              <label>
                First name
                <input value={profileForm.first_name} onChange={(event) => setProfileField("first_name", event.target.value)} />
              </label>
              <label>
                Last name
                <input value={profileForm.last_name} onChange={(event) => setProfileField("last_name", event.target.value)} />
              </label>
              <label>
                Email
                <input value={profileForm.email} onChange={(event) => setProfileField("email", event.target.value)} />
              </label>
              <label>
                Phone
                <input value={profileForm.phone} onChange={(event) => setProfileField("phone", event.target.value)} />
              </label>
              <label>
                Street address
                <input
                  value={profileForm.street_address}
                  onChange={(event) => setProfileField("street_address", event.target.value)}
                />
              </label>
              <label>
                Address line 2
                <input
                  value={profileForm.address_line_2}
                  onChange={(event) => setProfileField("address_line_2", event.target.value)}
                />
              </label>
              <label>
                City
                <input value={profileForm.city} onChange={(event) => setProfileField("city", event.target.value)} />
              </label>
              <label>
                Province / state
                <input
                  value={profileForm.province_state}
                  onChange={(event) => setProfileField("province_state", event.target.value)}
                />
              </label>
              <label>
                Postal code
                <input
                  value={profileForm.postal_code}
                  onChange={(event) => setProfileField("postal_code", event.target.value)}
                />
              </label>
              <label>
                Country
                <input value={profileForm.country} onChange={(event) => setProfileField("country", event.target.value)} />
              </label>
              <label>
                Willing to relocate
                <select
                  value={
                    profileForm.willing_to_relocate === null ? "" : profileForm.willing_to_relocate ? "true" : "false"
                  }
                  onChange={(event) =>
                    setProfileField(
                      "willing_to_relocate",
                      event.target.value === "" ? null : event.target.value === "true"
                    )
                  }
                >
                  <option value="">Not specified</option>
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              </label>
              <label>
                Work authorized countries
                <textarea
                  value={profileForm.work_authorized_countries}
                  onChange={(event) => setProfileField("work_authorized_countries", event.target.value)}
                />
              </label>
              <label>
                Requires sponsorship
                <select
                  value={
                    profileForm.requires_sponsorship === null ? "" : profileForm.requires_sponsorship ? "true" : "false"
                  }
                  onChange={(event) =>
                    setProfileField(
                      "requires_sponsorship",
                      event.target.value === "" ? null : event.target.value === "true"
                    )
                  }
                >
                  <option value="">Not specified</option>
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              </label>
              <label>
                Current year
                <input value={profileForm.current_year} onChange={(event) => setProfileField("current_year", event.target.value)} />
              </label>
              <label>
                Graduation month
                <select
                  value={profileForm.graduation_month}
                  onChange={(event) => setProfileField("graduation_month", event.target.value)}
                >
                  <option value="">Not specified</option>
                  {GRADUATION_MONTHS.map((month) => (
                    <option key={month} value={month}>
                      {month}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Graduation year
                <input
                  inputMode="numeric"
                  maxLength={4}
                  value={profileForm.graduation_year}
                  onChange={(event) => setProfileField("graduation_year", event.target.value)}
                />
              </label>
              <label>
                GPA
                <input value={profileForm.gpa} onChange={(event) => setProfileField("gpa", event.target.value)} />
              </label>
              <label>
                Co-op program
                <select
                  value={
                    profileForm.co_op_program === null ? "" : profileForm.co_op_program ? "true" : "false"
                  }
                  onChange={(event) =>
                    setProfileField(
                      "co_op_program",
                      event.target.value === "" ? null : event.target.value === "true"
                    )
                  }
                >
                  <option value="">Not specified</option>
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              </label>
              <label>
                LinkedIn URL
                <input
                  value={profileForm.linkedin_url}
                  onChange={(event) => setProfileField("linkedin_url", event.target.value)}
                />
              </label>
              <label>
                GitHub URL
                <input
                  value={profileForm.github_url}
                  onChange={(event) => setProfileField("github_url", event.target.value)}
                />
              </label>
              <label>
                Links
                <textarea value={profileForm.links} onChange={(event) => setProfileField("links", event.target.value)} />
              </label>
              <label className="fullWidth">
                Skills
                <textarea value={profileForm.skills} onChange={(event) => setProfileField("skills", event.target.value)} />
              </label>
              <label className="fullWidth">
                Target role include keywords
                <textarea
                  value={profileForm.target_role_include_keywords}
                  onChange={(event) => setProfileField("target_role_include_keywords", event.target.value)}
                />
              </label>
              <label className="fullWidth">
                Target role exclude keywords
                <textarea
                  value={profileForm.target_role_exclude_keywords}
                  onChange={(event) => setProfileField("target_role_exclude_keywords", event.target.value)}
                />
              </label>
              <label className="checkRow fullWidth">
                <input
                  checked={profileForm.language_filter_enabled}
                  onChange={(event) => setProfileField("language_filter_enabled", event.target.checked)}
                  type="checkbox"
                />
                Skip postings with unmet required language requirements
              </label>
              <label>
                English proficiency
                <select
                  value={profileForm.english_proficiency}
                  onChange={(event) =>
                    setProfileField("english_proficiency", event.target.value as LanguageProficiency)
                  }
                >
                  {LANGUAGE_PROFICIENCIES.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label>
                French proficiency
                <select
                  value={profileForm.french_proficiency}
                  onChange={(event) =>
                    setProfileField("french_proficiency", event.target.value as LanguageProficiency)
                  }
                >
                  {LANGUAGE_PROFICIENCIES.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="fullWidth">
                Summary
                <textarea value={profileForm.summary} onChange={(event) => setProfileField("summary", event.target.value)} />
              </label>
              <label className="fullWidth">
                Cover letter custom instructions
                <textarea
                  value={profileForm.cover_letter_custom_instructions}
                  onChange={(event) => setProfileField("cover_letter_custom_instructions", event.target.value)}
                />
              </label>
              <button type="submit">{hasProfile ? "Save edits" : "Create profile"}</button>
              <span className="formStatus">{profileStatus}</span>
            </form>
          </div>

          <div className="panel" id="materials">
            <div className="panelHeader">
              <h2>Documents</h2>
              <span>MD, TXT, DOCX, PDF</span>
            </div>
            <h3>Resumes</h3>
            <form className="uploadForm" onSubmit={uploadResume}>
              <input
                accept=".md,.txt,.docx,.pdf"
                onChange={(event) => setSelectedResume(event.target.files?.[0] ?? null)}
                type="file"
              />
              <button type="submit">Upload resume</button>
              <span className="formStatus">{uploadStatus}</span>
            </form>
            <ul className="artifactList">
              {(profileQuery.data?.resume_artifacts ?? []).map((artifact) => (
                <li key={artifact.artifact_id}>
                  <strong>{artifact.original_file_name}</strong>
                  <span>{Math.ceil(artifact.size_bytes / 1024)} KB</span>
                  <button onClick={() => void deleteResumeArtifact(artifact.artifact_id)} type="button">
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <TranscriptUploads hasProfile={hasProfile} />
            <h3>Cover letter sample</h3>
            <form className="uploadForm" onSubmit={uploadCoverLetterSample}>
              <input
                accept=".md,.txt,.docx,.pdf"
                onChange={(event) => setSelectedCoverLetterSample(event.target.files?.[0] ?? null)}
                type="file"
              />
              <button type="submit">Upload sample</button>
              <span className="formStatus">{coverLetterSampleStatus}</span>
            </form>
            <ul className="artifactList">
              {(profileQuery.data?.cover_letter_sample_artifacts ?? []).map((artifact) => (
                <li key={artifact.artifact_id}>
                  <strong>{artifact.original_file_name}</strong>
                  <span>{Math.ceil(artifact.size_bytes / 1024)} KB</span>
                  <button onClick={() => void deleteCoverLetterSampleArtifact(artifact.artifact_id)} type="button">
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="panel wide">
            <div className="panelHeader">
              <h2>Evidence review</h2>
              <span>{profileQuery.data?.evidence_items.length ?? 0} items</span>
            </div>
            <span className="formStatus evidenceStatus">{evidenceStatus}</span>
            <ul className="evidenceList">
              {(profileQuery.data?.evidence_items ?? []).map((item) => {
                const edit = evidenceEdits[item.evidence_id] ?? evidenceToForm(item);
                return (
                  <li key={item.evidence_id}>
                    <div className="evidenceEditor">
                      <label>
                        Claim
                        <textarea
                          value={edit.claim}
                          onChange={(event) => setEvidenceEditField(item.evidence_id, "claim", event.target.value)}
                        />
                      </label>
                      <label>
                        Skills
                        <input
                          value={edit.skills}
                          onChange={(event) => setEvidenceEditField(item.evidence_id, "skills", event.target.value)}
                        />
                      </label>
                    </div>
                    <div className="evidenceActions">
                      <label>
                        <input
                          checked={edit.private}
                          onChange={(event) => setEvidenceEditField(item.evidence_id, "private", event.target.checked)}
                          type="checkbox"
                        />
                        Private
                      </label>
                      <label>
                        <input
                          checked={edit.allowed_for_resume}
                          onChange={(event) => setEvidenceEditField(item.evidence_id, "allowed_for_resume", event.target.checked)}
                          type="checkbox"
                        />
                        Resume
                      </label>
                      <label>
                        <input
                          checked={edit.allowed_for_cover_letter}
                          onChange={(event) =>
                            setEvidenceEditField(item.evidence_id, "allowed_for_cover_letter", event.target.checked)
                          }
                          type="checkbox"
                        />
                        Cover letter
                      </label>
                      <button onClick={() => void saveEvidence(item.evidence_id)} type="button">Save evidence</button>
                      <button onClick={() => void deleteEvidence(item.evidence_id)} type="button">Delete</button>
                    </div>
                  </li>
                );
              })}
              {profileQuery.data?.evidence_items.length === 0 ? (
                <li className="emptyEvidence">Upload a resume to extract evidence.</li>
              ) : null}
            </ul>
          </div>
        </section>
      </section>
    </main>
  );
}
