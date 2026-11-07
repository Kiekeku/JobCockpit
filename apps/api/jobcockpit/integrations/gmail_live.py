from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from threading import Lock
from typing import Any

from jobcockpit.core.config import Settings


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_READONLY_SCOPES = [GMAIL_READONLY_SCOPE]
logger = logging.getLogger(__name__)
_gmail_request_lock = Lock()
_next_gmail_request_at = 0.0
_SPAM_OR_TRASH_QUERY_TERM = re.compile(
    r"(?:^|[\s{(])(?:in|label):(?:spam|trash)(?=$|[\s})])",
    re.IGNORECASE,
)


class GmailProviderConfigurationError(RuntimeError):
    pass


class GmailProviderOperationError(RuntimeError):
    """Attach a safe operation stage without exposing provider exception details."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"Gmail provider operation failed during {stage}")
        self.stage = stage
        self.cause = cause


def _wait_for_gmail_slot(interval: float) -> None:
    """Share pacing across previews and ingestions within this process."""
    global _next_gmail_request_at
    with _gmail_request_lock:
        delay = _next_gmail_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _next_gmail_request_at = time.monotonic() + interval


def _transient_gmail_error(error) -> bool:
    if error.resp.status == 429 or 500 <= error.resp.status < 600:
        return True
    if error.resp.status != 403:
        return False
    try:
        reasons = {
            item.get("reason")
            for item in json.loads(error.content).get("error", {}).get("errors", [])
        }
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})


def _gmail_retry_after(error) -> float:
    value = error.resp.get("retry-after")
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _execute_gmail_read(request, settings: Settings):
    """Retry only an idempotent read, retaining earlier messages and page position."""
    from googleapiclient.errors import HttpError

    for attempt in range(settings.gmail_read_max_retries + 1):
        _wait_for_gmail_slot(settings.gmail_request_min_interval_seconds)
        try:
            return request.execute()
        except HttpError as error:
            if attempt == settings.gmail_read_max_retries or not _transient_gmail_error(error):
                raise
            delay = max(_gmail_retry_after(error), min(32.0, 2 ** attempt + random.random()))
            # Leave long provider cooldowns as explicit failures; never retry early.
            if delay > 64:
                raise
            logger.warning("Gmail read returned HTTP %s; retry %s/%s in %.1fs",
                           error.resp.status, attempt + 1, settings.gmail_read_max_retries, delay)
            time.sleep(delay)


class GoogleGmailMessageProvider:
    def __init__(
        self,
        *,
        credentials_loader: Callable[[Settings], object] | None = None,
        service_factory: Callable[[object], Any] | None = None,
    ) -> None:
        self.credentials_loader = credentials_loader or load_google_credentials
        self.service_factory = service_factory or build_gmail_service

    def fetch_messages(
        self,
        *,
        source,
        settings: Settings,
        max_results: int | None = None,
    ) -> list[dict[str, str]]:
        gmail_query = _source_gmail_query(source)
        credentials = self.credentials_loader(settings)
        try:
            service = self.service_factory(credentials)
        except Exception as error:
            raise GmailProviderOperationError("service_build", error) from error
        total_limit = max_results or settings.gmail_max_results
        messages = []
        page_token = None
        while len(messages) < total_limit:
            list_kwargs = {
                "userId": "me",
                "q": gmail_query,
                "maxResults": min(settings.gmail_max_results, total_limit - len(messages), 500),
                "includeSpamTrash": _query_requests_spam_or_trash(gmail_query),
            }
            if page_token:
                list_kwargs["pageToken"] = page_token
            try:
                response = _execute_gmail_read(service.users().messages().list(**list_kwargs),
                                              settings)
            except Exception as error:
                raise GmailProviderOperationError("messages_list", error) from error
            for message_ref in response.get("messages", []):
                if len(messages) >= total_limit:
                    break
                try:
                    raw_message = _execute_gmail_read(
                        service.users()
                        .messages()
                        .get(userId="me", id=message_ref["id"], format="full"),
                        settings,
                    )
                except Exception as error:
                    raise GmailProviderOperationError("message_get", error) from error
                messages.append(gmail_api_message_to_internal(raw_message))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return messages


def _source_gmail_query(source) -> str:
    query = str(getattr(source, "gmail_query", "") or "").strip()
    if not query:
        raise GmailProviderConfigurationError("Gmail job alert source query cannot be blank")
    return query


def _query_requests_spam_or_trash(query: str) -> bool:
    return bool(_SPAM_OR_TRASH_QUERY_TERM.search(query))


def load_google_credentials(settings: Settings):
    token_path = settings.gmail_oauth_token_path
    if not token_path.exists():
        raise GmailProviderConfigurationError(
            f"Gmail OAuth token not found at {token_path}. Run `pnpm --silent cli gmail auth` first."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_READONLY_SCOPES)
    except (OSError, ValueError) as error:
        raise GmailProviderConfigurationError(
            f"Gmail OAuth token at {token_path} is unreadable or malformed. "
            "Run `pnpm --silent cli gmail auth` again."
        ) from error
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as error:
            raise GmailProviderOperationError("oauth_refresh", error) from error
        _write_private_text(token_path, credentials.to_json())
    if not credentials.valid:
        raise GmailProviderConfigurationError("Gmail OAuth credentials are invalid")
    return credentials


def build_gmail_service(credentials):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def authorize_google_gmail(settings: Settings) -> Path:
    client_secret_path = settings.gmail_oauth_client_secret_path
    if not client_secret_path:
        raise GmailProviderConfigurationError(
            "JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH must point to a Google OAuth client JSON file"
        )
    if not client_secret_path.exists():
        raise GmailProviderConfigurationError(f"Gmail OAuth client secret not found at {client_secret_path}")

    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), GMAIL_READONLY_SCOPES)
    except (OSError, ValueError) as error:
        raise GmailProviderConfigurationError(
            f"Gmail OAuth client secret at {client_secret_path} is unreadable or malformed."
        ) from error
    credentials = flow.run_local_server(
        port=settings.gmail_oauth_local_server_port,
        access_type="offline",
        prompt="consent",
    )
    _write_private_text(settings.gmail_oauth_token_path, credentials.to_json())
    return settings.gmail_oauth_token_path


def gmail_api_message_to_internal(raw_message: dict[str, Any]) -> dict[str, str]:
    message_id = str(raw_message.get("id", ""))
    thread_id = str(raw_message.get("threadId", ""))
    payload = raw_message.get("payload") or {}
    headers = _headers_by_name(payload.get("headers", []))
    sender_email = parseaddr(headers.get("from", ""))[1].lower()
    sender_domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
    plain_text, html = _extract_message_bodies(payload)
    if not plain_text:
        plain_text = str(raw_message.get("snippet") or "")
    received_at = _received_at(raw_message, headers)
    body_hash_source = "\n".join(part for part in (plain_text, html) if part)
    id_hash = _hash(message_id)
    return {
        "id": id_hash,
        "id_hash": id_hash,
        "thread_hash": _hash(thread_id),
        "subject": headers.get("subject", ""),
        "sender_hash": _hash(sender_email),
        "sender_domain": sender_domain,
        "received_at": received_at,
        "body_hash": _hash(body_hash_source),
        "plain_text": plain_text,
        "html": html,
    }


def _headers_by_name(headers: list[dict[str, str]]) -> dict[str, str]:
    return {header.get("name", "").lower(): header.get("value", "") for header in headers}


def _extract_message_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    plain_parts = []
    html_parts = []
    for part in _walk_payload_parts(payload):
        mime_type = part.get("mimeType", "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        decoded = _decode_gmail_body(data)
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
    return "\n".join(plain_parts).strip(), "\n".join(html_parts).strip()


def _walk_payload_parts(payload: dict[str, Any]):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_payload_parts(part)


def _decode_gmail_body(data: str) -> str:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _received_at(raw_message: dict[str, Any], headers: dict[str, str]) -> str:
    internal_date = raw_message.get("internalDate")
    if internal_date is not None:
        try:
            timestamp = int(internal_date) / 1000
            return datetime.fromtimestamp(timestamp, UTC).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    date_header = headers.get("date")
    if date_header:
        try:
            return parsedate_to_datetime(date_header).astimezone(UTC).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return ""


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
