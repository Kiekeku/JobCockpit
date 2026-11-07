"""Private, declarative publication policy. No destination or audience defaults."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str | None = None
    op: Literal["eq", "gte", "in", "between", "regex", "missing"] | None = None
    value: Any = None
    all: list[Rule] | None = None
    any: list[Rule] | None = None

    @model_validator(mode="after")
    def validate_rule(self):
        groups = int(self.all is not None) + int(self.any is not None)
        if groups:
            if groups != 1 or self.field or self.op or self.value is not None:
                raise ValueError("A rule must contain exactly one group or predicate")
            if not (self.all or self.any):
                raise ValueError("Rule groups cannot be empty")
            return self
        if not self.field or not self.op:
            raise ValueError("A predicate needs a field and operator")
        if self.op in {"in", "between"} and not isinstance(self.value, list):
            raise ValueError("List comparison requires an array")
        if self.op == "between" and len(self.value) != 2:
            raise ValueError("Range comparison requires two bounds")
        if self.op == "regex":
            try:
                re.compile(self.value)
            except (TypeError, re.error) as error:
                raise ValueError("Invalid rule expression") from error
        return self

    def matches(self, data: dict) -> bool:
        if self.all is not None:
            return bool(self.all) and all(rule.matches(data) for rule in self.all)
        if self.any is not None:
            return any(rule.matches(data) for rule in self.any)
        actual: Any = data
        for part in (self.field or "").split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if self.op == "missing":
            return actual is None or actual == ""
        if actual is None:
            return False
        try:
            if self.op == "eq":
                return actual == self.value
            if self.op == "gte":
                return isinstance(actual, (int, float)) and not isinstance(actual, bool) \
                    and actual >= self.value
            if self.op == "in":
                return actual in self.value
            if self.op == "between":
                return self.value[0] <= actual <= self.value[1]
            if self.op == "regex":
                return isinstance(actual, str) and bool(re.search(self.value, actual, re.I))
        except (TypeError, ValueError, re.error):
            return False
        return False


class PublicationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    rules: Rule
    excluded_providers: list[str] = Field(default_factory=list)
    source_hosts: list[str] = Field(default_factory=list)
    audience_brief: str = Field(min_length=1, max_length=4000)
    display_term: str = Field(min_length=1, max_length=80)
    standard_duration_months: int = Field(ge=1, le=36)
    country_names: dict[str, str]
    assessment_model: str | None = None

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
            raise ValueError("Publication endpoint must be a credential-free HTTPS URL")
        return value.rstrip("/")

    @property
    def destination_key(self) -> str:
        return hashlib.sha256(self.endpoint.encode()).hexdigest()[:16]


def load_policy(settings) -> PublicationPolicy | None:
    if not settings.publication_policy_path:
        return None
    return PublicationPolicy.model_validate_json(
        settings.publication_policy_path.read_text(encoding="utf-8")
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def iso_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def from_destination(job, policy: PublicationPolicy) -> bool:
    source = job.source or {}
    providers = {str(job.source_type or ""), str(source.get("platform", "")),
                 str(source.get("source_type", ""))}
    for observation in source.get("cross_source_matches", []):
        if isinstance(observation, dict):
            providers.add(str(observation.get("source_type", "")))
    if providers.intersection(policy.excluded_providers):
        return True
    hosts = {host.casefold() for host in policy.source_hosts}
    hosts.add(urlsplit(policy.endpoint).hostname or "")

    def contains_host(value):
        if isinstance(value, dict):
            return any(contains_host(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_host(item) for item in value)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return (urlsplit(value).hostname or "").casefold() in hosts
        return False

    return contains_host(source) or contains_host(job.source_url)


def eligible(job, policy: PublicationPolicy) -> bool:
    if job.status != "active" or (job.source or {}).get("merged_into_job_id"):
        return False
    if from_destination(job, policy):
        return False
    data = {"fit_analysis": job.fit_analysis or {}, "employment_timing": job.employment_timing or {},
            "role": job.role or {}, "location": job.location or {}, "role_type": job.role_type}
    return policy.rules.matches(data)
