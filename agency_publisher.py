from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import os
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


def _telegram_api_base() -> str:
    """
    Базовый адрес Telegram Bot API.

    Для больших роликов Агентство W использует собственный Local Telegram
    Bot API. Если секрет не задан, остаётся совместимый fallback на обычный
    облачный Bot API Telegram.
    """
    base = str(
        st.secrets.get("AGENCY_W_TELEGRAM_API_BASE")
        or os.environ.get("AGENCY_W_TELEGRAM_API_BASE")
        or "https://api.telegram.org"
    ).strip().rstrip("/")
    return base or "https://api.telegram.org"


def _telegram_call(
    method: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    token = _bot_token()
    response = requests.post(
        f"{_telegram_api_base()}/bot{token}/{method}",
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

def _split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Делит длинный пост без разрыва посреди абзаца, когда возможно."""
    clean = str(text or "").strip()
    if not clean:
        return []
    if len(clean) <= limit:
        return [clean]

    parts: list[str] = []
    rest = clean
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < int(limit * 0.55):
            cut = rest.rfind("\n", 0, limit)
        if cut < int(limit * 0.55):
            cut = rest.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        chunk = rest[:cut].strip()
        if chunk:
            parts.append(chunk)
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def _image_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if hasattr(value, "getvalue"):
        data = value.getvalue()
        if isinstance(data, bytes):
            return data
    try:
        data = bytes(value)
        if data:
            return data
    except Exception:
        pass
    return b""


def _video_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if hasattr(value, "getvalue"):
        data = value.getvalue()
        if isinstance(data, bytes):
            return data
    try:
        data = bytes(value)
        if data:
            return data
    except Exception:
        pass
    return b""


def _safe_video_file_name(file_name: str) -> str:
    clean = str(file_name or "").strip()
    if not clean:
        return "agency_w_video.mp4"
    # Для sendVideo используем MP4. Имя сохраняем только как имя файла.
    if not clean.lower().endswith(".mp4"):
        clean += ".mp4"
    return clean


def _publication_parts(text: str) -> tuple[str, str]:
    """
    Возвращает подпись к изображению и, при необходимости,
    отдельный длинный текст.

    Короткий материал публикуется только один раз — под картинкой.
    Длинный материал: под картинкой название, ниже сам текст.
    """
    clean = str(text or "").strip()
    if not clean:
        return "", ""

    if len(clean) <= 950:
        return clean, ""

    lines = clean.splitlines()
    first = lines[0].strip() if lines else ""
    title = first.lstrip("# ").strip()

    caption = title if title and len(title) <= 180 else "Агентство W"

    remainder = clean
    if first.startswith("#") and len(lines) > 1:
        remainder = "\n".join(lines[1:]).strip()

    return caption, remainder


def _send_photo(chat_id: int, image_bytes: Any, caption: str = "") -> dict[str, Any]:
    raw = _image_bytes(image_bytes)
    if not raw:
        raise ValueError("Изображение пустое.")

    token = _bot_token()
    response = requests.post(
        f"{_telegram_api_base()}/bot{token}/sendPhoto",
        data={
            "chat_id": str(int(chat_id)),
            "caption": str(caption or "")[:1000],
        },
        files={
            "photo": (
                "agency_w_publication.png",
                raw,
                "image/png",
            )
        },
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = str(data.get("description") or "") if isinstance(data, dict) else ""
        raise RuntimeError(description or "Telegram не принял изображение.")
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def _send_video(
    chat_id: int,
    video_bytes: Any,
    caption: str = "",
    *,
    file_name: str = "agency_w_video.mp4",
    mime_type: str = "video/mp4",
) -> dict[str, Any]:
    raw = _video_bytes(video_bytes)
    if not raw:
        raise ValueError("Видео пустое.")

    token = _bot_token()
    response = requests.post(
        f"{_telegram_api_base()}/bot{token}/sendVideo",
        data={
            "chat_id": str(int(chat_id)),
            "caption": str(caption or "")[:1000],
            "supports_streaming": "true",
        },
        files={
            "video": (
                _safe_video_file_name(file_name),
                raw,
                str(mime_type or "video/mp4"),
            )
        },
        timeout=900,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = str(data.get("description") or "") if isinstance(data, dict) else ""
        raise RuntimeError(description or "Telegram не принял видео.")
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def _send_video_by_file_id(
    chat_id: int,
    file_id: str,
    caption: str = "",
) -> dict[str, Any]:
    clean_file_id = str(file_id or "").strip()
    if not clean_file_id:
        raise ValueError("Telegram file_id видео пустой.")
    result = _telegram_call(
        "sendVideo",
        payload={
            "chat_id": int(chat_id),
            "video": clean_file_id,
            "caption": str(caption or "")[:1000],
            "supports_streaming": True,
        },
        timeout=45,
    )
    return result if isinstance(result, dict) else {}


def _send_text_chunks(
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
) -> list[int]:
    """
    Обычные последовательные сообщения без reply/цитаты.
    reply_to_message_id оставлен только для совместимости.
    """
    chunks = _split_telegram_text(text)
    message_ids: list[int] = []

    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": chunk,
            "disable_web_page_preview": False,
        }
        result = _telegram_call("sendMessage", payload=payload)
        if isinstance(result, dict):
            try:
                message_ids.append(int(result.get("message_id")))
            except (TypeError, ValueError):
                pass

    return message_ids


def publish_to_publisher_destinations(
    owner_telegram_id: int,
    text: str,
    *,
    image_bytes: Any | None = None,
    video_bytes: Any | None = None,
    video_file_name: str = "agency_w_video.mp4",
    video_mime_type: str = "video/mp4",
    destination_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    Публикует утверждённый материал только в площадки данного владельца.

    Статус success означает: Telegram Bot API принял отправку.
    Это не означает, что каждый участник площадки прочитал сообщение.
    """
    owner_telegram_id = int(owner_telegram_id)
    clean_text = str(text or "").strip()
    raw_image = _image_bytes(image_bytes) if image_bytes is not None else b""
    raw_video = _video_bytes(video_bytes) if video_bytes is not None else b""
    if raw_image and raw_video:
        raise ValueError("В одной публикации выберите либо изображение, либо видео.")
    if not clean_text and not raw_image and not raw_video:
        raise ValueError("Публикация пуста.")

    destinations = list_publisher_destinations(owner_telegram_id)
    wanted = {int(x) for x in destination_ids} if destination_ids else None
    destinations = [
        item for item in destinations
        if wanted is None or int(item.get("chat_id")) in wanted
    ]

    results: list[dict[str, Any]] = []
    # После первой загрузки Telegram возвращает file_id. Для остальных площадок
    # того же владельца повторно используем его — это заметно ускоряет массовую
    # публикацию большого ролика и не гоняет один и тот же файл по сети N раз.
    reusable_video_file_id: str | None = None

    for item in destinations:
        title = str(item.get("chat_title") or "Telegram-площадка").strip()
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue

        try:
            check = verify_publisher_destination(owner_telegram_id, chat_id)
            if not bool(check.get("ok")):
                raise RuntimeError("Площадка или права Publisher больше не подтверждены.")

            photo_message_id: int | None = None
            video_message_id: int | None = None
            text_to_send = clean_text

            if raw_image:
                caption, text_to_send = _publication_parts(clean_text)
                photo_result = _send_photo(
                    chat_id,
                    raw_image,
                    caption=caption,
                )
                try:
                    photo_message_id = int(photo_result.get("message_id"))
                except (TypeError, ValueError):
                    photo_message_id = None

            if raw_video:
                caption, text_to_send = _publication_parts(clean_text)
                if reusable_video_file_id:
                    video_result = _send_video_by_file_id(
                        chat_id,
                        reusable_video_file_id,
                        caption=caption,
                    )
                else:
                    video_result = _send_video(
                        chat_id,
                        raw_video,
                        caption=caption,
                        file_name=video_file_name,
                        mime_type=video_mime_type,
                    )
                    video_info = (
                        video_result.get("video")
                        if isinstance(video_result.get("video"), dict)
                        else {}
                    )
                    reusable_video_file_id = str(
                        video_info.get("file_id") or ""
                    ).strip() or None
                try:
                    video_message_id = int(video_result.get("message_id"))
                except (TypeError, ValueError):
                    video_message_id = None

            text_message_ids = []
            if text_to_send:
                text_message_ids = _send_text_chunks(
                    chat_id,
                    text_to_send,
                )

            results.append({
                "chat_id": chat_id,
                "title": title,
                "ok": True,
                "status": "accepted_by_telegram",
                "photo_message_id": photo_message_id,
                "video_message_id": video_message_id,
                "text_message_ids": text_message_ids,
            })
        except Exception as exc:
            results.append({
                "chat_id": chat_id,
                "title": title,
                "ok": False,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    success = sum(1 for item in results if item.get("ok") is True)
    return {
        "requested": len(destinations),
        "accepted": success,
        "failed": len(destinations) - success,
        "results": results,
    }

