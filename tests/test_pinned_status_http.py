from __future__ import annotations

import socket
import ssl
from typing import Any
from urllib.parse import urljoin

import httpcore
import pytest

from jobcockpit.ingestion.pinned_http import (
    PinnedEndpoint,
    PinnedHttpClient,
    PinnedNetworkBackend,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult


class _CannedHttpStream(httpcore.NetworkStream):
    def __init__(
        self,
        response: bytes = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b"Connection: close\r\n\r\nOK"
        )
    ) -> None:
        self._response = bytearray(response)
        self.writes: list[bytes] = []
        self.server_hostname: str | None = None
        self.ssl_context: ssl.SSLContext | None = None

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk = bytes(self._response[:max_bytes])
        del self._response[:max_bytes]
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    def close(self) -> None:
        return None

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.ssl_context = ssl_context
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, stream: _CannedHttpStream) -> None:
        self.stream = stream
        self.connects: list[tuple[str, int]] = []
        self.timeouts: list[float | None] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        self.connects.append((host, port))
        self.timeouts.append(timeout)
        return self.stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise AssertionError("unexpected Unix socket connection")


class _FailFirstRecordingBackend(_RecordingBackend):
    def __init__(self, stream: _CannedHttpStream, failed_host: str) -> None:
        super().__init__(stream)
        self.failed_host = failed_host

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        self.connects.append((host, port))
        self.timeouts.append(timeout)
        if host == self.failed_host:
            raise httpcore.ConnectError("synthetic unreachable address")
        return self.stream


def test_request_uses_policy_validated_ip_without_second_dns_lookup_and_keeps_https_hostname(
    monkeypatch,
):
    dns_calls = 0

    def rebinding_getaddrinfo(host: str, port: int, *args, **kwargs):
        nonlocal dns_calls
        assert host == "jobs.example.test"
        dns_calls += 1
        address = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    stream = _CannedHttpStream()
    delegate = _RecordingBackend(stream)
    client = PinnedHttpClient(
        timeout=1,
        network_backend_factory=lambda endpoint: PinnedNetworkBackend(
            endpoint,
            delegate=delegate,
        ),
    )
    source_url = "https://jobs.example.test/Careers/job/Platform-Engineer_R123"
    derived_workday_url = (
        "https://jobs.example.test/wday/cxs/acme/Careers/job/"
        "Platform-Engineer_R123"
    )

    policy_result = client.url_policy.check(source_url)
    response = client.get(derived_workday_url)

    assert policy_result.allowed is True
    assert policy_result.connect_host == "93.184.216.34"
    assert response.status_code == 200
    assert dns_calls == 1
    assert delegate.connects == [("93.184.216.34", 443)]
    assert stream.server_hostname == "jobs.example.test"
    assert b"Host: jobs.example.test\r\n" in b"".join(stream.writes)


def test_request_uses_configured_ssl_context(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    stream = _CannedHttpStream()
    delegate = _RecordingBackend(stream)
    custom_ssl_context = ssl.create_default_context()
    client = PinnedHttpClient(
        timeout=1,
        ssl_context=custom_ssl_context,
        network_backend_factory=lambda endpoint: PinnedNetworkBackend(
            endpoint,
            delegate=delegate,
        ),
    )

    response = client.get("https://jobs.example.test/job/123")

    assert response.status_code == 200
    assert stream.ssl_context is custom_ssl_context


def test_request_tries_all_policy_validated_ips_in_resolver_order_without_new_dns(
    monkeypatch,
):
    dns_calls = 0
    resolved_addresses = ("93.184.216.34", "93.184.216.35")

    def multi_address_getaddrinfo(host: str, port: int, *args, **kwargs):
        nonlocal dns_calls
        assert host == "jobs.example.test"
        dns_calls += 1
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in resolved_addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", multi_address_getaddrinfo)
    stream = _CannedHttpStream()
    delegate = _FailFirstRecordingBackend(stream, resolved_addresses[0])
    client = PinnedHttpClient(
        timeout=1,
        network_backend_factory=lambda endpoint: PinnedNetworkBackend(
            endpoint,
            delegate=delegate,
        ),
    )
    url = "https://jobs.example.test/Careers/job/Platform-Engineer_R123"

    policy_result = client.url_policy.check(url)
    response = client.get(url)

    assert policy_result.allowed is True
    assert policy_result.connect_hosts == resolved_addresses
    assert response.status_code == 200
    assert dns_calls == 1
    assert delegate.connects == [
        ("93.184.216.34", 443),
        ("93.184.216.35", 443),
    ]
    assert stream.server_hostname == "jobs.example.test"


def test_multi_address_attempts_share_one_connection_timeout_budget():
    endpoint = PinnedEndpoint(
        scheme="https",
        hostname="jobs.example.test",
        port=443,
        connect_host="93.184.216.34",
        connect_hosts=("93.184.216.34", "93.184.216.35"),
    )
    stream = _CannedHttpStream()
    delegate = _FailFirstRecordingBackend(stream, "93.184.216.34")
    clock_values = iter((0.0, 0.0, 0.6))
    backend = PinnedNetworkBackend(
        endpoint,
        delegate=delegate,
        clock=lambda: next(clock_values),
    )

    connected = backend.connect_tcp(
        "jobs.example.test",
        443,
        timeout=1.0,
    )

    assert connected is stream
    assert delegate.connects == [
        ("93.184.216.34", 443),
        ("93.184.216.35", 443),
    ]
    assert delegate.timeouts[0] == 1.0
    assert delegate.timeouts[1] == pytest.approx(0.4)


def test_duplicate_dns_answers_are_only_attempted_once(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        ],
    )

    result = UrlFetchPolicy().check("https://duplicate.example/jobs/123")

    assert result.connect_hosts == ("93.184.216.34",)


@pytest.mark.parametrize(
    "source_url",
    (
        "https://jobs.example.test:0/jobs/123",
        "http://jobs.example.test:0/jobs/123",
    ),
)
def test_zero_port_is_rejected_before_dns_or_connection(source_url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", pytest.fail)
    assert UrlFetchPolicy().check(source_url) == UrlPolicyResult(False, "invalid_port")


def test_localhost_pin_requires_exact_allowlisted_host_and_port(monkeypatch):
    def fake_getaddrinfo(host: str, *_args, **_kwargs):
        if host == "localhost.example":
            raise socket.gaierror
        raise AssertionError("localhost must not use DNS")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    policy = UrlFetchPolicy(allowed_localhosts={"localhost:8001"})

    allowed = policy.check("http://localhost:8001/ats/acme/jobs/123")
    wrong_port = policy.check("http://localhost:8002/ats/acme/jobs/123")
    lookalike = policy.check("http://localhost.example:8001/ats/acme/jobs/123")

    assert allowed.allowed is True
    assert allowed.connect_host == "127.0.0.1"
    assert wrong_port.allowed is False
    assert wrong_port.reason == "localhost_not_allowlisted"
    assert lookalike.allowed is False
    assert lookalike.reason == "dns_resolution_failed"


def test_each_redirect_origin_is_resolved_validated_and_pinned_once(monkeypatch):
    dns_calls: dict[str, int] = {}
    public_addresses = {
        "source.example.test": "93.184.216.34",
        "final.example.test": "93.184.216.35",
    }

    def rebinding_getaddrinfo(host: str, port: int, *args, **kwargs):
        dns_calls[host] = dns_calls.get(host, 0) + 1
        address = public_addresses[host] if dns_calls[host] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    connections: list[tuple[str, int]] = []

    def backend_for(endpoint: PinnedEndpoint) -> httpcore.NetworkBackend:
        if endpoint.hostname == "source.example.test":
            response = (
                b"HTTP/1.1 302 Found\r\n"
                b"Location: https://final.example.test/jobs/123\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        else:
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
        delegate = _RecordingBackend(_CannedHttpStream(response))
        original_connect = delegate.connect_tcp

        def record_connect(host: str, port: int, **kwargs):
            connections.append((host, port))
            return original_connect(host, port, **kwargs)

        delegate.connect_tcp = record_connect
        return PinnedNetworkBackend(endpoint, delegate=delegate)

    client = PinnedHttpClient(
        timeout=1,
        network_backend_factory=backend_for,
    )
    current_url = "https://source.example.test/short"

    first_policy = client.url_policy.check(current_url)
    first_response = client.get(current_url)
    current_url = urljoin(current_url, first_response.headers["Location"])
    second_policy = client.url_policy.check(current_url)
    second_response = client.get(current_url)

    assert first_policy.connect_host == "93.184.216.34"
    assert second_policy.connect_host == "93.184.216.35"
    assert second_response.status_code == 200
    assert dns_calls == {
        "source.example.test": 1,
        "final.example.test": 1,
    }
    assert connections == [
        ("93.184.216.34", 443),
        ("93.184.216.35", 443),
    ]


def test_url_policy_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ],
    )

    result = UrlFetchPolicy().check("https://mixed.example/jobs/123")

    assert result.allowed is False
    assert result.reason == "private_network_blocked"
    assert result.connect_host is None
    assert result.connect_hosts == ()


def test_url_policy_requires_globally_routable_unicast_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", port))
        ],
    )

    result = UrlFetchPolicy().check("https://carrier-grade-nat.example/jobs/123")

    assert result.allowed is False
    assert result.reason == "private_network_blocked"
