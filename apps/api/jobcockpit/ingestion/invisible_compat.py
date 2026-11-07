"""Narrow compatibility repairs for the pinned Invisible Playwright runtime."""

from importlib.metadata import version
from importlib import import_module
from typing import Any


def install_invisible_playwright_compatibility() -> None:
    if version("invisible-playwright") != "0.20.0":
        return
    server = import_module("invisible_playwright._juggler.server")
    RouteDispatcher = server.RouteDispatcher

    if getattr(RouteDispatcher._send, "_jobcockpit_session_scoped", False):
        return

    def send(self: Any, command: str, params: dict[str, Any]) -> Any:
        if self.answered:
            raise RuntimeError("browser route already answered")
        self.answered = True
        # The 0.20.0 wrapper sends route commands to BrowserHandler. The pinned
        # engine implements them on PageHandler, requiring the request's session.
        result = self.request.page.send(
            command, {**params, "requestId": self.request.request_id},
        )
        self.dispose()
        return result

    send._jobcockpit_session_scoped = True
    RouteDispatcher._send = send
