from jobcockpit.ingestion.url_hosts import hostname_matches_domain, url_matches_domain


def test_hostname_matches_domain_accepts_exact_and_subdomain():
    assert hostname_matches_domain("linkedin.com", "linkedin.com")
    assert hostname_matches_domain("www.linkedin.com", "linkedin.com")
    assert hostname_matches_domain("jobs.www.linkedin.com", "linkedin.com")


def test_hostname_matches_domain_rejects_suffix_lookalikes():
    assert not hostname_matches_domain("notlinkedin.com", "linkedin.com")
    assert not hostname_matches_domain("evil-linkedin.com", "linkedin.com")
    assert not hostname_matches_domain("linkedin.com.evil.test", "linkedin.com")


def test_url_matches_domain_uses_parsed_hostname():
    assert url_matches_domain("https://www.linkedin.com/jobs/view/123", "linkedin.com")
    assert not url_matches_domain("https://notlinkedin.com/authwall", "linkedin.com")
