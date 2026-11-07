from __future__ import annotations

import re
from datetime import date

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


_DOCUMENT_ORDER = (
    "resume",
    "cover_letter",
    "transcript",
    "portfolio",
    "writing_sample",
    "references",
)
_DOCUMENT_ALIASES = {
    "cv": "resume",
    "curriculum_vitae": "resume",
    "resume": "resume",
    "cover_letter": "cover_letter",
    "letter_of_interest": "cover_letter",
    "motivation_letter": "cover_letter",
    "transcript": "transcript",
    "transcripts": "transcript",
    "portfolio": "portfolio",
    "writing_sample": "writing_sample",
    "writing_samples": "writing_sample",
    "reference": "references",
    "references": "references",
    "reference_list": "references",
}


class ParsedSource(BaseModel):
    platform: str = "manual"
    source_type: str = "paste"
    source_url: str | None = None
    raw_content_artifact_id: str | None = None
    parser_provider: str | None = None
    parser_outcome: str | None = None
    parser_error_code: str | None = None


class ParsedEmployer(BaseModel):
    company_name: str | None = None
    division: str | None = None
    industry: str | None = None


class CapabilityMention(BaseModel):
    surface_text: str
    canonical_name: str | None = None
    entity_type: Literal[
        "language",
        "framework",
        "platform",
        "tool",
        "method",
        "domain",
        "credential",
        "soft_skill",
        "unknown",
    ] = "unknown"
    source_start: int | None = None
    source_end: int | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class RequirementExpression(BaseModel):
    node_id: str | None = None
    operator: Literal["claim", "all_of", "any_of", "alternative"]
    claim_id: str | None = None
    text: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    children: list["RequirementExpression"] = Field(default_factory=list)


class RoleClaim(BaseModel):
    claim_id: str | None = None
    kind: Literal[
        "responsibility",
        "qualification",
        "eligibility",
        "environment",
        "company_context",
    ]
    modality: Literal[
        "mandatory",
        "expected",
        "preferred",
        "optional",
        "descriptive",
        "prohibited",
        "unknown",
    ]
    owner: Literal["applicant", "role", "employer", "team", "unknown"] = "unknown"
    polarity: Literal["positive", "negative"] = "positive"
    condition_text: str | None = None
    condition_type: Literal[
        "geographic",
        "temporal",
        "applicant_subset",
        "contingency",
        "schedule",
        "unknown",
    ] | None = None
    coordination: Literal["single", "all_of", "any_of", "alternative"] = "single"
    coordination_group: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    text: str
    normalized_text: str | None = None
    entities: list[CapabilityMention] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    quality_flags: list[str] = Field(default_factory=list)
    decision_trace: list[str] = Field(default_factory=list)


class RoleEvidence(BaseModel):
    kind: Literal["responsibility", "requirement", "context"]
    modality: Literal["required", "preferred", "descriptive", "non_requirement"]
    source_section: Literal[
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
        "capabilities",
        "company",
        "unsectioned",
    ]
    text: str
    evidence_text: str
    evidence_start: int | None = None
    evidence_end: int | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    entities: list[str] = []
    claims: list[RoleClaim] = Field(default_factory=list)
    alternative_claims: list[RoleClaim] = Field(default_factory=list)
    requirement_expression: RequirementExpression | None = None
    quality_flags: list[str] = Field(default_factory=list)


class ParsedRole(BaseModel):
    job_title: str | None
    normalized_title: str | None
    job_family: str | None = None
    seniority: str | None = None
    role_type: str = "unknown"
    description_raw: str
    description_clean: str
    responsibilities: list[str] = []
    must_have_requirements: list[str] = []
    nice_to_have_requirements: list[str] = []
    evidence: list[RoleEvidence] = []
    required_capabilities: list[str] = []
    preferred_capabilities: list[str] = []
    responsibility_capabilities: list[str] = []
    environment_capabilities: list[str] = []
    capabilities: list[str] = []
    capability_domains: list[str] = []
    tech_stack: list[str] = []
    domain_keywords: list[str] = []


class ParsedTiming(BaseModel):
    raw_label: str | None = None
    timing_type: str = "unknown"
    start_date: str | None = None
    end_date: str | None = None
    duration_months: int | None = None
    duration_label: str | None = None
    hours_per_week_min: float | None = None
    hours_per_week_max: float | None = None
    schedule_type: str | None = None


class ProviderTimingContract(ParsedTiming):
    @field_validator("start_date", "end_date")
    @classmethod
    def validate_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
            raise ValueError("provider timing dates must use YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("provider timing date is not a valid calendar date") from error
        return value

    @field_validator("duration_months")
    @classmethod
    def validate_duration_months(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 120:
            raise ValueError("provider duration_months must be between 1 and 120")
        return value

    @field_validator("hours_per_week_min", "hours_per_week_max")
    @classmethod
    def validate_weekly_hours(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 168:
            raise ValueError("provider weekly hours must be between 0 and 168")
        return value


class ParsedLocation(BaseModel):
    work_arrangement: str = "unknown"
    city: str | None = None
    province_state: str | None = None
    country: str | None = None
    remote_eligibility: str | None = None


class ParsedEligibility(BaseModel):
    current_enrollment_required: bool = False
    co_op_enrollment_required: bool = False
    internship_eligible_only: bool = False
    work_authorization: str | None = None


class ParsedCompensation(BaseModel):
    pay_min: int | float | None = None
    pay_max: int | float | None = None
    pay_currency: str | None = None
    pay_period: str | None = None


class ParsedApplication(BaseModel):
    application_deadline: str | None = None
    application_method: str = "manual"
    external_apply_url: str | None = None
    documents_required: list[str] | None = None
    resume_required: bool | None = None
    cover_letter_required: bool | None = None
    requires_manual_submission: bool = True
    autofill_supported: bool = False
    autofill_support_level: str = "none"
    document_upload_supported: str = "none"
    upload_targets: list[dict] = []

    @field_validator("documents_required", mode="before")
    @classmethod
    def normalize_required_documents(cls, value):
        if value is None:
            return None
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        normalized: list[str] = []
        for item in value:
            slug = re.sub(r"[^a-z0-9]+", "_", str(item).strip().casefold()).strip("_")
            document = _DOCUMENT_ALIASES.get(slug)
            if document and document not in normalized:
                normalized.append(document)
        return sorted(normalized, key=_DOCUMENT_ORDER.index)

    @model_validator(mode="after")
    def synchronize_document_flags(self):
        if self.documents_required is None:
            if self.resume_required is not None or self.cover_letter_required is not None:
                self.documents_required = []
            else:
                return self

        documents = list(self.documents_required)
        if self.resume_required is True and "resume" not in documents:
            documents.append("resume")
        elif self.resume_required is False and "resume" in documents:
            documents.remove("resume")
        elif self.resume_required is None and "resume" in documents:
            self.resume_required = True

        if self.cover_letter_required is True and "cover_letter" not in documents:
            documents.append("cover_letter")
        elif self.cover_letter_required is False and "cover_letter" in documents:
            documents.remove("cover_letter")
        elif self.cover_letter_required is None and "cover_letter" in documents:
            self.cover_letter_required = True

        self.documents_required = sorted(set(documents), key=_DOCUMENT_ORDER.index)
        return self


class ParsedFit(BaseModel):
    fit_score: int | None = None
    technical_match_score: int | None = None
    eligibility_score: int | None = None
    interest_score: int | None = None
    compensation_score: int | None = None
    timing_location_score: int | None = None
    application_effort_score: int | None = None
    risk_flags: list[str] = []
    missing_requirements: list[str] = []
    ranking_reason: list[str] = []
    tailoring_strategy: str | None = None


class ParsedDataQuality(BaseModel):
    extraction_confidence: float = 0.7
    missing_fields: list[str] = []
    ambiguous_fields: list[str] = []
    inferred_fields: list[str] = []
    needs_manual_review: bool = True

    @field_validator("ambiguous_fields", mode="before")
    @classmethod
    def normalize_ambiguous_field_paths(cls, value):
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple, set)) else [value]
        normalized: list[str] = []
        for item in items:
            text = " ".join(str(item).split())
            matches = re.findall(
                r"\b(?:source|employer|role|employment_timing|location|eligibility|"
                r"compensation|application|fit_analysis|data_quality)\.[a-z][a-z0-9_]*",
                text,
                flags=re.IGNORECASE,
            )
            fields = [match.casefold() for match in matches]
            if re.search(r"\bwork authori[sz]ation\b", text, re.I):
                fields.append("eligibility.work_authorization")
            if not fields:
                fields.append("data_quality.ambiguous_fields")
            for field in fields:
                if field not in normalized:
                    normalized.append(field)
        return normalized


class ParsedJob(BaseModel):
    source: ParsedSource
    employer: ParsedEmployer
    role: ParsedRole
    employment_timing: ParsedTiming
    location: ParsedLocation
    eligibility: ParsedEligibility
    compensation: ParsedCompensation
    application: ParsedApplication
    fit_analysis: ParsedFit
    data_quality: ParsedDataQuality
