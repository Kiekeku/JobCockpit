from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtensionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionOption(ExtensionContract):
    value: str = Field(max_length=1000)
    label: str = Field(max_length=1000)
    disabled: bool = False


class ApplicationQuestion(ExtensionContract):
    id: str = Field(min_length=1, max_length=100)
    document_token: str = Field(min_length=1, max_length=100)
    snapshot_token: str = Field(min_length=1, max_length=100)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(max_length=4000)
    section: str = Field(default="", max_length=1000)
    help_text: str = Field(default="", max_length=4000)
    kind: Literal[
        "text", "textarea", "email", "tel", "url", "number", "date", "month",
        "select", "radio", "checkbox", "file", "unsupported",
    ]
    required: bool = False
    options: list[QuestionOption] = Field(default_factory=list, max_length=300)
    current_value: str = Field(default="", max_length=10000)
    max_length: int | None = Field(default=None, ge=0, le=100000)
    minimum: str = Field(default="", max_length=100)
    maximum: str = Field(default="", max_length=100)
    step: str = Field(default="", max_length=100)
    autocomplete: str = Field(default="", max_length=100)
    purpose: Literal["resume", "cover_letter", "transcript", "unknown"] = "unknown"
    fillable: bool = False
    unsupported_reason: str = Field(default="", max_length=1000)
    frame_id: int = Field(default=0, ge=0)
    frame_url: str = Field(max_length=2048)

    @field_validator("frame_url")
    @classmethod
    def validate_frame_url(cls, value: str) -> str:
        return application_url(value)


def application_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("An HTTP(S) application URL without credentials is required")
    return value


class ApplicationSessionRequest(ExtensionContract):
    job_id: str = Field(min_length=1, max_length=64)
    page_url: str = Field(max_length=2048)
    page_title: str = Field(default="", max_length=500)
    questions: list[ApplicationQuestion] = Field(min_length=1, max_length=100)
    job_confirmed: Literal[True]

    @field_validator("page_url")
    @classmethod
    def validate_page_url(cls, value: str) -> str:
        return application_url(value)

    @model_validator(mode="after")
    def bounded_unique_questions(self):
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("Question IDs must be unique across all frames")
        if len(self.model_dump_json()) > 250000:
            raise ValueError("Application page is too large; scan one section at a time")
        return self


class DraftAnswersRequest(ExtensionContract):
    revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=80)
    instructions: str = Field(default="", max_length=4000)
    consent_to_use_sources: Literal[True]


class AnswerReview(ExtensionContract):
    question_id: str = Field(min_length=1, max_length=100)
    answer: str = Field(default="", max_length=10000)
    decision: Literal["approve", "skip"]
    replace_existing: bool = False
    confirmed_sensitive: bool = False
    remember: bool = False


class ReviewAnswersRequest(ExtensionContract):
    revision: int = Field(ge=1)
    reviews: list[AnswerReview] = Field(min_length=1, max_length=100)

    @field_validator("reviews")
    @classmethod
    def unique_reviews(cls, value):
        if len({item.question_id for item in value}) != len(value):
            raise ValueError("A question may be reviewed only once per request")
        return value


class FillReceipt(ExtensionContract):
    question_id: str = Field(max_length=100)
    status: Literal["filled", "unchanged", "skipped", "failed", "stale", "undone", "attached"]
    actual_value: str = Field(default="", max_length=10000)
    message: str = Field(default="", max_length=1000)


class FillReceiptsRequest(ExtensionContract):
    revision: int = Field(ge=1)
    results: list[FillReceipt] = Field(min_length=1, max_length=100)


class DocumentApprovalRequest(ExtensionContract):
    revision: int = Field(ge=1)
    question_id: str = Field(min_length=1, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    replace_existing: bool = False
    reviewed_document_and_target: Literal[True]


def question_is_manual(question: ApplicationQuestion) -> bool:
    text = f"{question.text} {question.section}"
    return bool(re.search(
        r"\b(signature|e-sign|certif(?:y|ication)|attest|consent|agree to|terms (?:and|of)|"
        r"privacy policy|gender|sex|disability|veteran|race|ethnicity|criminal|background check)\b",
        text, re.I,
    ))


def question_is_sensitive(question: ApplicationQuestion) -> bool:
    return bool(re.search(
        r"\b(sponsor(?:ship)?|authori[sz](?:ed|ation)|citizen(?:ship)?|visa|immigration|"
        r"available|availability|relocat(?:e|ion)|salary|compensation|legally|eligible|eligibility)\b",
        f"{question.text} {question.help_text}", re.I,
    ))
