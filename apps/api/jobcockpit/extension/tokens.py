from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from jobcockpit.core.config import Settings


@dataclass(frozen=True)
class ExtensionDownloadToken:
    package_id: str
    document_id: str
    expires_at: int


def create_extension_download_token(
    settings: Settings,
    *,
    package_id: str,
    document_id: str,
    expires_in_seconds: int = 300,
) -> str:
    payload = {
        "package_id": package_id,
        "document_id": document_id,
        "expires_at": int(time.time()) + expires_in_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_part = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = _sign(settings, payload_part)
    return f"{payload_part}.{signature}"


def verify_extension_download_token(settings: Settings, token: str) -> ExtensionDownloadToken | None:
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _sign(settings, payload_part)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = payload_part + ("=" * (-len(payload_part) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    expires_at = int(payload.get("expires_at", 0))
    if expires_at < int(time.time()):
        return None
    package_id = payload.get("package_id")
    document_id = payload.get("document_id")
    if not isinstance(package_id, str) or not isinstance(document_id, str):
        return None
    return ExtensionDownloadToken(package_id=package_id, document_id=document_id, expires_at=expires_at)


def _sign(settings: Settings, payload_part: str) -> str:
    digest = hmac.new(
        settings.get_extension_token_secret().encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
