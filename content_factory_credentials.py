"""Refresh an owner's long-lived social token before it expires.

This module has no storage or import-time side effects. The worker persists a
returned updated connection through its encrypted owner-scoped store.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests


class CredentialError(RuntimeError):
    """An actionable connection error that never contains token/provider data."""


def _utc(value: datetime | str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise CredentialError("Не удалось определить срок доступа к площадке. Переподключите аккаунт.") from None


def refresh_connection(connection: dict, now: datetime, session=None) -> dict:
    """Return the same object if no refresh is due; otherwise return a copy.

    Long-lived Instagram/Threads tokens renew during their last seven days.
    Expired tokens cannot renew. Missing expiry metadata represents a manually
    connected credential and is preserved without inventing a validity period.
    """
    if not isinstance(connection, dict):
        raise CredentialError("Подключение площадки не найдено. Подключите аккаунт заново.")
    extra = connection.get("extra") or {}
    if not isinstance(extra, dict):
        raise CredentialError("Не удалось проверить подключение площадки. Переподключите аккаунт.")
    expiry = extra.get("token_expires_at")
    if not expiry:
        return connection
    current = _utc(now)
    expires_at = _utc(expiry)
    if expires_at <= current:
        raise CredentialError("Срок доступа к площадке истёк. Переподключите аккаунт для публикации.")
    if expires_at - current > timedelta(days=7):
        return connection
    platform = connection.get("platform")
    endpoints = {
        "instagram": ("https://graph.instagram.com/refresh_access_token", "ig_refresh_token"),
        "threads": ("https://graph.threads.net/refresh_access_token", "th_refresh_token"),
    }
    if platform not in endpoints:
        raise CredentialError("Срок доступа к площадке заканчивается. Переподключите аккаунт.")
    token = str(connection.get("access_token") or "").strip()
    if not token:
        raise CredentialError("Не найден доступ к площадке. Переподключите аккаунт.")
    endpoint, grant_type = endpoints[platform]
    transport = session if session is not None else requests
    try:
        response = transport.get(endpoint, params={"grant_type": grant_type, "access_token": token},
                                 timeout=(15, 30), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise CredentialError("Соцсеть не продлила доступ. Повторите позже или переподключите аккаунт.")
        data = response.json()
    except (requests.RequestException, ValueError, TypeError):
        raise CredentialError("Не удалось продлить доступ к площадке. Повторите позже или переподключите аккаунт.") from None
    if not isinstance(data, dict) or data.get("error"):
        raise CredentialError("Соцсеть не подтвердила продление доступа. Переподключите аккаунт.")
    new_token = data.get("access_token")
    raw_seconds = data.get("expires_in")
    if not isinstance(new_token, str) or not new_token.strip() or isinstance(raw_seconds, bool):
        raise CredentialError("Соцсеть не подтвердила новый срок доступа. Переподключите аккаунт.")
    try:
        if not isinstance(raw_seconds, (int, str)) or (isinstance(raw_seconds, str) and not raw_seconds.isdigit()):
            raise ValueError
        seconds = int(raw_seconds)
        if seconds <= 0:
            raise ValueError
        next_expiry = current + timedelta(seconds=seconds)
    except (ValueError, TypeError, OverflowError):
        raise CredentialError("Соцсеть не подтвердила новый срок доступа. Переподключите аккаунт.") from None
    result = deepcopy(connection)
    result["access_token"] = new_token.strip()
    result["extra"]["token_expires_at"] = next_expiry.isoformat()
    return result
