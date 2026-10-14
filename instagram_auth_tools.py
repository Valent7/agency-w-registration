"""Safe Instagram OAuth token exchange and identity checks for Agency W.

No requests are made on import. The primary exchange uses Meta's documented GET
route. Some Instagram setups reject GET with IGApiException code 100; only that
specific case is retried once using form-encoded POST with the same short token.
Never retry the one-time OAuth authorization code automatically.
"""
from __future__ import annotations

import requests


def _api_reason(response: requests.Response) -> str:
    try:
        obj = response.json()
        error = obj.get("error") if isinstance(obj, dict) else {}
        if isinstance(error, dict):
            msg = error.get("message") or error.get("error_user_msg") or ""
            code = error.get("code")
        else:
            msg = obj.get("error_message") or obj.get("message") or "" if isinstance(obj, dict) else ""
            code = obj.get("code") if isinstance(obj, dict) else None
        cleaned = str(msg).strip().replace("\r", " ").replace("\n", " ")[:350]
        return f"HTTP {response.status_code}" + (f": {cleaned}" if cleaned else "") + (
            f" (code {code})" if code is not None else ""
        )
    except (TypeError, ValueError, AttributeError):
        return f"HTTP {response.status_code}"


def _unsupported_get(response: requests.Response) -> bool:
    try:
        obj = response.json()
        error = obj.get("error") if isinstance(obj, dict) else None
        return bool(
            response.status_code == 400
            and isinstance(error, dict)
            and str(error.get("code")) == "100"
            and "unsupported request" in str(error.get("message") or "").lower()
            and "method type: get" in str(error.get("message") or "").lower()
        )
    except (TypeError, ValueError, AttributeError):
        return False


def exchange_long_lived_ig_token(
    short_token: str,
    client_secret: str,
    *,
    session=requests,
) -> dict:
    if not short_token or not client_secret:
        raise RuntimeError("Instagram OAuth token or app secret missing.")
    params = {
        "grant_type": "ig_exchange_token",
        "client_secret": client_secret,
        "access_token": short_token,
    }
    response = session.get(
        "https://graph.instagram.com/access_token",
        params=params,
        timeout=30,
    )
    if not response.ok and _unsupported_get(response):
        # Retry only after Meta explicitly rejected the GET method. Never redo
        # the one-time authorization-code exchange performed before this step.
        response = session.post(
            "https://graph.instagram.com/access_token",
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    if not response.ok:
        raise RuntimeError("Instagram long-lived token: " + _api_reason(response))

    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Instagram returned a malformed long-lived token response.") from exc
    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        raise RuntimeError("Instagram did not return a long-lived access token.")
    return payload


def fetch_instagram_profile(token: str, *, session=requests) -> dict:
    if not token:
        raise RuntimeError("Instagram access token missing.")
    response = session.get(
        "https://graph.instagram.com/me",
        params={
            # "id" is often app-scoped and does NOT match messaging webhook
            # recipient.id. "user_id" identifies the Instagram professional
            # account that receives Direct and comment webhooks.
            "fields": "id,user_id,username,account_type",
            "access_token": token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError("Instagram profile verification: " + _api_reason(response))
    try:
        obj = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Instagram profile verification returned invalid JSON.") from exc
    if not isinstance(obj, dict) or not str(obj.get("id") or "").strip():
        raise RuntimeError("Instagram did not confirm the account identity.")
    return {
        "id": str(obj["id"]).strip(),
        "user_id": str(obj.get("user_id") or "").strip(),
        "username": str(obj.get("username") or "").strip(),
        "account_type": str(obj.get("account_type") or "").strip(),
    }
