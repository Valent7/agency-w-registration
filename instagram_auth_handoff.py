"""Cross-service signed Instagram login handoff.

Streamlit and Render both already use the Agency W Supabase secret. Streamlit
signs the owner assertion; Render verifies it, then Render itself generates the
Fernet OAuth state that it will later decrypt. Therefore different FERNET_KEY
values across services never break a new Instagram OAuth handshake.

No network calls, secret values, or login side effects on import.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

CONTEXT = b"AgencyW Instagram OAuth handoff v1\0"
MAX_HANDOFF_AGE = 3600
MAX_FUTURE_SKEW = 120


def _urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(payload: str, shared_secret: str) -> str:
    key = str(shared_secret or "").strip()
    if len(key) < 24:
        raise ValueError("Instagram state signing key unavailable.")
    mac = hmac.new(key.encode("utf-8"), CONTEXT + payload.encode("ascii"), hashlib.sha256)
    return _urlsafe(mac.digest())


def create_signed_handoff(
    owner_id: int,
    owner_name: str,
    shared_secret: str,
    *,
    now: int | None = None,
) -> str:
    if isinstance(owner_id, bool) or int(owner_id) <= 0:
        raise ValueError("Invalid owner.")
    payload = {
        "v": 1,
        "owner_id": int(owner_id),
        "owner_name": str(owner_name or "").strip()[:90],
        "purpose": "agency_w_instagram_connect",
        "iat": int(time.time() if now is None else now),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _urlsafe(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"h1.{encoded}.{_signature(encoded, shared_secret)}"


def verify_signed_handoff(
    raw: str,
    shared_secret: str,
    *,
    now: int | None = None,
) -> dict:
    token = str(raw or "").strip()
    if len(token) > 1600 or not token.startswith("h1."):
        raise ValueError("Invalid Instagram handoff.")
    try:
        prefix, payload, sent_mac = token.split(".", 2)
        if prefix != "h1" or not hmac.compare_digest(_signature(payload, shared_secret), sent_mac):
            raise ValueError("Invalid Instagram handoff signature.")
        data = json.loads(_decode_urlsafe(payload).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid Instagram handoff content.")
        ts = int(data.get("iat") or 0)
        current = int(time.time() if now is None else now)
        if ts <= 0 or current - ts > MAX_HANDOFF_AGE or ts - current > MAX_FUTURE_SKEW:
            raise ValueError("Instagram handoff expired.")
        if data.get("v") != 1 or data.get("purpose") != "agency_w_instagram_connect":
            raise ValueError("Invalid Instagram handoff purpose.")
        owner_id = data.get("owner_id")
        if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
            raise ValueError("Invalid owner ID.")
        if not isinstance(data.get("nonce"), str) or len(data["nonce"]) < 12:
            raise ValueError("Invalid Instagram handoff nonce.")
        return {
            "owner_id": str(owner_id),
            "owner_name": str(data.get("owner_name") or "").strip()[:90],
            "purpose": "agency_w_instagram_connect",
        }
    except (UnicodeError, TypeError, OverflowError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Instagram authorization request expired or could not be verified.") from exc
