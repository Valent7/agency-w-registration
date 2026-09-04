from __future__ import annotations

"""OAuth 2.1 + PKCE for Agency W VK Scout.

Initial authorization is started from Streamlit. The access/refresh token pair
is encrypted with the existing FERNET_KEY and stored in Supabase. The background
worker reads the pair and refreshes it shortly before expiry.

Refresh tokens are rotating (one-time use), therefore they are never treated as
static Render/Streamlit environment variables.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

try:
    import streamlit as st
except Exception:  # worker mode
    st = None

UTC = timezone.utc
VKID_AUTHORIZE_URL = "https://id.vk.ru/authorize"
VKID_TOKEN_URL = "https://id.vk.ru/oauth2/auth"


class VKScoutOAuthError(RuntimeError):
    pass


def _secret(name: str, default: str = "") -> str:
    if st is not None:
        try:
            value = st.secrets.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        except Exception:
            pass
    return str(os.getenv(name, default) or default).strip()


def _cfg() -> dict[str, str]:
    cfg = {
        "supabase_url": _secret("SUPABASE_URL").rstrip("/"),
        "supabase_key": _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_SERVICE_ROLE_KEY"),
        "fernet_key": _secret("FERNET_KEY"),
        "client_id": _secret("VK_SCOUT_APP_ID", "54753903"),
        "redirect_uri": _secret("VK_SCOUT_REDIRECT_URI", "https://agency-w.streamlit.app/"),
        # groups.getMembers does not itself require a write permission. Keep
        # scope empty by default; it can be widened later without a code change.
        "scope": _secret("VK_SCOUT_SCOPE", ""),
    }
    missing = [
        key for key in ("supabase_url", "supabase_key", "fernet_key", "client_id", "redirect_uri")
        if not cfg[key]
    ]
    if missing:
        raise VKScoutOAuthError("Не хватает настроек VK Scout OAuth: " + ", ".join(missing))
    return cfg


def _headers(prefer: str = "") -> dict[str, str]:
    cfg = _cfg()
    out = {
        "apikey": cfg["supabase_key"],
        "Authorization": f"Bearer {cfg['supabase_key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        out["Prefer"] = prefer
    return out


def _sb_get(table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = _cfg()
    r = requests.get(
        f"{cfg['supabase_url']}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=30,
    )
    if not r.ok:
        raise VKScoutOAuthError(f"Supabase GET {table}: {r.status_code}: {r.text[:500]}")
    data = r.json() if r.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_post(table: str, payload: dict[str, Any], *, on_conflict: str = "") -> list[dict[str, Any]]:
    cfg = _cfg()
    url = f"{cfg['supabase_url']}/rest/v1/{table}"
    prefer = "return=representation"
    if on_conflict:
        url += "?on_conflict=" + on_conflict
        prefer = "resolution=merge-duplicates,return=representation"
    r = requests.post(url, headers=_headers(prefer), json=payload, timeout=30)
    if not r.ok:
        raise VKScoutOAuthError(f"Supabase POST {table}: {r.status_code}: {r.text[:500]}")
    data = r.json() if r.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_patch(table: str, filters: dict[str, Any], payload: dict[str, Any], *, return_rows: bool = False) -> list[dict[str, Any]]:
    cfg = _cfg()
    r = requests.patch(
        f"{cfg['supabase_url']}/rest/v1/{table}",
        headers=_headers("return=representation" if return_rows else "return=minimal"),
        params=filters,
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise VKScoutOAuthError(f"Supabase PATCH {table}: {r.status_code}: {r.text[:500]}")
    if not return_rows or not r.text.strip():
        return []
    data = r.json()
    return data if isinstance(data, list) else []


def _sb_delete(table: str, filters: dict[str, Any]) -> None:
    cfg = _cfg()
    r = requests.delete(
        f"{cfg['supabase_url']}/rest/v1/{table}",
        headers=_headers("return=minimal"),
        params=filters,
        timeout=30,
    )
    if not r.ok:
        raise VKScoutOAuthError(f"Supabase DELETE {table}: {r.status_code}: {r.text[:500]}")


def _cipher() -> Fernet:
    return Fernet(_cfg()["fernet_key"].encode("utf-8"))


def _encrypt(value: str) -> str:
    if not value:
        return ""
    return _cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise VKScoutOAuthError("Не удалось расшифровать VK Scout token") from exc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def begin_vk_scout_authorization(owner_id: int) -> str:
    """Creates a short-lived PKCE transaction and returns VK ID authorize URL."""
    cfg = _cfg()
    owner_id = int(owner_id)
    verifier, challenge = _pkce_pair()
    state = "vks_" + secrets.token_urlsafe(32)
    now = _now()

    # Remove stale requests for this owner; one active attempt is enough.
    try:
        _sb_delete("agency_vk_oauth_pending", {"owner_telegram_id": f"eq.{owner_id}"})
    except Exception:
        pass

    _sb_post(
        "agency_vk_oauth_pending",
        {
            "state": state,
            "owner_telegram_id": owner_id,
            "code_verifier_encrypted": _encrypt(verifier),
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(minutes=15)),
        },
    )

    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "state": state,
    }
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    return VKID_AUTHORIZE_URL + "?" + urlencode(params)


def _extract_callback_params(query_params: Any) -> dict[str, str]:
    def pick(name: str) -> str:
        try:
            value = query_params.get(name, "")
        except Exception:
            value = ""
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    out = {name: pick(name) for name in ("code", "state", "device_id", "type", "expires_in", "payload")}
    if out["payload"]:
        try:
            payload = json.loads(out["payload"])
            if isinstance(payload, dict):
                for name in ("code", "state", "device_id", "type", "expires_in"):
                    if not out[name] and payload.get(name) is not None:
                        out[name] = str(payload.get(name))
        except json.JSONDecodeError:
            pass
    return out


def _exchange_code(*, code: str, device_id: str, state: str, verifier: str) -> dict[str, Any]:
    cfg = _cfg()
    r = requests.post(
        VKID_TOKEN_URL,
        params={
            "grant_type": "authorization_code",
            "redirect_uri": cfg["redirect_uri"],
            "client_id": cfg["client_id"],
            "code_verifier": verifier,
            "state": state,
            "device_id": device_id,
        },
        data={"code": code},
        timeout=45,
    )
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not r.ok or not isinstance(data, dict) or data.get("error"):
        raise VKScoutOAuthError(
            "VK ID не обменял код на токены: "
            + str((data or {}).get("error_description") or (data or {}).get("error") or r.text[:500])
        )
    if str(data.get("state") or state) != state:
        raise VKScoutOAuthError("VK ID вернул другой state — авторизация остановлена")
    if not str(data.get("access_token") or "").strip():
        raise VKScoutOAuthError("VK ID не вернул access_token")
    return data


def _save_tokens(owner_id: int, device_id: str, data: dict[str, Any]) -> None:
    now = _now()
    try:
        expires_in = max(60, int(data.get("expires_in") or 3600))
    except (TypeError, ValueError):
        expires_in = 3600

    payload = {
        "owner_telegram_id": int(owner_id),
        "vk_user_id": int(data.get("user_id")) if str(data.get("user_id") or "").isdigit() else None,
        "access_token_encrypted": _encrypt(str(data.get("access_token") or "")),
        "refresh_token_encrypted": _encrypt(str(data.get("refresh_token") or "")),
        "device_id": str(device_id or ""),
        "scope": str(data.get("scope") or ""),
        "access_expires_at": _iso(now + timedelta(seconds=expires_in)),
        "connected_at": _iso(now),
        "updated_at": _iso(now),
        "refresh_lock_until": "1970-01-01T00:00:00+00:00",
        "last_error": None,
    }
    _sb_post(
        "agency_vk_oauth_tokens",
        payload,
        on_conflict="owner_telegram_id",
    )


def handle_vk_scout_oauth_callback() -> bool:
    """Handles VK ID callback if the query contains our vks_ state.

    Returns True only when this page load was a VK Scout callback.
    Safe to call globally near the beginning of streamlit_app.py.
    """
    if st is None:
        return False

    params = _extract_callback_params(st.query_params)
    state = params["state"]
    if not state.startswith("vks_"):
        return False

    try:
        rows = _sb_get(
            "agency_vk_oauth_pending",
            {"state": f"eq.{state}", "select": "*", "limit": 1},
        )
        if not rows:
            raise VKScoutOAuthError("Сессия подключения VK Scout не найдена или уже использована")
        pending = rows[0]
        expires_at = _parse_dt(pending.get("expires_at"))
        if not expires_at or expires_at <= _now():
            raise VKScoutOAuthError("Время подключения VK Scout истекло. Запустите подключение ещё раз")

        code = params["code"]
        device_id = params["device_id"]
        if not code or not device_id:
            raise VKScoutOAuthError("VK ID не вернул code или device_id")

        owner_id = int(pending["owner_telegram_id"])
        verifier = _decrypt(str(pending.get("code_verifier_encrypted") or ""))
        token_data = _exchange_code(
            code=code,
            device_id=device_id,
            state=state,
            verifier=verifier,
        )
        _save_tokens(owner_id, device_id, token_data)
        _sb_delete("agency_vk_oauth_pending", {"state": f"eq.{state}"})
        st.session_state["vk_scout_oauth_success"] = True
        st.session_state["vk_scout_oauth_owner"] = owner_id
    except Exception as exc:
        st.session_state["vk_scout_oauth_error"] = str(exc)
    finally:
        # Do not disturb unrelated query params (Telegram/referrals/etc.).
        for key in ("code", "state", "device_id", "type", "expires_in", "payload"):
            try:
                if key in st.query_params:
                    del st.query_params[key]
            except Exception:
                pass
    return True


def get_vk_scout_connection(owner_id: int) -> dict[str, Any]:
    rows = _sb_get(
        "agency_vk_oauth_tokens",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "select": "owner_telegram_id,vk_user_id,scope,access_expires_at,connected_at,updated_at,last_error",
            "limit": 1,
        },
    )
    if not rows:
        return {"connected": False}
    row = rows[0]
    return {"connected": True, **row}


def _read_token_row(owner_id: int) -> dict[str, Any] | None:
    rows = _sb_get(
        "agency_vk_oauth_tokens",
        {"owner_telegram_id": f"eq.{int(owner_id)}", "select": "*", "limit": 1},
    )
    return rows[0] if rows else None


def _refresh_tokens(owner_id: int, row: dict[str, Any]) -> str:
    """Refreshes a rotating token pair with a small DB lock."""
    cfg = _cfg()
    owner_id = int(owner_id)
    now = _now()
    lock_token = str(uuid.uuid4())
    lock_until = now + timedelta(seconds=60)

    claimed = _sb_patch(
        "agency_vk_oauth_tokens",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "refresh_lock_until": f"lt.{_iso(now)}",
        },
        {
            "refresh_lock_token": lock_token,
            "refresh_lock_until": _iso(lock_until),
            "updated_at": _iso(now),
        },
        return_rows=True,
    )
    if not claimed:
        # Another worker may be refreshing during a deploy overlap.
        time.sleep(2)
        latest = _read_token_row(owner_id)
        if latest:
            exp = _parse_dt(latest.get("access_expires_at"))
            if exp and exp > _now() + timedelta(minutes=2):
                return _decrypt(str(latest.get("access_token_encrypted") or ""))
        raise VKScoutOAuthError("VK Scout token сейчас обновляется другим worker")

    current = claimed[0]
    refresh_token = _decrypt(str(current.get("refresh_token_encrypted") or ""))
    device_id = str(current.get("device_id") or "").strip()
    if not refresh_token or not device_id:
        raise VKScoutOAuthError("Нет refresh_token или device_id для VK Scout")

    state = "vkr_" + secrets.token_urlsafe(24)
    try:
        r = requests.post(
            VKID_TOKEN_URL,
            params={
                "grant_type": "refresh_token",
                "redirect_uri": cfg["redirect_uri"],
                "client_id": cfg["client_id"],
                "device_id": device_id,
                "state": state,
            },
            data={"refresh_token": refresh_token},
            timeout=45,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not r.ok or not isinstance(data, dict) or data.get("error"):
            message = str((data or {}).get("error_description") or (data or {}).get("error") or r.text[:500])
            _sb_patch(
                "agency_vk_oauth_tokens",
                {"owner_telegram_id": f"eq.{owner_id}", "refresh_lock_token": f"eq.{lock_token}"},
                {
                    "refresh_lock_until": "1970-01-01T00:00:00+00:00",
                    "refresh_lock_token": None,
                    "last_error": message[:1000],
                    "updated_at": _iso(_now()),
                },
            )
            raise VKScoutOAuthError("VK ID не обновил токен: " + message)

        # Refresh token is rotating: save the NEW pair before anybody can use
        # the old refresh token again.
        new_access = str(data.get("access_token") or "").strip()
        new_refresh = str(data.get("refresh_token") or "").strip()
        if not new_access or not new_refresh:
            raise VKScoutOAuthError("VK ID не вернул новую пару access/refresh token")
        try:
            expires_in = max(60, int(data.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        patch = {
            "access_token_encrypted": _encrypt(new_access),
            "refresh_token_encrypted": _encrypt(new_refresh),
            "access_expires_at": _iso(_now() + timedelta(seconds=expires_in)),
            "scope": str(data.get("scope") or current.get("scope") or ""),
            "updated_at": _iso(_now()),
            "refresh_lock_until": "1970-01-01T00:00:00+00:00",
            "refresh_lock_token": None,
            "last_error": None,
        }
        # Some VK responses may rotate/return device_id too.
        if str(data.get("device_id") or "").strip():
            patch["device_id"] = str(data.get("device_id")).strip()
        _sb_patch(
            "agency_vk_oauth_tokens",
            {"owner_telegram_id": f"eq.{owner_id}", "refresh_lock_token": f"eq.{lock_token}"},
            patch,
        )
        return new_access
    except Exception:
        # Best-effort unlock if we failed before the explicit error patch.
        try:
            _sb_patch(
                "agency_vk_oauth_tokens",
                {"owner_telegram_id": f"eq.{owner_id}", "refresh_lock_token": f"eq.{lock_token}"},
                {
                    "refresh_lock_until": "1970-01-01T00:00:00+00:00",
                    "refresh_lock_token": None,
                    "updated_at": _iso(_now()),
                },
            )
        except Exception:
            pass
        raise



def force_refresh_vk_scout_access_token(owner_id: int) -> str:
    """Force refreshes the rotating VK ID token pair for this owner.

    Used when VK rejects an otherwise valid access token because it was issued
    from another IP address (error 5). The refresh request is made by the
    background worker itself, so the replacement access token is issued from
    the worker side and the new rotating refresh token is saved immediately.
    """
    row = _read_token_row(int(owner_id))
    if not row:
        raise VKScoutOAuthError("VK Scout ещё не авторизован через VK ID")
    return _refresh_tokens(int(owner_id), row)

def get_valid_vk_scout_access_token(owner_id: int, *, refresh_before_minutes: int = 5) -> str:
    row = _read_token_row(int(owner_id))
    if not row:
        raise VKScoutOAuthError("VK Scout ещё не авторизован через VK ID")
    access = _decrypt(str(row.get("access_token_encrypted") or ""))
    exp = _parse_dt(row.get("access_expires_at"))
    if access and exp and exp > _now() + timedelta(minutes=max(1, refresh_before_minutes)):
        return access
    return _refresh_tokens(int(owner_id), row)
