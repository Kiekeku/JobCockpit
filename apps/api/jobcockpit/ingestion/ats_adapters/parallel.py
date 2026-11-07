"""Bounded detail fetching; listing traversal and accounting stay on the caller."""
from contextlib import closing

from jobcockpit.ingestion.concurrency import link_request_slot, prepare_links


def fetch_details(request, candidates):
    """Candidates yield (URL, zero-argument fetch) after listing filters.

    Reserve the link budget before dispatch. Keep successful results in listing
    order while allowing independent failures and out-of-order completion.
    A fetch returning None was cancelled by a provider-wide stop condition.
    """
    def admitted():
        for index, (url, fetch) in enumerate(candidates):
            if not request.try_begin_link_attempt():
                break
            yield index, url, fetch

    def prepare(item):
        _, url, fetch = item
        with link_request_slot(url):
            return fetch()

    jobs = {}
    failures = {}
    with closing(prepare_links(admitted(), prepare, concurrency=request.link_concurrency)) as results:
        for (index, url, _), result in results:
            if isinstance(result, Exception):
                failures[index] = (url, result)
            elif result is not None:
                jobs[index] = result
            else:
                request.stats.links_seen_count -= 1
    for index in sorted(failures):
        request.stats.record_link_failure(*failures[index])
    return [jobs[index] for index in sorted(jobs)]
