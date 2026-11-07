from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import NoneType
from typing import get_args

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from jobcockpit.ingestion.codex_cli import json_object_from_output, run_codex_prompt
from jobcockpit.ingestion.parser_models import ParsedJob
from jobcockpit.ingestion.posting_scope import _posting_content_scope
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text


Runner = Callable[..., subprocess.CompletedProcess]


class CodexCliPostingParser:
    def __init__(
        self,
        *,
        command: str = "codex",
        runner: Runner = subprocess.run,
        timeout_seconds: int = 60,
        max_input_chars: int = 12000,
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

    def parse(
        self,
        raw_text: str,
        source_url: str | None = None,
        baseline: ParsedJob | None = None,
    ) -> ParsedJob | None:
        self.last_error = ""
        raw_text = clean_aggregator_posting_text(raw_text, source_url=source_url)
        posting_scope = _posting_content_scope(raw_text)
        prompt = _codex_parse_prompt(
            posting_scope.text,
            source_url=source_url,
            baseline=baseline,
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
            operation="posting_parse",
        )
        if result.error:
            self.last_error = result.error
            return None
        payload = json_object_from_output(result.stdout)
        if payload is None:
            self.last_error = "codex_cli_invalid_json"
            return None
        payload = _normalize_provider_payload_shape(
            payload,
            baseline=baseline,
            raw_text=raw_text,
            description_text=posting_scope.text,
        )
        try:
            parsed = ParsedJob.model_validate(payload)
        except (TypeError, ValidationError, ValueError) as error:
            self.last_error = f"codex_cli_invalid_parsed_job: {error}"
            return None
        return _normalize_codex_parsed_job(
            parsed,
            raw_text=raw_text,
            description_text=posting_scope.text,
            source_url=source_url,
        )


def _normalize_provider_payload_shape(
    payload: dict,
    *,
    baseline: ParsedJob | None,
    raw_text: str,
    description_text: str,
) -> dict:
    normalized = deepcopy(payload)
    _restore_nonnullable_provider_nulls(
        normalized,
        ParsedJob,
        baseline.model_dump() if baseline else None,
    )
    location = normalized.get("location")
    if isinstance(location, dict) and isinstance(location.get("remote_eligibility"), bool):
        location["remote_eligibility"] = (
            baseline.location.remote_eligibility if baseline else None
        )
    application = normalized.get("application")
    if isinstance(application, dict) and isinstance(application.get("upload_targets"), list):
        application["upload_targets"] = [
            target for target in application["upload_targets"] if isinstance(target, dict)
        ]
    role = normalized.get("role")
    if isinstance(role, dict):
        title = role.get("title")
        if not role.get("job_title") and isinstance(title, str) and title.strip():
            role["job_title"] = title.strip()
        if not role.get("normalized_title"):
            baseline_title = baseline.role.normalized_title if baseline else None
            role["normalized_title"] = baseline_title or role.get("job_title")
        if not isinstance(role.get("description_raw"), str):
            role["description_raw"] = raw_text
        if not isinstance(role.get("description_clean"), str):
            role["description_clean"] = description_text

        capabilities = role.get("capabilities")
        if isinstance(capabilities, list):
            normalized_capabilities = []
            for capability in capabilities:
                if isinstance(capability, dict):
                    name = capability.get("name")
                    normalized_capabilities.append(
                        name.strip() if isinstance(name, str) and name.strip() else capability
                    )
                else:
                    normalized_capabilities.append(capability)
            role["capabilities"] = normalized_capabilities

        evidence_items = role.get("evidence")
        if isinstance(evidence_items, list):
            for evidence in evidence_items:
                if not isinstance(evidence, dict):
                    continue
                evidence["kind"] = {
                    "qualification": "requirement",
                    "eligibility": "requirement",
                    "environment": "context",
                    "company_context": "context",
                }.get(evidence.get("kind"), evidence.get("kind"))
                evidence["modality"] = {
                    "mandatory": "required",
                    "expected": "required",
                    "optional": "non_requirement",
                    "prohibited": "non_requirement",
                    "unknown": "descriptive",
                }.get(evidence.get("modality"), evidence.get("modality"))
                if not isinstance(evidence.get("text"), str):
                    exact_text = evidence.get("exact_text")
                    if isinstance(exact_text, str) and exact_text.strip():
                        evidence["text"] = exact_text.strip()
                for collection_name in ("claims", "alternative_claims"):
                    claims = evidence.get(collection_name)
                    if not isinstance(claims, list):
                        continue
                    for claim in claims:
                        if not isinstance(claim, dict):
                            continue
                        if not isinstance(claim.get("text"), str):
                            for alias in ("grounded_text", "evidence_text", "source_text"):
                                value = claim.get(alias)
                                if isinstance(value, str) and value.strip():
                                    claim["text"] = value.strip()
                                    break
                        decision_trace = claim.get("decision_trace")
                        if isinstance(decision_trace, str):
                            claim["decision_trace"] = (
                                [decision_trace.strip()] if decision_trace.strip() else []
                            )
                        conditions = claim.get("conditions")
                        if not claim.get("condition_text") and isinstance(conditions, list):
                            condition_texts = [
                                str(condition.get("text") or "").strip()
                                for condition in conditions
                                if isinstance(condition, dict)
                                and str(condition.get("text") or "").strip()
                            ]
                            if condition_texts:
                                claim["condition_text"] = "; ".join(condition_texts)
                        coordination = claim.get("coordination")
                        if isinstance(coordination, dict):
                            coordination_type = coordination.get("type")
                            claim["coordination"] = (
                                coordination_type
                                if coordination_type
                                in {"single", "all_of", "any_of", "alternative"}
                                else "single"
                            )

    timing = normalized.get("employment_timing")
    if isinstance(timing, dict) and timing.get("timing_type") is None:
        baseline_timing_type = baseline.employment_timing.timing_type if baseline else None
        timing["timing_type"] = baseline_timing_type or "unknown"
    return normalized


def _restore_nonnullable_provider_nulls(
    payload: dict,
    model_type: type[BaseModel],
    baseline: dict | None,
) -> None:
    """Resolve provider `null` values against the validated parser contract."""
    for name, field in model_type.model_fields.items():
        if name not in payload:
            continue
        value = payload[name]
        annotation = field.annotation
        nested_model = (
            annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel)
            else None
        )
        baseline_value = baseline.get(name) if isinstance(baseline, dict) else None
        if isinstance(value, dict) and nested_model is not None:
            _restore_nonnullable_provider_nulls(
                value,
                nested_model,
                baseline_value if isinstance(baseline_value, dict) else None,
            )
            continue
        if value is not None or NoneType in get_args(annotation):
            continue
        if baseline_value is not None:
            payload[name] = deepcopy(baseline_value)
            continue
        default = field.get_default(call_default_factory=True)
        if default is not PydanticUndefined:
            payload[name] = deepcopy(default)


def _codex_parse_prompt(
    raw_text: str,
    *,
    source_url: str | None,
    baseline: ParsedJob | None,
    max_input_chars: int,
) -> str:
    baseline_json = _baseline_prompt_json(baseline) if baseline else None
    bounded_text = raw_text[:max_input_chars]
    return (
        "Parse this public job posting into a JSON object matching the provided schema. "
        "Return only JSON, no markdown. Do not invent facts; use null, empty lists, "
        "missing_fields, ambiguous_fields, inferred_fields, and needs_manual_review when uncertain. "
        "Preserve the source_url and include evidence-backed dates, deadlines, compensation, "
        "location, eligibility, requirements, responsibilities, capability_domains, and capabilities when present. "
        "For role meaning, preserve exact source evidence and decompose each evidence span into proposition-level "
        "role.evidence[].claims. Each claim must use kind responsibility, qualification, eligibility, environment, or "
        "company_context; modality mandatory, expected, preferred, optional, descriptive, prohibited, or unknown; "
        "owner applicant, role, employer, team, or unknown; grounded text and source offsets; polarity; conditions; "
        "condition_type geographic, temporal, applicant_subset, contingency, schedule, or unknown; coordination; "
        "a stable claim_id; a required text field containing the exact grounded source text; optional normalized_text "
        "for consumer display; and decision_trace as an array of concise strings. Keep exact "
        "grounded text unchanged when normalized_text is present. Use text (not exact_text) for the enclosing evidence "
        "text. Claim coordination must be the scalar single, all_of, any_of, or alternative, never an object. "
        "The enclosing role.evidence object uses a different "
        "legacy vocabulary: kind must be responsibility, requirement, or context; modality must be required, preferred, "
        "descriptive, or non_requirement. Keep those enclosing fields compatible with the strongest "
        "claim so older consumers continue to work. Use source_section responsibilities, qualifications, "
        "preferred_qualifications, role_overview, capabilities, company, or unsectioned; exact text and evidence_text copied from "
        "the posting; optional evidence_start/evidence_end offsets; confidence from 0 to 1; and entities containing "
        "only capability names explicitly supported by that clause, never durations, "
        "qualification nouns, modality words, or whole requirement phrases. Do not treat technologies mentioned only in company "
        "descriptions, historical systems, benefits, or team background as candidate capabilities. Preserve coordinated "
        "alternatives such as degree or equivalent experience in one evidence clause instead of flattening them into "
        "multiple mandatory requirements. Preserve multiple modalities or conditions in separate claims under the "
        "same evidence span. Populate required_capabilities, preferred_capabilities, responsibility_capabilities, "
        "and environment_capabilities consistently with role.evidence claims. Use requirement_expression to preserve "
        "nested all_of, any_of, and alternative structure when scalar coordination would lose meaning. Give every "
        "expression node a stable node_id. Set a leaf claim_id only when its text and offsets exactly match that claim. "
        "For application documents, use only evidence-backed submission instructions. Normalize required "
        "documents to resume, cover_letter, transcript, portfolio, writing_sample, or references. "
        "Use null document fields when requirements are not stated, exclude optional documents, honor "
        "explicit not-required language, and mark contradictory instructions ambiguous. "
        "Map application or posting deadline labels such as Posting Close Date, Closing Date, "
        "Close Date, Application Deadline, or Apply By to application.application_deadline. "
        "Use capability_domains from this vocabulary when supported: software, cybersecurity, data, "
        "hardware_engineering, quality_assurance, product_management, project_management, "
        "business_analysis, finance, sales, marketing, operations, customer_support, design, "
        "research, hr_recruiting, legal_policy, and general_business. "
        "Use the parsed job title as a strong prior for capability domains, but use department, category, requirements, and body evidence as fallback when the title is generic. "
        "Populate tech_stack only for explicit software, data, cybersecurity, hardware, lab/test, QA/testing, engineering tools, frameworks, languages, or methods. Treat hardware, electrical, optical, lab/test, ECAD, CAD, circuit, and measurement tools or methods as tech stack for hardware roles. "
        "Extract current employment academic terms such as Fall 2026, Winter 2027, "
        "Summer 2026, Spring 2027, and numeric or written-out co-op durations such as "
        "4mo, 8 month, four-month, or eight months from the title, details, or description "
        "into employment_timing.raw_label, timing_type, start_date, end_date, duration_months, "
        "and schedule_type. Do not use education, application, interview, recruiting, historical "
        "experience, or related-job sidebar terms as employment timing. Convert written-out "
        "durations to integer month counts. Preserve exact "
        "labeled dates such as 'Start Date of Internship: Tuesday, September 8th, 2026', "
        "'End Date of Internship: Friday, April 30th, 2027', 'Desired Start Date: 09/01/2026', "
        "and 'Position End Date: 12/23/2026' as YYYY-MM-DD start_date/end_date; "
        "also treat phrases such as '12-month term commencing on August 31, 2026' as fixed-term "
        "employment timing with duration_months=12 and start_date=2026-08-31. "
        "treat numeric ATS dates as MM/DD/YYYY unless the value is impossible, and ignore "
        "application or posting window labels such as Application Start Date/Application End Date, "
        "Posting Start Date, Open Date, Close Date, Closing Date, or Requisition Date for employment_timing. If no employment start date, end date, duration, or academic term is available, use an explicit Posted, Date Posted, Posted On, or date posted value as employment_timing.start_date with timing_type posted_date_fallback and add employment_timing.start_date to inferred_fields. "
        "When an exact September start date is present, set raw_label to Fall <year>. "
        "Use YYYY-MM-DD dates; for Canadian co-op terms, Fall starts in September, Winter in January, "
        "and Spring/Summer in May when no exact date is stated.\n\n"
        f"Source URL: {source_url or ''}\n"
        f"Baseline heuristic parse JSON: {json.dumps(baseline_json, ensure_ascii=False)}\n"
        "Required top-level keys: source, employer, role, employment_timing, location, eligibility, "
        "compensation, application, fit_analysis, data_quality.\n"
        "The role object must use job_title and normalized_title (never title), include "
        "description_raw and description_clean strings, and represent capabilities as an array of "
        "strings, never objects. role.evidence must be an array of evidence objects, never prose. "
        "employment_timing.timing_type must be a non-null string; use "
        "unknown when timing is not stated.\n"
        "Job posting text:\n"
        f"{bounded_text}"
    )


def _baseline_prompt_json(baseline: ParsedJob) -> dict:
    payload = baseline.model_dump()
    role = payload.get("role")
    if isinstance(role, dict):
        role.pop("description_raw", None)
        unresolved_evidence = [
            item
            for item in role.get("evidence") or []
            if item.get("quality_flags") or item.get("alternative_claims")
        ]
        if unresolved_evidence:
            role["evidence"] = unresolved_evidence
        else:
            role.pop("evidence", None)
        description_clean = role.get("description_clean")
        if isinstance(description_clean, str) and len(description_clean) > 1000:
            role["description_clean"] = description_clean[:1000]
    return payload


def _normalize_codex_parsed_job(
    parsed: ParsedJob,
    *,
    raw_text: str,
    description_text: str,
    source_url: str | None,
) -> ParsedJob:
    normalized = parsed.model_copy(deep=True)
    if source_url:
        normalized.source.source_url = source_url
    if not normalized.role.description_raw:
        normalized.role.description_raw = raw_text
    if not normalized.role.description_clean:
        normalized.role.description_clean = description_text
    if source_url and not normalized.application.external_apply_url:
        normalized.application.external_apply_url = source_url
    return normalized
