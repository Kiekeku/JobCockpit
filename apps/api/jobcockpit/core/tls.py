"""Narrow workaround for native Windows CA-file crashes (see dev-crash-dumps.md)."""
from __future__ import annotations

import os
import ssl
import sys
from functools import wraps
from pathlib import Path

import certifi


def install_windows_certifi_workaround() -> bool:
    """Keep the same trust roots while avoiding OpenSSL's native FILE/CRT path.

    HTTPX and Requests (including the browser helper) share this SSLContext method.
    Only their unmodified Certifi bundle is redirected; custom trust stores,
    certificate directories, client keys, and verification policy are untouched.
    Install once during API or CLI startup, before worker threads start.
    """
    if sys.platform != "win32":
        return False
    original = ssl.SSLContext.load_verify_locations
    if getattr(original, "_jobcockpit_certifi_memory", False):
        return True
    bundle_path = os.path.normcase(os.path.abspath(certifi.where()))
    # Python owns the file lifecycle. Failure remains a Python startup exception
    # rather than entering the native fgets/_read fail-fast path seen in both dumps.
    bundle_pem = Path(bundle_path).read_text(encoding="ascii")

    @wraps(original)
    def load_verify_locations(self, cafile=None, capath=None, cadata=None):
        if cafile is not None and cadata is None:
            supplied_path = os.path.normcase(os.path.abspath(os.fsdecode(cafile)))
            if supplied_path == bundle_path:
                return original(self, cafile=None, capath=capath, cadata=bundle_pem)
        return original(self, cafile=cafile, capath=capath, cadata=cadata)

    load_verify_locations._jobcockpit_certifi_memory = True
    ssl.SSLContext.load_verify_locations = load_verify_locations
    return True
