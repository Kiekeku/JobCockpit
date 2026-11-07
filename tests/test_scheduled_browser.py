import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion import scheduled_browser
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, PostingResolution
from jobcockpit.ingestion.scheduled_browser import ScheduledInvisiblePlaywrightResolver
from jobcockpit.ingestion.status_reconciliation import _rendered_resolver_for_remaining_budget


def _fake_browser_worker(connection, settings, hint, method):
    if os.name != "nt":
        os.setsid()
    connection.recv()
    # A real descendant simulates the browser driver. Its heartbeat must stop
    # after success, a deadline, or cancellation from the coordinator thread.
    heartbeat = Path(settings.artifact_root) / "heartbeat"
    subprocess.Popen([
        sys.executable, "-c",
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.02)",
        str(heartbeat),
    ])
    while not heartbeat.exists():
        time.sleep(.01)
    if hint.source_kind == "crash":
        os._exit(1)
    if hint.source_kind == "success":
        connection.send(PostingResolution(status="not_found", error=method))
    connection.recv()


def _probe_spawned_tls_worker(connection, settings, hint, method):
    import ssl
    from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver

    loader = ssl.SSLContext.load_verify_locations
    ssl.SSLContext.load_verify_locations = getattr(loader, "__wrapped__", loader)

    class Resolver:
        def load_page(self, _hint):
            assert getattr(ssl.SSLContext.load_verify_locations, "_jobcockpit_certifi_memory", False)
            return PostingResolution(status="not_found", error="tls_installed_in_spawned_worker")

    InvisiblePlaywrightRenderedPageResolver.from_settings = lambda *args, **kwargs: Resolver()
    scheduled_browser._render_in_worker(connection, settings, hint, method)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TLS workaround")
def test_fresh_spawn_installs_tls_before_browser_network_work(monkeypatch):
    monkeypatch.setattr(scheduled_browser, "_render_in_worker", _probe_spawned_tls_worker)
    resolver = ScheduledInvisiblePlaywrightResolver(Settings(), time.monotonic() + 20)
    result = resolver.load_page(ExternalPostingHint(source_url="https://example.com/job", source_kind="generic"))
    assert result.error == "tls_installed_in_spawned_worker"
    assert not resolver._process.is_alive()


@pytest.mark.parametrize("mode", ["success", "deadline", "cancel", "crash"])
def test_scheduled_worker_cleans_real_process_tree(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(scheduled_browser, "_render_in_worker", _fake_browser_worker)
    resolver = ScheduledInvisiblePlaywrightResolver(
        Settings(artifact_root=tmp_path), time.monotonic() + 20,
    )
    results = []
    worker = threading.Thread(target=lambda: results.append(resolver.load_page(
        ExternalPostingHint(source_url="https://example.com/job", source_kind=mode),
    )))
    worker.start()
    heartbeat = tmp_path / "heartbeat"
    try:
        ready_deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < ready_deadline:
            time.sleep(.02)
        assert heartbeat.exists()
        if mode == "cancel":
            resolver.cancel_active_pages()
        elif mode == "deadline":
            resolver.deadline = time.monotonic()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not resolver._process.is_alive()
        before = heartbeat.read_text()
        time.sleep(.15)
        assert heartbeat.read_text() == before
        assert results[0].error == {
            "success": "load_page", "deadline": "playwright_timeout",
            "cancel": "playwright_failed: browser_render_cancelled",
            "crash": "invisible_playwright_worker_failed",
        }[mode]
    finally:
        resolver.cancel_active_pages()
        worker.join(timeout=5)


def test_cancel_before_start_never_launches_worker(monkeypatch):
    resolver = ScheduledInvisiblePlaywrightResolver(Settings(), time.monotonic() + 10)
    resolver.cancel_active_pages()
    result = resolver.resolve(ExternalPostingHint(source_url="https://example.com/job", source_kind="generic"))
    assert "cancelled" in result.error
    assert resolver._process is None


def test_scheduled_factory_preserves_firefox_budget_and_policy_settings():
    settings = Settings(
        posting_rendered_fallback_provider="invisible_playwright",
        posting_rendered_fallback_retries=3,
        posting_rendered_fallback_min_interval_seconds=15,
    )
    deadline = time.monotonic() + 10
    resolver = _rendered_resolver_for_remaining_budget(settings, deadline)
    assert isinstance(resolver, ScheduledInvisiblePlaywrightResolver)
    assert resolver.deadline == deadline
    assert resolver.settings.posting_rendered_fallback_retries == 0
    assert resolver.settings.posting_rendered_fallback_min_interval_seconds == 0
    assert resolver.settings.posting_rendered_fallback_timeout_seconds <= 10


def test_real_spawned_worker_preserves_private_network_block():
    resolver = ScheduledInvisiblePlaywrightResolver(
        Settings(posting_rendered_fallback_provider="invisible_playwright"), time.monotonic() + 20,
    )
    result = resolver.load_page(ExternalPostingHint(
        source_url="http://127.0.0.1:12345/job", source_kind="generic",
    ))
    assert result.status == "needs_review"
    assert "url_policy_blocked" in result.error
    assert not resolver._process.is_alive()


def test_child_uses_scheduled_policy_and_disables_preparation(monkeypatch):
    from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    captured = {}
    events = []
    monkeypatch.setattr(scheduled_browser, "install_windows_certifi_workaround",
                        lambda: events.append("tls"))

    class Resolver:
        def load_page(self, hint):
            return PostingResolution(status="not_found")

    def factory(settings, **options):
        assert events == ["tls"]
        captured.update(options)
        return Resolver()

    class Connection:
        def send(self, result):
            assert result["resolution"].status == "not_found"

        def recv(self):
            return None

    monkeypatch.setattr(InvisiblePlaywrightRenderedPageResolver, "from_settings", factory)
    if os.name != "nt":
        monkeypatch.setattr(os, "setsid", lambda: None)
    scheduled_browser._render_in_worker(
        Connection(), Settings(),
        ExternalPostingHint(source_url="https://example.com/job", source_kind="generic"), "load_page",
    )
    assert isinstance(captured["url_policy"], ScheduledRenderedUrlPolicy)
    assert captured["url_policy"].allowed_localhosts == set()
    assert captured["auto_prepare"] is False


@pytest.mark.parametrize('grant,enabled,site,preparation_fails,expected', [
    (True, True, 'indeed', False, ['load', 'prepare', 'load']),
    (False, True, 'indeed', False, ['load']),
    (True, False, 'indeed', False, ['load']),
    (True, True, 'linkedin', False, ['load']),
    (True, True, 'indeed', True, ['load', 'prepare']),
])
def test_scheduled_indeed_recovery_retries_with_same_guarded_resolver(monkeypatch, grant, enabled, site, preparation_fails, expected):
    from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
    from jobcockpit.ingestion.posting_discovery import _PlaywrightRenderedError
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    events = []
    class Resolver:
        prepare_timeout_seconds = 120
        def load_page(self, hint):
            events.append('load')
            return PostingResolution(status='resolved' if 'prepare' in events else 'needs_review',
                                     error='' if 'prepare' in events else 'playwright_failed: blocked_rendered_page')
        def _prepare_session(self, hint, *, started_at):
            assert self.prepare_timeout_seconds <= 60
            events.append('prepare')
            if preparation_fails:
                raise _PlaywrightRenderedError('invisible_session_preparation_needs_review')

    def factory(settings, **options):
        assert isinstance(options['url_policy'], ScheduledRenderedUrlPolicy)
        assert options['auto_prepare'] is False
        return Resolver()

    messages = []
    responses = iter(['start', grant, None])
    class Connection:
        def send(self, value):
            messages.append(value)
        def recv(self):
            return next(responses)

    monkeypatch.setattr(InvisiblePlaywrightRenderedPageResolver, 'from_settings', factory)
    monkeypatch.setattr(scheduled_browser, 'install_windows_certifi_workaround', lambda: None)
    if os.name != 'nt':
        monkeypatch.setattr(os, 'setsid', lambda: None)
    url = 'https://ca.indeed.com/viewjob?jk=0000000000000001' if site == 'indeed' else 'https://www.linkedin.com/jobs/view/1234567890'
    scheduled_browser._render_in_worker(Connection(), Settings(invisible_playwright_auto_prepare=enabled),
                                       ExternalPostingHint(source_url=url, source_kind=site), 'load_page')
    assert events == expected
    assert (messages[-1]['resolution'].status == 'resolved') is (len(expected) == 3)
    if enabled and site == 'indeed':
        assert messages[0] == {'prepare_site': 'indeed'}
    else:
        assert len(messages) == 1


def test_session_recovery_requires_completion_and_is_deadline_bounded():
    from jobcockpit.ingestion.scheduled_browser import ScheduledSessionRecovery

    budget = ScheduledSessionRecovery()
    assert not budget.claim('linkedin', time.monotonic() + 100)
    assert not budget.claim('indeed', time.monotonic() - 1)
    assert budget.claim('indeed', time.monotonic() + 100)
    assert not budget.claim('indeed', time.monotonic() + 100)
    assert ScheduledSessionRecovery().claim('indeed', time.monotonic() + 100)


def test_session_recovery_allows_one_later_attempt_after_completion_and_cooldown():
    # October 1: 64 Indeed pages loaded before five later verification walls.
    now = [100.0]
    budget = scheduled_browser.ScheduledSessionRecovery(clock=lambda: now[0])
    assert budget.claim('indeed', now[0] + 120)
    now[0] += 600
    assert not budget.claim('indeed', now[0] + 120)  # The first worker is still active.
    budget.complete()
    now[0] += 299
    assert not budget.claim('indeed', now[0] + 120)
    now[0] += 1
    assert budget.claim('indeed', now[0] + 120)
    budget.complete()
    now[0] += 600
    assert not budget.claim('indeed', now[0] + 120)  # Hard cap, even after cooldown.


def test_recovery_denied_near_deadline_does_not_consume_a_grant():
    now = [100.0]
    budget = scheduled_browser.ScheduledSessionRecovery(clock=lambda: now[0])
    assert not budget.claim('indeed', now[0] + 4)
    assert not budget.claim('indeed', now[0] + 89)
    assert budget.claim('indeed', now[0] + 120)


def test_concurrent_recovery_requests_share_the_two_attempt_limit():
    from concurrent.futures import ThreadPoolExecutor

    now = [100.0]
    budget = scheduled_browser.ScheduledSessionRecovery(clock=lambda: now[0])
    for _ in range(2):
        with ThreadPoolExecutor(max_workers=8) as workers:
            grants = list(workers.map(lambda _: budget.claim('indeed', now[0] + 120), range(16)))
        assert sum(grants) == 1
        budget.complete()
        now[0] += 300
    assert not budget.claim('indeed', now[0] + 120)


def _fake_recovery_worker(connection, settings, hint, method):
    if os.name != 'nt':
        os.setsid()
    connection.recv()
    connection.send({'prepare_site': 'indeed'})
    allowed = connection.recv()
    if hint.source_kind == 'crash':
        os._exit(1)
    connection.send({'resolution': PostingResolution(status='needs_review', error=str(allowed)), 'signals': []})
    connection.recv()


def test_spawned_workers_share_cooldown_and_two_grant_limit(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from collections import Counter
    from jobcockpit.ingestion.browser_health import _recent_attempts

    now = [time.monotonic()]
    budget = scheduled_browser.ScheduledSessionRecovery(clock=lambda: now[0])
    monkeypatch.setattr(scheduled_browser, '_render_in_worker', _fake_recovery_worker)
    results = []
    for index in range(4):
        if index >= 2:
            now[0] += 300
        resolver = ScheduledInvisiblePlaywrightResolver(
            Settings(artifact_root=tmp_path, browser_health_enabled=True), now[0] + 120,
        )
        resolver.session_recovery = budget
        results.append(resolver.load_page(ExternalPostingHint(
            source_url='https://ca.indeed.com/viewjob?jk=0000000000000001', source_kind='indeed')).error)
        assert not resolver._process.is_alive()
    assert results == ['True', 'False', 'True', 'False']
    rows, _, incomplete = _recent_attempts(tmp_path, datetime.now(UTC))
    assert not incomplete
    assert Counter(tuple(row['session_recovery']) for row in rows) == {
        ('preparation_granted', 'recheck_inconclusive'): 2,
        ('preparation_denied_cooldown',): 1,
        ('preparation_denied_limit',): 1,
    }


def test_crashed_recovery_worker_releases_grant_after_cleanup_and_records_interruption(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from jobcockpit.ingestion.browser_health import _recent_attempts

    now = [time.monotonic()]
    budget = scheduled_browser.ScheduledSessionRecovery(clock=lambda: now[0])
    monkeypatch.setattr(scheduled_browser, '_render_in_worker', _fake_recovery_worker)
    resolver = ScheduledInvisiblePlaywrightResolver(
        Settings(artifact_root=tmp_path, browser_health_enabled=True), now[0] + 120,
    )
    resolver.session_recovery = budget
    result = resolver.load_page(ExternalPostingHint(
        source_url='https://ca.indeed.com/viewjob?jk=0000000000000001', source_kind='crash'))
    assert result.error == 'invisible_playwright_worker_failed'
    assert not resolver._process.is_alive()
    assert not budget.claim('indeed', now[0] + 120)
    now[0] += 300
    assert budget.claim('indeed', now[0] + 120)
    rows, _, incomplete = _recent_attempts(tmp_path, datetime.now(UTC))
    assert not incomplete
    assert rows[0]['session_recovery'] == ['preparation_granted', 'recovery_interrupted']


@pytest.mark.parametrize("clears_after", [None, 2])
@pytest.mark.parametrize("site", ["indeed", "linkedin"])
def test_scheduled_auth_wall_wait_is_bounded_and_allows_passive_recovery(
    tmp_path, monkeypatch, allow_public_dns, site, clears_after,
):
    from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
    from test_invisible_browser import BODY, browser_resolver
    from test_posting_discovery import FakePage

    # The September 29 scan spent ~195 seconds each on 24 Indeed challenges
    # and five LinkedIn walls with the interactive wait configured to 180s.
    source = (
        "https://ca.indeed.com/viewjob?jk=0000000000000001" if site == "indeed"
        else "https://www.linkedin.com/jobs/view/1234567890"
    )
    allow_public_dns({"ca.indeed.com", "www.linkedin.com"})

    class WallPage(FakePage):
        elapsed = 0.0

        def wait_for_timeout(self, milliseconds):
            self.elapsed += milliseconds / 1000
            if clears_after is not None and self.elapsed >= clears_after:
                self._title = "Platform Engineer - Acme"
                self._body_text = BODY

    page = WallPage(
        source,
        "Just a moment..." if site == "indeed" else "Sign In | LinkedIn",
        "Verify you are human" if site == "indeed" else "Sign in to continue\nEmail or phone",
    )
    fake, context, launches = browser_resolver(tmp_path, page)
    settings = Settings(
        posting_rendered_fallback_provider="invisible_playwright",
        playwright_auth_wall_wait_seconds=180,
        playwright_auth_wall_settle_seconds=0,
    )
    scheduled = _rendered_resolver_for_remaining_budget(settings, time.monotonic() + 7200)
    original_factory = InvisiblePlaywrightRenderedPageResolver.from_settings

    def factory(value, **options):
        return original_factory(
            value, **options, invisible_factory=fake.invisible_factory,
            user_data_dir=tmp_path / "profile", clock=lambda: page.elapsed,
        )

    results = []

    class Connection:
        def send(self, value):
            if 'resolution' in value:
                results.append(value["resolution"])

        def recv(self):
            return None

    monkeypatch.setattr(InvisiblePlaywrightRenderedPageResolver, "from_settings", factory)
    monkeypatch.setattr(scheduled_browser, "install_windows_certifi_workaround", lambda: None)
    if os.name != "nt":
        monkeypatch.setattr(os, "setsid", lambda: None)
    scheduled_browser._render_in_worker(
        Connection(), scheduled.settings,
        ExternalPostingHint(source_url=source, source_kind=site), "load_page",
    )

    assert page.elapsed <= 10
    assert len(page.goto_calls) == 1
    assert len(launches) == 2  # One browser launch and its context exit.
    assert context.closed
    assert settings.playwright_auth_wall_wait_seconds == 180
    if clears_after is None:
        assert results[0].status == "needs_review"
        assert results[0].posting is None
        assert results[0].error == "playwright_failed: auth_wall_unresolved"
    else:
        assert results[0].status == "resolved"
        assert "Build internal platforms" in results[0].posting.raw_text
