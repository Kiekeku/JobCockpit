"""Minimized employer-page and source-link contracts from the Eluta audit."""
import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsJob
from jobcockpit.ingestion.ats_handlers.apply import _job_from_generic_apply_page, AtsPostingContext
from jobcockpit.ingestion.posting_discovery import (
    PostingDiscoveryResolver, _official_posting_confidence, _official_candidate_url_matches_hint,
)
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint


def test_empty_location_label_skips_next_label():
    html = '''<h1>Maintenance Analytics Developer</h1><div>Location:</div>
    <div>Experience Level:</div><div>Apply Now</div><h2>Job Description</h2>
    <div>Location:</div><div>Fort Saskatchewan, AB, CAN</div>
    <div>Experience Level:</div><div>Internship</div>'''
    job = _job_from_generic_apply_page('https://jobs.example.test/job/123', html, AtsPostingContext(), None)
    assert 'Location: Fort Saskatchewan, AB, CAN' in job.observed_raw_text
    assert 'Location: Experience Level:' not in job.observed_raw_text


@pytest.mark.parametrize('actual,accepted', [('8-12', True), ('8–12', True), ('4-8', False), ('8', False)])
def test_matching_duration_range_is_not_missing_duration(actual, accepted):
    hint = ExternalPostingHint(source_url='', source_kind='eluta', company='Acme',
        title='Maintenance Analytics Developer', term='8-12 month term')
    observed = f'Title: Maintenance Analytics Developer\nCompany: Acme\nAn exciting co-op opportunity awaits for an {actual} month term.'
    assert (_official_posting_confidence(hint, observed) >= .8) is accepted


def test_school_return_alternatives_do_not_override_work_term():
    hint = ExternalPostingHint(source_url='', source_kind='eluta', company='Acme',
        title='Software Developer I - 4 months internship / Co-op (Winter 2027)',
        term='Start Date: 2027-01-01\nDuration: 4 months')
    observed = f'Title: {hint.title}\nCompany: Acme\nAvailable to work with us full-time, 35 hours per week, for the Winter 2027 Term (January to April)\nMust be an active student during your internship and/or returning to school in next Summer or Fall 2027 following your internship'
    assert _official_posting_confidence(hint, observed) >= .8


@pytest.mark.parametrize('company,host,accepted', [
    ('Intact Financial', 'careers.intactfc.com', True),
    ('Intact Financial', 'intactfc.wd3.myworkdayjobs.com', True),
    ('Unrelated Insurance', 'careers.intactfc.com', False),
    ('Intact Financial', 'careers.intactfc.com.example.test', False),
])
def test_reviewed_intact_domain(company, host, accepted):
    hint = ExternalPostingHint(source_url='', source_kind='eluta', company=company)
    assert _official_candidate_url_matches_hint(f'https://{host}/en-US/intactfc/job/City/Developer_R12345', hint) is accepted


@pytest.mark.parametrize('seed', ['https://www.eluta.ca/spl/software-intern-example', 'https://careers.acme.com/jobs/123'])
@pytest.mark.parametrize('search_configured', [False, True])
def test_saved_links_verified_before_search(seed, search_configured):
    seen = []
    class Apply:
        def resolve(self, url, context):
            seen.append(url)
            return AtsJob(source_url='https://careers.acme.com/jobs/123', title='Software Intern',
                raw_text='Title: Software Intern\nCompany: Acme\nBuild software for Acme.', has_posting_body=True)
    class Search:
        def search(self, *args):
            pytest.fail('A verified saved link should resolve before web search')
    resolver = PostingDiscoveryResolver(apply_resolver=Apply(), search_client=Search() if search_configured else None,
        allow_aggregator_fallback=False)
    hint = ExternalPostingHint(source_url=seed, source_kind='eluta', company='Acme', title='Software Intern',
        candidate_urls=(seed,))
    result = resolver.resolve(hint)
    assert result.status == 'resolved'
    assert seen == [seed]


def test_saved_link_is_only_a_candidate():
    class Apply:
        def resolve(self, url, context):
            return AtsJob(source_url='https://careers.unrelated.com/jobs/123', title='Software Intern',
                raw_text='Title: Software Intern\nCompany: Unrelated\nBuild software.', has_posting_body=True)
    hint = ExternalPostingHint(source_url='https://www.eluta.ca/spl/intern-example', source_kind='eluta',
        company='Acme', title='Software Intern', candidate_urls=('https://careers.unrelated.com/jobs/123',))
    result = PostingDiscoveryResolver(apply_resolver=Apply(), allow_aggregator_fallback=False).resolve(hint)
    assert result.status != 'resolved'
