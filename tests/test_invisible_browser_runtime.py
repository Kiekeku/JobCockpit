"""Opt-in contract check against the pinned engine, using local pages only."""

import os
from pathlib import Path
from contextlib import ExitStack
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
from jobcockpit.ingestion.posting_discovery import ExternalPostingHint
from jobcockpit.ingestion.posting_discovery import (
    _enter_pinned_browser_proxy, _install_rendered_context_hardening,
    _verify_rendered_page_hardening, _PlaywrightUrlPolicyGuard,
)
from jobcockpit.ingestion.invisible_session import PreparationUrlPolicy
from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy, UrlFetchPolicy


pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("JOBCOCKPIT_TEST_INVISIBLE_BROWSER") != "1",
        reason="Opt in after installing the invisible-browser extra and fetching its engine",
    ),
]


@pytest.mark.parametrize("engine", ["firefox", "chromium"])
def test_public_redirects_preserve_assets_cookies_and_post_methods(tmp_path, engine):
    import base64
    import hashlib
    module = b"import {value} from './chunk.js'; document.body.dataset.module = value; document.body.dataset.moduleUrl = import.meta.url;"
    integrity = "sha256-" + base64.b64encode(hashlib.sha256(module).digest()).decode()
    target_requests = []
    resource_posts = []
    post_requests = []
    get_requests = []
    observed_requests = []
    private_requests = []

    class PrivateHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            private_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_args):
            pass

    forbidden = ThreadingHTTPServer(("127.0.0.1", 0), PrivateHandler)
    private_url = f"http://127.0.0.1:{forbidden.server_port}/private.js"

    class AssetHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            resource_posts.append((self.path, body, self.headers.get("Origin")))
            self.send_response(307 if self.path == "/api-start" else 200)
            self.send_header("Access-Control-Allow-Origin", source)
            if self.path == "/api-start":
                self.send_header("Location", "/v2/api")
            self.end_headers()
            if self.path != "/api-start":
                self.wfile.write(body)

        def do_GET(self):
            import gzip
            target_requests.append(self.path)
            redirects = {"/module-start.js": "/v2/main.js", "/style-start.css": "/v2/style.css",
                         "/private-start.js": private_url}
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.send_header("Access-Control-Allow-Origin", source)
                self.end_headers()
                return
            resources = {
                "/landing": ("text/html", b"<body>Unapproved final source</body>"),
                "/v2/main.js": ("application/javascript", module),
                "/v2/chunk.js": ("application/javascript", b"export const value = 'loaded';"),
                "/v2/style.css": ("text/css", b"@import './nested.css'; body {background-image: url('./tile.svg');}"),
                "/v2/nested.css": ("text/css", b"body {color: rgb(12, 34, 56);}"),
                "/v2/tile.svg": ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
            }
            if self.path in resources:
                mime, body = resources[self.path]
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Access-Control-Allow-Origin", source)
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/asset-start.js":
                self.send_response(302)
                self.send_header("Location", "/asset.js")
                self.send_header("Set-Cookie", "assetCookie=present; Path=/")
                self.send_header("Set-Cookie", "expiredResourceSession=; Max-Age=0; Path=/")
                self.end_headers()
                return
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", source + "/job")
                self.end_headers()
                return
            self.send_response(200)
            if self.path == "/asset.js":
                assert "assetCookie=present" in self.headers.get("Cookie", "")
                assert "expiredResourceSession=" not in self.headers.get("Cookie", "")
            self.send_header("Access-Control-Allow-Origin", source)
            self.send_header("Content-Type", "application/json" if self.path == "/worker-data" else "application/javascript")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(gzip.compress(b'{"loaded":true}' if self.path == "/worker-data" else b"document.body.dataset.publicAsset = 'loaded';"))

        def log_message(self, *_args):
            pass

    assets = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
    asset_url = f"http://127.0.0.1:{assets.server_port}"

    class PostingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            get_requests.append(self.path)
            if self.path in {"/allowed", "/roundtrip"}:
                self.send_response(302)
                self.send_header("Location", "/job" if self.path == "/allowed" else asset_url + "/hop")
                self.send_header("Set-Cookie", "redirectCookie=present; Path=/")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((f'<body data-instance="{len(get_requests)}">Job <script src="{asset_url}/asset-start.js"></script>'
                f'<script type="module" src="{asset_url}/module-start.js" integrity="{integrity}" crossorigin="anonymous"></script>'
                f'<link rel="stylesheet" href="{asset_url}/style-start.css">'
                '<form action="/submit303" method="post"><input name="fixture" value="kept">'
                '<button>303</button></form>'
                '<form action="/submit307" method="post"><input name="fixture" value="kept">'
                '<button>307</button></form></body>').encode("utf-8"))

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            post_requests.append((self.path, body, self.headers.get("Content-Type"), self.headers.get("Origin")))
            if self.path == "/post-landing":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<body>POST retained</body>")
                return
            self.send_response(303 if self.path == "/submit303" else 307)
            self.send_header("Location", "/job" if self.path == "/submit303" else "/post-landing")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PostingHandler)
    source = f"http://127.0.0.1:{server.server_port}"
    for item in (server, assets, forbidden):
        threading.Thread(target=item.serve_forever, daemon=True).start()
    policy = ScheduledRenderedUrlPolicy(allowed_localhosts={
        f"127.0.0.1:{server.server_port}", f"127.0.0.1:{assets.server_port}",
    })
    try:
        with ExitStack() as stack:
            proxy = _enter_pinned_browser_proxy(stack, source, policy)
            if engine == "firefox":
                resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path / "firefox")
                context = resolver._launch_context(stack, tmp_path / "firefox", proxy, 10000)
            else:
                from playwright.sync_api import sync_playwright
                playwright = stack.enter_context(sync_playwright())
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                stack.callback(browser.close)
                context = browser.new_context(proxy=proxy.playwright_proxy, service_workers="block")
                stack.callback(context.close)
            _install_rendered_context_hardening(context)
            guard = _PlaywrightUrlPolicyGuard(source_url=source, url_policy=policy)
            routed_requests = []
            original_request_handler = guard._handle_request
            def handle_request(route, request, **kwargs):
                routed_requests.append(request.url)
                original_request_handler(route, request, **kwargs)
            guard._handle_request = handle_request
            guard.install(context, route_websockets=engine == "chromium")
            stack.callback(guard.close)
            page = context.new_page()
            context.add_cookies([{
                "name": "expiredResourceSession", "value": "stale", "url": asset_url,
            }])
            context.on("request", lambda request: observed_requests.append((request.url, request.resource_type, request.is_navigation_request(), request.headers.get("sec-fetch-dest"))))
            page.goto(source + "/allowed")
            guard.raise_if_blocked()
            assert page.url == source + "/job"
            try:
                page.wait_for_function("document.body.dataset.publicAsset === 'loaded'")
            except Exception:
                pytest.fail(f"public asset failed: {guard.blocked_subresource_diagnostic()}, target requests={target_requests}, observed={observed_requests}, routed={routed_requests}")
            assert "redirectCookie=present" in page.evaluate("document.cookie")
            assert "assetCookie=present" in page.evaluate("document.cookie")
            assert "expiredResourceSession=" not in page.evaluate("document.cookie")
            page.wait_for_function("document.body.dataset.module === 'loaded'", timeout=3000)
            assert page.evaluate("document.body.dataset.moduleUrl") == asset_url + "/v2/main.js"
            assert page.evaluate("getComputedStyle(document.body).color") == "rgb(12, 34, 56)"
            assert {"/v2/chunk.js", "/v2/nested.css", "/v2/tile.svg"} <= set(target_requests)
            response = page.evaluate("""async url => {
                const response = await fetch(url, {method: 'POST', body: 'fixture=kept'});
                return {url: response.url, redirected: response.redirected, body: await response.text()};
            }""", asset_url + "/api-start")
            assert response == {"url": asset_url + "/v2/api", "redirected": True, "body": "fixture=kept"}
            assert resource_posts == [("/api-start", b"fixture=kept", source), ("/v2/api", b"fixture=kept", source)]
            private_loaded = page.evaluate("""url => new Promise(resolve => {
                const script = document.createElement('script');
                script.src = url; script.onload = () => resolve(true); script.onerror = () => resolve(false);
                document.body.append(script);
            })""", asset_url + "/private-start.js")
            assert private_loaded is False
            assert private_requests == []
            guard.raise_if_blocked()  # A denied resource does not invalidate the document.
            if engine == "firefox":
                assert routed_requests  # A disabled interception controller must fail this test.
                assert page.evaluate("typeof navigator.serviceWorker") == "undefined"
                assert page.evaluate("""() => {
                    const frame = document.createElement('iframe'); document.body.append(frame);
                    return typeof frame.contentWindow.navigator.serviceWorker;
                }""") == "undefined"
            worker_loaded = page.evaluate("""url => new Promise(resolve => {
                const script = `fetch(${JSON.stringify(url)}).then(r => r.json()).then(r => postMessage(r.loaded), () => postMessage(false));`;
                const worker = new Worker(URL.createObjectURL(new Blob([script], {type:'text/javascript'})));
                worker.onmessage = e => { worker.terminate(); resolve(e.data); };
                setTimeout(() => { worker.terminate(); resolve(false); }, 5000);
            })""", asset_url + "/worker-data")
            assert worker_loaded is True
            assert "/worker-data" in target_requests
            with page.expect_event("domcontentloaded"):
                page.get_by_role("button", name="303", exact=True).click()
            assert page.url == source + "/job"
            page.wait_for_function("document.body.dataset.publicAsset === 'loaded'")
            with page.expect_event("domcontentloaded"):
                page.get_by_role("button", name="307", exact=True).click()
            page.get_by_text("POST retained", exact=True).wait_for()
            assert page.url == source + "/post-landing"
            assert page.locator("body").inner_text() == "POST retained"
            assert [row[:2] for row in post_requests] == [
                ("/submit303", b"fixture=kept"), ("/submit307", b"fixture=kept"),
                ("/post-landing", b"fixture=kept"),
            ]
            assert post_requests[1][2:] == post_requests[2][2:]
            page.goto(source + "/roundtrip")
            assert page.url == source + "/job"
            guard.ensure_final_url_allowed(page.url)
            page.wait_for_function("document.body.dataset.publicAsset === 'loaded'")
            assert "/hop" in target_requests
            assert "/asset.js" in target_requests
            # Public destinations may be visited, but only approved final pages
            # can be accepted as evidence for the source job.
            page.goto(asset_url + "/landing")
            assert "/landing" in target_requests
            with pytest.raises(Exception, match="rendered_cross_origin_blocked"):
                guard.ensure_final_url_allowed(page.url)
    finally:
        for item in (server, assets, forbidden):
            item.shutdown()
            item.server_close()


def test_linkedin_capture_bounds_the_requested_job_before_recommendations(tmp_path, monkeypatch):
    from invisible_playwright import InvisiblePlaywright
    from jobcockpit.ingestion.posting_discovery import _playwright_page_snapshot
    monkeypatch.setenv("INVISIBLE_PLAYWRIGHT_CACHE_DIR", str(Path("var/invisible-playwright-cache").resolve()))
    url = "https://www.linkedin.com/jobs/view/1234567890"
    hint = ExternalPostingHint(source_url=url, source_kind="linkedin")
    # Minimized from the observed signed-in LinkedIn posting: the requested
    # AboutTheJob component ends before an oversized More jobs section.
    with InvisiblePlaywright(profile_dir=str(tmp_path / "firefox"), headless=True,
                             timezone="America/Denver", locale="en-US") as context:
        page = context.new_page()
        class Surface:
            def __init__(self, source):
                self.url = source
            def __getattr__(self, name):
                return getattr(page, name)
        for component_id, body, source, truncated in [
            ("1234567890", "Build platforms and analyze operational data. " * 80, url, False),
            ("1234567890", "A very long job description. " * 1500, url, True),
            ("9876543210", "Build platforms. " * 80, url, True),
            ("1234567890", "Build platforms. " * 80, url.replace("1234567890", "9876543210"), True),
        ]:
            page.set_content(f'<title>Engineer | Acme | LinkedIn</title><main><h1>Engineer</h1><p>Acme</p>'
                f'<div id="JobDetails_AboutTheJob_{component_id}"><h2>About the job</h2><p>{body}</p></div>'
                '<section><h2>More jobs</h2>' + '<p>Other employer hiring unrelated roles.</p>' * 1500 + '</section></main>')
            snapshot = _playwright_page_snapshot(Surface(source), hint)
            assert snapshot.content_truncated is truncated
            if not truncated:
                assert "operational data" in snapshot.body_text
                assert "Other employer" not in snapshot.body_text


def _local_scheduled_worker(connection, settings, hint, method):
    """Allow only this test server in the otherwise unchanged production worker."""
    from urllib.parse import urlparse
    from jobcockpit.ingestion import scheduled_browser, url_policy

    original = url_policy.ScheduledRenderedUrlPolicy
    url_policy.ScheduledRenderedUrlPolicy = lambda **options: original(
        allowed_localhosts={urlparse(hint.source_url).netloc}, **options,
    )
    scheduled_browser._render_in_worker(connection, settings, hint, method)


@pytest.mark.parametrize("cancel", [False, True])
def test_scheduled_real_firefox_capture_and_cancellation(tmp_path, monkeypatch, cancel):
    import time
    import psutil
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion import scheduled_browser

    requested = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.set()
            if cancel:
                release.wait(timeout=30)
            body = b'<html><title>Platform Engineer - Acme</title><body>Platform Engineer Company: Acme Build internal platforms.</body></html>'
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(scheduled_browser, "_render_in_worker", _local_scheduled_worker)
    # Preserve the real target in the spawned child; the patch exists only here.
    settings = Settings(
        invisible_playwright_user_data_dir=tmp_path / "firefox",
        posting_rendered_fallback_retries=0,
        posting_rendered_fallback_min_interval_seconds=0,
        posting_rendered_fallback_timeout_seconds=30,
    )
    resolver = scheduled_browser.ScheduledInvisiblePlaywrightResolver(settings, time.monotonic() + 60)
    results = []
    worker = threading.Thread(target=lambda: results.append(resolver.load_page(ExternalPostingHint(
        source_url=f"http://127.0.0.1:{server.server_port}/job", source_kind="generic",
        title="Platform Engineer", company="Acme",
    ))))
    worker.start()
    descendants = []
    try:
        assert requested.wait(timeout=45), results
        if resolver._process.is_alive():
            descendants = psutil.Process(resolver._process.pid).children(recursive=True)
        assert descendants
        if cancel:
            resolver.cancel_active_pages()
        worker.join(timeout=35)
        assert not worker.is_alive()
        assert not resolver._process.is_alive()
        _, alive = psutil.wait_procs(descendants, timeout=5)
        assert not alive
        if cancel:
            assert "cancelled" in results[0].error
        else:
            assert results[0].status == "resolved", results[0].error
            assert "Platform Engineer" in results[0].posting.raw_text
    finally:
        resolver.cancel_active_pages()
        release.set()
        worker.join(timeout=5)
        server.shutdown()
        server.server_close()


def test_real_firefox_capture_session_reuse_redirect_guard_and_cleanup(tmp_path):
    import psutil

    forbidden_requests = []

    class ForbiddenHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            forbidden_requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    forbidden = ThreadingHTTPServer(("127.0.0.1", 0), ForbiddenHandler)
    threading.Thread(target=forbidden.serve_forever, daemon=True).start()

    class PostingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{forbidden.server_port}/private")
                self.end_headers()
                return
            body = b"""<html><title>Platform Engineer - Acme</title><body>
                <h1>Platform Engineer</h1><p>Company: Acme</p><p>Build internal platforms.</p>
                <script>
                document.body.dataset.networkApis =
                    [typeof WebSocket, typeof Worker, typeof SharedWorker].join(',');
                document.body.dataset.previousSession = document.cookie;
                document.cookie = 'fixtureSession=present; max-age=600; path=/';
                </script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PostingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_port}"
    profile_dir = tmp_path / "firefox"
    resolver = InvisiblePlaywrightRenderedPageResolver(
        user_data_dir=profile_dir, timeout_seconds=15, auth_wall_wait_seconds=0,
        url_policy=UrlFetchPolicy(allowed_localhosts={f"127.0.0.1:{server.server_port}"}),
    )

    def resolve(path):
        return resolver.resolve(ExternalPostingHint(
            source_url=root + path, source_kind="generic", company="Acme", title="Platform Engineer",
        ))

    try:
        first = resolve("/job")
        assert first.status == "resolved", first.error
        assert 'data-network-apis="function,function,function"' in first.posting.rendered_html
        second = resolve("/job")
        assert second.status == "resolved", second.error
        assert 'data-previous-session="fixtureSession=present"' in second.posting.rendered_html
        blocked = resolve("/redirect")
        assert blocked.status == "needs_review"
        assert blocked.posting is None
        assert forbidden_requests == []
        for child in psutil.Process().children(recursive=True):
            try:
                assert str(profile_dir) not in " ".join(child.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    finally:
        server.shutdown()
        server.server_close()
        forbidden.shutdown()
        forbidden.server_close()


@pytest.mark.parametrize("preparation", [False, True])
def test_native_workers_and_websockets_cannot_reach_unapproved_local_port(tmp_path, preparation):
    """Worker/socket APIs remain usable; their connections must still use our proxy."""
    import base64
    import hashlib

    forbidden_requests = []
    allowed_requests = []

    class ForbiddenHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            forbidden_requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    class ForbiddenServer(ThreadingHTTPServer):
        def get_request(self):
            connection = super().get_request()
            forbidden_requests.append("tcp-connection")
            return connection

    forbidden = ForbiddenServer(("127.0.0.1", 0), ForbiddenHandler)
    threading.Thread(target=forbidden.serve_forever, daemon=True).start()

    class AllowedHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            allowed_requests.append(self.path)
            if self.headers.get("Upgrade", "").lower() == "websocket":
                key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"<html><body>Local preparation fixture</body></html>")

        def log_message(self, *_args):
            pass

    allowed = ThreadingHTTPServer(("127.0.0.1", 0), AllowedHandler)
    threading.Thread(target=allowed.serve_forever, daemon=True).start()
    source = f"http://127.0.0.1:{allowed.server_port}"
    target = f"http://127.0.0.1:{forbidden.server_port}"
    resolver = InvisiblePlaywrightRenderedPageResolver(
        user_data_dir=tmp_path / "firefox", headless=True,
    )
    policy = PreparationUrlPolicy(source, UrlFetchPolicy(allowed_localhosts={f"127.0.0.1:{allowed.server_port}"}))
    try:
        with ExitStack() as stack:
            proxy = _enter_pinned_browser_proxy(stack, source, policy)
            context = resolver._launch_context(stack, tmp_path / "firefox", proxy, 10000, preparation=preparation)
            _install_rendered_context_hardening(context)
            page = context.new_page()
            _verify_rendered_page_hardening(page)
            page.goto(source)
            assert page.evaluate("() => [typeof Worker, typeof SharedWorker, typeof WebSocket]") == ["function"] * 3
            result = page.evaluate("""async ({source, target}) => {
                const socket = url => new Promise(resolve => {
                    const ws = new WebSocket(url.replace('https:', 'wss:').replace('http:', 'ws:'));
                    ws.onopen = () => { resolve(true); ws.close(); };
                    ws.onerror = () => resolve(false);
                    setTimeout(() => { resolve(false); ws.close(); }, 5000);
                });
                const script = `
                    const socket = ${socket.toString()};
                    async function run() {
                        const allowed = await fetch('${source}/worker-allowed').then(r => r.ok, () => false);
                        const blocked = await fetch('${target}/worker-forbidden').then(() => true, () => false);
                        const wsAllowed = await socket('${source}/worker-socket');
                        const wsBlocked = await socket('${target}/worker-socket-forbidden');
                        return {allowed, blocked, wsAllowed, wsBlocked};
                    }
                `;
                const worker = new Promise(resolve => {
                    setTimeout(() => resolve({timedOut: true}), 20000);
                    const url = URL.createObjectURL(new Blob([script + 'run().then(r => postMessage(r));'], {type: 'text/javascript'}));
                    const w = new Worker(url);
                    w.onmessage = e => { w.terminate(); URL.revokeObjectURL(url); resolve(e.data); };
                });
                const shared = new Promise(resolve => {
                    setTimeout(() => resolve({timedOut: true}), 20000);
                    const url = URL.createObjectURL(new Blob([script + 'onconnect = e => run().then(r => e.ports[0].postMessage(r));'], {type: 'text/javascript'}));
                    const w = new SharedWorker(url);
                    w.port.onmessage = e => { w.port.close(); URL.revokeObjectURL(url); resolve(e.data); };
                    w.port.start();
                });
                return {worker: await worker, shared: await shared,
                    socketAllowed: await socket(source + '/page-socket'),
                    socketBlocked: await socket(target + '/page-socket-forbidden'),
                    rtc: typeof RTCPeerConnection, webtransport: typeof WebTransport};
            }""", {"source": source, "target": target})
            for kind in ("worker", "shared"):
                assert result[kind] == {"allowed": True, "blocked": False, "wsAllowed": True, "wsBlocked": False}, result
            assert result['socketAllowed'] is True, result
            assert result['socketBlocked'] is False, result
            assert result['rtc'] == result['webtransport'] == 'undefined'
            assert forbidden_requests == []
            assert '/worker-allowed' in allowed_requests
    finally:
        allowed.shutdown()
        allowed.server_close()
        forbidden.shutdown()
        forbidden.server_close()


def test_automatic_preparation_relaunches_guarded_browser_and_reuses_profile(tmp_path, monkeypatch):
    import json
    import jobcockpit.ingestion.invisible_browser as integration

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if 'prepared=yes' in self.headers.get('Cookie', ''):
                body = """<html><title>Platform Engineer - Acme</title><body>
                    <h1>Platform Engineer</h1><p>Company: Acme</p>
                    <h2>Responsibilities</h2><p>Build internal platforms, maintain deployment
                    pipelines, improve reliability, and collaborate with engineering teams.</p>
                    <h2>Qualifications</h2><p>Experience with Python, Linux, databases,
                    automated testing, and cloud infrastructure.</p></body></html>"""
            else:
                body = '''<title>Are you a human?</title><body>Are you a human?
                    <button onclick="document.cookie='prepared=yes; max-age=600; path=/'; location.reload()">Verify fixture</button></body>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = f'http://127.0.0.1:{server.server_port}/job'
    original = integration.verification_site
    monkeypatch.setattr(integration, 'verification_site', lambda url: 'eluta' if url == source else original(url))
    clicks = []

    def verify(page, url, progress, *, timeout_ms):
        assert url == source
        if not progress.checkbox_clicked:
            # Native APIs exist in this phase while WebRTC remains unavailable.
            assert page.evaluate('() => typeof Worker') == 'function'
            assert page.evaluate('() => typeof RTCPeerConnection') == 'undefined'
            progress.checkbox_clicked = True
            page.get_by_role('button', name='Verify fixture', exact=True).click(timeout=timeout_ms)
            clicks.append(True)

    monkeypatch.setattr(integration, 'advance_verification', verify)
    resolver = InvisiblePlaywrightRenderedPageResolver(
        user_data_dir=tmp_path / 'firefox', headless=True,
        timeout_seconds=10, prepare_timeout_seconds=15,
        auth_wall_settle_seconds=0,
        url_policy=UrlFetchPolicy(allowed_localhosts={f'127.0.0.1:{server.server_port}'}),
    )
    hint = ExternalPostingHint(source_url=source, source_kind='generic', company='Acme', title='Platform Engineer')
    try:
        first = resolver.resolve(hint)
        assert first.status == 'resolved', first.error
        assert 'automatic session preparation completed' in first.posting.evidence
        state = json.loads((resolver._profile_directory(hint) / '.jobcockpit-preparation.json').read_text())
        assert state['status'] == 'ready'
        second = resolver.resolve(hint)
        assert second.status == 'resolved', second.error
        assert 'automatic session preparation completed' not in second.posting.evidence
        assert clicks == [True]
    finally:
        server.shutdown()
        server.server_close()
