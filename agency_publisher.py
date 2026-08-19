from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
import streamlit as st

UTC = ZoneInfo("UTC")


def _supabase_config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise RuntimeError("Не найдены настройки Supabase.")
    return url, key


def _db_headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _supabase_config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _bot_token() -> str:
    token = str(
        st.secrets.get("AGENCY_W_PUBLISHER_BOT_TOKEN") or ""
    ).strip()
    if not token:
        raise RuntimeError(
            "Не найден AGENCY_W_PUBLISHER_BOT_TOKEN в Streamlit Secrets."
        )
    return token


def _telegram_call(
    method: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    token = _bot_token()
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        params=params,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = (
            str(data.get("description") or "")
            if isinstance(data, dict)
            else ""
        )
        raise RuntimeError(
            description or f"Telegram не выполнил {method}."
        )
    return data.get("result")


def publisher_bot_info() -> dict[str, Any]:
    result = _telegram_call("getMe")
    return result if isinstance(result, dict) else {}


def _get_chat_member(chat_id: int, user_id: int) -> dict[str, Any]:
    result = _telegram_call(
        "getChatMember",
        payload={
            "chat_id": int(chat_id),
            "user_id": int(user_id),
        },
    )
    return result if isinstance(result, dict) else {}


def _get_chat(chat_id: int) -> dict[str, Any]:
    result = _telegram_call(
        "getChat",
        payload={"chat_id": int(chat_id)},
    )
    return result if isinstance(result, dict) else {}


def _upsert_destination(
    owner_telegram_id: int,
    chat: dict[str, Any],
    *,
    owner_status: str,
    bot_status: str,
) -> None:
    url, _ = _supabase_config()
    now = datetime.now(UTC).isoformat()

    payload = {
        "owner_telegram_id": int(owner_telegram_id),
        "chat_id": int(chat.get("id")),
        "chat_title": str(
            chat.get("title")
            or chat.get("username")
            or "Telegram-площадка"
        ).strip(),
        "chat_type": str(chat.get("type") or "").strip(),
        "chat_username": str(chat.get("username") or "").strip() or None,
        "owner_status": str(owner_status or "").strip(),
        "bot_status": str(bot_status or "").strip(),
        "is_active": True,
        "verified_at": now,
        "updated_at": now,
    }

    response = requests.post(
        f"{url}/rest/v1/agency_publishing_destinations",
        headers=_db_headers(
            "resolution=merge-duplicates,return=minimal"
        ),
        params={
            "on_conflict": "owner_telegram_id,chat_id",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def _eligible_owner_status(status: str) -> bool:
    return str(status or "") in {"creator", "administrator"}


def _eligible_bot_status(status: str) -> bool:
    return str(status or "") in {"creator", "administrator"}


def _register_chat_for_user(
    user_id: int,
    chat: dict[str, Any],
    bot_id: int,
) -> bool:
    chat_type = str(chat.get("type") or "")
    if chat_type not in {"group", "supergroup", "channel"}:
        return False

    try:
        chat_id = int(chat.get("id"))
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    try:
        owner_member = _get_chat_member(chat_id, user_id)
        bot_member = _get_chat_member(chat_id, bot_id)
    except Exception:
        return False

    owner_status = str(owner_member.get("status") or "")
    bot_status = str(bot_member.get("status") or "")

    if not _eligible_owner_status(owner_status):
        return False
    if not _eligible_bot_status(bot_status):
        return False

    try:
        full_chat = _get_chat(chat_id)
    except Exception:
        full_chat = dict(chat)

    _upsert_destination(
        user_id,
        full_chat or chat,
        owner_status=owner_status,
        bot_status=bot_status,
    )
    return True


def _candidate_from_update(
    update: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """
    Два удобных пути подключения:
    1) my_chat_member — человек добавил Publisher в группу/канал;
    2) message — человек отправил /start@AgencyWPublisherBot в группе.
    """
    membership = update.get("my_chat_member")
    if isinstance(membership, dict):
        actor = membership.get("from")
        chat = membership.get("chat")
        new_member = membership.get("new_chat_member")
        if (
            isinstance(actor, dict)
            and isinstance(chat, dict)
            and isinstance(new_member, dict)
            and str(new_member.get("status") or "")
            in {"member", "administrator", "creator"}
        ):
            try:
                return int(actor.get("id")), chat
            except (TypeError, ValueError):
                pass

    message = update.get("message")
    if isinstance(message, dict):
        actor = message.get("from")
        chat = message.get("chat")
        text = str(message.get("text") or "").strip().lower()
        is_connect_command = (
            text.startswith("/start@agencywpublisherbot")
            or text.startswith("/connect@agencywpublisherbot")
            or text == "/connect"
        )
        if (
            is_connect_command
            and isinstance(actor, dict)
            and isinstance(chat, dict)
        ):
            try:
                return int(actor.get("id")), chat
            except (TypeError, ValueError):
                pass

    return None


def sync_publisher_destinations() -> dict[str, Any]:
    """
    Забирает накопившиеся Telegram updates и раскладывает площадки
    по их реальным владельцам/администраторам.

    Один пользователь может нажать «Найти площадки», а обновления
    других пользователей не потеряются: каждая найденная площадка
    сохраняется под Telegram ID того, кто добавил бота/дал команду.
    """
    webhook = _telegram_call("getWebhookInfo")
    if isinstance(webhook, dict) and str(webhook.get("url") or "").strip():
        raise RuntimeError(
            "У Publisher уже включён webhook. Для реестра нужно использовать "
            "единый входящий обработчик, а не getUpdates."
        )

    updates = _telegram_call(
        "getUpdates",
        payload={
            "offset": -100,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": ["message", "my_chat_member"],
        },
        timeout=20,
    )
    updates = updates if isinstance(updates, list) else []

    bot = publisher_bot_info()
    try:
        bot_id = int(bot.get("id"))
    except (TypeError, ValueError):
        raise RuntimeError("Telegram не вернул ID Publisher-бота.")

    connected = 0
    processed = 0
    max_update_id: int | None = None

    for update in updates:
        if not isinstance(update, dict):
            continue
        try:
            update_id = int(update.get("update_id"))
            max_update_id = (
                update_id
                if max_update_id is None
                else max(max_update_id, update_id)
            )
        except (TypeError, ValueError):
            pass

        candidate = _candidate_from_update(update)
        if candidate is None:
            continue
        processed += 1
        user_id, chat = candidate
        try:
            if _register_chat_for_user(user_id, chat, bot_id):
                connected += 1
        except Exception:
            # Ошибка одной площадки не должна потерять остальные.
            continue

    # Подтверждаем обработанные Telegram updates.
    # Если в этот самый момент пришёл новый update, Telegram вернёт его снова
    # при следующей синхронизации, поэтому он не потеряется.
    if max_update_id is not None:
        try:
            _telegram_call(
                "getUpdates",
                payload={
                    "offset": max_update_id + 1,
                    "limit": 1,
                    "timeout": 0,
                    "allowed_updates": ["message", "my_chat_member"],
                },
                timeout=20,
            )
        except Exception:
            pass

    return {
        "updates": len(updates),
        "candidates": processed,
        "connected": connected,
        "bot_username": str(bot.get("username") or "AgencyWPublisherBot"),
    }


def list_publisher_destinations(
    owner_telegram_id: int,
) -> list[dict[str, Any]]:
    url, _ = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/agency_publishing_destinations",
        headers=_db_headers(),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "is_active": "eq.true",
            "select": "*",
            "order": "created_at.asc",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def verify_publisher_destination(
    owner_telegram_id: int,
    chat_id: int,
) -> dict[str, Any]:
    bot = publisher_bot_info()
    bot_id = int(bot.get("id"))
    owner_telegram_id = int(owner_telegram_id)
    chat_id = int(chat_id)

    chat = _get_chat(chat_id)
    owner_member = _get_chat_member(chat_id, owner_telegram_id)
    bot_member = _get_chat_member(chat_id, bot_id)

    owner_status = str(owner_member.get("status") or "")
    bot_status = str(bot_member.get("status") or "")
    ok = (
        _eligible_owner_status(owner_status)
        and _eligible_bot_status(bot_status)
    )

    if ok:
        _upsert_destination(
            owner_telegram_id,
            chat,
            owner_status=owner_status,
            bot_status=bot_status,
        )
    else:
        _set_destination_active(
            owner_telegram_id,
            chat_id,
            False,
            owner_status=owner_status,
            bot_status=bot_status,
        )

    return {
        "ok": ok,
        "chat_title": str(chat.get("title") or "Telegram-площадка"),
        "chat_type": str(chat.get("type") or ""),
        "owner_status": owner_status,
        "bot_status": bot_status,
    }


def _set_destination_active(
    owner_telegram_id: int,
    chat_id: int,
    active: bool,
    *,
    owner_status: str = "",
    bot_status: str = "",
) -> None:
    url, _ = _supabase_config()
    payload: dict[str, Any] = {
        "is_active": bool(active),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if owner_status:
        payload["owner_status"] = owner_status
    if bot_status:
        payload["bot_status"] = bot_status
    payload["verified_at"] = datetime.now(UTC).isoformat()

    response = requests.patch(
        f"{url}/rest/v1/agency_publishing_destinations",
        headers=_db_headers("return=minimal"),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "chat_id": f"eq.{int(chat_id)}",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def remove_publisher_destination(
    owner_telegram_id: int,
    chat_id: int,
) -> None:
    _set_destination_active(
        int(owner_telegram_id),
        int(chat_id),
        False,
    )
