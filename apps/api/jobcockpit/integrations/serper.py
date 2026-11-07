from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from jobcockpit.ingestion.posting_resolution import SearchResult


SERPER_SEARCH_URL = "https://google.serper.dev/search"
_MAX_RESULTS = 20
_MAX_ATTEMPTS = 2
_MAX_RETRY_DELAY_SECONDS = 5.0

Post = Callable[..., Any]
Sleeper = Callable[[float], None]


class SerperSearchClient:
    provider_code = "serper"

    def __init__(
        self,
        *,
        api_key: str,
        country: str = "ca",
        language: str = "en",
        timeout_seconds: float = 15.0,
        max_attempts: int = 2,
        post: Post | None = None,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.api_key = api_key.strip()
        self.country = country.strip().casefold() or "ca"
        self.language = language.strip().casefold() or "en"
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, min(int(max_attempts), _MAX_ATTEMPTS))
        self._post = post or self._http_post
        self._sleep = sleeper
        self.last_error = ""

    def search(self, query: str, limit: int) -> Sequence[SearchResult]:
        self.last_error = ""
        if not self.api_key:
            self.last_error = "serper_missing_api_key"
            return []
        normalized_query = " ".join(str(query or "").split())
        if not normalized_query:
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._post(
                    SERPER_SEARCH_URL,
                    headers={
                        "X-API-KEY": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "q": normalized_query,
                        "num": bounded_limit,
                        "gl": self.country,
                        "hl": self.language,
                    },
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException:
                if attempt < self.max_attempts:
                    self._sleep(0.25)
                    continue
                self.last_error = "serper_timeout"
                return []
            except (httpx.RequestError, OSError):
                if attempt < self.max_attempts:
                    self._sleep(0.25)
                    continue
                self.last_error = "serper_request_failed"
                return []

            status_code = int(getattr(response, "status_code", 0) or 0)
            error_code, retryable = _response_error(status_code)
            if error_code:
                if retryable and attempt < self.max_attempts:
                    self._sleep(_retry_delay(response))
                    continue
                self.last_error = error_code
                return []
            try:
                payload = response.json()
            except (TypeError, ValueError):
                self.last_error = "serper_invalid_response"
                return []
            if not isinstance(payload, dict):
                self.last_error = "serper_invalid_response"
                return []
            organic = payload.get("organic", [])
            if not isinstance(organic, list):
                self.last_error = "serper_invalid_response"
                return []
            return _search_results(organic, bounded_limit)
        self.last_error = "serper_request_failed"
        return []

    @staticmethod
    def _http_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(trust_env=False) as client:
            return client.post(url, **kwargs)


def _response_error(status_code: int) -> tuple[str, bool]:
    if 200 <= status_code < 300:
        return "", False
    if status_code in {401, 403}:
        return "serper_unauthorized", False
    if status_code == 429:
        return "serper_rate_limited", True
    if status_code >= 500:
        return "serper_unavailable", True
    return "serper_http_error", False


def _retry_delay(response: Any) -> float:
    headers = getattr(response, "headers", {})
    try:
        delay = float(headers.get("Retry-After", 0.25))
    except (TypeError, ValueError):
        delay = 0.25
    return max(0.0, min(delay, _MAX_RETRY_DELAY_SECONDS))


def _search_results(rows: list[object], limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title_value = row.get("title")
        url_value = row.get("link")
        snippet_value = row.get("snippet")
        if not isinstance(title_value, str) or not isinstance(url_value, str):
            continue
        if snippet_value is not None and not isinstance(snippet_value, str):
            continue
        title = title_value.strip()
        url = url_value.strip()
        if not title or not url:
            continue
        results.append(
            SearchResult(
                title=title[:500],
                url=url[:2048],
                snippet=(snippet_value or "").strip()[:2000],
            )
        )
        if len(results) >= limit:
            break
    return results


def serper_search_client_from_settings(settings: object) -> SerperSearchClient:
    secret = getattr(settings, "serper_api_key", None)
    api_key = (
        secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or "")
    )
    return SerperSearchClient(
        api_key=api_key,
        country=str(getattr(settings, "serper_country", "ca")),
        language=str(getattr(settings, "serper_language", "en")),
        timeout_seconds=float(getattr(settings, "serper_timeout_seconds", 15.0)),
        max_attempts=int(getattr(settings, "serper_max_attempts", 2)),
    )
