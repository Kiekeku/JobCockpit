"""Minimized acquisition regressions from the October saved-posting status audit.

Golden availability cases cover the corresponding identity and marker failures.
The negative cases below verify the transport/identity contract of those fixes.
"""

from io import BytesIO
import threading

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jobcockpit.core.config import Settings
from jobcockpit.ingestion import status_reconciliation as sr
from jobcockpit.ingestion.ats_adapters.workday import parse_workday_url


def target_for(url, company="Acme", title="Platform Engineer"):
    candidate = sr.StatusUrlCandidate(url, "source", sr.posting_identity(url).source_kind)
    return sr.JobStatusTarget(
        "audit_fixture", "active", "url", url, company, title, "Canada",
        (candidate,), 0, 0, None,
    )


def direct(monkeypatch, target, response, final_url=None):
    monkeypatch.setattr(
        sr, "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url or target.source_url),
    )
    return sr._direct_check(target, target.candidates[0], Settings())


@pytest.mark.parametrize("visible", ["", "Recruitment"])
def test_observed_script_shell_requests_render(monkeypatch, visible):
    target = target_for("https://careers.example.test/jobs/123")
    result = direct(monkeypatch, target, httpx.Response(
        200, text=f"<html><body>{visible}<script src='/app.js'></script></body></html>",
        headers={"Content-Type": "text/html"},
    ))
    assert isinstance(result, sr.RenderRequired)


@pytest.mark.parametrize("body,final_url", [
    ("<script src='/app.js'></script>", "https://careers.example.test/jobs/456"),
    ("Acme Platform Analyst<script src='/app.js'></script>", None),
    ("Access denied<script src='/app.js'></script>", None),
])
def test_render_fallback_preserves_identity_and_access_guards(monkeypatch, body, final_url):
    result = direct(monkeypatch, target_for("https://careers.example.test/jobs/123"),
                    httpx.Response(200, text=body, headers={"Content-Type": "text/html"}),
                    final_url)
    assert isinstance(result, sr.StatusCheckDecision) and result.result == "inconclusive"


def test_observed_oversized_html_uses_renderer_and_preserves_fallback(monkeypatch):
    target = target_for("https://careers.example.test/jobs/123")
    response = httpx.Response(200, headers={"Content-Type": "text/html"},
                              extensions={sr._RESPONSE_TOO_LARGE_EXTENSION: True})
    direct(monkeypatch, target, response)
    result = sr._direct_target_check(target, Settings(), {}, threading.Lock())
    assert isinstance(result, sr.RenderRequired)
    assert result.prior_decision.evidence_code == "response_too_large"


@pytest.mark.parametrize("content_type,extension", [
    ("application/pdf", sr._RESPONSE_TOO_LARGE_EXTENSION),
    ("text/html", sr._RESPONSE_UNSUPPORTED_ENCODING_EXTENSION),
])
def test_uninspectable_non_html_and_encoding_keep_transport_limits(
    monkeypatch, content_type, extension,
):
    result = direct(monkeypatch, target_for("https://careers.example.test/jobs/123"),
                    httpx.Response(200, headers={"Content-Type": content_type},
                                   extensions={extension: True}))
    assert isinstance(result, sr.StatusCheckDecision) and result.result == "inconclusive"


def test_observed_greenhouse_board_error_requires_complete_scoped_feed(monkeypatch):
    target = target_for("https://job-boards.greenhouse.io/acme/jobs/1234567")
    calls = []

    def fetch(url, *_args, **_kwargs):
        calls.append(url)
        if url == target.source_url:
            return (httpx.Response(200, text="<html>Current openings</html>"),
                    "https://job-boards.greenhouse.io/acme?error=true")
        assert url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        return httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}), url

    monkeypatch.setattr(sr, "_get_with_safe_redirects", fetch)
    result = sr._direct_check(target, target.candidates[0], Settings())
    assert result.result == "closed" and len(calls) == 2


@pytest.mark.parametrize("payload", [
    {"jobs": []}, {"jobs": [], "meta": {"total": 1}},
    {"jobs": [{"title": "Other job"}], "meta": {"total": 1}},
    {"jobs": [{"id": "bad"}], "meta": {"total": 1}},
])
def test_greenhouse_partial_or_malformed_feed_cannot_close(monkeypatch, payload):
    target = target_for("https://job-boards.greenhouse.io/acme/jobs/1234567")
    monkeypatch.setattr(sr, "_get_with_safe_redirects", lambda url, *_a, **_k: (
        httpx.Response(200, json=payload), url,
    ))
    result = sr._public_provider_feed_check(target, target.candidates[0], Settings())
    assert result is None or result.result == "inconclusive"


@pytest.mark.parametrize("final_url", [
    "https://job-boards.greenhouse.io/other?error=true",
    "https://job-boards.greenhouse.io/acme",
    "https://job-boards.greenhouse.io/acme/jobs/7654321?error=true",
])
def test_greenhouse_other_redirects_do_not_use_absence_as_closure(monkeypatch, final_url):
    result = direct(monkeypatch, target_for(
        "https://job-boards.greenhouse.io/acme/jobs/1234567"),
        httpx.Response(200, text="Current openings"), final_url)
    assert result.result == "inconclusive"


def test_observed_workday_selected_details_maps_to_job_endpoint():
    site = parse_workday_url(
        "https://example.wd3.myworkdayjobs.com/en-US/Careers/details/Canada/Engineer_JR1234567"
    )
    assert site.site == "Careers"
    assert site.external_path == "/job/Canada/Engineer_JR1234567"


def posting_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 700 Td (Acme Platform Engineer Apply now) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.compress_content_streams()
    content = BytesIO()
    writer.write(content)
    return content.getvalue()


def test_observed_pdf_detail_is_extracted_before_matching(monkeypatch):
    result = direct(monkeypatch, target_for("https://careers.example.test/jobs/123.pdf"),
                    httpx.Response(200, content=posting_pdf(),
                                   headers={"Content-Type": "application/pdf"}))
    assert result.result == "open"


def test_unreadable_pdf_is_not_a_job_identity_mismatch(monkeypatch):
    result = direct(monkeypatch, target_for("https://careers.example.test/jobs/123.pdf"),
                    httpx.Response(200, content=b"not a PDF",
                                   headers={"Content-Type": "application/pdf"}))
    assert result.result == "inconclusive" and result.evidence_code == "pdf_text_unavailable"


def test_alias_does_not_match_inside_another_word():
    assert not sr._identity_values_match("Northern Devices Inc. / NDI", "Engineer",
                                         "Engineer working on conditional systems")


@pytest.mark.parametrize("title,expected", [
    ("Platform Engineer", "open"), ("Payroll Administrator", "inconclusive"),
])
def test_greenhouse_present_id_keeps_title_check(monkeypatch, title, expected):
    target = target_for("https://job-boards.greenhouse.io/acme/jobs/1234567")
    monkeypatch.setattr(sr, "_get_with_safe_redirects", lambda url, *_a, **_k: (
        httpx.Response(200, json={"jobs": [{"id": 1234567, "title": title}],
                                 "meta": {"total": 1}}), url,
    ))
    result = sr._public_provider_feed_check(target, target.candidates[0], Settings())
    assert result.result == expected


def test_availability_corpus_projection_is_offline(monkeypatch):
    from jobcockpit.ingestion.parser_corpus import load_parser_corpus, run_parser_corpus

    def forbidden(*_args, **_kwargs):
        pytest.fail("Golden availability checks must not fetch real postings")

    monkeypatch.setattr(sr, "_get_with_safe_redirects", forbidden)
    corpus = load_parser_corpus()
    ids = {case.id for case in corpus.cases if case.availability_check}
    assert len(ids) == 8
    assert run_parser_corpus(corpus, case_ids=ids).passed


def test_availability_corpus_requires_observed_grounding():
    from jobcockpit.ingestion.parser_corpus import (
        ParserCorpusError, _validate_corpus_contract, load_parser_corpus,
    )

    corpus = load_parser_corpus()
    case = next(case for case in corpus.cases if case.availability_check)
    invalid = case.model_copy(update={"grounding": None})
    with pytest.raises(ParserCorpusError, match="not posting-grounded"):
        _validate_corpus_contract(corpus.model_copy(update={"cases": [invalid]}))
