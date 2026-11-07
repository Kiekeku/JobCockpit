from __future__ import annotations

import base64
import hmac
import ipaddress
import re
import secrets
import selectors
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any
from urllib.parse import urlparse

from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult

_MAX_PROXY_HEADER_BYTES = 64 * 1024
_MAX_PROXY_REQUEST_BODY_BYTES = 1024 * 1024
_MAX_RELAY_BUFFER_BYTES = 256 * 1024
_PROXY_USERNAME = "jobcockpit"
# Two browser workers can each open dozens of persistent CONNECT tunnels.
# Keep one browser's asset burst from consuming the other browser's allowance.
_MAX_PROXY_CLIENTS_PER_BROWSER = 64
_MAX_CONCURRENT_PROXY_CLIENTS = 2 * _MAX_PROXY_CLIENTS_PER_BROWSER
_PROXY_CLIENT_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_PROXY_CLIENTS)
_PROXY_HANDLER_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_PROXY_CLIENTS + 64)
_VALID_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_VALID_METHOD = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")

SocketConnector = Callable[[str, int, float], Any]


class PinnedBrowserProxyBlocked(RuntimeError):
    """A browser request failed the proxy's final URL or DNS validation."""


class PinnedBrowserProxyCapacityError(RuntimeError):
    """A bounded wait could not accommodate another browser connection."""


class _ProxyRequestError(RuntimeError):
    pass


class PinnedBrowserProxy:
    """Authenticated loopback proxy that connects only to validated numeric IPs.

    The browser sends the original hostname to this proxy. Immediately before
    opening the upstream socket, the proxy re-runs the network URL/DNS policy,
    then connects to one of the exact numeric addresses returned by that policy.
    Navigation and assets share this public-network boundary. The browser handles
    redirects natively; callers validate the final page before accepting content.
    The browser never performs connection-time DNS resolution for the target.
    """

    def __init__(
        self,
        *,
        source_url: str,
        url_policy: UrlFetchPolicy,
        connector: SocketConnector | None = None,
        connect_timeout_seconds: float = 10.0,
        relay_idle_timeout_seconds: float = 300.0,
        header_timeout_seconds: float = 5.0,
        capacity_wait_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            connect_timeout_seconds <= 0
            or relay_idle_timeout_seconds <= 0
            or header_timeout_seconds <= 0
            or capacity_wait_seconds <= 0
        ):
            raise ValueError("proxy timeouts must be positive")

        self.source_url = source_url
        self.url_policy = url_policy
        self.connector = connector or _connect_numeric_socket
        self.connect_timeout_seconds = connect_timeout_seconds
        self.relay_idle_timeout_seconds = relay_idle_timeout_seconds
        self.clock = clock
        self.username = _PROXY_USERNAME
        self.header_timeout_seconds = header_timeout_seconds
        self.capacity_wait_seconds = capacity_wait_seconds
        self._client_slots = threading.BoundedSemaphore(_MAX_PROXY_CLIENTS_PER_BROWSER)
        # A handler may finish after close(). Release the pool this proxy owns,
        # even when a subsequent test or configuration replaces the global.
        self._global_client_slots = _PROXY_CLIENT_SLOTS
        self.password = secrets.token_urlsafe(32)
        credentials = f"{self.username}:{self.password}".encode("utf-8")
        self._expected_authorization = (
            "Basic " + base64.b64encode(credentials).decode("ascii")
        )
        self._server: _ThreadingProxyServer | None = None
        self._server_thread: threading.Thread | None = None
        self._blocked_reason = ""
        self._capacity_exhausted = False
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._active_clients: set[Any] = set()
        self._active_upstreams: set[Any] = set()
        self._has_started = False
        self._closed = False

    def __enter__(self) -> PinnedBrowserProxy:
        return self.start()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False

    def start(self) -> PinnedBrowserProxy:
        with self._state_lock:
            if self._server is not None:
                return self
            if self._has_started or self._closed:
                raise RuntimeError("pinned browser proxy cannot be restarted")
            self._has_started = True
            server: _ThreadingProxyServer | None = None
            try:
                server = _ThreadingProxyServer(("127.0.0.1", 0), _ProxyHandler)
                server.proxy = self
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.1},
                    name="jobcockpit-pinned-browser-proxy",
                    daemon=True,
                )
                self._server = server
                self._server_thread = thread
                thread.start()
            except Exception:
                self._server = None
                self._server_thread = None
                self._closed = True
                self._stop_event.set()
                if server is not None:
                    server.server_close()
                raise
        return self

    def close(self) -> None:
        with self._state_lock:
            server = self._server
            thread = self._server_thread
            self._server = None
            self._server_thread = None
            self._stop_event.set()
            self._closed = True
            active_clients = tuple(self._active_clients)
            active_upstreams = tuple(self._active_upstreams)
            self._active_clients.clear()
            self._active_upstreams.clear()
        for stream in {*active_clients, *active_upstreams}:
            _close_stream(stream)
        if server is not None:
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2.0)

    @property
    def active_connection_count(self) -> int:
        with self._state_lock:
            return len(self._active_clients)

    @property
    def playwright_proxy(self) -> dict[str, str]:
        with self._state_lock:
            server = self._server
            if server is None:
                raise RuntimeError("pinned browser proxy is not running")
            host, port = server.server_address[:2]
        return {
            "server": f"http://{host}:{port}",
            "username": self.username,
            "password": self.password,
            "bypass": "",
        }

    def raise_if_blocked(self) -> None:
        with self._state_lock:
            reason = self._blocked_reason
            capacity_exhausted = self._capacity_exhausted
        if reason:
            raise PinnedBrowserProxyBlocked(
                f"rendered_url_policy_blocked: {reason}"
            )
        if capacity_exhausted:
            raise PinnedBrowserProxyCapacityError("browser_proxy_capacity_exhausted")

    def _record_capacity_exhausted(self) -> None:
        with self._state_lock:
            self._capacity_exhausted = True

    def _acquire_client_capacity(self) -> bool:
        deadline = self.clock() + self.capacity_wait_seconds
        acquired = []
        try:
            for slot in (self._client_slots, self._global_client_slots):
                while not self._stop_event.is_set():
                    if slot.acquire(blocking=False):
                        acquired.append(slot)
                        break
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        self._record_capacity_exhausted()
                        return False
                    # Closing the proxy wakes capacity waiters immediately.
                    self._stop_event.wait(min(0.05, remaining))
                else:
                    return False
            return True
        finally:
            if len(acquired) != 2:
                for slot in reversed(acquired):
                    slot.release()

    def _release_client_capacity(self) -> None:
        self._global_client_slots.release()
        self._client_slots.release()

    def _connect_upstream(self, request_url: str) -> Any:
        self._raise_if_stopped()
        try:
            policy_result = self.url_policy.check(request_url)
        except Exception as error:
            self._block("url_policy_check_failed", error)
        return self._connect_validated(request_url, policy_result)

    def _connect_validated(self, request_url: str, policy_result: UrlPolicyResult) -> Any:
        self._raise_if_stopped()
        if not policy_result.allowed:
            # Reject secondary connections without discarding a readable page.
            # Main-source failures remain fatal; neither path opens a socket.
            if (
                urlparse(request_url).netloc.casefold()
                != urlparse(self.source_url).netloc.casefold()
            ):
                raise PinnedBrowserProxyBlocked(policy_result.reason)
            self._block(policy_result.reason)

        parsed = urlparse(request_url)
        try:
            explicit_port = parsed.port
        except ValueError as error:
            self._block("invalid_port", error)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            self._block("invalid_proxy_target")
        if explicit_port is not None and explicit_port < 1:
            self._block("invalid_port")
        port = explicit_port if explicit_port is not None else (
            443 if parsed.scheme.casefold() == "https" else 80
        )

        raw_connect_hosts = policy_result.connect_hosts
        if not raw_connect_hosts and policy_result.connect_host:
            raw_connect_hosts = (policy_result.connect_host,)
        connect_hosts: list[str] = []
        for raw_host in raw_connect_hosts:
            try:
                numeric_host = str(ipaddress.ip_address(raw_host))
            except ValueError as error:
                self._block("unvalidated_connect_host", error)
            if numeric_host not in connect_hosts:
                connect_hosts.append(numeric_host)
        if not connect_hosts:
            self._block("missing_validated_connect_host")

        deadline = self.clock() + self.connect_timeout_seconds
        last_error: Exception | None = None
        for connect_host in connect_hosts:
            self._raise_if_stopped()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("pinned_proxy_connect_timeout") from last_error
            try:
                upstream = self.connector(connect_host, port, remaining)
            except Exception as error:
                self._raise_if_stopped()
                last_error = error
                continue
            if self._stop_event.is_set():
                _close_stream(upstream)
                self._raise_if_stopped()
            return upstream
        raise OSError("pinned_proxy_connect_failed") from last_error

    def _authorization_matches(self, value: str) -> bool:
        try:
            candidate = value.strip().encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(
            candidate,
            self._expected_authorization.encode("ascii"),
        )


    def _register_client(self, client: Any) -> bool:
        with self._state_lock:
            if self._stop_event.is_set():
                return False
            self._active_clients.add(client)
        try:
            client.settimeout(self.header_timeout_seconds)
        except OSError:
            self._unregister_client(client)
            return False
        return True

    def _unregister_client(self, client: Any) -> None:
        with self._state_lock:
            self._active_clients.discard(client)


    def _register_upstream(self, upstream: Any) -> None:
        with self._state_lock:
            if self._stop_event.is_set():
                accepted = False
            else:
                self._active_upstreams.add(upstream)
                accepted = True
        if not accepted:
            _close_stream(upstream)
            raise OSError("pinned_proxy_stopped")

    def _unregister_upstream(self, upstream: Any) -> None:
        with self._state_lock:
            self._active_upstreams.discard(upstream)

    def _raise_if_stopped(self) -> None:
        if self._stop_event.is_set():
            raise OSError("pinned_proxy_stopped")

    def _block(self, reason: str, cause: Exception | None = None) -> None:
        clean_reason = reason or "unknown"
        with self._state_lock:
            if not self._blocked_reason:
                self._blocked_reason = clean_reason
        if cause is None:
            raise PinnedBrowserProxyBlocked(clean_reason)
        raise PinnedBrowserProxyBlocked(clean_reason) from cause


class _ThreadingProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False
    daemon_threads = True
    proxy: PinnedBrowserProxy

    def server_bind(self) -> None:
        exclusive_address_use = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_address_use is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive_address_use, 1)
        super().server_bind()


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _ThreadingProxyServer

    def handle(self) -> None:
        proxy = self.server.proxy
        upstream = None
        response_started = False
        registered = False
        capacity_owned = False
        # Bound both active handlers and the number waiting for capacity.
        if not _PROXY_HANDLER_SLOTS.acquire(blocking=False):
            proxy._record_capacity_exhausted()
            _send_proxy_error(self.request, 503, "Proxy capacity exhausted")
            return
        try:
            if not proxy._register_client(self.request):
                return
            registered = True
            if not proxy._acquire_client_capacity():
                if not proxy._stop_event.is_set():
                    _send_proxy_error(self.request, 503, "Proxy capacity exhausted")
                return
            capacity_owned = True
            proxy._raise_if_stopped()
            request_head, buffered_body = _read_request_head(
                self.request,
                deadline=(
                    self.server.proxy.clock() + self.server.proxy.header_timeout_seconds
                ),
                clock=self.server.proxy.clock,
            )
            method, target, version, headers = _parse_request_head(request_head)
            authorization_values = _header_values(headers, "proxy-authorization")
            if (
                len(authorization_values) != 1
                or not self.server.proxy._authorization_matches(
                    authorization_values[0]
                )
            ):
                self.request.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="JobCockpit"\r\n'
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
                return

            if method == "CONNECT":
                request_url = _connect_target_url(
                    target,
                    source_url=self.server.proxy.source_url,
                )
                upstream = self.server.proxy._connect_upstream(request_url)
                self.server.proxy._register_upstream(upstream)
                upstream.settimeout(self.server.proxy.header_timeout_seconds)
                self.request.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n"
                    b"Proxy-Agent: JobCockpit\r\n\r\n"
                )
                response_started = True
                if buffered_body:
                    upstream.sendall(buffered_body)
            else:
                request_url, forwarded_head, body_length = _forward_request_head(
                    method,
                    target,
                    version,
                    headers,
                )
                request_body = _read_exact_request_body(
                    self.request,
                    buffered_body,
                    body_length=body_length,
                    deadline=(
                        self.server.proxy.clock()
                        + self.server.proxy.header_timeout_seconds
                    ),
                    clock=self.server.proxy.clock,
                )
                upstream = self.server.proxy._connect_upstream(request_url)
                self.server.proxy._register_upstream(upstream)
                upstream.settimeout(self.server.proxy.header_timeout_seconds)
                upstream.sendall(forwarded_head)
                response_started = True
                if request_body:
                    upstream.sendall(request_body)

            _relay_bidirectionally(
                self.request,
                upstream,
                stop_event=self.server.proxy._stop_event,
                idle_timeout_seconds=self.server.proxy.relay_idle_timeout_seconds,
                clock=self.server.proxy.clock,
                client_to_upstream=(method == "CONNECT"),
            )
        except PinnedBrowserProxyBlocked:
            if not response_started:
                _send_proxy_error(self.request, 502, "Blocked by URL policy")
        except _ProxyRequestError:
            if not response_started:
                _send_proxy_error(self.request, 400, "Invalid proxy request")
        except (OSError, TimeoutError):
            if not response_started:
                _send_proxy_error(self.request, 502, "Upstream unavailable")
        finally:
            if upstream is not None:
                self.server.proxy._unregister_upstream(upstream)
                _close_stream(upstream)
            if registered:
                proxy._unregister_client(self.request)
            if capacity_owned:
                proxy._release_client_capacity()
            _PROXY_HANDLER_SLOTS.release()


def _read_request_head(
    client: Any,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("proxy_header_timeout")
        client.settimeout(remaining)
        chunk = client.recv(min(8192, _MAX_PROXY_HEADER_BYTES - len(data)))
        if not chunk:
            raise _ProxyRequestError("proxy request ended before headers")
        data.extend(chunk)
        if len(data) >= _MAX_PROXY_HEADER_BYTES and b"\r\n\r\n" not in data:
            raise _ProxyRequestError("proxy request headers too large")
    header_end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:header_end]), bytes(data[header_end:])


def _parse_request_head(
    request_head: bytes,
) -> tuple[str, str, str, list[tuple[str, str]]]:
    try:
        lines = request_head[:-4].decode("latin-1").split("\r\n")
    except UnicodeDecodeError as error:
        raise _ProxyRequestError("invalid proxy request encoding") from error
    if not lines:
        raise _ProxyRequestError("missing proxy request line")
    request_parts = lines[0].split(" ")
    if len(request_parts) != 3:
        raise _ProxyRequestError("invalid proxy request line")
    method, target, version = request_parts
    if not _VALID_METHOD.fullmatch(method) or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise _ProxyRequestError("invalid proxy request line")
    if any(ord(character) < 33 or ord(character) == 127 for character in target):
        raise _ProxyRequestError("invalid proxy request target")

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or line[0].isspace() or ":" not in line:
            raise _ProxyRequestError("invalid proxy header")
        name, value = line.split(":", 1)
        if not _VALID_HEADER_NAME.fullmatch(name):
            raise _ProxyRequestError("invalid proxy header")
        if any(
            (ord(character) < 32 and character != "\t") or ord(character) == 127
            for character in value
        ):
            raise _ProxyRequestError("invalid proxy header")
        headers.append((name, value.strip()))
    return method, target, version, headers


def _connect_target_url(target: str, *, source_url: str = "") -> str:
    parsed = urlparse(f"//{target}")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _ProxyRequestError("invalid CONNECT target")
    try:
        port = parsed.port
    except ValueError as error:
        raise _ProxyRequestError("invalid CONNECT port") from error
    if port is None or port < 1:
        raise _ProxyRequestError("CONNECT target must include a port")
    scheme = "http" if port == 80 else "https"
    source = urlparse(source_url)
    source_hostname = (source.hostname or "").rstrip(".").casefold()
    target_hostname = parsed.hostname.rstrip(".").casefold()
    try:
        source_explicit_port = source.port
    except ValueError:
        source_explicit_port = None
    if source.scheme.casefold() in {"http", "https"} and source_hostname == target_hostname:
        source_port = (
            source_explicit_port
            if source_explicit_port is not None
            else (443 if source.scheme.casefold() == "https" else 80)
        )
        if source_port == port:
            scheme = source.scheme.casefold()
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{_authority(parsed.hostname, port, default_port=default_port)}/"


def _forward_request_head(
    method: str,
    target: str,
    version: str,
    headers: list[tuple[str, str]],
) -> tuple[str, bytes, int]:
    parsed = urlparse(target)
    if (
        parsed.scheme.casefold() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _ProxyRequestError("forward proxy target must be an absolute HTTP URL")
    try:
        explicit_port = parsed.port
    except ValueError as error:
        raise _ProxyRequestError("invalid forward proxy port") from error
    if explicit_port is not None and explicit_port < 1:
        raise _ProxyRequestError("invalid forward proxy port")
    port = explicit_port if explicit_port is not None else 80
    request_url = parsed._replace(fragment="").geturl()
    path = parsed.path or "/"
    if parsed.params:
        path = f"{path};{parsed.params}"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    content_lengths = _header_values(headers, "content-length")
    if (
        any(not value.isascii() or not value.isdecimal() for value in content_lengths)
        or len(set(content_lengths)) > 1
    ):
        raise _ProxyRequestError("ambiguous Content-Length")
    transfer_encodings = _header_values(headers, "transfer-encoding")
    if transfer_encodings:
        raise _ProxyRequestError("Transfer-Encoding is not supported")
    if _header_values(headers, "expect"):
        raise _ProxyRequestError("Expect is not supported")
    body_length = int(content_lengths[0]) if content_lengths else 0
    if body_length > _MAX_PROXY_REQUEST_BODY_BYTES:
        raise _ProxyRequestError("proxy request body too large")

    connection_tokens = {
        token.strip().casefold()
        for value in _header_values(headers, "connection")
        for token in value.split(",")
        if token.strip()
    }
    protected_connection_tokens = {
        "content-length",
        "host",
        "proxy-authorization",
        "transfer-encoding",
    }
    if connection_tokens & protected_connection_tokens:
        raise _ProxyRequestError("Connection header names a protected field")

    removed_headers = {
        "connection",
        "host",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        *connection_tokens,
    }
    forwarded_headers = [
        (name, value)
        for name, value in headers
        if name.casefold() not in removed_headers
    ]
    forwarded_headers.append(
        ("Host", _authority(parsed.hostname, port, default_port=80))
    )
    forwarded_headers.append(("Connection", "close"))
    lines = [f"{method} {path} {version}"]
    lines.extend(f"{name}: {value}" for name, value in forwarded_headers)
    return (
        request_url,
        ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"),
        body_length,
    )


def _read_exact_request_body(
    client: Any,
    initial: bytes,
    *,
    body_length: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    if len(initial) > body_length:
        raise _ProxyRequestError("unexpected bytes after proxy request body")
    body = bytearray(initial)
    while len(body) < body_length:
        remaining_time = deadline - clock()
        if remaining_time <= 0:
            raise TimeoutError("proxy_body_timeout")
        client.settimeout(remaining_time)
        chunk = client.recv(min(64 * 1024, body_length - len(body)))
        if not chunk:
            raise _ProxyRequestError("proxy request ended before body")
        body.extend(chunk)
    return bytes(body)


def _header_values(headers: list[tuple[str, str]], name: str) -> list[str]:
    expected = name.casefold()
    return [value for header, value in headers if header.casefold() == expected]


def _last_header_value(headers: list[tuple[str, str]], name: str) -> str:
    values = _header_values(headers, name)
    return values[-1] if values else ""


def _authority(hostname: str, port: int, *, default_port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return host if port == default_port else f"{host}:{port}"


def _relay_bidirectionally(
    client: Any,
    upstream: Any,
    *,
    stop_event: threading.Event,
    idle_timeout_seconds: float,
    clock: Callable[[], float],
    client_to_upstream: bool = True,
) -> None:
    client.setblocking(False)
    upstream.setblocking(False)
    sockets = (client, upstream)
    pending = {
        client: bytearray(),
        upstream: bytearray(),
    }
    read_open = {
        client: client_to_upstream,
        upstream: True,
    }
    write_shutdown: set[Any] = set()
    if not client_to_upstream:
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        write_shutdown.add(upstream)
    deadline = clock() + idle_timeout_seconds
    # epoll/poll/kqueue avoid select()'s POSIX FD_SETSIZE limit. Windows
    # keeps its socket-compatible selector without changing relay semantics.
    with selectors.DefaultSelector() as selector:
        while not stop_event.is_set():
            if not any(read_open.values()) and not any(pending.values()):
                return
            remaining = deadline - clock()
            if remaining <= 0:
                return
            readable_candidates = [
                source
                for source, destination in ((client, upstream), (upstream, client))
                if read_open[source]
                and len(pending[destination]) < _MAX_RELAY_BUFFER_BYTES
            ]
            writable_candidates = [
                destination for destination in sockets if pending[destination]
            ]
            for stream in sockets:
                events = (selectors.EVENT_READ if stream in readable_candidates else 0)
                events |= selectors.EVENT_WRITE if stream in writable_candidates else 0
                registered = selector.get_map().get(stream)
                if not events:
                    if registered is not None:
                        selector.unregister(stream)
                elif registered is None:
                    selector.register(stream, events)
                elif registered.events != events:
                    selector.modify(stream, events)
            ready = selector.select(min(remaining, 0.25))
            readable = [key.fileobj for key, events in ready if events & selectors.EVENT_READ]
            writable = [key.fileobj for key, events in ready if events & selectors.EVENT_WRITE]
            for source in readable:
                destination = upstream if source is client else client
                capacity = _MAX_RELAY_BUFFER_BYTES - len(pending[destination])
                try:
                    chunk = source.recv(min(64 * 1024, capacity))
                except BlockingIOError:
                    continue
                except OSError:
                    read_open[source] = False
                    continue
                if not chunk:
                    read_open[source] = False
                    continue
                pending[destination].extend(chunk)
                deadline = clock() + idle_timeout_seconds
            for destination in writable:
                try:
                    sent = destination.send(pending[destination])
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if sent <= 0:
                    return
                del pending[destination][:sent]
                deadline = clock() + idle_timeout_seconds
            for source, destination in ((client, upstream), (upstream, client)):
                if (
                    not read_open[source]
                    and not pending[destination]
                    and destination not in write_shutdown
                ):
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_shutdown.add(destination)



def _close_stream(stream: Any) -> None:
    shutdown = getattr(stream, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _send_proxy_error(client: Any, status: int, message: str) -> None:
    reason = {
        400: "Bad Request",
        503: "Service Unavailable",
    }.get(status, "Bad Gateway")
    body = message.encode("ascii")
    response = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Connection: close\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    try:
        client.sendall(response + body)
    except OSError:
        pass


def _connect_numeric_socket(host: str, port: int, timeout: float) -> socket.socket:
    numeric = ipaddress.ip_address(host)
    family = socket.AF_INET6 if numeric.version == 6 else socket.AF_INET
    address: tuple[Any, ...]
    if family == socket.AF_INET6:
        address = (str(numeric), port, 0, 0)
    else:
        address = (str(numeric), port)
    upstream = socket.socket(family, socket.SOCK_STREAM)
    try:
        upstream.settimeout(timeout)
        upstream.connect(address)
        return upstream
    except Exception:
        upstream.close()
        raise
