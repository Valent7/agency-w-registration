"""Owner-bound OAuth for content-factory Instagram and Threads connections.

Run ``uvicorn content_factory_oauth:runtime_app --factory`` behind HTTPS.
Importing this module does not create a store or make provider requests.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from html import escape
import ipaddress
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import requests
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from agency_access_control import is_agency_access_revoked
from instagram_auth_tools import exchange_long_lived_ig_token, fetch_instagram_profile

PLATFORMS = ("instagram", "threads")
SCOPES = {
    "instagram": ("instagram_business_basic", "instagram_business_content_publish"),
    "threads": ("threads_basic", "threads_content_publish"),
}
HANDOFF_TTL = 120
STATE_TTL = 600
CONTEXT = b"AgencyW content factory OAuth v1\0"


class OAuthError(ValueError):
    """An intentionally credential-free OAuth failure."""


class OAuthExpired(OAuthError):
    """A correctly signed request that has passed its short validity window."""


def _https_url(value: str, *, origin: bool = False) -> str:
    try:
        parts = urlsplit(str(value or ""))
        host = str(parts.hostname or "").lower().rstrip(".")
        if (parts.scheme != "https" or not host or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment
                or host == "localhost" or host.endswith((".local", ".localhost", ".internal"))
                or "." not in host or host.isdigit() or host.startswith("0x")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[\d.]+", host):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        if origin and (parts.path not in ("", "/") or parts.query):
            raise ValueError
    except (TypeError, ValueError):
        raise OAuthError("Для подключения нужен публичный HTTPS-адрес сервиса.") from None
    return str(value).rstrip("/") if origin else str(value)


def _key(config: dict) -> str:
    value = str(config.get("CONTENT_FACTORY_OAUTH_SIGNING_KEY") or config.get("SUPABASE_SECRET_KEY") or "").strip()
    if len(value.encode("utf-8")) < 32:
        raise OAuthError("Не настроена защищённая авторизация сервиса подключения.")
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signature(encoded: str, key: str) -> str:
    return _b64(hmac.new(key.encode("utf-8"), CONTEXT + encoded.encode("ascii"), hashlib.sha256).digest())


def _identity(owner_id, project_id, platform):
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0 or platform not in PLATFORMS:
        raise OAuthError("Некорректный владелец или площадка подключения.")
    try:
        project_id = str(uuid.UUID(str(project_id)))
    except (ValueError, TypeError, AttributeError):
        raise OAuthError("Некорректный проект подключения.") from None
    return {"owner_id": owner_id, "project_id": project_id, "platform": platform}


def _sign(payload: dict, key: str) -> str:
    encoded = _b64(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return "cf1." + encoded + "." + _signature(encoded, key)


def _verify(raw: str, key: str, *, purpose: str, platform: str, ttl: int) -> dict:
    try:
        if not isinstance(raw, str) or len(raw) > 2048:
            raise ValueError
        prefix, encoded, signature = raw.split(".")
        if prefix != "cf1" or not hmac.compare_digest(_signature(encoded, key), signature):
            raise ValueError
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.get("purpose") != purpose or data.get("platform") != platform:
            raise ValueError
        _identity(data.get("owner_id"), data.get("project_id"), platform)
        issued = data.get("iat")
        if isinstance(issued, bool) or not isinstance(issued, int) or issued <= 0:
            raise ValueError
        if data.get("v") != 1:
            raise ValueError
        age = int(time.time()) - issued
        if age < -15:
            raise ValueError
        if age > ttl:
            raise OAuthExpired(
                "Срок действия ссылки истёк. Вернитесь в Агентство W, обновите страницу "
                "и снова нажмите кнопку подключения."
            )
        return data
    except OAuthExpired:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise OAuthError("Ссылка подключения недействительна или истекла. Начните подключение заново.") from None


def make_connect_url(owner_id: int, project_id: str, platform: str, config: dict) -> str | None:
    """Mint a two-minute owner assertion in the authenticated Agency W UI.

    The frontend needs the service URL and shared signing key. Provider app
    secrets belong only to the OAuth service. VK uses its community admin key.
    """
    if platform not in PLATFORMS:
        return None
    try:
        identity = _identity(owner_id, project_id, platform)
        service = _https_url(config.get("CONTENT_FACTORY_SERVICE_URL", ""), origin=True)
        key = _key(config)
    except OAuthError:
        return None
    assertion = _sign({**identity, "v": 1, "purpose": "content_factory_connect", "iat": int(time.time())}, key)
    return f"{service}/connect/{platform}?" + urlencode({"assertion": assertion})


def _settings(config: dict, platform: str) -> dict:
    if platform not in PLATFORMS:
        raise OAuthError("Эта площадка не поддерживает автоматическое подключение.")
    service = _https_url(config.get("CONTENT_FACTORY_SERVICE_URL", ""), origin=True)
    prefix = "INSTAGRAM" if platform == "instagram" else "THREADS"
    client_id = str(config.get(prefix + "_APP_ID") or "").strip()
    client_secret = str(config.get(prefix + "_APP_SECRET") or "").strip()
    if not client_id.isdigit() or not client_secret:
        raise OAuthError("Оператор ещё не настроил приложение этой соцсети для подключения.")
    return {"client_id": client_id, "client_secret": client_secret,
            "redirect_uri": f"{service}/oauth/{platform}/callback", "key": _key(config)}


def _provider(session, method, url, **kwargs) -> dict:
    try:
        response = getattr(session, method)(url, timeout=30, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            raise OAuthError("Соцсеть отклонила подключение. Проверьте доступ приложения и повторите вход.")
        data = response.json()
    except (requests.RequestException, ValueError, TypeError):
        raise OAuthError("Не удалось завершить проверку подключения в соцсети. Повторите вход.") from None
    if not isinstance(data, dict) or data.get("error"):
        raise OAuthError("Соцсеть не подтвердила подключение. Повторите вход.")
    return data


def _exchange(platform, settings, code, session):
    data = {"client_id": settings["client_id"], "client_secret": settings["client_secret"],
            "grant_type": "authorization_code", "redirect_uri": settings["redirect_uri"], "code": code}
    endpoint = "https://api.instagram.com/oauth/access_token" if platform == "instagram" else "https://graph.threads.net/oauth/access_token"
    short = _provider(session, "post", endpoint, data=data)
    short_token = str(short.get("access_token") or "").strip()
    if not short_token:
        raise OAuthError("Соцсеть не выдала доступ для подключения.")
    if platform == "instagram":
        try:
            long = exchange_long_lived_ig_token(short_token, settings["client_secret"], session=session)
            token = str(long.get("access_token") or "").strip()
            profile = fetch_instagram_profile(token, session=session)
        except Exception:
            # Existing helper errors can contain provider response text. Keep it
            # strictly inside this boundary; never return/log those exceptions.
            raise OAuthError("Instagram не подтвердил долгосрочный доступ или профиль. Проверьте разрешения приложения.") from None
        account_id = str(profile.get("user_id") or "").strip()
        if not account_id:
            raise OAuthError("Instagram не вернул ID профессионального аккаунта. Повторите подключение.")
    else:
        long = _provider(session, "get", "https://graph.threads.net/access_token", params={
            "grant_type": "th_exchange_token", "client_secret": settings["client_secret"], "access_token": short_token})
        token = str(long.get("access_token") or "").strip()
        if not token:
            raise OAuthError("Threads не выдал долгосрочный доступ.")
        profile = _provider(session, "get", "https://graph.threads.net/v1.0/me",
                            params={"fields": "id,username"}, headers={"Authorization": "Bearer " + token})
        account_id = str(profile.get("id") or "").strip()
    if not token or not account_id.isdigit() or int(account_id) <= 0:
        raise OAuthError("Соцсеть не подтвердила аккаунт для публикации.")
    try:
        expires_in = int(long.get("expires_in") or 0)
        if not 60 <= expires_in <= 2 * 366 * 86400:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise OAuthError("Соцсеть не подтвердила срок действия долгосрочного доступа.") from None
    permissions = short.get("permissions") or short.get("scope") or long.get("scope") or []
    if isinstance(permissions, str):
        permissions = [item for item in re.split(r"[ ,]+", permissions) if item]
    if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
        raise OAuthError("Соцсеть вернула некорректные разрешения подключения.")
    if permissions and not set(SCOPES[platform]).issubset(permissions):
        raise OAuthError("Не выдано разрешение на публикацию. Подключите аккаунт с доступом к созданию контента.")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    return account_id, str(profile.get("username") or account_id), token, {
        "token_expires_at": expires_at, "scopes": permissions,
        "scopes_requested": list(SCOPES[platform]), "scopes_verified": bool(permissions),
        "api_version": "v23.0" if platform == "instagram" else "v1.0",
    }


def _cookie(platform):
    return "cf_oauth_" + platform


def _secure_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def create_app(store, config: dict) -> Starlette:
    """Create an OAuth service over the same encrypted store as the UI/worker."""
    config = dict(config)
    return_url = _https_url(config.get("CONTENT_FACTORY_RETURN_URL") or "https://agency-w.streamlit.app/")
    used_nonces = {}
    nonce_lock = threading.Lock()

    def project(identity):
        owner = identity["owner_id"]
        if is_agency_access_revoked(owner) or not store.get_project(owner, identity["project_id"]):
            raise OAuthError("Проект недоступен этому владельцу. Откройте подключение из своего кабинета.")

    def failure(message, platform=None, status=400):
        response = HTMLResponse(
            "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Подключение соцсети</title>"
            "<h1>Подключение не завершено</h1><p>" + escape(message) + "</p><p><a href='" +
            escape(return_url, quote=True) + "'>Вернуться в Агентство W</a></p></html>", status_code=status)
        if platform in PLATFORMS:
            response.delete_cookie(_cookie(platform), path=f"/oauth/{platform}/callback", secure=True, httponly=True, samesite="lax")
        return _secure_response(response)

    def start(request: Request):
        platform = request.path_params["platform"]
        try:
            settings = _settings(config, platform)
            identity = _verify(request.query_params.get("assertion", ""), settings["key"],
                               purpose="content_factory_connect", platform=platform, ttl=HANDOFF_TTL)
            project(identity)
            nonce = secrets.token_urlsafe(32)
            state = _sign({**_identity(identity["owner_id"], identity["project_id"], platform),
                           "v": 1, "iat": int(time.time()), "purpose": "content_factory_provider", "nonce": nonce}, settings["key"])
            params = {"client_id": settings["client_id"], "redirect_uri": settings["redirect_uri"],
                      "response_type": "code", "scope": ",".join(SCOPES[platform]), "state": state}
            endpoint = "https://www.instagram.com/oauth/authorize" if platform == "instagram" else "https://threads.net/oauth/authorize"
            if platform == "instagram":
                params.update({"enable_fb_login": "0", "force_authentication": "1"})
            response = RedirectResponse(endpoint + "?" + urlencode(params), status_code=302)
            response.set_cookie(_cookie(platform), nonce, max_age=STATE_TTL,
                                path=f"/oauth/{platform}/callback", secure=True, httponly=True, samesite="lax")
            return _secure_response(response)
        except OAuthError as exc:
            return failure(str(exc), status=403)
        except Exception:
            return failure("Сервис подключения временно недоступен. Повторите попытку позже.", status=503)

    def callback(request: Request):
        platform = request.path_params["platform"]
        try:
            settings = _settings(config, platform)
            identity = _verify(request.query_params.get("state", ""), settings["key"],
                               purpose="content_factory_provider", platform=platform, ttl=STATE_TTL)
            nonce = str(identity.get("nonce") or "")
            browser_nonce = str(request.cookies.get(_cookie(platform)) or "")
            if len(nonce) < 32 or not browser_nonce or not hmac.compare_digest(nonce, browser_nonce):
                raise OAuthError("Браузер не подтвердил начало подключения. Начните его заново из Агентства W.")
            project(identity)
            with nonce_lock:
                now = int(time.time())
                for previous, used_at in list(used_nonces.items()):
                    if now - used_at > STATE_TTL:
                        used_nonces.pop(previous, None)
                if nonce in used_nonces:
                    raise OAuthError("Этот вход уже обработан. Начните подключение заново.")
                used_nonces[nonce] = now
            if request.query_params.get("error"):
                raise OAuthError("Вход отменён или необходимые разрешения не выданы.")
            code = str(request.query_params.get("code") or "").strip()
            if not code or len(code) > 4096:
                raise OAuthError("Соцсеть не вернула код подключения.")
            account, name, token, extra = _exchange(platform, settings, code, request.app.state.oauth_session)
            store.save_connection(identity["owner_id"], identity["project_id"], platform, account, name, token, extra)
            parts = urlsplit(return_url)
            query = parse_qsl(parts.query, keep_blank_values=True)
            query.append(("content_factory_connected", platform))
            destination = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
            response = RedirectResponse(destination, status_code=303)
            response.delete_cookie(_cookie(platform), path=f"/oauth/{platform}/callback", secure=True, httponly=True, samesite="lax")
            return _secure_response(response)
        except OAuthError as exc:
            return failure(str(exc), platform)
        except Exception:
            # No provider/store exception detail crosses this public endpoint.
            return failure("Не удалось сохранить подключение. Повторите его из кабинета Агентства W.", platform, 503)

    def health(_):
        return _secure_response(JSONResponse({"status": "ok"}))

    app = Starlette(routes=[Route("/health", health), Route("/connect/{platform}", start),
                            Route("/oauth/{platform}/callback", callback)])
    app.state.oauth_session = requests
    return app


def runtime_app() -> Starlette:
    """Uvicorn --factory entrypoint; initialization is intentionally lazy."""
    from content_factory_runtime import _configuration, build_store
    return create_app(build_store(), _configuration())
