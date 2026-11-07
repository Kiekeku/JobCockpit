from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
from jobcockpit.ingestion.invisible_session import (
    PreparationUrlPolicy, VerificationProgress, advance_verification, verification_site, session_page_error,
)
from jobcockpit.ingestion.posting_discovery import (
    PlaywrightProfileRenderedPageResolver, _PlaywrightRenderedError,
)
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, ResolvedExternalPosting
from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult


INDEED = "https://ca.indeed.com/viewjob?jk=0000000000000001"
ELUTA = "https://www.eluta.ca/spl/example-job"


@pytest.mark.parametrize("url,title,error", [
    (INDEED, "Authenticating...", "auth_wall_unresolved"),
    ("https://www.indeed.com/account/login", "Just a moment...", ""),
    ("https://www.indeed.com/account/login", "Sign in", "invisible_session_sign_in_required"),
    ("https://secure.indeed.com/auth;jsessionid=fixture", "Error 400 Bad Request", "rendered_http_error"),
    ("https://careers.example/job", "Error 400 Bad Request", "rendered_http_error"),
    (INDEED, "Software Developer Intern", ""),
])
def test_observed_indeed_authentication_pages_are_not_prepared_postings(url, title, error):
    # Minimized from the September 22 live Indeed browser diagnostics.
    assert session_page_error(url, title) == error


def control():
    result = Mock()
    result.count.return_value = 1
    result.is_visible.return_value = True
    return result


def verification_page(site):
    checkbox = control()
    checkbox.get_attribute.return_value = "false"
    url = INDEED if site == "indeed" else ELUTA
    frame_url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/test" if site == "indeed" else (
        "https://www.recaptcha.net/recaptcha/api2/anchor?size=normal")
    frame = SimpleNamespace(url=frame_url, get_by_role=Mock(return_value=checkbox))
    page = Mock(url=url, frames=[frame])
    page.title.return_value = "Just a moment..." if site == "indeed" else "User Verification"
    form = control()
    destination = control()
    destination.input_value.return_value = url
    form.locator.return_value = destination
    continuation = control()
    form.get_by_role.return_value = continuation
    page.locator.return_value = form
    return page, checkbox, continuation


@pytest.mark.parametrize("url", [
    "https://indeed.com.evil.example/viewjob", "http://ca.indeed.com/viewjob",
    "https://www.eluta.ca:444/spl/job", "https://other.eluta.ca/spl/job",
    INDEED.replace("://", "://user@"), "https://ca.indeed.com:bad/viewjob",
    "https://careers.example/job",
])
def test_unknown_origins_do_not_enable_preparation(url):
    assert not verification_site(url)


def test_indeed_only_clicks_one_observed_widget_and_never_application_controls():
    page, checkbox, continuation = verification_page("indeed")
    progress = VerificationProgress()
    advance_verification(page, INDEED, progress, timeout_ms=100)
    advance_verification(page, INDEED, progress, timeout_ms=100)
    checkbox.click.assert_called_once_with(timeout=100)
    continuation.click.assert_not_called()
    page.get_by_role.assert_not_called()


@pytest.mark.parametrize("change", ["foreign_frame", "job_page", "foreign_page", "ambiguous"])
def test_indeed_does_not_click_unrecognized_or_ambiguous_controls(change):
    page, checkbox, _ = verification_page("indeed")
    if change == "foreign_frame":
        page.frames[0].url = "https://challenges.cloudflare.com.evil.example/widget"
    elif change == "job_page":
        page.title.return_value = "Platform Engineer - Acme"
    elif change == "foreign_page":
        page.url = "https://careers.example/apply"
    else:
        checkbox.count.return_value = 2
    advance_verification(page, INDEED, VerificationProgress(), timeout_ms=100)
    checkbox.click.assert_not_called()


def test_eluta_continues_only_verified_matching_sandbox_form_once():
    page, checkbox, continuation = verification_page("eluta")
    progress = VerificationProgress()
    advance_verification(page, ELUTA, progress, timeout_ms=100)
    advance_verification(page, ELUTA, progress, timeout_ms=100)
    continuation.click.assert_not_called()
    checkbox.get_attribute.return_value = "true"
    advance_verification(page, ELUTA, progress, timeout_ms=100)
    advance_verification(page, ELUTA, progress, timeout_ms=100)
    checkbox.click.assert_called_once_with(timeout=100)
    continuation.click.assert_called_once_with(timeout=100)
    page.locator.assert_called_with('form[action="/sandbox"]')
    page.locator.return_value.get_by_role.assert_called_with("button", name="Continue", exact=True)


def test_eluta_does_not_submit_another_destination():
    page, checkbox, continuation = verification_page("eluta")
    page.locator.return_value.locator.return_value.input_value.return_value = "https://www.eluta.ca/apply"
    advance_verification(page, ELUTA, VerificationProgress(), timeout_ms=100)
    checkbox.click.assert_not_called()
    continuation.click.assert_not_called()


def test_preparation_policy_intersects_scope_and_retains_dns_validation():
    delegate = Mock(spec=UrlFetchPolicy)
    delegate.check.return_value = UrlPolicyResult(False, "private_network_blocked")
    policy = PreparationUrlPolicy(INDEED, delegate)
    assert not policy.check("https://challenges.cloudflare.com/widget").allowed
    delegate.check.assert_called_once_with("https://challenges.cloudflare.com/widget")
    assert not policy.check_browser_final_url(INDEED, "https://arbitrary.example/").allowed
    assert not policy.check_browser_final_url(INDEED, "https://challenges.cloudflare.com:8443/").allowed
    delegate.check_browser_final_url = Mock(return_value=UrlPolicyResult(False, "caller_scope_blocked"))
    assert policy.check_browser_final_url(INDEED, INDEED).reason == "caller_scope_blocked"


def test_blocked_guarded_load_prepares_then_reloads_with_same_profile(tmp_path, monkeypatch):
    expected = ResolvedExternalPosting(source_url=INDEED, raw_text="Actual posting", confidence=.9)
    render = Mock(side_effect=[_PlaywrightRenderedError("auth_wall_unresolved"), expected])
    monkeypatch.setattr(PlaywrightProfileRenderedPageResolver, "_render", render)
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    resolver._run_preparation = Mock()
    hint = ExternalPostingHint(source_url=INDEED, source_kind="indeed")
    result = resolver._render(hint)
    assert result.raw_text == expected.raw_text
    assert "automatic session preparation" in result.evidence
    assert render.call_count == 2
    resolver._run_preparation.assert_called_once_with(hint, tmp_path / "indeed" / "seed-42")


def test_failed_preparation_is_bounded_across_resolvers(tmp_path):
    hint = ExternalPostingHint(source_url=ELUTA, source_kind="eluta")
    first = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    first._run_preparation = Mock(side_effect=_PlaywrightRenderedError("invisible_session_preparation_needs_review"))
    with pytest.raises(_PlaywrightRenderedError, match="needs_review"):
        first._prepare_session(hint, started_at=0)
    second = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    second._run_preparation = Mock()
    with pytest.raises(_PlaywrightRenderedError, match="cooldown"):
        second._prepare_session(hint, started_at=0)
    second._run_preparation.assert_not_called()


@pytest.mark.parametrize("enabled,error,url", [
    (False, "auth_wall_unresolved", INDEED),
    (True, "rendered_url_policy_blocked: private_network_blocked", INDEED),
    (True, "auth_wall_unresolved", "https://careers.example/job"),
])
def test_preparation_never_recovers_a_policy_rejection_or_unsupported_site(tmp_path, monkeypatch, enabled, error, url):
    monkeypatch.setattr(PlaywrightProfileRenderedPageResolver, "_render", Mock(side_effect=_PlaywrightRenderedError(error)))
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path, auto_prepare=enabled)
    resolver._prepare_session = Mock()
    with pytest.raises(_PlaywrightRenderedError, match=error):
        resolver._render(ExternalPostingHint(source_url=url, source_kind="generic"))
    resolver._prepare_session.assert_not_called()


def test_profile_identity_is_stable_and_separate_by_site_and_seed(tmp_path):
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    indeed = resolver._profile_directory(ExternalPostingHint(source_url=INDEED, source_kind="indeed"))
    assert indeed != resolver._profile_directory(ExternalPostingHint(source_url=ELUTA, source_kind="eluta"))
    another = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path, seed=43)
    assert indeed != another._profile_directory(ExternalPostingHint(source_url=INDEED, source_kind="indeed"))


def test_recent_success_from_another_importer_is_reused(tmp_path):
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    hint = ExternalPostingHint(source_url=INDEED, source_kind="indeed")
    profile = resolver._profile_directory(hint)
    profile.mkdir(parents=True)
    resolver._write_preparation_state(profile, "ready")
    resolver._run_preparation = Mock()
    resolver._prepare_session(hint, started_at=0)
    resolver._run_preparation.assert_not_called()


def test_failed_guarded_recheck_cools_down_without_recursive_preparation(tmp_path, monkeypatch):
    hint = ExternalPostingHint(source_url=INDEED, source_kind="indeed")
    render = Mock(side_effect=_PlaywrightRenderedError("blocked_rendered_page"))
    monkeypatch.setattr(PlaywrightProfileRenderedPageResolver, "_render", render)
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path)
    resolver._run_preparation = Mock()
    with pytest.raises(_PlaywrightRenderedError, match="blocked_rendered_page"):
        resolver._render(hint)
    assert render.call_count == 2
    with pytest.raises(_PlaywrightRenderedError, match="cooldown"):
        resolver._render(hint)
    assert resolver._run_preparation.call_count == 1


def test_click_timeout_is_not_retried():
    page, checkbox, _ = verification_page("indeed")
    checkbox.click.side_effect = TimeoutError("unknown whether dispatched")
    progress = VerificationProgress()
    with pytest.raises(TimeoutError):
        advance_verification(page, INDEED, progress, timeout_ms=100)
    advance_verification(page, INDEED, progress, timeout_ms=100)
    assert checkbox.click.call_count == 1


@pytest.mark.parametrize("returned_search", ["original", "bootstrap"])
@pytest.mark.parametrize("authenticating_first", [False, True])
def test_eluta_search_prepares_safe_query_then_checks_original_identity(tmp_path, monkeypatch, returned_search, authenticating_first):
    import jobcockpit.ingestion.invisible_browser as integration

    # Observed in both saved Eluta search challenge pages: the destination is
    # decoded and truncated at the encoded ampersand in the field expression.
    source = 'https://www.eluta.ca/search?q=co-op+field%3A%22Computing+-+Software+%26+Web+Development%22&l=&qc='
    truncated_destination = 'https://www.eluta.ca/search?q=co-op field:"Computing - Software '
    bootstrap = "https://www.eluta.ca/search?q=co-op"
    page, checkbox, continuation = verification_page("eluta")
    elapsed = [0.0]
    content = ["Authenticating..." if authenticating_first else "User Verification"]

    def navigate(url, **_kwargs):
        if authenticating_first:
            page.title.return_value = "Authenticating..."
        page.url = "https://www.eluta.ca/sandbox"
        destination = truncated_destination if url == source else url
        page.locator.return_value.locator.return_value.input_value.return_value = destination

    def finish():
        page.url = bootstrap
        page.title.return_value = "Available co-op jobs"
        content[0] = "Available co-op jobs"

    def tick(ms):
        elapsed[0] += ms / 1000
        if page.title.return_value == "Authenticating...":
            page.title.return_value = "User Verification"
            content[0] = "User Verification"

    page.goto.side_effect = navigate
    page.wait_for_timeout.side_effect = tick
    checkbox.click.side_effect = lambda **_kwargs: setattr(checkbox.get_attribute, "return_value", "true")
    continuation.click.side_effect = lambda **_kwargs: finish()
    context = Mock()
    context.new_page.return_value = page
    guard = Mock()
    monkeypatch.setattr(integration, "_enter_pinned_browser_proxy", Mock())
    monkeypatch.setattr(integration, "_PlaywrightUrlPolicyGuard", Mock(return_value=guard))
    monkeypatch.setattr(integration, "_install_rendered_context_hardening", Mock())
    monkeypatch.setattr(integration, "_verify_rendered_page_hardening", Mock())
    monkeypatch.setattr(integration, "_raise_if_pinned_proxy_blocked", Mock())
    monkeypatch.setattr(integration, "close_owned_browser_context", Mock(return_value=True))
    monkeypatch.setattr(integration, "_safe_rendered_page_text", lambda *_args: content[0])
    policy = Mock(spec=UrlFetchPolicy)
    policy.check.return_value = UrlPolicyResult(True, "test_source")
    resolver = InvisiblePlaywrightRenderedPageResolver(
        user_data_dir=tmp_path, prepare_timeout_seconds=5,
        url_policy=policy, clock=lambda: elapsed[0],
    )
    resolver._launch_context = Mock(return_value=context)
    hint = ExternalPostingHint(
        source_url=source, source_kind="eluta", url_identity="path_and_query",
        identity_query_groups=(("q",),),
    )
    posting = ResolvedExternalPosting(
        source_url=source if returned_search == "original" else bootstrap,
        raw_text="Search results", confidence=.9,
    )
    render = Mock(side_effect=[_PlaywrightRenderedError("auth_wall_unresolved"), posting])
    monkeypatch.setattr(PlaywrightProfileRenderedPageResolver, "_render", render)

    result = resolver.load_page(hint)

    assert result.status == ("resolved" if returned_search == "original" else "needs_review"), result.error
    if returned_search == "bootstrap":
        assert result.error == "playwright_url_mismatch"
    page.goto.assert_called_once_with(bootstrap, wait_until="domcontentloaded")
    checkbox.click.assert_called_once()
    continuation.click.assert_called_once()
    assert [call.args[0] for call in render.call_args_list] == [hint, hint]
    assert guard.ensure_final_url_allowed.call_count >= 2
    context.new_page.assert_called_once()
