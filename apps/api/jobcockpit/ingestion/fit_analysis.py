from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jobcockpit.db.models import EvidenceItem
from jobcockpit.ingestion.codex_cli import json_object_from_output, run_codex_prompt
from jobcockpit.ingestion.parser import ParsedFit, ParsedJob


Runner = Callable[..., subprocess.CompletedProcess]

SAFE_PROFILE_FIELDS = (
    "summary",
    "skills",
    "city",
    "province_state",
    "country",
    "current_year",
    "graduation_date",
    "gpa",
    "co_op_program",
    "willing_to_relocate",
    "work_authorized_countries",
    "requires_sponsorship",
)
MAX_ALLOWED_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_CLAIM_CHARS = 500
MAX_EVIDENCE_SKILLS = 12
MAX_EVIDENCE_SKILL_CHARS = 80
MAX_PROFILE_SUMMARY_CHARS = 1000
MAX_PROFILE_SKILLS = 30
MAX_PROFILE_SKILL_CHARS = 80


class FitAnalysisUnavailableError(RuntimeError):
    """Raised when an explicit fit-analysis refresh cannot produce a fresh result."""


class CodexCliFitAnalyzer:
    def __init__(
        self,
        *,
        command: str = "codex",
        runner: Runner = subprocess.run,
        timeout_seconds: int = 60,
        max_input_chars: int = 16000,
        workspace: str | Path | None = None,
        ignore_user_config: bool = True,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.command = command
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.max_input_chars = max_input_chars
        self.workspace = workspace
        self.ignore_user_config = ignore_user_config
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.last_error = ""

    def analyze(
        self,
        parsed_job: ParsedJob,
        *,
        raw_text: str,
        source_url: str | None,
        profile_data: dict[str, Any] | None,
        evidence_items: list[EvidenceItem],
    ) -> ParsedFit | None:
        self.last_error = ""
        prompt = fit_analysis_prompt(
            parsed_job,
            raw_text=raw_text,
            source_url=source_url,
            profile_data=profile_data,
            evidence_items=evidence_items,
            max_input_chars=self.max_input_chars,
        )
        result = run_codex_prompt(
            self.command,
            runner=self.runner,
            prompt=prompt,
            timeout_seconds=self.timeout_seconds,
            workspace=self.workspace,
            ignore_user_config=self.ignore_user_config,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            operation="fit_analysis",
        )
        if result.error:
            self.last_error = result.error
            return None
        payload = json_object_from_output(result.stdout)
        if payload is None:
            self.last_error = "codex_cli_invalid_json"
            return None
        try:
            return ParsedFit.model_validate(payload)
        except (TypeError, ValidationError, ValueError) as error:
            self.last_error = f"codex_cli_invalid_fit_analysis: {error}"
            return None


def analyze_fit_with_settings(
    parsed_job: ParsedJob,
    *,
    raw_text: str,
    source_url: str | None,
    settings: object | None,
    profile_data: dict[str, Any] | None,
    evidence_items: list[EvidenceItem],
    analyzer: Any | None = None,
) -> ParsedFit:
    if _fit_analysis_provider(settings) == "disabled":
        return parsed_job.fit_analysis
    try:
        return require_fit_analysis_with_settings(
            parsed_job,
            raw_text=raw_text,
            source_url=source_url,
            settings=settings,
            profile_data=profile_data,
            evidence_items=evidence_items,
            analyzer=analyzer,
        )
    except FitAnalysisUnavailableError:
        return parsed_job.fit_analysis


def require_fit_analysis_with_settings(
    parsed_job: ParsedJob,
    *,
    raw_text: str,
    source_url: str | None,
    settings: object | None,
    profile_data: dict[str, Any] | None,
    evidence_items: list[EvidenceItem],
    analyzer: Any | None = None,
) -> ParsedFit:
    if _fit_analysis_provider(settings) == "disabled":
        raise FitAnalysisUnavailableError("fit_analysis_provider_disabled")
    if analyzer is None:
        from jobcockpit.ingestion.llm_providers import create_fit_analyzer

        analyzer = create_fit_analyzer(settings)
    try:
        fit = analyzer.analyze(
            parsed_job,
            raw_text=raw_text,
            source_url=source_url,
            profile_data=profile_data,
            evidence_items=evidence_items,
        )
    except Exception as error:
        raise FitAnalysisUnavailableError(f"fit_analysis_failed: {error}") from error
    if fit is None:
        detail = str(getattr(analyzer, "last_error", "") or "fit_analysis_no_result")
        raise FitAnalysisUnavailableError(detail)
    return fit


def _fit_analysis_provider(settings: object | None) -> str:
    return str(getattr(settings, "fit_analysis_provider", "disabled") or "disabled").strip().lower()


def fit_analysis_prompt(
    parsed_job: ParsedJob,
    *,
    raw_text: str,
    source_url: str | None,
    profile_data: dict[str, Any] | None,
    evidence_items: list[EvidenceItem],
    max_input_chars: int,
) -> str:
    payload = {
        "source_url": source_url,
        "job": _job_context(parsed_job),
        "candidate_profile": _safe_profile_context(profile_data or {}),
        "allowed_evidence": _allowed_evidence_context(evidence_items),
        "posting_text": raw_text[:max_input_chars],
    }
    return (
        "Analyze candidate fit for this public job posting. Return only JSON matching the "
        "fit_analysis schema below. Use only the candidate_profile and allowed_evidence for "
        "candidate claims. Do not infer private facts or unsupported experience. Scores are "
        "0-100 integers or null when evidence is insufficient. Use risk_flags and "
        "missing_requirements for gaps, and ranking_reason for concise evidence-backed reasons. "
        "Interpret technical_match_score as capability/requirements match for non-technical roles. "
        "Use job.employment_timing for the resolved work period. When its dates are listed in "
        "job.data_quality.inferred_fields and conflict with the posting text, describe the "
        "source-date discrepancy as needing verification; do not classify the opportunity "
        "as already past solely from that conflicting source date. An inferred year is not "
        "an employer-confirmed date. "
        "If candidate_profile.willing_to_relocate is true, do not lower timing_location_score "
        "or fit_score solely because the job is in a different city or province. Still consider "
        "hard location constraints, such as must already be local, local candidates only, or "
        "in-person availability that conflicts with candidate facts. If willing_to_relocate is "
        "false or missing, keep normal location-mismatch scoring behavior.\n\n"
        "If a posting requires legal authorization in a country listed in "
        "candidate_profile.work_authorized_countries, treat that work authorization requirement as "
        "satisfied. If a posting says sponsorship is unavailable and "
        "candidate_profile.requires_sponsorship is false, treat the no-sponsorship requirement as "
        "satisfied. If requires_sponsorship is true and the posting says no sponsorship, reflect "
        "that in eligibility_score, missing_requirements, and/or risk_flags. If eligibility fields "
        "are missing or unspecified, do not infer private immigration facts.\n\n"
        "Schema keys: fit_score, technical_match_score, eligibility_score, interest_score, "
        "compensation_score, timing_location_score, application_effort_score, risk_flags, "
        "missing_requirements, ranking_reason, tailoring_strategy.\n\n"
        f"Context JSON:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _job_context(parsed_job: ParsedJob) -> dict[str, Any]:
    payload = parsed_job.model_dump()
    role = payload.get("role") or {}
    if isinstance(role, dict):
        for key in ("description_raw", "description_clean"):
            value = role.get(key)
            if isinstance(value, str):
                role[key] = value[:1200]
    return payload


def _safe_profile_context(profile_data: dict[str, Any]) -> dict[str, Any]:
    profile = {
        key: profile_data[key]
        for key in SAFE_PROFILE_FIELDS
        if key in profile_data and profile_data[key] not in ("", None, [])
    }
    summary = profile.get("summary")
    if isinstance(summary, str):
        profile["summary"] = summary[:MAX_PROFILE_SUMMARY_CHARS]
    skills = profile.get("skills")
    if isinstance(skills, list):
        profile["skills"] = [
            skill[:MAX_PROFILE_SKILL_CHARS]
            for skill in skills[:MAX_PROFILE_SKILLS]
            if isinstance(skill, str) and skill
        ]
    return profile


def _allowed_evidence_context(evidence_items: list[EvidenceItem]) -> list[dict[str, Any]]:
    return [
        {
            "claim": item.claim[:MAX_EVIDENCE_CLAIM_CHARS],
            "skills": [
                skill[:MAX_EVIDENCE_SKILL_CHARS]
                for skill in (item.skills or [])[:MAX_EVIDENCE_SKILLS]
                if isinstance(skill, str) and skill
            ],
            "source_type": item.source_type,
        }
        for item in [item for item in evidence_items if item.allowed_for_resume][:MAX_ALLOWED_EVIDENCE_ITEMS]
    ]
