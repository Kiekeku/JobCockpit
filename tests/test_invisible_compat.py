import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import jobcockpit.ingestion.invisible_compat as compatibility


def test_pinned_route_commands_use_request_page_session_and_answer_once(monkeypatch):
    class Dispatcher:
        def _send(self, *_args):
            pytest.fail("unscoped upstream handler used")

    monkeypatch.setattr(compatibility, "version", lambda _: "0.20.0")
    monkeypatch.setitem(sys.modules, "invisible_playwright._juggler.server", SimpleNamespace(RouteDispatcher=Dispatcher))
    compatibility.install_invisible_playwright_compatibility()
    installed = Dispatcher._send
    compatibility.install_invisible_playwright_compatibility()
    assert Dispatcher._send is installed
    page = Mock()
    dispatcher = Dispatcher()
    dispatcher.answered = False
    dispatcher.request = SimpleNamespace(page=page, request_id="fixture-request")
    dispatcher.dispose = Mock()
    dispatcher._send("Network.resumeInterceptedRequest", {})
    page.send.assert_called_once_with("Network.resumeInterceptedRequest", {"requestId": "fixture-request"})
    page.conn.send.assert_not_called()
    dispatcher.dispose.assert_called_once()
    with pytest.raises(RuntimeError, match="already answered"):
        dispatcher._send("Network.abortInterceptedRequest", {})


def test_compatibility_repair_does_not_modify_another_version(monkeypatch):
    class Dispatcher:
        def _send(self, *_args):
            return None

    original = Dispatcher._send
    monkeypatch.setattr(compatibility, "version", lambda _: "future-version")
    monkeypatch.setitem(sys.modules, "invisible_playwright._juggler.server", SimpleNamespace(RouteDispatcher=Dispatcher))
    compatibility.install_invisible_playwright_compatibility()
    assert Dispatcher._send is original
