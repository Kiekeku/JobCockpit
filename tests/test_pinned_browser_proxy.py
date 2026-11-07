from __future__ import annotations

import base64
from contextlib import ExitStack
import socket
import threading
import time

import pytest

import jobcockpit.ingestion.pinned_browser_proxy as pinned_proxy_module
from jobcockpit.ingestion.pinned_browser_proxy import (
    _connect_target_url,
    _forward_request_head,
    _parse_request_head,
    _ProxyRequestError,
    PinnedBrowserProxy,
    _relay_bidirectionally,
    _read_request_head,
    PinnedBrowserProxyBlocked,
)
from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy, UrlPolicyResult


class _SequencedPolicy:
    def __init__(self, results: list[UrlPolicyResult]) -> None:
        self.results = list(results)
        self.checked_urls: list[str] = []

    def check(self, url: str) -> UrlPolicyResult:
        self.checked_urls.append(url)
        return self.results.pop(0)


def test_parallel_browser_pages_can_hold_asset_and_verification_connections():
    # The observed Eluta verification page loses its CAPTCHA assets at the old
    # process-wide limit of 16 CONNECT tunnels, even with just one browser.
    class Policy:
        def check(self, _url):
            return UrlPolicyResult(True, "public_hostname", "93.184.216.34")

    with ExitStack() as stack:
        def connect(*_args):
            relay, peer = socket.socketpair()
            stack.callback(peer.close)
            return relay

        proxies = [stack.enter_context(PinnedBrowserProxy(
            source_url="https://jobs.example.test/jobs/123", url_policy=Policy(),
            connector=connect,
        )) for _ in range(2)]
        clients = []
        try:
            for proxy in proxies:
                config = proxy.playwright_proxy
                host, port = config["server"].removeprefix("http://").split(":")
                credentials = base64.b64encode(
                    f"{config['username']}:{config['password']}".encode()
                ).decode()
                for _ in range(24):
                    client = socket.create_connection((host, int(port)), timeout=2)
                    clients.append(client)
                    client.settimeout(2)
                    client.sendall((
                        "CONNECT jobs.example.test:443 HTTP/1.1\r\n"
                        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
                    ).encode())
                    assert client.recv(4096).startswith(b"HTTP/1.1 200 Connection Established")
            assert [proxy.active_connection_count for proxy in proxies] == [24, 24]
        finally:
            for client in clients:
                client.close()


def _open_authenticated_tunnel(proxy):
    config = proxy.playwright_proxy
    host, port = config["server"].removeprefix("http://").split(":")
    client = socket.create_connection((host, int(port)), timeout=2)
    client.settimeout(2)
    credentials = base64.b64encode(
        f"{config['username']}:{config['password']}".encode()
    ).decode()
    client.sendall((
        "CONNECT jobs.example.test:443 HTTP/1.1\r\n"
        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
    ).encode())
    return client


@pytest.mark.parametrize("release_capacity", [True, False])
def test_proxy_waits_for_a_burst_but_reports_persistent_exhaustion(monkeypatch, release_capacity):
    monkeypatch.setattr(pinned_proxy_module, "_MAX_PROXY_CLIENTS_PER_BROWSER", 1)

    class Policy:
        def check(self, _url):
            return UrlPolicyResult(True, "public_hostname", "93.184.216.34")

    peers = []
    with ExitStack() as stack:
        def connect(*_args):
            relay, peer = socket.socketpair()
            peers.append(peer)
            stack.callback(peer.close)
            return relay

        proxy = stack.enter_context(PinnedBrowserProxy(
            source_url="https://jobs.example.test/jobs/123", url_policy=Policy(),
            connector=connect, capacity_wait_seconds=1 if release_capacity else 0.1,
        ))
        first = _open_authenticated_tunnel(proxy)
        try:
            assert first.recv(4096).startswith(b"HTTP/1.1 200")
            second = _open_authenticated_tunnel(proxy)
            try:
                if release_capacity:
                    deadline = time.monotonic() + 0.5
                    while proxy.active_connection_count < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert proxy.active_connection_count == 2
                    first.close()
                    peers[0].close()  # Finish both sides of the CONNECT tunnel.
                    assert second.recv(4096).startswith(b"HTTP/1.1 200")
                    proxy.raise_if_blocked()
                else:
                    assert second.recv(4096).startswith(b"HTTP/1.1 503")
                    from jobcockpit.ingestion.posting_discovery import (
                        _PlaywrightRenderedError, _raise_if_pinned_proxy_blocked,
                    )
                    with pytest.raises(_PlaywrightRenderedError, match="browser_proxy_capacity_exhausted"):
                        _raise_if_pinned_proxy_blocked(proxy)
            finally:
                second.close()
        finally:
            first.close()


def test_proxy_close_wakes_connections_waiting_for_capacity(monkeypatch):
    monkeypatch.setattr(pinned_proxy_module, "_MAX_PROXY_CLIENTS_PER_BROWSER", 1)
    proxy = PinnedBrowserProxy(source_url="https://jobs.example.test/jobs/123",
                               url_policy=_SequencedPolicy([]), capacity_wait_seconds=60)
    proxy._client_slots.acquire()
    proxy.start()
    client = _open_authenticated_tunnel(proxy)
    try:
        deadline = time.monotonic() + 1
        while proxy.active_connection_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert proxy.active_connection_count == 1
        started = time.monotonic()
        proxy.close()
        assert time.monotonic() - started < 1
        try:
            received = client.recv(1)
        except OSError:
            received = b""
        assert received == b""
        assert not proxy._capacity_exhausted
        assert not proxy._acquire_client_capacity()
    finally:
        client.close()
        proxy.close()
        proxy._client_slots.release()


def test_global_capacity_timeout_releases_the_browser_permit(monkeypatch):
    global_slot = threading.BoundedSemaphore(1)
    global_slot.acquire()
    monkeypatch.setattr(pinned_proxy_module, "_PROXY_CLIENT_SLOTS", global_slot)
    monkeypatch.setattr(pinned_proxy_module, "_MAX_PROXY_CLIENTS_PER_BROWSER", 1)
    proxy = PinnedBrowserProxy(source_url="https://jobs.example.test/jobs/123",
                               url_policy=_SequencedPolicy([]), capacity_wait_seconds=0.05)
    assert not proxy._acquire_client_capacity()
    assert proxy._client_slots.acquire(blocking=False)
    proxy._client_slots.release()
    global_slot.release()


@pytest.mark.parametrize("close_before_release", [False, True])
def test_late_proxy_cleanup_releases_the_pool_it_acquired(monkeypatch, close_before_release):
    original_slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(pinned_proxy_module, "_PROXY_CLIENT_SLOTS", original_slot)
    earlier_proxy = PinnedBrowserProxy(
        source_url="https://jobs.example.test/jobs/123",
        url_policy=_SequencedPolicy([]), capacity_wait_seconds=0.05,
    )
    assert earlier_proxy._acquire_client_capacity()

    # CI can replace this pool while a previous proxy's daemon handler is
    # finishing. Its late release must not unlock the new test's exhausted pool.
    replacement_slot = threading.BoundedSemaphore(1)
    replacement_slot.acquire()
    monkeypatch.setattr(pinned_proxy_module, "_PROXY_CLIENT_SLOTS", replacement_slot)
    later_proxy = PinnedBrowserProxy(
        source_url="https://jobs.example.test/jobs/123",
        url_policy=_SequencedPolicy([]), capacity_wait_seconds=0.05,
    )
    try:
        if close_before_release:
            earlier_proxy.close()
        earlier_proxy._release_client_capacity()
        assert original_slot.acquire(blocking=False)
        original_slot.release()
        assert not later_proxy._acquire_client_capacity()
        assert later_proxy._client_slots.acquire(blocking=False)
        later_proxy._client_slots.release()
        replacement_slot.release()
    finally:
        earlier_proxy.close()
        later_proxy.close()


def test_proxy_revalidates_dns_and_rejects_public_to_private_rebinding():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy(
        [
            UrlPolicyResult(
                True,
                "public_hostname",
                "93.184.216.34",
                ("93.184.216.34",),
            ),
            UrlPolicyResult(False, "private_network_blocked"),
        ]
    )
    connected: list[tuple[str, int, float]] = []

    # Simulate the Playwright route guard seeing a public answer first.
    assert policy.check(source_url).allowed is True
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=lambda host, port, timeout: connected.append(
            (host, port, timeout)
        ),
    )

    with pytest.raises(PinnedBrowserProxyBlocked, match="private_network_blocked"):
        proxy._connect_upstream(source_url)

    assert connected == []
    with pytest.raises(PinnedBrowserProxyBlocked, match="private_network_blocked"):
        proxy.raise_if_blocked()


def test_unresolvable_analytics_host_does_not_poison_posting_connection():
    # Kinaxis iCIMS loads an obsolete CloudFront analytics script whose DNS
    # lookup fails. Reject that request without discarding the readable job.
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy([
        UrlPolicyResult(False, "dns_resolution_failed"),
        UrlPolicyResult(True, "public_hostname", "93.184.216.34", ("93.184.216.34",)),
    ])
    sentinel = object()
    connected = []

    def connect(host, port, timeout):
        connected.append(host)
        return sentinel

    proxy = PinnedBrowserProxy(source_url=source_url, url_policy=policy, connector=connect)
    with pytest.raises(PinnedBrowserProxyBlocked, match="dns_resolution_failed"):
        proxy._connect_upstream("https://analytics.example.test/tracker.js")
    assert connected == []
    proxy.raise_if_blocked()
    assert proxy._connect_upstream(source_url) is sentinel
    assert connected == ["93.184.216.34"]


def test_source_dns_failure_remains_fatal():
    source_url = "https://jobs.example.test/jobs/123"
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=_SequencedPolicy([UrlPolicyResult(False, "dns_resolution_failed")]),
        connector=lambda *_args: pytest.fail("unresolved hosts must not be connected"),
    )
    with pytest.raises(PinnedBrowserProxyBlocked, match="dns_resolution_failed"):
        proxy._connect_upstream(source_url)
    with pytest.raises(PinnedBrowserProxyBlocked, match="dns_resolution_failed"):
        proxy.raise_if_blocked()


@pytest.mark.parametrize("slow_phase", ["dns", "connect"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_slow_connection_does_not_block_other_assets_or_shutdown(slow_phase, shutdown):
    from unittest.mock import Mock
    waiting, release, progressed = (threading.Event() for _ in range(3))
    streams = {host: Mock() for host in ("93.184.216.34", "93.184.216.35")}
    connected, resource_errors, control_errors = [], [], []

    def slow_wait():
        waiting.set()
        assert release.wait(5)

    class Policy:
        def check(self, url):
            slow = "slow.example" in url
            if slow and slow_phase == "dns":
                slow_wait()
            host = "93.184.216.34" if slow else "93.184.216.35"
            return UrlPolicyResult(True, "public_hostname", host, (host,))

    def connect(host, *_args):
        connected.append(host)
        if host == "93.184.216.34" and slow_phase == "connect":
            slow_wait()
        return streams[host]

    proxy = PinnedBrowserProxy(source_url="https://jobs.example/job",
                              url_policy=Policy(), connector=connect)

    def load_slow_resource():
        try:
            stream = proxy._connect_upstream("https://slow.example/asset")
            proxy._register_upstream(stream)
        except Exception as error:
            resource_errors.append(error)

    def load_fast_resource():
        try:
            stream = proxy._connect_upstream("https://fast.example/asset")
            assert stream is streams["93.184.216.35"]
            proxy._register_upstream(stream)
            if shutdown:
                proxy.close()
        except Exception as error:
            control_errors.append(error)
        finally:
            progressed.set()

    resource_thread = threading.Thread(target=load_slow_resource)
    control_thread = threading.Thread(target=load_fast_resource)
    try:
        resource_thread.start()
        assert waiting.wait(2)
        control_thread.start()
        assert progressed.wait(1), "slow asset blocked another asset or shutdown"
        assert control_errors == []
        release.set()
        resource_thread.join(3)
        control_thread.join(3)
        assert not resource_thread.is_alive()
        if shutdown:
            assert len(resource_errors) == 1
            assert isinstance(resource_errors[0], OSError)
            if slow_phase == "connect":
                streams["93.184.216.34"].close.assert_called()
            else:
                assert "93.184.216.34" not in connected
            assert not proxy._active_upstreams
            streams["93.184.216.35"].close.assert_called()
        else:
            assert resource_errors == []
            assert proxy._active_upstreams == set(streams.values())
        proxy.raise_if_blocked()
    finally:
        release.set()
        resource_thread.join(3)
        if control_thread.ident is not None:
            control_thread.join(3)
        proxy.close()


def test_preparation_accepts_public_connections_without_approving_final_source(allow_public_dns):
    from jobcockpit.ingestion.invisible_session import PreparationUrlPolicy
    from jobcockpit.ingestion.url_policy import UrlFetchPolicy

    source = "https://www.eluta.ca/search?q=intern"
    target = "https://unrelated-cdn.example/"
    allow_public_dns({"www.eluta.ca", "unrelated-cdn.example"}, ip="93.184.216.34")
    policy = PreparationUrlPolicy(source, UrlFetchPolicy())
    assert not policy.check_browser_final_url(source, target).allowed
    connected = []
    sentinel = object()

    def connect(host, port, timeout):
        connected.append(host)
        return sentinel

    proxy = PinnedBrowserProxy(source_url=source, url_policy=policy, connector=connect)
    assert proxy._connect_upstream(target) is sentinel
    assert proxy._connect_upstream(source) is sentinel
    assert connected == ["93.184.216.34", "93.184.216.34"]
    proxy.raise_if_blocked()


def test_unlisted_resource_dns_rebinding_is_blocked_at_connection_time(allow_public_dns):
    source = "https://jobs.example/posting/42"
    asset = "https://new-cdn.example/runtime.js"
    policy = ScheduledRenderedUrlPolicy()
    allow_public_dns({"new-cdn.example"}, ip="93.184.216.34")
    assert policy.check(asset).allowed
    allow_public_dns({"new-cdn.example"}, ip="127.0.0.1")
    proxy = PinnedBrowserProxy(
        source_url=source, url_policy=policy,
        connector=lambda *_args: pytest.fail("private addresses must never be connected"),
    )
    with pytest.raises(PinnedBrowserProxyBlocked, match="private_network_blocked"):
        proxy._connect_upstream(asset)
    proxy.raise_if_blocked()


def test_proxy_connects_only_to_the_numeric_address_returned_by_validation():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy(
        [
            UrlPolicyResult(
                True,
                "public_hostname",
                "93.184.216.34",
                ("93.184.216.34",),
            )
        ]
    )
    sentinel = object()
    connected: list[tuple[str, int, float]] = []

    def connect(host: str, port: int, timeout: float):
        connected.append((host, port, timeout))
        return sentinel

    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=connect,
        connect_timeout_seconds=3.5,
    )

    assert proxy._connect_upstream(source_url) is sentinel
    assert connected == [("93.184.216.34", 443, pytest.approx(3.5))]


def test_proxy_rejects_non_numeric_connect_hosts_even_if_policy_claims_allowed():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy(
        [
            UrlPolicyResult(
                True,
                "public_hostname",
                "attacker-controlled.example",
                ("attacker-controlled.example",),
            )
        ]
    )

    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=lambda *_args: pytest.fail("connector must not be called"),
    )

    with pytest.raises(PinnedBrowserProxyBlocked, match="unvalidated_connect_host"):
        proxy._connect_upstream(source_url)


def test_proxy_lifecycle_exposes_authenticated_loopback_configuration():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy([])
    proxy = PinnedBrowserProxy(source_url=source_url, url_policy=policy)

    with proxy:
        configuration = proxy.playwright_proxy
        server = configuration["server"]
        assert server.startswith("http://127.0.0.1:")
        assert configuration["username"] == "jobcockpit"
        assert configuration["password"]
        host, port_text = server.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port_text)), timeout=1):
            pass
        competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competitor.bind((host, int(port_text)))
        finally:
            competitor.close()

    with pytest.raises(OSError):
        socket.create_connection((host, int(port_text)), timeout=0.2)


def test_proxy_header_read_is_bounded():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy([])
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        header_timeout_seconds=0.05,
    )

    with proxy:
        configuration = proxy.playwright_proxy
        host, port_text = configuration["server"].removeprefix("http://").split(":")
        client = socket.create_connection((host, int(port_text)), timeout=1)
        try:
            client.settimeout(1)
            client.sendall(b"CONNECT jobs.example.test:443 HTTP/1.1\r\n")
            response = client.recv(4096)
        finally:
            client.close()

    assert response.startswith(b"HTTP/1.1 502")


def test_proxy_close_interrupts_clients_stalled_before_headers():
    source_url = "https://jobs.example.test/jobs/123"
    policy = _SequencedPolicy([])
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        header_timeout_seconds=60,
    )
    proxy.start()
    configuration = proxy.playwright_proxy
    host, port_text = configuration["server"].removeprefix("http://").split(":")
    client = socket.create_connection((host, int(port_text)), timeout=1)
    try:
        client.settimeout(1)
        client.sendall(b"CON")
        deadline = time.monotonic() + 1
        while proxy.active_connection_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert proxy.active_connection_count == 1

        proxy.close()

        assert proxy.active_connection_count == 0
        try:
            received = client.recv(1)
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            received = b""
        assert received == b""
    finally:
        client.close()
        proxy.close()


@pytest.mark.parametrize("high_descriptors", [False, True])
def test_relay_flushes_final_bytes_before_propagating_half_close(high_descriptors):
    if high_descriptors:
        fcntl = pytest.importorskip("fcntl", reason="POSIX descriptor limit regression")
        resource = pytest.importorskip("resource")
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit != resource.RLIM_INFINITY and soft_limit < 2050:
            pytest.skip("The process limit cannot accommodate descriptors above 2048")
    downstream_relay, downstream_peer = socket.socketpair()
    upstream_relay, upstream_peer = socket.socketpair()
    if high_descriptors:
        downstream_high = socket.socket(fileno=fcntl.fcntl(downstream_relay, fcntl.F_DUPFD, 2048))
        upstream_high = socket.socket(fileno=fcntl.fcntl(upstream_relay, fcntl.F_DUPFD, 2048))
        downstream_relay.close()
        upstream_relay.close()
        downstream_relay, upstream_relay = downstream_high, upstream_high
        assert min(downstream_relay.fileno(), upstream_relay.fileno()) >= 2048
    stop_event = threading.Event()
    relay_thread = threading.Thread(
        target=_relay_bidirectionally,
        args=(downstream_relay, upstream_relay),
        kwargs={
            "stop_event": stop_event,
            "idle_timeout_seconds": 2.0,
            "clock": time.monotonic,
        },
        daemon=True,
    )
    try:
        downstream_peer.settimeout(1)
        upstream_peer.settimeout(1)
        relay_thread.start()

        upstream_peer.sendall(b"final-response")
        upstream_peer.shutdown(socket.SHUT_WR)

        received = bytearray()
        while True:
            chunk = downstream_peer.recv(1024)
            if not chunk:
                break
            received.extend(chunk)
        assert bytes(received) == b"final-response"

        downstream_peer.shutdown(socket.SHUT_WR)
        assert upstream_peer.recv(1) == b""
        relay_thread.join(timeout=1)
        assert relay_thread.is_alive() is False
    finally:
        stop_event.set()
        for stream in (
            downstream_relay,
            downstream_peer,
            upstream_relay,
            upstream_peer,
        ):
            try:
                stream.close()
            except OSError:
                pass
        relay_thread.join(timeout=1)


class _SlowHeaderClient:
    def __init__(self) -> None:
        self.recv_calls = 0
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recv(self, _size: int) -> bytes:
        self.recv_calls += 1
        return b"x"


def test_header_limit_is_an_absolute_deadline_not_per_byte_inactivity():
    client = _SlowHeaderClient()
    clock_values = iter((0.0, 0.04, 0.08, 0.12))

    with pytest.raises(TimeoutError, match="proxy_header_timeout"):
        _read_request_head(
            client,
            deadline=0.1,
            clock=lambda: next(clock_values),
        )

    assert client.recv_calls == 3
    assert client.timeouts == [
        pytest.approx(0.1),
        pytest.approx(0.06),
        pytest.approx(0.02),
    ]


class _ResetAfterChunkSocket:
    def __init__(self, stream: socket.socket) -> None:
        self.stream = stream
        self.recv_calls = 0

    def fileno(self) -> int:
        return self.stream.fileno()

    def settimeout(self, timeout: float | None) -> None:
        self.stream.settimeout(timeout)

    def setblocking(self, value: bool) -> None:
        self.stream.setblocking(value)

    def send(self, data: bytes | bytearray) -> int:
        return self.stream.send(data)

    def sendall(self, data: bytes | bytearray) -> None:
        self.stream.sendall(data)

    def recv(self, size: int) -> bytes:
        self.recv_calls += 1
        if self.recv_calls > 1:
            raise ConnectionResetError("simulated upstream reset")
        return self.stream.recv(size)

    def shutdown(self, how: int) -> None:
        self.stream.shutdown(how)

    def close(self) -> None:
        self.stream.close()


def test_http_proxy_never_appends_502_after_upstream_response_started():
    source_url = "http://jobs.example.test/jobs/123"
    policy = _SequencedPolicy(
        [
            UrlPolicyResult(
                True,
                "public_hostname",
                "93.184.216.34",
                ("93.184.216.34",),
            )
        ]
    )
    upstream_relay, upstream_peer = socket.socketpair()
    reset_stream = _ResetAfterChunkSocket(upstream_relay)
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=lambda *_args: reset_stream,
    )
    expected_response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\npartial"
    )

    try:
        with proxy:
            configuration = proxy.playwright_proxy
            host, port_text = configuration["server"].removeprefix("http://").split(":")
            client = socket.create_connection((host, int(port_text)), timeout=1)
            try:
                client.settimeout(1)
                credentials = base64.b64encode(
                    f"{configuration['username']}:{configuration['password']}".encode()
                ).decode()
                client.sendall(
                    (
                        f"GET {source_url} HTTP/1.1\r\n"
                        "Host: jobs.example.test\r\n"
                        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
                    ).encode()
                )
                forwarded_request = upstream_peer.recv(4096)
                assert forwarded_request.startswith(b"GET /jobs/123 HTTP/1.1\r\n")
                upstream_peer.sendall(expected_response)
                upstream_peer.shutdown(socket.SHUT_WR)

                received = bytearray()
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
            finally:
                client.close()
    finally:
        upstream_peer.close()
        upstream_relay.close()

    assert bytes(received) == expected_response
    assert b"502 Bad Gateway" not in received


class _BlockingPolicy:
    def __init__(self) -> None:
        self.check_entered = threading.Event()
        self.release_check = threading.Event()
        self.check_finished = threading.Event()

    def check(self, _url: str) -> UrlPolicyResult:
        self.check_entered.set()
        self.release_check.wait(timeout=2)
        self.check_finished.set()
        return UrlPolicyResult(
            True,
            "public_hostname",
            "93.184.216.34",
            ("93.184.216.34",),
        )


def test_proxy_close_prevents_egress_after_blocked_policy_check_resumes():
    source_url = "http://jobs.example.test/jobs/123"
    policy = _BlockingPolicy()
    connector_called = threading.Event()
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=lambda *_args: connector_called.set(),
    )
    proxy.start()
    configuration = proxy.playwright_proxy
    host, port_text = configuration["server"].removeprefix("http://").split(":")
    client = socket.create_connection((host, int(port_text)), timeout=1)
    try:
        credentials = base64.b64encode(
            f"{configuration['username']}:{configuration['password']}".encode()
        ).decode()
        client.sendall(
            (
                f"GET {source_url} HTTP/1.1\r\n"
                "Host: jobs.example.test\r\n"
                f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
            ).encode()
        )
        assert policy.check_entered.wait(timeout=1)

        proxy.close()
        policy.release_check.set()

        assert policy.check_finished.wait(timeout=1)
        assert connector_called.wait(timeout=0.2) is False
    finally:
        policy.release_check.set()
        client.close()
        proxy.close()


def test_proxy_instances_are_explicitly_one_shot():
    source_url = "https://jobs.example.test/jobs/123"
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=_SequencedPolicy([]),
    )

    proxy.start()
    proxy.close()

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        proxy.start()


@pytest.mark.parametrize(
    "headers",
    (
        [
            ("Content-Length", "4"),
            ("Transfer-Encoding", "chunked"),
        ],
        [
            ("Content-Length", "4"),
            ("Content-Length", "5"),
        ],
        [
            ("Connection", "Content-Length"),
            ("Content-Length", "4"),
        ],
        [
            ("Connection", "Host"),
            ("Host", "jobs.example.test"),
        ],
    ),
)
def test_http_forward_proxy_rejects_ambiguous_or_stripped_framing(headers):
    with pytest.raises(_ProxyRequestError):
        _forward_request_head(
            "POST",
            "http://jobs.example.test/jobs/123",
            "HTTP/1.1",
            headers,
        )


@pytest.mark.parametrize(
    "request_head",
    (
        (
            b"GET http://jobs.example.test/jobs/123 HTTP/1.1\r\n"
            b"X-Test: safe\x00unsafe\r\n\r\n"
        ),
        (
            b"GET http://jobs.example.test/jobs/123 HTTP/1.1\r\n"
            b"Bad Header: value\r\n\r\n"
        ),
    ),
)
def test_proxy_rejects_control_characters_and_invalid_header_names(request_head):
    with pytest.raises(_ProxyRequestError):
        _parse_request_head(request_head)


def test_private_speculative_connection_does_not_poison_render():
    proxy = PinnedBrowserProxy(
        source_url="https://jobs.example/job",
        url_policy=ScheduledRenderedUrlPolicy(),
        connector=lambda *_args: pytest.fail("connector must not run"),
    )
    with pytest.raises(PinnedBrowserProxyBlocked, match="private_network_blocked"):
        proxy._connect_upstream("http://10.0.0.1/")
    proxy.raise_if_blocked()


def test_non_ascii_proxy_authorization_is_rejected_without_handler_failure():
    proxy = PinnedBrowserProxy(
        source_url="https://jobs.example.test/jobs/123",
        url_policy=_SequencedPolicy([]),
        connector=lambda *_args: pytest.fail("connector must not run"),
    )

    with proxy:
        configuration = proxy.playwright_proxy
        host, port_text = configuration["server"].removeprefix("http://").split(":")
        client = socket.create_connection((host, int(port_text)), timeout=1)
        try:
            client.settimeout(1)
            client.sendall(
                b"CONNECT jobs.example.test:443 HTTP/1.1\r\n"
                b"Host: jobs.example.test:443\r\n"
                b"Proxy-Authorization: \xff\r\n\r\n"
            )
            response = client.recv(4096)
        finally:
            client.close()

    assert response.startswith(b"HTTP/1.1 407")


def test_http_proxy_rejects_a_buffered_second_request_before_connecting():
    source_url = "http://jobs.example.test/jobs/123"
    connector_called = threading.Event()
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=_SequencedPolicy(
            [
                UrlPolicyResult(
                    True,
                    "public_hostname",
                    "93.184.216.34",
                    ("93.184.216.34",),
                )
            ]
        ),
        connector=lambda *_args: connector_called.set(),
    )

    with proxy:
        configuration = proxy.playwright_proxy
        host, port_text = configuration["server"].removeprefix("http://").split(":")
        credentials = base64.b64encode(
            f"{configuration['username']}:{configuration['password']}".encode()
        ).decode()
        client = socket.create_connection((host, int(port_text)), timeout=1)
        try:
            client.settimeout(1)
            client.sendall(
                (
                    f"GET {source_url} HTTP/1.1\r\n"
                    "Host: jobs.example.test\r\n"
                    f"Proxy-Authorization: Basic {credentials}\r\n"
                    "Content-Length: 0\r\n\r\n"
                    "GET http://attacker.example/ HTTP/1.1\r\n"
                    "Host: attacker.example\r\n\r\n"
                ).encode()
            )
            response = client.recv(4096)
        finally:
            client.close()

    assert response.startswith(b"HTTP/1.1 400")
    assert connector_called.is_set() is False


def test_http_proxy_does_not_tunnel_a_late_second_request():
    source_url = "http://jobs.example.test/jobs/123"
    policy = _SequencedPolicy(
        [
            UrlPolicyResult(
                True,
                "public_hostname",
                "93.184.216.34",
                ("93.184.216.34",),
            )
        ]
    )
    upstream_relay, upstream_peer = socket.socketpair()
    proxy = PinnedBrowserProxy(
        source_url=source_url,
        url_policy=policy,
        connector=lambda *_args: upstream_relay,
    )

    try:
        with proxy:
            configuration = proxy.playwright_proxy
            host, port_text = configuration["server"].removeprefix("http://").split(":")
            credentials = base64.b64encode(
                f"{configuration['username']}:{configuration['password']}".encode()
            ).decode()
            client = socket.create_connection((host, int(port_text)), timeout=1)
            try:
                client.settimeout(1)
                upstream_peer.settimeout(1)
                client.sendall(
                    (
                        f"GET {source_url} HTTP/1.1\r\n"
                        "Host: jobs.example.test\r\n"
                        f"Proxy-Authorization: Basic {credentials}\r\n\r\n"
                    ).encode()
                )
                forwarded = bytearray()
                while b"\r\n\r\n" not in forwarded:
                    forwarded.extend(upstream_peer.recv(4096))
                client.sendall(
                    b"GET http://attacker.example/ HTTP/1.1\r\n"
                    b"Host: attacker.example\r\n\r\n"
                )
                assert upstream_peer.recv(4096) == b""
                assert b"attacker.example" not in forwarded

                upstream_peer.sendall(
                    b"HTTP/1.1 204 No Content\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                upstream_peer.shutdown(socket.SHUT_WR)
                assert client.recv(4096).startswith(b"HTTP/1.1 204")
            finally:
                client.close()
    finally:
        upstream_peer.close()
        upstream_relay.close()


def test_proxy_start_failure_closes_the_bound_listener(monkeypatch):
    closed = threading.Event()
    original_server_close = pinned_proxy_module._ThreadingProxyServer.server_close

    def recording_server_close(server):
        closed.set()
        original_server_close(server)

    def fail_to_start(_thread):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(
        pinned_proxy_module._ThreadingProxyServer,
        "server_close",
        recording_server_close,
    )
    monkeypatch.setattr(threading.Thread, "start", fail_to_start)
    proxy = PinnedBrowserProxy(
        source_url="https://jobs.example.test/jobs/123",
        url_policy=_SequencedPolicy([]),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        proxy.start()

    assert closed.is_set()
    assert proxy.active_connection_count == 0


def test_connect_target_preserves_same_origin_http_for_websocket_tunnels():
    assert _connect_target_url(
        "jobs.example.test:80",
        source_url="http://jobs.example.test/jobs/123",
    ) == "http://jobs.example.test/"
    assert _connect_target_url(
        "jobs.example.test:443",
        source_url="https://jobs.example.test/jobs/123",
    ) == "https://jobs.example.test/"
