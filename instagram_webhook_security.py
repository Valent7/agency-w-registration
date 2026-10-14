"""Validate Meta/Instagram webhook authenticity and minimize logging.

Meta signs the EXACT raw HTTP body using HMAC-SHA256 keyed by the Instagram
app secret. A public POST endpoint must not trust unsigned JSON, even when
the payload contains a legitimate-looking Instagram account ID.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

MAX_WEBHOOK_BYTES = 1_048_576


def valid_instagram_signature(raw_body: bytes, signature: str, app_secret: str) -> bool:
    """Return False for missing, malformed, or forged signatures.

    Never raise and never log the raw body or shared secret.
    """
    if (
        not isinstance(raw_body, bytes)
        or not isinstance(signature, str)
        or not isinstance(app_secret, str)
        or not app_secret
    ):
        return False
    supplied = signature.strip()
    if not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", supplied):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def safe_instagram_event_summary(payload: Any) -> dict:
    """Counts only: no sender IDs, access tokens, private messages or captions."""
    if not isinstance(payload, dict):
        return {"valid": False}
    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        entries = []
    messages = 0
    changes = 0
    for entry in entries[:100]:
        if not isinstance(entry, dict):
            continue
        for field, count in (("messaging", "messages"), ("changes", "changes")):
            items = entry.get(field) or []
            if isinstance(items, list):
                if field == "messaging":
                    messages += len(items)
                else:
                    changes += len(items)
    return {
        "object": str(payload.get("object") or "")[:32],
        "entries": len(entries),
        "messages": messages,
        "changes": changes,
    }
