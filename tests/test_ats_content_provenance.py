from types import SimpleNamespace

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.base import AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.posting_batch import ats_job_needs_source_fetch
from jobcockpit.ingestion.posting_links import PostingLinkContext, process_posting_link
from jobcockpit.ingestion.posting_discovery import PostingResolution

BOARDS = [
    ('ats_eluta', 'https://www.eluta.ca/spl/example-123'),
    ('ats_hanzilla', 'https://jobs.hanzilla.co/jobs/example-123/'),
    ('ats_rankalpha', 'https://rankalpha.io/jobs/123'),
    ('ats_csjobs', 'https://www.linkedin.com/jobs/view/123456'),
]
OFFICIAL = 'http://localhost:8001/ats/generic/acme/jobs/intern'


@pytest.mark.parametrize('source_type,board', BOARDS)
@pytest.mark.parametrize('outcome', ['success', 'failed', 'empty', 'shell'])
def test_board_snapshot_fetches_official_even_with_board_archive(source_type, board, outcome):
    archive = SourceArchive(board, board, 200, 'text/html', b'Board snapshot')
    job = AtsJob(OFFICIAL, 'Software Intern\nCompany: Acme\nBoard description',
                 portal_source_url=board, source_archive=archive, content_source_url=board)
    calls, imported = [], []

    class Fetcher:
        def resolve(self, url, context=None):
            calls.append(url)
            if outcome == 'failed':
                raise AtsProviderError(502, 'Apply page request failed')
            return AtsJob(OFFICIAL, '' if outcome == 'empty' else 'Software Intern\nOfficial description',
                          has_posting_body=outcome != 'shell')

    def importer(request, *args):
        imported.append(request)
        return SimpleNamespace(payload={'id': 'example'}, created=True)

    result = process_posting_link(
        PostingLinkContext(source_type, job.source_url, job.raw_text, trusted_posting=True,
                           portal_source_url=board, source_archive=archive,
                           fetch_source_archive=ats_job_needs_source_fetch(job), content_source_url=board),
        db=object(), storage=object(), settings=Settings(ats_mock_port=8001, posting_rendered_fallback_provider='disabled'),
        posting_fetcher=Fetcher(), job_importer=importer,
    )
    assert calls == [OFFICIAL]
    assert result.ingestion_status == 'imported'
    request = imported[0]
    assert request.portal_source_url == board
    if outcome == 'success':
        assert request.raw_text == 'Software Intern\nOfficial description'
        assert request.source_url == OFFICIAL
        assert request.source_archive is None  # Never attach the board archive to official text.
        assert request.source_resolution['kind'] == 'direct'
    else:
        assert request.raw_text == job.raw_text
        assert request.source_url == board
        assert request.source_archive == archive
        assert request.source_resolution['kind'] == 'aggregator_fallback'
        assert request.source_resolution['official_discovery_status'] == 'failed'
        assert 'official_url' not in request.source_resolution


def test_official_adapter_snapshot_is_not_fetched_twice():
    assert not ats_job_needs_source_fetch(AtsJob(OFFICIAL, 'Official posting'))


@pytest.mark.parametrize('outcome', ['not_found', 'exception', 'disabled'])
def test_board_only_url_attempts_discovery_then_preserves_snapshot(allow_public_dns, outcome):
    board = 'https://www.linkedin.com/jobs/view/123456'
    allow_public_dns({'www.linkedin.com'})
    calls, imported = [], []

    class Resolver:
        def resolve(self, hint):
            calls.append(hint.source_url)
            if outcome == 'exception':
                raise RuntimeError('Search unavailable')
            return PostingResolution(status='not_found', error='official_posting_not_found')

    def importer(request, *args):
        imported.append(request)
        return SimpleNamespace(payload={'id': 'example'}, created=True)

    job = AtsJob(board, 'Software Intern\nCompany: Acme\nBoard description')
    result = process_posting_link(
        PostingLinkContext('ats_csjobs', board, job.raw_text, trusted_posting=True,
                           fetch_source_archive=ats_job_needs_source_fetch(job)),
        db=object(), storage=object(), settings=Settings(
            posting_rendered_fallback_provider='disabled', posting_resolver_provider='disabled',
        ),
        posting_resolver=None if outcome == 'disabled' else Resolver(), job_importer=importer,
    )
    assert calls == ([] if outcome == 'disabled' else [board])
    assert result.ingestion_status == 'imported'
    assert imported[0].raw_text == job.raw_text
    assert imported[0].source_resolution['kind'] == 'aggregator_fallback'
    if outcome == 'not_found':
        assert imported[0].source_resolution['official_discovery_status'] == 'not_found'

def test_oracle_public_sections_and_date_range_are_preserved():
    from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
        _job_from_detail, parse_oracle_candidate_experience_url,
    )
    from jobcockpit.ingestion.parser import parse_job_text

    url = 'https://example.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123'
    # Minimized structure observed in the user-reported official Oracle posting.
    payload = {'items': [{
        'Title': 'Software Tools Co-op/Intern',
        'ExternalDescriptionStr': '<p>Duration: 4 Months</p><p>Date: Jan 4th, 2027, to Apr 16th, 2027</p>',
        'ExternalQualificationsStr': '<p>You must have:</p><ul><li>Strong programming skills</li></ul>',
        'ExternalResponsibilitiesStr': '<p>Responsibilities:</p><ul><li>Write scripts to automate test tools</li></ul>',
        'InternalQualificationsStr': '<p>Internal-only guidance</p>',
    }]}
    archive = SourceArchive(url, url, 200, 'application/json', b'{}')
    job = _job_from_detail(parse_oracle_candidate_experience_url(url), payload, url, archive)
    assert 'Strong programming skills' in job.raw_text
    assert 'Write scripts to automate test tools' in job.raw_text
    assert 'Internal-only guidance' not in job.raw_text
    timing = parse_job_text(job.raw_text, url).employment_timing
    assert timing.start_date == '2027-01-04'
    assert timing.end_date == '2027-04-16'
