from __future__ import annotations

import re
from urllib.parse import urlparse

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


JobOpportunityStatus = Literal["active", "expired", "closed", "filled", "withdrawn"]


class JobIngestRequest(BaseModel):
    source_type: str = Field(pattern="^(paste|file|url|gmail_alert|outlook_alert|ats_mock)$")
    raw_text: str = Field(min_length=1)
    source_url: str | None = None
    skip_language_filter: bool = False


class JobOpportunityStatusUpdateRequest(BaseModel):
    status: JobOpportunityStatus


class JobOpportunityStatusBulkUpdateRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=500)
    status: JobOpportunityStatus

    @field_validator("job_ids")
    @classmethod
    def validate_unique_job_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("job_ids must be unique")
        return value


class JobSourceUpdateRequest(BaseModel):
    source_url: str = Field(min_length=1, max_length=1024)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an http or https URL")
        return cleaned


class ExtensionPageCaptureRequest(BaseModel):
    source_url: str = Field(min_length=1, max_length=2048)
    page_title: str = Field(default="", max_length=500)
    visible_text: str = Field(min_length=1, max_length=200_000)
    html: str | None = Field(default=None, max_length=1_000_000)
    target_job_id: str | None = Field(default=None, max_length=64)
    skip_language_filter: bool = False

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an http or https URL")
        return cleaned

    @field_validator("page_title", "visible_text", "html", "target_job_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned and value is not None:
            return ""
        return cleaned

    @field_validator("visible_text")
    @classmethod
    def validate_visible_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("visible_text cannot be blank")
        return cleaned

class AtsIngestRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    search_text: str = Field(default="", max_length=255)
    applied_facets: dict = Field(default_factory=dict)
    skip_interest_filter: bool = False
    skip_language_filter: bool = False

    @field_validator("source_url", "search_text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_provider_limit(self):
        if self.provider != "csjobs" and self.limit > 50:
            raise ValueError("limit must be at most 50 for this ATS provider")
        if self.provider == "rankalpha_jobs":
            from jobcockpit.ingestion.ats_adapters.base import AtsProviderError
            from jobcockpit.ingestion.ats_adapters.rankalpha_jobs import (
                validate_rankalpha_jobs_request_inputs,
            )

            try:
                validate_rankalpha_jobs_request_inputs(
                    self.source_url,
                    self.applied_facets,
                )
            except AtsProviderError as error:
                raise ValueError(error.detail) from error
        return self


class WorkdayIngestRequest(BaseModel):
    source_url: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    search_text: str = Field(default="", max_length=255)
    applied_facets: dict = Field(default_factory=dict)
    skip_interest_filter: bool = False
    skip_language_filter: bool = False

    @field_validator("source_url", "search_text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ApplicationDecisionRequest(BaseModel):
    decision: str
    would_accept_if_offered: bool | None = None


class JobAlertSourceCreate(BaseModel):
    display_name: str
    gmail_query: str
    sender_allowlist: list[str] = []
    enabled: bool = True
    skip_interest_filter: bool = False
    skip_language_filter: bool = False

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("display_name cannot be blank")
        return name

    @field_validator("gmail_query")
    @classmethod
    def validate_gmail_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("gmail_query cannot be blank")
        return query

    @field_validator("sender_allowlist")
    @classmethod
    def clean_sender_allowlist(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip().lower()
            if not item or item in seen:
                continue
            seen.add(item)
            cleaned.append(item)
        return cleaned


class JobAlertSourceUpdate(JobAlertSourceCreate):
    pass


class GmailPreviewRequest(BaseModel):
    source_id: str


class GmailImportRequest(BaseModel):
    source_id: str
    approved_message_review_ids: list[str]


class ExtensionPackageSessionRequest(BaseModel):
    document_ids: list[str] | None = None
    expires_in_seconds: int = Field(default=300, ge=1, le=900)


class CoverLetterDraftRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    resume_artifact_id: str | None = None

    @field_validator("resume_artifact_id")
    @classmethod
    def strip_resume_artifact_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None



LanguageProficiency = Literal["none", "basic", "intermediate", "professional", "fluent", "native"]


def default_profile_languages() -> list[ProfileLanguage]:
    return [
        ProfileLanguage(language="English", proficiency="fluent"),
        ProfileLanguage(language="French", proficiency="none"),
    ]


class ProfileLanguage(BaseModel):
    language: str = Field(min_length=1, max_length=64)
    proficiency: LanguageProficiency = "none"

    @field_validator("language")
    @classmethod
    def strip_language(cls, value: str) -> str:
        language = value.strip()
        if not language:
            raise ValueError("language cannot be blank")
        return language

GRADUATION_MONTHS = (
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
    "December",
)


def normalize_graduation_month(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.isdigit():
        month_index = int(cleaned)
        if 1 <= month_index <= 12:
            return GRADUATION_MONTHS[month_index - 1]
    lowered = cleaned.casefold()
    for month in GRADUATION_MONTHS:
        if lowered in {month.casefold(), month[:3].casefold()}:
            return month
    raise ValueError("graduation_month must be a month name or number")


def _split_full_name(value: str) -> tuple[str, str]:
    parts = value.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


class ProfileRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=255)
    phone: str = Field(default="", max_length=64)
    street_address: str = Field(default="", max_length=255)
    address_line_2: str = Field(default="", max_length=255)
    city: str = Field(default="", max_length=128)
    province_state: str = Field(default="", max_length=128)
    postal_code: str = Field(default="", max_length=32)
    country: str = Field(default="", max_length=128)
    current_year: str = Field(default="", max_length=64)
    graduation_date: str = Field(default="", max_length=64)
    graduation_month: str = Field(default="", max_length=16)
    graduation_year: str = Field(default="", max_length=16)
    gpa: str = Field(default="", max_length=32)
    co_op_program: bool | None = None
    willing_to_relocate: bool | None = None
    work_authorized_countries: list[str] = Field(default_factory=list)
    requires_sponsorship: bool | None = None
    linkedin_url: str = Field(default="", max_length=255)
    github_url: str = Field(default="", max_length=255)
    links: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=2000)
    cover_letter_custom_instructions: str = Field(default="", max_length=8000)
    target_role_include_keywords: list[str] = Field(default_factory=list)
    target_role_exclude_keywords: list[str] = Field(default_factory=list)
    language_filter_enabled: bool = False
    languages: list[ProfileLanguage] = Field(default_factory=default_profile_languages)
    skills: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def populate_split_names_from_legacy_full_name(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not data.get("full_name"):
            return data
        next_data = dict(data)
        first_name, last_name = _split_full_name(str(data["full_name"]))
        next_data.setdefault("first_name", first_name)
        next_data.setdefault("last_name", last_name)
        return next_data

    @field_validator(
        "first_name",
        "last_name",
        "email",
        "phone",
        "street_address",
        "address_line_2",
        "city",
        "province_state",
        "postal_code",
        "country",
        "current_year",
        "graduation_date",
        "graduation_year",
        "gpa",
        "linkedin_url",
        "github_url",
        "summary",
        "cover_letter_custom_instructions",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("graduation_month")
    @classmethod
    def validate_graduation_month(cls, value: str) -> str:
        return normalize_graduation_month(value)

    @field_validator("graduation_year")
    @classmethod
    def validate_graduation_year(cls, value: str) -> str:
        if value and not re.fullmatch(r"\d{4}", value):
            raise ValueError("graduation_year must be a four-digit year")
        return value

    @field_validator("first_name")
    @classmethod
    def validate_first_name(cls, value: str) -> str:
        if not value:
            raise ValueError("first_name cannot be blank")
        return value

    @field_validator("last_name")
    @classmethod
    def validate_last_name(cls, value: str) -> str:
        if not value:
            raise ValueError("last_name cannot be blank")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address")
        return value

    @field_validator(
        "links",
        "skills",
        "target_role_include_keywords",
        "target_role_exclude_keywords",
        "work_authorized_countries",
    )
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(item)
        return cleaned

    @field_validator("languages")
    @classmethod
    def clean_languages(cls, values: list[ProfileLanguage]) -> list[ProfileLanguage]:
        cleaned: list[ProfileLanguage] = []
        seen: set[str] = set()
        for value in values:
            key = value.language.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        return cleaned or default_profile_languages()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EvidencePatchRequest(BaseModel):
    claim: str | None = Field(default=None, min_length=1)
    skills: list[str] | None = None
    private: bool | None = None
    allowed_for_resume: bool | None = None
    allowed_for_cover_letter: bool | None = None

    @field_validator("claim")
    @classmethod
    def strip_claim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("claim cannot be blank")
        return cleaned

    @field_validator("skills")
    @classmethod
    def clean_skills(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return ProfileRequest.clean_list(values)
