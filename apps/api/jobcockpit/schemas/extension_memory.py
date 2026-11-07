from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from jobcockpit.schemas.extension_assistance import ExtensionContract


class ObservedAnswer(ExtensionContract):
    question_id: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=10000)
    source: Literal["page", "panel"]


class LearnAnswersRequest(ExtensionContract):
    session_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    observations: list[ObservedAnswer] = Field(min_length=1, max_length=30)
    confirmed_my_answers: Literal[True]

    @model_validator(mode="after")
    def unique(self):
        if len({o.question_id for o in self.observations}) != len(self.observations):
            raise ValueError("Choose one answer per question")
        return self


class PersonalFactFields(ExtensionContract):
    subject_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    value: str = Field(min_length=1, max_length=1000)
    statement: str = Field(min_length=5, max_length=2500)
    scope: Literal["general", "country", "term", "job"]
    country: str = Field(default="", max_length=100)
    term: Literal["", "Winter", "Summer", "Fall"] = ""
    year: int | None = Field(default=None, ge=2020, le=2100)
    job_id: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def scope_is_explicit(self):
        self.value = self.value.strip()
        self.statement = self.statement.strip()
        self.country = self.country.strip()
        if not self.value or not self.statement:
            raise ValueError("A fact must contain a value and a complete statement")
        if self.scope == "country" and not self.country:
            raise ValueError("Country-specific facts need a country")
        if self.scope == "term" and (not self.term or self.year is None):
            raise ValueError("Term-specific facts need a canonical term and year")
        if self.scope == "job" and not self.job_id:
            raise ValueError("Application-specific facts need a job")
        if self.scope != "job" and self.job_id:
            raise ValueError("Only application-specific facts have a job scope")
        if self.scope != "term" and (self.term or self.year):
            raise ValueError("Only term-specific facts have a term and year")
        if self.scope != "country" and self.country:
            raise ValueError("Only country-specific facts have a country scope")
        return self


class ConfirmPersonalFact(PersonalFactFields):
    observation_id: str = Field(min_length=1, max_length=100)
    run_revision: int = Field(ge=1)
    replace_revisions: dict[str, int] = Field(default_factory=dict, max_length=50)
    confirmed: Literal[True]


class UpdatePersonalFact(PersonalFactFields):
    revision: int = Field(ge=1)
    replace_revisions: dict[str, int] = Field(default_factory=dict, max_length=50)
    confirmed: Literal[True]
