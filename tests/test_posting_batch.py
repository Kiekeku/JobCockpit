from __future__ import annotations

from dataclasses import dataclass

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.base import AtsJob
from jobcockpit.ingestion import posting_batch
from jobcockpit.ingestion.posting_batch import (
    PostingBatchItem, prepare_posting_batch_item, persist_posting_batch_item,
)
from jobcockpit.ingestion.posting_links import PostingLinkClassification, PostingLinkProcessingResult


@dataclass(frozen=True)
class ExistingJob:
    payload: dict
    created: bool = False


def _fail(*args, **kwargs):
    raise AssertionError("unexpected preparation or duplicate lookup")


def _item(raw_text="Data Engineer\nPython"):
    return PostingBatchItem(
        source_type="ats_workday", source_url="https://jobs.example.com/data-engineer",
        raw_text=raw_text, trusted_posting=True, fetch_source_archive=False,
    )


def _prepare(item, **kwargs):
    return prepare_posting_batch_item(
        item, settings=Settings(), skip_interest_filter=False, skip_language_filter=False, **kwargs,
    )


def _persist(prepared, **kwargs):
    return persist_posting_batch_item(prepared, db=None, storage=None, settings=Settings(), **kwargs)


def test_ats_job_defaults_portal_source_url_to_source_url():
    job = AtsJob(source_url="https://jobs.example.com/postings/123", raw_text="Developer Intern")
    assert job.portal_source_url == job.source_url


def test_posting_batch_skips_interest_mismatch_before_processing(monkeypatch):
    monkeypatch.setattr(posting_batch, "prepare_posting_link", _fail)
    prepared = _prepare(_item("Accounting Intern\nExcel"), profile_data={
        "target_role_include_keywords": ["software"], "target_role_exclude_keywords": [],
    })
    result = _persist(prepared, duplicate_lookup=_fail)
    assert result.counts.interest_skipped == 1
    assert result.counts.created == result.counts.reused == 0
    assert result.processing_result.ingestion_status == "skipped_by_interest_filter"
    assert result.processing_result.error_message == "missing_include_keyword"
    assert result.interest_match.matched_include is None


def test_posting_batch_language_skip_wins_over_duplicate_shortcut(monkeypatch):
    monkeypatch.setattr(posting_batch, "prepare_posting_link", _fail)
    prepared = _prepare(
        _item("Data Engineer\nFrench required for this role."), precheck_language_filter=True,
        profile_data={"language_filter_enabled": True, "languages": [
            {"language": "English", "proficiency": "fluent"},
            {"language": "French", "proficiency": "none"},
        ]},
    )
    result = _persist(prepared, duplicate_lookup=_fail)
    assert result.counts.language_skipped == 1
    assert result.counts.reused == 0
    assert result.processing_result.error_message == "unmet_required_language: French"


def test_posting_batch_duplicate_shortcut_skips_fetch_and_does_not_refilter(monkeypatch):
    monkeypatch.setattr(posting_batch, "prepare_posting_link", _fail)
    prepared = _prepare(_item(), profile_data={}, precheck_language_filter=True, defer_source_fetch=True)
    monkeypatch.setattr(posting_batch, "match_job_text_interest", _fail)
    monkeypatch.setattr(posting_batch, "evaluate_language_requirements", _fail)
    result = _persist(prepared, duplicate_lookup=lambda _: ExistingJob({"id": "job_existing"}))
    assert result.counts.reused == 1
    assert result.processing_result.linked_job_id == "job_existing"
    assert result.processing_result.created is False


def test_posting_batch_prepares_disappeared_index_hit_and_counts_terminal_skip(monkeypatch):
    item = _item()
    prepared = _prepare(item, profile_data=None, defer_source_fetch=True)
    preparations = []

    def terminal_result(context, **kwargs):
        preparations.append(context.source_url)
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification("direct_posting", 1, "test"),
            ingestion_status="terminal_status_detected", original_url=context.source_url,
            resolved_url=context.source_url, canonical_url=context.source_url, detected_status="closed",
        )

    monkeypatch.setattr(posting_batch, "prepare_posting_link", terminal_result)
    result = _persist(
        prepared, duplicate_lookup=lambda _: None,
        prepare_missing=lambda item: _prepare(item, profile_data=None),
    )
    assert preparations == [item.source_url]
    assert result.counts.terminal_skipped == 1
    assert result.counts.failed == 0
