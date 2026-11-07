from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult


@dataclass(frozen=True)
class PinnedEndpoint:
    scheme: str
    hostname: str
    port: int
    connect_host: str
    connect_hosts: tuple[str, ...] = ()

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.hostname, self.port)

    @property
    def validated_connect_hosts(self) -> tuple[str, ...]:
        return self.connect_hosts or (self.connect_host,)


class UrlPolicyViolation(RuntimeError):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"url_policy_blocked:{reason}")
        self.url = url
        self.reason = reason


class PinningUrlFetchPolicy(UrlFetchPolicy):
    """URL policy that hands the exact validated address to the next request."""

    def __init__(self, allowed_localhosts: set[str] | None = None) -> None:
        super().__init__(allowed_localhosts=allowed_localhosts)
        self._pins: dict[tuple[str, str, int], PinnedEndpoint] = {}
        self._pins_lock = threading.Lock()

    def check(self, url: str) -> UrlPolicyResult:
        result = super().check(url)
        origin = _url_origin(url)
        if origin is None:
            return result
        with self._pins_lock:
            self._pins.pop(origin, None)
            if result.allowed and result.connect_host:
                connect_hosts = result.connect_hosts or (result.connect_host,)
                self._pins[origin] = PinnedEndpoint(
                    scheme=origin[0],
                    hostname=origin[1],
                    port=origin[2],
                    connect_host=connect_hosts[0],
                    connect_hosts=connect_hosts,
                )
        return result

    def endpoint_for_request(self, url: str) -> PinnedEndpoint:
        origin = _url_origin(url)
        if origin is None:
            result = super().check(url)
            raise UrlPolicyViolation(url, result.reason)
        with self._pins_lock:
            endpoint = self._pins.pop(origin, None)
        if endpoint is not None:
            return endpoint
        result = self.check(url)
        if not result.allowed:
            raise UrlPolicyViolation(url, result.reason)
        with self._pins_lock:
            endpoint = self._pins.pop(origin, None)
        if endpoint is None:
            raise UrlPolicyViolation(url, "dns_resolution_failed")
        return endpoint


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect to a validated numeric address while leaving the HTTP origin intact."""

    def __init__(
        self,
        endpoint: PinnedEndpoint,
        *,
        delegate: httpcore.NetworkBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._endpoint = endpoint
        self._delegate = delegate or httpcore.SyncBackend()
        self._clock = clock

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        if host.casefold() != self._endpoint.hostname or port != self._endpoint.port:
            raise httpcore.ConnectError("request origin did not match the pinned endpoint")
        last_error: Exception | None = None
        deadline = self._clock() + timeout if timeout is not None else None
        for connect_host in self._endpoint.validated_connect_hosts:
            attempt_timeout = timeout
            if deadline is not None:
                attempt_timeout = deadline - self._clock()
                if attempt_timeout <= 0:
                    raise httpcore.ConnectTimeout(
                        "pinned address connection budget was exhausted"
                    ) from last_error
            try:
                return self._delegate.connect_tcp(
                    connect_host,
                    port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        if last_error is None:
            raise httpcore.ConnectError("pinned endpoint has no validated addresses")
        raise last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not supported for pinned URL fetches")


NetworkBackendFactory = Callable[[PinnedEndpoint], httpcore.NetworkBackend]


class _PinnedHttpTransport(httpx.HTTPTransport):
    def __init__(
        self,
        endpoint: PinnedEndpoint,
        network_backend_factory: NetworkBackendFactory,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(trust_env=False)
        original_pool = self._pool
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=network_backend_factory(endpoint),
        )
        original_pool.close()


class PinnedHttpClient:
    """Small httpx-compatible client that performs one pinned request at a time."""

    def __init__(
        self,
        *,
        timeout: float,
        allowed_localhosts: set[str] | None = None,
        follow_redirects: bool = False,
        headers: Mapping[str, str] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        network_backend_factory: NetworkBackendFactory | None = None,
    ) -> None:
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self.headers = httpx.Headers(headers)
        self.ssl_context = ssl_context
        self.url_policy = PinningUrlFetchPolicy(
            allowed_localhosts=allowed_localhosts
        )
        self._network_backend_factory = (
            network_backend_factory
            or (lambda endpoint: PinnedNetworkBackend(endpoint))
        )

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        **request_kwargs: Any,
    ) -> Iterator[httpx.Response]:
        endpoint = self.url_policy.endpoint_for_request(url)
        transport = _PinnedHttpTransport(
            endpoint,
            self._network_backend_factory,
            self.ssl_context,
        )
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=self.follow_redirects,
            headers=self.headers,
            transport=transport,
        ) as client:
            with client.stream(method, url, **request_kwargs) as response:
                yield response

    def get(self, url: str, **request_kwargs: Any) -> httpx.Response:
        with self.stream("GET", url, **request_kwargs) as response:
            response.read()
            return response

    def close(self) -> None:
        # Each request owns and closes its transport so no pinned connection can
        # be accidentally reused for a later DNS answer.
        return None


def _url_origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    try:
        explicit_port = parsed.port
    except ValueError:
        return None
    if explicit_port is not None and explicit_port < 1:
        return None
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    return (parsed.scheme, hostname.casefold(), port)
