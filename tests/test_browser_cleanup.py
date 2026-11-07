from __future__ import annotations

import asyncio
import threading
import time
import pytest


from jobcockpit.ingestion.browser_cleanup import (
    BrowserCleanupHandoffError,
    BrowserCleanupJournal,
    BrowserPageCleanupLease,
    BrowserCleanupState,
    BrowserPageCleanupSupervisor,
    capture_new_browser_page_cleanup_lease,
    capture_browser_page_cleanup_lease,
    cleanup_owned_browser_context,
    retain_pending_browser_cleanup,
    close_owned_browser_context,
    close_owned_page,
)


def test_new_target_fallback_captures_only_one_unambiguous_page(monkeypatch) -> None:
    from jobcockpit.ingestion import browser_cleanup

    monkeypatch.setattr(
        browser_cleanup,
        "_browser_page_targets",
        lambda *_args, **_kwargs: {
            "existing": "https://example.com/existing",
            "new-target": "https://example.com/job",
        },
    )
    monkeypatch.setattr(
        browser_cleanup,
        "_http_target_closer",
        lambda _endpoint: (lambda _target_id: True),
    )

    lease = capture_new_browser_page_cleanup_lease(
        "http://127.0.0.1:9222",
        {"existing"},
        expected_url="https://example.com/job",
    )

    assert lease is not None
    assert lease.target_id == "new-target"


def test_new_target_fallback_never_guesses_between_concurrent_tabs(monkeypatch) -> None:
    from jobcockpit.ingestion import browser_cleanup

    monkeypatch.setattr(
        browser_cleanup,
        "_browser_page_targets",
        lambda *_args, **_kwargs: {
            "existing": "https://example.com/existing",
            "our-candidate": "https://example.com/job",
            "unrelated-user-tab": "https://example.com/job",
        },
    )
    monkeypatch.setattr(
        browser_cleanup,
        "_http_target_closer",
        lambda _endpoint: (lambda _target_id: True),
    )

    assert (
        capture_new_browser_page_cleanup_lease(
            "http://127.0.0.1:9222",
            {"existing"},
            expected_url="https://example.com/job",
        )
        is None
    )


def test_new_target_fallback_refuses_unidentifiable_blank_page(monkeypatch) -> None:
    from jobcockpit.ingestion import browser_cleanup

    monkeypatch.setattr(
        browser_cleanup,
        "_browser_page_targets",
        lambda *_args, **_kwargs: {"new-target": "about:blank"},
    )
    monkeypatch.setattr(
        browser_cleanup,
        "_http_target_closer",
        lambda _endpoint: (lambda _target_id: True),
    )

    assert (
        capture_new_browser_page_cleanup_lease(
            "http://127.0.0.1:9222",
            set(),
            expected_url="about:blank",
        )
        is None
    )


def test_context_cleanup_watchdog_closes_owned_target_when_context_close_blocks() -> None:
    release_close = threading.Event()
    emergency_called = threading.Event()

    class Page:
        closed = False

        def close(self, **_kwargs) -> None:
            self.closed = True

    page = Page()

    class Session:
        def send(self, command: str, _params=None):
            assert command == "Target.getTargetInfo"
            return {"targetInfo": {"targetId": "owned-target"}}

        def detach(self) -> None:
            return None

    class Context:
        pages = [page]
        closed = False

        def new_cdp_session(self, target_page):
            assert target_page is page
            return Session()

        def close(self) -> None:
            assert release_close.wait(timeout=2)
            self.closed = True

    def emergency_close(target_id: str) -> bool:
        assert target_id == "owned-target"
        page.closed = True
        emergency_called.set()
        release_close.set()
        return True

    started_at = time.monotonic()
    closed = close_owned_browser_context(
        Context(),
        cleanup_timeout_seconds=0.2,
        emergency_target_closer=emergency_close,
    )

    assert closed is True
    assert emergency_called.is_set()
    assert page.closed is True
    assert time.monotonic() - started_at < 1


def test_context_cleanup_never_closes_pages_outside_the_owned_context() -> None:
    class Page:
        def __init__(self) -> None:
            self.closed = False

        def close(self, **_kwargs) -> None:
            self.closed = True

    owned_page = Page()
    user_page = Page()

    class Context:
        pages = [owned_page]

        def close(self) -> None:
            owned_page.close()

    assert close_owned_browser_context(Context()) is True
    assert owned_page.closed is True
    assert user_page.closed is False


def test_page_cleanup_returns_when_close_never_unblocks() -> None:
    close_started = threading.Event()
    never_release = threading.Event()
    result: list[bool] = []

    class Page:
        def close(self, **_kwargs) -> None:
            close_started.set()
            never_release.wait()

    page = Page()

    class Session:
        def send(self, command: str, _params=None):
            assert command == "Target.getTargetInfo"
            return {"targetInfo": {"targetId": "stuck-target"}}

        def detach(self) -> None:
            return None

    class Context:
        def new_cdp_session(self, target_page):
            assert target_page is page
            return Session()

    cleanup = threading.Thread(
        target=lambda: result.append(
                close_owned_page(
                    Context(),
                    page,
                    cleanup_timeout_seconds=0.2,
                    emergency_target_closer=lambda target_id: target_id == "stuck-target",
                )
        ),
        daemon=True,
    )
    cleanup.start()
    cleanup.join(timeout=1.0)

    assert close_started.is_set()
    assert cleanup.is_alive() is False
    assert result == [True]


def test_context_target_fallback_does_not_claim_context_was_disposed() -> None:
    never_release = threading.Event()

    class Page:
        def close(self, **_kwargs) -> None:
            return None

    page = Page()

    class Session:
        def send(self, command: str, _params=None):
            assert command == "Target.getTargetInfo"
            return {"targetInfo": {"targetId": "owned-target"}}

        def detach(self) -> None:
            return None

    class Context:
        pages = [page]

        def new_cdp_session(self, target_page):
            assert target_page is page
            return Session()

        def close(self) -> None:
            never_release.wait()

    started_at = time.monotonic()
    closed = close_owned_browser_context(
        Context(),
        cleanup_timeout_seconds=0.05,
        emergency_target_closer=lambda target_id: target_id == "owned-target",
    )

    assert closed is False
    assert time.monotonic() - started_at < 0.25


def test_playwright_cleanup_stays_on_owner_thread_and_has_hard_timeout() -> None:
    owner_thread = threading.get_ident()
    close_threads: list[int] = []

    class Impl:
        async def close(self, **_kwargs) -> None:
            close_threads.append(threading.get_ident())
            await asyncio.Event().wait()

    class Page:
        _impl_obj = Impl()

        def _sync(self, operation):
            assert threading.get_ident() == owner_thread
            return asyncio.run(operation)

        def close(self, **_kwargs) -> None:
            raise AssertionError("public close must not move to a helper thread")

    started_at = time.monotonic()
    closed = close_owned_page(
        object(),
        Page(),
        cleanup_timeout_seconds=0.05,
    )

    assert closed is False
    assert close_threads == [owner_thread]
    assert time.monotonic() - started_at < 0.2


def test_context_cleanup_is_bounded_when_no_target_can_be_discovered() -> None:
    never_release = threading.Event()

    class Context:
        pages = []

        def close(self) -> None:
            never_release.wait()

    started_at = time.monotonic()
    closed = close_owned_browser_context(
        Context(),
        cleanup_timeout_seconds=0.05,
    )

    assert closed is False
    assert time.monotonic() - started_at < 0.2


def test_stuck_target_discovery_does_not_prevent_page_close() -> None:
    never_release = threading.Event()

    class Page:
        closed = False

        def close(self, **_kwargs) -> None:
            self.closed = True

    page = Page()

    class Context:
        def new_cdp_session(self, _page):
            never_release.wait()

    started_at = time.monotonic()
    closed = close_owned_page(
        Context(),
        page,
        cleanup_timeout_seconds=0.05,
        emergency_target_closer=lambda _target_id: True,
    )

    assert closed is True
    assert page.closed is True
    assert time.monotonic() - started_at < 0.2


def test_timed_out_playwright_context_is_not_retried_or_reported_disposed() -> None:
    owner_thread = threading.get_ident()

    class Impl:
        def __init__(self) -> None:
            self.close_calls = 0
            self.closing = False
            self.disposed = False

        async def close(self, **_kwargs) -> None:
            self.close_calls += 1
            self.closing = True
            await asyncio.Event().wait()
            self.disposed = True

    impl = Impl()

    class Context:
        pages = []
        _impl_obj = impl

        def _sync(self, operation):
            assert threading.get_ident() == owner_thread
            return asyncio.run(operation)

        def close(self) -> None:
            raise AssertionError("public close must not move to a helper thread")

    result = cleanup_owned_browser_context(
        Context(),
        cleanup_timeout_seconds=0.05,
    )

    assert result.state is BrowserCleanupState.UNCONFIRMED
    assert result.context_disposed is False
    assert impl.close_calls == 1
    assert impl.closing is True
    assert impl.disposed is False


def test_context_fallback_requires_every_owned_target_to_close() -> None:
    never_release = threading.Event()

    class Page:
        def __init__(self, target_id: str) -> None:
            self.target_id = target_id

        def close(self, **_kwargs) -> None:
            raise RuntimeError("force target fallback")

    pages = [Page("first"), Page("second")]

    class Session:
        def __init__(self, page: Page) -> None:
            self.page = page

        def send(self, command: str, _params=None):
            assert command == "Target.getTargetInfo"
            return {"targetInfo": {"targetId": self.page.target_id}}

        def detach(self) -> None:
            return None

    class Context:
        def __init__(self) -> None:
            self.pages = pages

        def new_cdp_session(self, page: Page):
            return Session(page)

        def close(self) -> None:
            never_release.wait()

    closed_targets: list[str] = []

    def close_only_first(target_id: str) -> bool:
        closed_targets.append(target_id)
        return target_id == "first"

    result = cleanup_owned_browser_context(
        Context(),
        cleanup_timeout_seconds=0.1,
        emergency_target_closer=close_only_first,
    )

    assert result.state is BrowserCleanupState.UNCONFIRMED
    assert result.all_tabs_closed is False
    assert set(closed_targets) == {"first", "second"}


def test_page_cleanup_lease_closes_only_the_captured_target() -> None:
    closed_targets: list[str] = []

    class Session:
        def send(self, command: str, _params=None):
            assert command == "Target.getTargetInfo"
            return {"targetInfo": {"targetId": "leased-target"}}

        def detach(self) -> None:
            return None

    class Context:
        def new_cdp_session(self, _page):
            return Session()

    lease = capture_browser_page_cleanup_lease(
        Context(),
        object(),
        emergency_target_closer=lambda target_id: closed_targets.append(target_id) is None,
    )

    assert lease is not None
    assert lease.close(timeout_seconds=0.1) is True
    assert closed_targets == ["leased-target"]


def test_context_cleanup_can_skip_pages_already_owned_by_another_cleanup_state() -> None:
    class Page:
        def close(self, **_kwargs) -> None:
            raise AssertionError("page cleanup must remain with its existing owner")

    class Context:
        pages = [Page()]

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    context = Context()
    result = cleanup_owned_browser_context(
        context,
        skip_page_cleanup=True,
    )

    assert result.state is BrowserCleanupState.CONTEXT_DISPOSED
    assert context.closed is True


def test_cleanup_supervisor_retains_a_lease_until_it_is_confirmed_closed() -> None:
    class FlakyLease:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 0.5
            self.close_calls += 1
            return self.close_calls >= 3

    lease = FlakyLease()
    supervisor = BrowserPageCleanupSupervisor(retry_interval_seconds=0.01)

    assert supervisor.adopt((lease,)) == 1
    assert supervisor.wait_until_idle(timeout_seconds=1) is True
    assert lease.close_calls == 3
    assert supervisor.pending_count == 0


def test_cleanup_journal_recovers_a_target_in_a_new_supervisor(tmp_path) -> None:
    journal = BrowserCleanupJournal(tmp_path / "browser-cleanup")
    journal.persist(
        BrowserPageCleanupLease(
            "target-123",
            lambda _target_id: False,
            "http://127.0.0.1:9222",
        )
    )
    closed: list[tuple[str, str]] = []

    def closer_factory(endpoint_url: str):
        def close_target(target_id: str) -> bool:
            closed.append((endpoint_url, target_id))
            return True

        return close_target

    supervisor = BrowserPageCleanupSupervisor(
        retry_interval_seconds=0.01,
        journal=journal,
        recovery_closer_factory=closer_factory,
    )

    assert supervisor.recover_pending() == 1
    assert supervisor.wait_until_idle(timeout_seconds=1) is True
    assert closed == [("http://127.0.0.1:9222", "target-123")]
    assert list(journal.root.glob("*.json")) == []


def test_cleanup_journal_ignores_valid_json_with_the_wrong_shape(tmp_path) -> None:
    journal = BrowserCleanupJournal(tmp_path / "browser-cleanup")
    journal.root.mkdir(parents=True)
    (journal.root / "unexpected.json").write_text("[]", encoding="utf-8")

    assert journal.load_entries() == ()


def test_durable_handoff_keeps_owner_state_when_lease_is_not_recoverable(
    tmp_path,
) -> None:
    lease = BrowserPageCleanupLease("target-123", lambda _target_id: False)

    class Owner:
        def __init__(self) -> None:
            self.leases = [lease]
            self.release_calls = 0

        def pending_cleanup_leases(self):
            return tuple(self.leases)

        def release_pending_cleanup_leases(self, leases):
            self.release_calls += 1
            for item in leases:
                self.leases.remove(item)
            return tuple(leases)

    owner = Owner()
    supervisor = BrowserPageCleanupSupervisor(
        journal=BrowserCleanupJournal(tmp_path / "browser-cleanup")
    )

    with pytest.raises(
        BrowserCleanupHandoffError,
        match="not durably recoverable",
    ):
        retain_pending_browser_cleanup(owner, supervisor=supervisor)

    assert owner.leases == [lease]
    assert owner.release_calls == 0


def test_durable_handoff_persists_before_releasing_owner(tmp_path) -> None:
    lease = BrowserPageCleanupLease(
        "target-123",
        lambda _target_id: False,
        "http://127.0.0.1:9222",
    )

    class Owner:
        def __init__(self) -> None:
            self.leases = [lease]

        def pending_cleanup_leases(self):
            return tuple(self.leases)

        def release_pending_cleanup_leases(self, leases):
            assert list(journal.root.glob("*.json"))
            self.leases.clear()
            return tuple(leases)

    journal = BrowserCleanupJournal(tmp_path / "browser-cleanup")
    supervisor = BrowserPageCleanupSupervisor(journal=journal)
    owner = Owner()

    assert retain_pending_browser_cleanup(owner, supervisor=supervisor) == 1
    assert owner.leases == []


def test_supervisor_owns_registered_target_before_close_is_requested(tmp_path) -> None:
    journal = BrowserCleanupJournal(tmp_path / "browser-cleanup")
    attempts: list[str] = []

    def close_target(target_id: str) -> bool:
        attempts.append(target_id)
        return len(attempts) >= 2

    lease = BrowserPageCleanupLease(
        "owned-before-navigation",
        close_target,
        "http://127.0.0.1:9222",
    )
    supervisor = BrowserPageCleanupSupervisor(
        retry_interval_seconds=0.01,
        journal=journal,
    )

    supervisor.register(lease)
    time.sleep(0.03)

    assert attempts == []
    assert list(journal.root.glob("*.json"))
    assert supervisor.request_close((lease,)) == 1
    assert supervisor.wait_until_idle(timeout_seconds=1.0)
    assert attempts == ["owned-before-navigation", "owned-before-navigation"]
    assert list(journal.root.glob("*.json")) == []


def test_close_request_does_not_requeue_a_concurrently_confirmed_target(tmp_path) -> None:
    journal = BrowserCleanupJournal(tmp_path / "browser-cleanup")
    supervisor = BrowserPageCleanupSupervisor(journal=journal)
    lease = BrowserPageCleanupLease(
        "concurrently-closed",
        lambda _target_id: True,
        "http://127.0.0.1:9222",
    )
    original_register = supervisor.register

    def register_then_confirm(target_lease):
        key = original_register(target_lease)
        assert supervisor.confirm_closed(target_lease)
        return key

    supervisor.register = register_then_confirm

    assert supervisor.request_close((lease,)) == 0
    assert supervisor.pending_count == 0
    assert supervisor.wait_until_idle(timeout_seconds=0.1)
    assert list(journal.root.glob("*.json")) == []
