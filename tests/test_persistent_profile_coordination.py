from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from jobcockpit.ingestion import posting_discovery as discovery
from jobcockpit.ingestion.url_policy import UrlPolicyResult


def _profile_launch_probe(profile, started, entered, release):
    """Exercise the real resolver up to its injected browser launch boundary."""
    class Browser:
        def launch_persistent_context(self, **kwargs):
            assert Path(kwargs["user_data_dir"]).resolve() == Path(profile).resolve()
            entered.set()
            assert release.wait(15)
            raise RuntimeError("probe launch failed")

    resolver = discovery.PlaywrightProfileRenderedPageResolver(
        user_data_dir=profile,
        url_policy=SimpleNamespace(check=lambda url: UrlPolicyResult(True, "probe")),
        playwright_factory=lambda: nullcontext(SimpleNamespace(chromium=Browser())),
    )
    started.set()
    return resolver.resolve(discovery.ExternalPostingHint(
        source_url="https://ca.indeed.com/viewjob?jk=profileprobe", source_kind="indeed",
    ))


def _profile_probe_child(profile, started, entered, release):
    # No browser or network is opened. File locking and the resolver are real.
    discovery._enter_pinned_browser_proxy = lambda *args: None
    result = _profile_launch_probe(profile, started, entered, release)
    assert result.status == "needs_review"
    assert "probe launch failed" in result.error


def test_profile_launches_are_serialized_between_threads_and_release_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_enter_pinned_browser_proxy", lambda *args: None)
    first_started, first_entered, first_release = Event(), Event(), Event()
    second_started, second_entered, second_release = Event(), Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_profile_launch_probe, tmp_path / "profile",
                                first_started, first_entered, first_release)
        assert first_entered.wait(10)
        second = executor.submit(_profile_launch_probe, tmp_path / "profile",
                                 second_started, second_entered, second_release)
        try:
            assert second_started.wait(10)
            assert not second_entered.wait(0.4)
            first_release.set()
            assert second_entered.wait(10)
        finally:
            first_release.set()
            second_release.set()
        assert "probe launch failed" in first.result().error
        assert "probe launch failed" in second.result().error


@pytest.mark.parametrize("mode", ["same_profile", "different_profiles", "owner_crash"])
def test_profile_coordination_across_processes(tmp_path, mode):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    first_started, first_entered, first_release = (context.Event() for _ in range(3))
    second_started, second_entered, second_release = (context.Event() for _ in range(3))
    profile = tmp_path / "profile"
    other_profile = tmp_path / "other-profile" if mode == "different_profiles" else profile
    first = context.Process(target=_profile_probe_child, args=(profile, first_started, first_entered, first_release))
    second = context.Process(target=_profile_probe_child, args=(other_profile, second_started, second_entered, second_release))
    try:
        first.start()
        assert first_entered.wait(15)
        second.start()
        assert second_started.wait(15)
        if mode == "different_profiles":
            assert second_entered.wait(10)
        else:
            assert not second_entered.wait(0.4)
        if mode == "owner_crash":
            first.terminate()
            first.join(timeout=10)
        else:
            first_release.set()
        assert second_entered.wait(10)
        second_release.set()
        for process in (first, second):
            process.join(timeout=15)
            if process is not first or mode != "owner_crash":
                assert process.exitcode == 0
    finally:
        # Terminating a process can abandon a multiprocessing Event's internal
        # semaphore. Never touch that shared event after its owner was killed.
        if first.exitcode is None:
            first_release.set()
        second_release.set()
        for process in (first, second):
            if process.pid and process.is_alive():
                process.terminate()
                process.join(timeout=5)
