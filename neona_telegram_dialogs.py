from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import mimetypes
import tempfile
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession

from agency_core import agency_core_prompt

NEONA_DIALOG_CORE = agency_core_prompt(
    "Неона",
    "вести живой диалог после первого ответа, понимать возражения и двигаться только к осознанной встрече без давления",
)

# Экономный режим Telegram. Новое входящее само поднимает диалог наверх,
# поэтому нет смысла каждые секунды перечитывать старые переписки.
NEONA_MIN_POLL_SECONDS = 120
NEONA_DEFAULT_POLL_SECONDS = 300
NEONA_RECENT_DIALOG_HOURS = 48


def _flood_wait_seconds(error: Exception) -> int:
    if error.__class__.__name__ != "FloodWaitError":
        return 0
    try:
        return max(1, int(getattr(error, "seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0


_NEONA_BASE_DIR = Path(__file__).resolve().parent


def _load_neona_reference_file(filename: str) -> str:
    """Безопасно загружает локальные правила/знания Неоны из корня репозитория."""
    try:
        path = _NEONA_BASE_DIR / filename
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        # Отсутствие справочного файла не должно останавливать Telegram-worker.
        return ""


NEONA_CONSTITUTION = _load_neona_reference_file("NEONA_CONSTITUTION.txt")
NEONA_KNOWLEDGE_BASE = _load_neona_reference_file("NEONA_KNOWLEDGE_BASE.txt")

def _owner_text_forms(owner_name: str) -> dict[str, str]:
    raw = re.sub(r"\s+", " ", str(owner_name or "").strip())
    if not raw:
        return {
            "nominative": "владелец кабинета",
            "genitive": "владельца кабинета",
            "instrumental": "владельцем кабинета",
        }
    low = raw.casefold()
    known = {
        "valentina": ("Валентина", "Валентины", "Валентиной"),
        "walentina": ("Валентина", "Валентины", "Валентиной"),
        "валентина": ("Валентина", "Валентины", "Валентиной"),
        "надежда": ("Надежда", "Надежды", "Надеждой"),
    }
    if low in known:
        nominative, genitive, instrumental = known[low]
        return {
            "nominative": nominative,
            "genitive": genitive,
            "instrumental": instrumental,
        }
    if re.fullmatch(r"[А-Яа-яЁё-]+", raw):
        if raw.endswith("а"):
            stem = raw[:-1]
            genitive = stem + (
                "и" if stem.casefold().endswith(("г", "к", "х", "ж", "ч", "ш", "щ"))
                else "ы"
            )
            return {
                "nominative": raw,
                "genitive": genitive,
                "instrumental": stem + "ой",
            }
        if raw.endswith("я"):
            stem = raw[:-1]
            return {
                "nominative": raw,
                "genitive": stem + "и",
                "instrumental": stem + "ей",
            }
    return {
        "nominative": raw,
        "genitive": raw,
        "instrumental": raw,
    }


def _personalize_neona_reference_text(value: str, owner_name: str) -> str:
    forms = _owner_text_forms(owner_name)
    text = str(value or "")
    for old, new in (
        ("Валентиной", forms["instrumental"]),
        ("Валентины", forms["genitive"]),
        ("Валентина", forms["nominative"]),
    ):
        text = text.replace(old, new)
    return text


try:
    import streamlit as st
except Exception:  # pragma: no cover - standalone worker mode
    st = None

UTC = timezone.utc

_LAST_VOICE_DIAGNOSTICS: list[dict] = []

# Защита от повторных платных вызовов на одном и том же входящем сообщении.
# Кэш в Supabase (context) переживает перезапуск worker; память защищает от
# повторной оплаты даже если сохранение состояния временно не удалось.
VOICE_TRANSCRIPT_CACHE_LIMIT = 20
MAX_MESSAGE_PROCESSING_ATTEMPTS = 3
_VOICE_TRANSCRIPTION_MEMORY_CACHE: dict[tuple[int, int, int], dict[str, str]] = {}

# Диагностика Неоны: печатаем подробный снимок только один раз на владельца
# после каждого запуска worker. На логику диалога и отправку сообщений не влияет.
_NEONA_DIAG_PRINTED_OWNERS: set[int] = set()

def _voice_diag_reset() -> None:
    _LAST_VOICE_DIAGNOSTICS.clear()

def _voice_diag_add(stage: str, **data) -> None:
    item = {"stage": stage, "at": datetime.now(UTC).isoformat()}
    item.update(data)
    _LAST_VOICE_DIAGNOSTICS.append(item)

def get_last_voice_diagnostics() -> list[dict]:
    return list(_LAST_VOICE_DIAGNOSTICS)


def _trim_context_map(raw: Any, limit: int) -> dict[str, Any]:
    """Оставляет только последние записи словаря с числовыми message_id."""
    data = dict(raw) if isinstance(raw, dict) else {}

    def sort_key(item: tuple[str, Any]) -> int:
        try:
            return int(item[0])
        except (TypeError, ValueError):
            return 0

    if len(data) > max(1, int(limit)):
        data = dict(sorted(data.items(), key=sort_key)[-int(limit):])
    return data


def _voice_cache_get(context: dict[str, Any], message_id: int) -> dict[str, str] | None:
    cache = context.get("voice_transcription_cache")
    if not isinstance(cache, dict):
        return None
    entry = cache.get(str(int(message_id)))
    return dict(entry) if isinstance(entry, dict) else None


def _voice_cache_put(
    context: dict[str, Any],
    message_id: int,
    *,
    status: str,
    text: str = "",
    error: str = "",
) -> dict[str, Any]:
    updated = dict(context or {})
    cache = _trim_context_map(
        updated.get("voice_transcription_cache"),
        VOICE_TRANSCRIPT_CACHE_LIMIT,
    )
    cache[str(int(message_id))] = {
        "status": str(status),
        "text": str(text or ""),
        "error": str(error or "")[:500],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    updated["voice_transcription_cache"] = _trim_context_map(
        cache,
        VOICE_TRANSCRIPT_CACHE_LIMIT,
    )
    return updated


def _processing_attempts_put(
    context: dict[str, Any],
    message_id: int,
    attempts: int,
) -> dict[str, Any]:
    updated = dict(context or {})
    data = _trim_context_map(updated.get("message_processing_attempts"), 20)
    data[str(int(message_id))] = int(attempts)
    updated["message_processing_attempts"] = _trim_context_map(data, 20)
    return updated


def _processing_attempts_remove(
    context: dict[str, Any],
    message_id: int,
) -> dict[str, Any]:
    updated = dict(context or {})
    data = _trim_context_map(updated.get("message_processing_attempts"), 20)
    data.pop(str(int(message_id)), None)
    updated["message_processing_attempts"] = data
    return updated


MSK = ZoneInfo("Europe/Moscow")
DURATION_MINUTES = 30

MEETING_FORMATS = ("Zoom", "Telegram", "WhatsApp")

# Официальный Telegram-канал с материалами Агентства W.
# Ссылка НЕ используется в первом холодном сообщении: Неона даёт её
# только после явного запроса человека или после его согласия посмотреть материал.
TELEGRAM_AGENCY_MATERIAL_URL = "https://t.me/schtab_aiTM"

TZ_ALIASES = {
    "мск": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "москов": "Europe/Moscow",
    "berlin": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "germany": "Europe/Berlin",
    "германи": "Europe/Berlin",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "казахстан": "Asia/Almaty",
    "бишкек": "Asia/Bishkek",
    "киргиз": "Asia/Bishkek",
    "кыргыз": "Asia/Bishkek",
    "лондон": "Europe/London",
    "киев": "Europe/Kyiv",
    "украин": "Europe/Kyiv",
    "рига": "Europe/Riga",
    "вильнюс": "Europe/Vilnius",
    "таллин": "Europe/Tallinn",
    "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan",
    "ташкент": "Asia/Tashkent",
    "нью-йорк": "America/New_York",
    "new york": "America/New_York",
}

WEEKDAYS_RU = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "четверга": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}

YES_WORDS = {
    "да", "подтверждаю", "подтверждаем", "согласен", "согласна",
    "договорились", "ок", "okay", "yes", "подходит", "устраивает",
}

# Дополнительные естественные ответы, которые считаем явным согласием
# ТОЛЬКО на финальном этапе подтверждения уже предложенной встречи.
MEETING_CONFIRMATION_WORDS = {
    "готов", "готова",
    "все верно", "всё верно",
    "отлично", "замечательно",
}

NO_WORDS = {"нет", "не подходит", "неудобно", "другое время", "перенести"}


class DialogError(RuntimeError):
    pass


@dataclass
class Config:
    supabase_url: str
    supabase_secret_key: str
    fernet_key: str
    telegram_api_id: int
    telegram_api_hash: str
    openai_api_key: str


def _value(name: str, default: str = "") -> str:
    env_value = os.getenv(name)
    if env_value:
        return str(env_value)
    if st is not None:
        try:
            value = st.secrets.get(name, default)
            if value is not None:
                return str(value)
        except Exception:
            pass
    return default


def load_config() -> Config:
    required = {
        "SUPABASE_URL": _value("SUPABASE_URL"),
        "SUPABASE_SECRET_KEY": _value("SUPABASE_SECRET_KEY"),
        "FERNET_KEY": _value("FERNET_KEY"),
        "TELEGRAM_API_ID": _value("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": _value("TELEGRAM_API_HASH"),
        "OPENAI_API_KEY": _value("OPENAI_API_KEY"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise DialogError("Не найдены настройки: " + ", ".join(missing))
    return Config(
        supabase_url=required["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=required["SUPABASE_SECRET_KEY"],
        fernet_key=required["FERNET_KEY"],
        telegram_api_id=int(required["TELEGRAM_API_ID"]),
        telegram_api_hash=required["TELEGRAM_API_HASH"],
        openai_api_key=required["OPENAI_API_KEY"],
    )


def _headers(config: Config, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": config.supabase_secret_key,
        "Authorization": f"Bearer {config.supabase_secret_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _decrypt(config: Config, encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return Fernet(config.fernet_key.encode("utf-8")).decrypt(
            encrypted.encode("utf-8")
        ).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def _get_telegram_session(config: Config, owner_id: int) -> str:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/telegram_sessions",
        headers=_headers(config),
        params={
            "telegram_id": f"eq.{int(owner_id)}",
            "select": "encrypted_session",
            "limit": 1,
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return ""
    return _decrypt(config, str(rows[0].get("encrypted_session") or ""))


def _load_workspace(config: Config, owner_id: int) -> dict[str, Any]:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_workspace_states",
        headers=_headers(config),
        params={
            "telegram_id": f"eq.{int(owner_id)}",
            "select": "encrypted_state",
            "limit": 1,
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return {}
    raw = _decrypt(config, str(rows[0].get("encrypted_state") or ""))
    if not raw:
        return {}
    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except json.JSONDecodeError:
        return {}


def _persisted_dialog_contacts(
    config: Config,
    owner_id: int,
) -> dict[int, dict[str, Any]]:
    """Возвращает все диалоги, которые Неона уже когда-либо вела.

    Эти контакты не должны выпадать из наблюдения только потому, что сегодняшняя
    пятёрка/очередь Неонии изменилась. Таблица agency_dialog_states — постоянная
    память живых диалогов Неоны.
    """
    response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_dialog_states",
        headers=_headers(config),
        params={
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "select": "contact_telegram_id,stage,context,updated_at",
            "limit": 5000,
        },
        timeout=20,
    )
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    rows = response.json() if response.text.strip() else []

    result: dict[int, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            contact_id = int(row.get("contact_telegram_id"))
        except (TypeError, ValueError):
            continue
        if contact_id <= 0:
            continue

        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        try:
            first_message_id = int(
                context.get("story_reply_message_id")
                or context.get("first_message_id")
                or 0
            )
        except (TypeError, ValueError):
            first_message_id = 0

        result[contact_id] = {
            "sent_at": str(
                context.get("first_message_sent_at")
                or row.get("updated_at")
                or ""
            ),
            "recipient_name": str(
                context.get("contact_name")
                or context.get("recipient_name")
                or ""
            ),
            "message_id": first_message_id,
            "kind": "persisted_dialog",
            "stage": str(row.get("stage") or "idle"),
        }
    return result


def _allowed_contacts(config: Config, owner_id: int) -> dict[int, dict[str, Any]]:
    workspace = _load_workspace(config, owner_id)
    allowed: dict[int, dict[str, Any]] = {}
    for event in workspace.get("sent_log", []) if isinstance(workspace.get("sent_log"), list) else []:
        if not isinstance(event, dict):
            continue
        event_kind = str(event.get("kind") or "").strip()
        if event_kind not in {"first_message", "first_video", "story_reply"}:
            continue
        try:
            contact_id = int(event.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        try:
            first_message_id = int(event.get("message_id") or 0)
        except (TypeError, ValueError):
            first_message_id = 0
        sent_at = (
            str(event.get("story_sent_at") or "")
            if event_kind == "story_reply"
            else str(event.get("sent_at") or "")
        )
        allowed[contact_id] = {
            "sent_at": sent_at,
            "recipient_name": str(event.get("recipient_name") or ""),
            "message_id": first_message_id,
            "kind": event_kind,
        }

    # Постоянная память Неоны имеет меньший приоритет, чем свежий sent_log,
    # но гарантирует: уже начатый диалог не исчезнет после смены дневной пятёрки.
    for contact_id, item in _persisted_dialog_contacts(config, owner_id).items():
        allowed.setdefault(contact_id, item)

    return allowed




def _load_partner_lifecycle_map(
    config: Config,
    owner_id: int,
    contact_ids: list[int] | set[int] | tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    """Возвращает актуальный статус людей относительно партнёрства в Агентстве W.

    Это стоп-сигнал для кандидатского диалога Неоны:
    - candidate: человека ещё можно вести по воронке;
    - registered: человек уже зарегистрирован партнёром, продажи прекращаются;
    - active: 5 лож подтверждены, кандидатский диалог закрыт окончательно и
      дальнейшее сопровождение переходит к Неоле/разделу «Команда».

    Проверка выполняется по серверной базе Агентства W, а не по словам в чате.
    Поэтому Неона не должна угадывать, стал ли человек партнёром.
    """
    ids = sorted({int(value) for value in contact_ids if int(value) > 0})
    result: dict[int, dict[str, Any]] = {
        contact_id: {
            "state": "candidate",
            "is_member": False,
            "is_direct_partner": False,
            "member_code": "",
            "referrer_code": "",
            "registered_at": "",
            "activation_status": "",
            "lodges_count": 0,
            "onboarding_status": "",
        }
        for contact_id in ids
    }
    if not ids:
        return result

    # Код владельца нужен, чтобы отличать прямого партнёра от участника другой ветки.
    owner_response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_members",
        headers=_headers(config),
        params={
            "telegram_id": f"eq.{int(owner_id)}",
            "select": "telegram_id,member_code",
            "limit": 1,
        },
        timeout=20,
    )
    owner_response.raise_for_status()
    owner_rows = owner_response.json()
    owner_code = ""
    if isinstance(owner_rows, list) and owner_rows:
        owner_code = str(owner_rows[0].get("member_code") or "").strip()

    members_by_id: dict[int, dict[str, Any]] = {}
    activations_by_id: dict[int, dict[str, Any]] = {}

    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        in_filter = "in.(" + ",".join(str(value) for value in chunk) + ")"

        member_response = requests.get(
            f"{config.supabase_url}/rest/v1/agency_members",
            headers=_headers(config),
            params={
                "telegram_id": in_filter,
                "select": (
                    "telegram_id,member_code,referrer_code,created_at,"
                    "first_name,username"
                ),
                "limit": 1000,
            },
            timeout=20,
        )
        member_response.raise_for_status()
        for row in member_response.json() if member_response.text.strip() else []:
            try:
                members_by_id[int(row.get("telegram_id"))] = row
            except (TypeError, ValueError):
                continue

        activation_response = requests.get(
            f"{config.supabase_url}/rest/v1/partner_activations",
            headers=_headers(config),
            params={
                "telegram_id": in_filter,
                "select": (
                    "telegram_id,status,lodges_count,onboarding_status,"
                    "reviewed_at,last_action_at"
                ),
                "limit": 1000,
            },
            timeout=20,
        )
        # Таблица partner_activations уже является частью текущего онбординга.
        # Если она временно недоступна, НЕ продолжаем вслепую продажный диалог:
        # лучше пропустить цикл, чем снова продавать уже зарегистрированному партнёру.
        activation_response.raise_for_status()
        for row in activation_response.json() if activation_response.text.strip() else []:
            try:
                activations_by_id[int(row.get("telegram_id"))] = row
            except (TypeError, ValueError):
                continue

    for contact_id in ids:
        member = members_by_id.get(contact_id)
        activation = activations_by_id.get(contact_id)
        referrer_code = str((member or {}).get("referrer_code") or "").strip()
        activation_status = str((activation or {}).get("status") or "").strip()
        is_member = bool(member) and contact_id != int(owner_id)
        is_registered = bool(
            is_member
            and (
                referrer_code
                or activation is not None
            )
        )
        is_active = activation_status in {"confirmed", "legacy_active"}

        if is_active:
            lifecycle_state = "active"
        elif is_registered:
            lifecycle_state = "registered"
        else:
            lifecycle_state = "candidate"

        result[contact_id] = {
            "state": lifecycle_state,
            "is_member": is_member,
            "is_direct_partner": bool(owner_code and referrer_code == owner_code),
            "member_code": str((member or {}).get("member_code") or "").strip(),
            "referrer_code": referrer_code,
            "registered_at": str((member or {}).get("created_at") or "").strip(),
            "activation_status": activation_status,
            "lodges_count": int((activation or {}).get("lodges_count") or 0),
            "onboarding_status": str((activation or {}).get("onboarding_status") or "").strip(),
        }

    return result


def _partner_handoff_reply(first_name: str, lifecycle: dict[str, Any]) -> str:
    """Одно переходное сообщение: закрывает воронку кандидата без новой продажи."""
    prefix = f"{first_name}, " if first_name else ""
    state = str(lifecycle.get("state") or "candidate")

    if state == "active":
        return (
            prefix
            + "вижу, что вы уже активный партнёр Агентства W. "
            "Поэтому наш ознакомительный диалог я закрываю. "
            "Дальше по первым рабочим шагам вас сопровождает Неола, "
            "а связь с наставником и командой идёт уже в партнёрском контуре Агентства W."
        )

    return (
        prefix
        + "вижу, что вы уже зарегистрировались в Агентстве W. "
        "Поэтому я больше не веду вас как кандидата. "
        "Следующий шаг — подтвердить в Агентстве W не меньше 5 лож Neonexa; "
        "после подтверждения подключится Неола и поведёт вас по первым рабочим шагам."
    )


def _partner_lifecycle_context(
    context: dict[str, Any],
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(context or {})
    now = datetime.now(UTC).isoformat()
    state = str(lifecycle.get("state") or "candidate")
    if not updated.get("candidate_dialog_closed_at"):
        updated["candidate_dialog_closed_at"] = now
    updated.update(
        {
            "relationship_status": (
                "partner_active" if state == "active" else "partner_registered"
            ),
            "partner_status_checked_at": now,
            "partner_activation_status": str(
                lifecycle.get("activation_status") or ""
            ),
            "partner_lodges_count": int(lifecycle.get("lodges_count") or 0),
            "partner_member_code": str(lifecycle.get("member_code") or ""),
            "partner_referrer_code": str(lifecycle.get("referrer_code") or ""),
            "partner_is_direct": bool(lifecycle.get("is_direct_partner")),
            "candidate_dialog_closed": True,
            "handoff_target": "neola_and_team",
        }
    )
    return updated


def _load_stagirite_zoom_link(config: Config, owner_id: int) -> tuple[str, str]:
    """Берёт сохранённую владельцем ссылку Zoom из настроек Стагирита."""
    try:
        response = requests.get(
            f"{config.supabase_url}/rest/v1/agency_stagirite_tasks",
            headers=_headers(config),
            params={
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "task_kind": "eq.settings",
                "select": "result,updated_at",
                "order": "updated_at.desc",
                "limit": 1,
            },
            timeout=20,
        )
        if not response.ok:
            return "", ""
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return "", ""
        result = rows[0].get("result")
        if not isinstance(result, dict):
            return "", ""
        return (
            str(result.get("zoom_link") or "").strip(),
            str(result.get("zoom_note") or "").strip(),
        )
    except Exception:
        return "", ""


def _dialog_state(config: Config, owner_id: int, contact_id: int) -> dict[str, Any] | None:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_dialog_states",
        headers=_headers(config),
        params={
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "contact_telegram_id": f"eq.{int(contact_id)}",
            "select": "*",
            "limit": 1,
        },
        timeout=20,
    )
    if response.status_code == 404:
        raise DialogError(
            "Таблица agency_dialog_states ещё не создана. Выполните SQL-файл из комплекта."
        )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def _save_dialog_state(
    config: Config,
    owner_id: int,
    contact_id: int,
    *,
    last_incoming_id: int,
    stage: str,
    greeted: bool,
    context: dict[str, Any],
) -> None:
    payload = {
        "owner_telegram_id": int(owner_id),
        "contact_telegram_id": int(contact_id),
        "last_incoming_message_id": int(last_incoming_id),
        "stage": stage,
        "greeted": bool(greeted),
        "context": context,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    response = requests.post(
        f"{config.supabase_url}/rest/v1/agency_dialog_states?on_conflict=owner_telegram_id,contact_telegram_id",
        headers=_headers(config, "resolution=merge-duplicates,return=minimal"),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def reopen_last_incoming_for_retry(
    owner_id: int,
    contact_id: int,
) -> int:
    """Безопасно возвращает последнее входящее в очередь по явному решению владельца.

    Используется только когда Telegram уже показал входящий ответ, но в
    agency_dialog_states он отмечен как обработанный без подтверждённого ответа
    Неоны. Старую переписку не открывает и новые первые сообщения не создаёт.
    """
    config = load_config()
    state = _dialog_state(config, int(owner_id), int(contact_id))
    if not isinstance(state, dict):
        raise DialogError("Состояние этого диалога Неоны не найдено.")

    last_id = int(state.get("last_incoming_message_id") or 0)
    if last_id <= 0:
        raise DialogError("Последнего входящего сообщения для повтора нет.")

    context = (
        dict(state.get("context"))
        if isinstance(state.get("context"), dict)
        else {}
    )
    owner_fence_id = int(context.get("owner_outgoing_fence_id") or 0)
    if owner_fence_id >= last_id:
        raise DialogError(
            "После этого входящего владелец уже писал вручную. "
            "Автоматически повторять обработку небезопасно."
        )

    failed_id = int(context.get("last_processing_failed_id") or 0)
    last_reply_id = int(context.get("last_reply_id") or 0)
    last_reply_verified = bool(context.get("last_reply_verified"))

    if last_reply_id and last_reply_verified and failed_id != last_id:
        raise DialogError(
            "У последнего входящего уже есть подтверждённый ответ Неоны. "
            "Повторная отправка заблокирована."
        )

    attempts_map = (
        dict(context.get("message_processing_attempts"))
        if isinstance(context.get("message_processing_attempts"), dict)
        else {}
    )
    attempts_map.pop(str(last_id), None)

    cleaned_context = {
        **context,
        "message_processing_attempts": attempts_map,
        "manual_retry_message_id": last_id,
        "manual_retry_requested_at": datetime.now(UTC).isoformat(),
    }
    for key in (
        "last_processing_failed_id",
        "last_processing_failed_attempts",
        "last_processing_failed_at",
        "last_processing_error",
        "last_processing_suppressed_id",
        "last_processing_suppressed_at",
    ):
        cleaned_context.pop(key, None)

    _save_dialog_state(
        config,
        int(owner_id),
        int(contact_id),
        last_incoming_id=max(0, last_id - 1),
        stage=str(state.get("stage") or "idle"),
        greeted=bool(state.get("greeted", False)),
        context=cleaned_context,
    )
    return last_id


def initialize_dialog_after_first_message(
    owner_id: int,
    contact_id: int,
    *,
    baseline_incoming_id: int = 0,
    sent_at: str = "",
) -> None:
    """Создаёт точку отсчёта сразу после первого исходящего сообщения.

    Старую переписку Неона не трогает. Первый новый ответ человека уже
    окажется после baseline_incoming_id и будет обработан.
    """
    config = load_config()
    _save_dialog_state(
        config,
        int(owner_id),
        int(contact_id),
        last_incoming_id=int(baseline_incoming_id or 0),
        stage="idle",
        greeted=False,
        context={
            "first_message_sent_at": str(sent_at or ""),
            "activated_by": "agency_w_first_message",
        },
    )


def initialize_dialog_after_story_reply(
    owner_id: int,
    contact_id: int,
    *,
    baseline_incoming_id: int = 0,
    sent_at: str = "",
    story_id: int = 0,
    recipient_name: str = "",
) -> None:
    """Подключает Неону после вручную отправленного тёплого ответа на Story.

    Старую переписку Неона не трогает: baseline_incoming_id фиксирует последний
    входящий до касания, поэтому обрабатывается только следующий новый ответ.
    """
    config = load_config()
    _save_dialog_state(
        config,
        int(owner_id),
        int(contact_id),
        last_incoming_id=int(baseline_incoming_id or 0),
        stage="idle",
        greeted=False,
        context={
            "story_reply_sent_at": str(sent_at or ""),
            "story_id": int(story_id or 0),
            "recipient_name": str(recipient_name or ""),
            "channel": "telegram",
            "activated_by": "telegram_story_reply",
        },
    )


def _list_meetings(config: Config, owner_id: int, start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_meetings",
        headers=_headers(config),
        params={
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "start_at": f"lt.{end_utc.astimezone(UTC).isoformat()}",
            "end_at": f"gt.{start_utc.astimezone(UTC).isoformat()}",
            "status": "not.in.(Отменена,Перенесена)",
            "select": "*",
            "order": "start_at.asc",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _slot_free(config: Config, owner_id: int, start_utc: datetime, end_utc: datetime) -> bool:
    return not _list_meetings(config, owner_id, start_utc, end_utc)


def _create_meeting(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{config.supabase_url}/rest/v1/agency_meetings",
        headers=_headers(config, "return=representation"),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise DialogError("Supabase не подтвердил создание встречи.")
    return rows[0]


URGENT_CALLBACK_STATUS = "Просили срочно позвонить"
URGENT_CALLBACK_SOURCE = "Неона — срочный обратный звонок"


def _urgent_callback_intent(text: str) -> bool:
    """Распознаёт прямую просьбу, чтобы владелец сам позвонил человеку.

    Важно не путать её с «я сам позвоню Валентине»: такой ответ не создаёт
    задачу владельцу.
    """
    value = _normalize_intent_text(text)
    if not value:
        return False

    self_call = (
        r"\bя\s+(?:сам|сама)?\s*(?:ей|ему|вам|валентин\w*)?\s*позвон\w*",
        r"\bсам(?:а)?\s+позвон\w*",
        r"\bя\s+наберу\b",
    )
    if any(re.search(pattern, value) for pattern in self_call):
        return False

    callback_patterns = (
        r"\bпусть\s+(?:она|он|валентин\w*)?\s*позвон\w*",
        r"\bпозвоните\s+мне\b",
        r"\bпозвони\s+мне\b",
        r"\bможет\s+(?:она|он|валентин\w*)?\s*позвон\w*",
        r"\bхочу,?\s+чтобы\s+.*позвон\w*",
        r"\bжду\s+(?:е[её]|его|ваш)?\s*звон\w*",
        r"\bперезвоните\s+мне\b",
        r"\bперезвони\s+мне\b",
        r"\bсвяжитесь\s+со\s+мной\s+по\s+телефону\b",
    )
    return any(re.search(pattern, value) for pattern in callback_patterns)


def _ensure_urgent_callback_marker(
    config: Config,
    *,
    owner_id: int,
    owner_name: str,
    contact_id: int,
    contact_name: str,
    username: str,
    message_dt: datetime,
    incoming_message_id: int,
    incoming_text: str,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Создаёт в календаре одну заметную карточку срочного обратного звонка.

    Карточка не является назначенной встречей: она фиксирует просьбу человека
    позвонить как можно скорее. Повторный цикл worker не создаёт дубль.
    """
    updated = dict(context or {})
    existing_id = str(updated.get("urgent_callback_id") or "").strip()
    if existing_id:
        return None, updated

    # Дополнительная серверная защита от дублей при перезапуске worker или
    # временной ошибке сохранения dialog_state.
    response = requests.get(
        f"{config.supabase_url}/rest/v1/agency_meetings",
        headers=_headers(config),
        params={
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "contact_telegram_id": f"eq.{int(contact_id)}",
            "status": f"eq.{URGENT_CALLBACK_STATUS}",
            "source": f"eq.{URGENT_CALLBACK_SOURCE}",
            "select": "id,start_at,status",
            "limit": 1,
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json() if response.text.strip() else []
    if isinstance(rows, list) and rows:
        updated["urgent_callback_id"] = rows[0].get("id")
        updated["urgent_callback_status"] = URGENT_CALLBACK_STATUS
        updated["urgent_callback_requested_at"] = str(rows[0].get("start_at") or "")
        return rows[0], updated

    start_utc = message_dt.astimezone(UTC) if message_dt.tzinfo else message_dt.replace(tzinfo=UTC)
    # Для отображения в календаре нужна временная точка. Это короткая 10-минутная
    # карточка-напоминание в момент просьбы, а не забронированный слот встречи.
    end_utc = start_utc + timedelta(minutes=10)
    compact_text = re.sub(r"\s+", " ", str(incoming_text or "")).strip()[:500]
    notes = (
        "🔴 Просили срочно позвонить. "
        + (f"Сообщение: «{compact_text}». " if compact_text else "")
        + f"Telegram message_id: {int(incoming_message_id)}."
    )

    created = _create_meeting(
        config,
        {
            "owner_telegram_id": int(owner_id),
            "owner_name": owner_name,
            "contact_telegram_id": int(contact_id),
            "contact_name": contact_name or "Без имени",
            "contact_username": username or None,
            "contact_city": updated.get("contact_city") or None,
            "contact_timezone": updated.get("contact_timezone") or "Europe/Moscow",
            "start_at": start_utc.isoformat(),
            "end_at": end_utc.isoformat(),
            "meeting_format": "Телефонный звонок",
            "meeting_link": None,
            "status": URGENT_CALLBACK_STATUS,
            "notes": notes,
            "source": URGENT_CALLBACK_SOURCE,
        },
    )
    updated["urgent_callback_id"] = created.get("id")
    updated["urgent_callback_status"] = URGENT_CALLBACK_STATUS
    updated["urgent_callback_requested_at"] = start_utc.isoformat()
    updated["urgent_callback_incoming_message_id"] = int(incoming_message_id)
    return created, updated


def _first_name(entity: Any, fallback: str = "") -> str:
    """Возвращает только безопасное личное имя для обращения Неоны.

    Telegram иногда кладёт в first_name имя и фамилию целиком или ник/декор.
    Неона использует только первое понятное имя. Если имя неясно — не угадывает.
    """

    blocked_tokens = {
        "business", "biznes", "бизнес", "crypto", "крипто", "money", "деньги",
        "coach", "коуч", "manager", "менеджер", "admin", "админ",
        "official", "shop", "магазин", "team", "команда", "project", "проект",
        "partner", "партнер", "партнёр", "online",
    }

    def clean_name_token(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        # Только первый словесный фрагмент: «Зинаида Жук» -> «Зинаида».
        raw = re.split(r"[\s|,/]+", raw, maxsplit=1)[0].strip()

        chars = []
        for character in raw:
            if character.isalpha() or (character in "-'’" and chars):
                chars.append(character)
                continue
            break

        token = "".join(chars).strip("-'’")
        if len(token) < 2 or len(token) > 40:
            return ""
        if token.casefold() in blocked_tokens:
            return ""

        # Человеческий вид без попыток угадать неизвестное имя.
        if token.isupper() and len(token) > 1:
            token = token.title()
        elif token.islower() and len(token) > 1:
            token = token[:1].upper() + token[1:]

        return token

    name = clean_name_token(getattr(entity, "first_name", ""))
    if name:
        return name

    return clean_name_token(fallback)


def _greeting(name: str) -> str:
    return f"Здравствуйте, {name}!" if name else "Здравствуйте!"


def _detect_format(text: str) -> str | None:
    lowered = text.lower()
    if "zoom" in lowered or "зум" in lowered:
        return "Zoom"
    if "whatsapp" in lowered or "ватсап" in lowered or "вацап" in lowered:
        return "WhatsApp"
    if "telegram" in lowered or "телеграм" in lowered:
        return "Telegram"
    return None


def _detect_timezone(text: str) -> str | None:
    lowered = text.lower().strip()
    iana = re.search(r"\b[A-Za-z_]+/[A-Za-z_+-]+\b", text)
    if iana:
        try:
            ZoneInfo(iana.group(0))
            return iana.group(0)
        except ZoneInfoNotFoundError:
            pass
    for token, zone in TZ_ALIASES.items():
        if token in lowered:
            return zone
    return None


def _detect_times(text: str) -> list[str]:
    """
    Возвращает ВСЕ явно названные варианты времени в порядке сообщения.

    Понимает, в частности: 11:00, 11.00, 11-00, 11–00, 11.00ч,
    «в 11», «11 часов». Это важно, когда человек сам предлагает
    несколько вариантов: «завтра в 11.00ч, 12.00ч, 16.00ч по МСК».
    """
    raw = str(text or "")
    lowered = raw.casefold()
    found: list[tuple[int, str]] = []

    # Время с минутами. Разрешаем необязательное «ч» сразу после минут.
    for match in re.finditer(
        r"(?<!\d)([01]?\d|2[0-3])\s*[:.\-–—]\s*([0-5]\d)\s*(?:ч\b|час(?:а|ов)?\b)?",
        lowered,
    ):
        value = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        found.append((match.start(), value))

    # Часы без минут: «в 11», «11 часов». Не захватываем числа, уже
    # являющиеся частью записи 11.00 / даты / другого числового выражения.
    for match in re.finditer(
        r"(?<![\d.:/\-])(?:в\s+)?([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|ч)\b",
        lowered,
    ):
        value = f"{int(match.group(1)):02d}:00"
        found.append((match.start(), value))

    # Отдельное «в 11» без слова «час».
    for match in re.finditer(
        r"(?<!\w)в\s+([01]?\d|2[0-3])(?=\s|[,;!?]|$)",
        lowered,
    ):
        value = f"{int(match.group(1)):02d}:00"
        found.append((match.start(), value))

    result: list[str] = []
    seen: set[str] = set()
    for _, value in sorted(found, key=lambda item: item[0]):
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _detect_time(text: str) -> str | None:
    """Совместимость со старой логикой: возвращает первый найденный вариант."""
    times = _detect_times(text)
    return times[0] if times else None


def _detect_date(text: str, message_dt: datetime, tz_name: str | None) -> str | None:
    lowered = text.lower()
    tz = ZoneInfo(tz_name) if tz_name else MSK
    base = message_dt.astimezone(tz).date()
    if "послезавтра" in lowered:
        return (base + timedelta(days=2)).isoformat()
    if "завтра" in lowered:
        return (base + timedelta(days=1)).isoformat()
    if "сегодня" in lowered:
        return base.isoformat()

    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year_raw = match.group(3)
        year = int(year_raw) if year_raw else base.year
        if year < 100:
            year += 2000
        try:
            candidate = date(year, month, day)
            if not year_raw and candidate < base - timedelta(days=2):
                candidate = date(year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            return None

    for word, weekday in WEEKDAYS_RU.items():
        if word in lowered:
            delta = (weekday - base.weekday()) % 7
            if delta == 0:
                delta = 7
            return (base + timedelta(days=delta)).isoformat()
    return None


def _meeting_intent(text: str) -> bool:
    """Определяет именно намерение назначить встречу, а не просто поговорить/связаться."""
    lowered = _normalize_intent_text(text)

    # Явный отказ от встречи никогда не должен запускать календарную ветку.
    negative_markers = (
        "не хочу встреч",
        "не нужна встреч",
        "без встречи",
        "не надо встреч",
        "не хочу созвон",
        "созвон не нужен",
    )
    if any(marker in lowered for marker in negative_markers):
        return False

    # Календарь запускаем только по явным признакам встречи/созвона.
    if any(token in lowered for token in ("встреч", "созвон", "zoom", "зум")):
        return True

    # Либо когда человек сам дал конкретные дату + время.
    has_date_hint = any(token in lowered for token in ("сегодня", "завтра", "послезавтра")) or bool(
        re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
    ) or any(word in lowered for word in WEEKDAYS_RU)
    return bool(_detect_time(text) and has_date_hint)


def _is_yes(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    tokens = set(lowered.split())
    return any((" " in word and word in lowered) or (" " not in word and word in tokens) for word in YES_WORDS)


def _is_meeting_confirmation(text: str) -> bool:
    """Явное согласие именно на уже предложенные дату/время/формат встречи."""
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    tokens = set(lowered.split())
    words = YES_WORDS | MEETING_CONFIRMATION_WORDS
    return any((" " in word and word in lowered) or (" " not in word and word in tokens) for word in words)


def _is_no(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    tokens = set(lowered.split())
    return any((" " in word and word in lowered) or (" " not in word and word in tokens) for word in NO_WORDS)


def _is_positive_interest(text: str) -> bool:
    """Явный интерес к показу/встрече после первого сообщения."""
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    if _is_yes(text):
        return True
    markers = (
        "интересно",
        "мне интересно",
        "хочу",
        "хочу посмотреть",
        "покажи",
        "покажите",
        "давайте",
        "готов",
        "готова",
        "можно посмотреть",
        "хочу увидеть",
    )
    return any(marker in lowered for marker in markers)


def _contact_stop_intent(text: str) -> bool:
    """Явная просьба прекратить автоматический диалог или признак, что сообщения мешают.

    Это жёсткий стоп: после него Неона отправляет одно короткое завершение и
    больше не пишет автоматически, пока человек сам явно не возобновит диалог.
    """
    value = _normalize_intent_text(text)
    if not value:
        return False

    patterns = (
        r"\bне\s+хочу\s+(?:с\s+(?:тобой|вами)\s+)?общат",
        r"\bмне\s+(?:с\s+(?:тобой|вами)\s+)?не\s*интересн\w*\s+общат",
        r"\bне\s+интересн\w*\s+(?:с\s+(?:тобой|вами)\s+)?общат",
        r"\bне\s+(?:пиши|пишите)\b",
        r"\b(?:не\s+)?(?:беспокой|беспокойте)\s+(?:меня)?\b",
        r"\bне\s+надо\s+(?:мне\s+)?писать\b",
        r"\b(?:вы\s+)?(?:только\s+)?(?:лишь\s+)?отвлека(?:ете|ешь)\s+меня\b",
        r"\bсообщени\w*\s+.*\bне\s+по\s+тем",
        r"\bоставьте\s+меня\s+в\s+покое\b",
        r"\bотстаньте\b",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def _contact_reopen_intent(text: str) -> bool:
    """Человек после стопа сам явно просит снова продолжить разговор."""
    value = _normalize_intent_text(text)
    if not value:
        return False
    phrases = (
        "давайте продолжим",
        "можно продолжить",
        "продолжим разговор",
        "хочу продолжить",
        "мне интересно узнать",
        "расскажите об агентстве",
        "расскажи об агентстве",
        "можно вопрос",
    )
    return any(phrase in value for phrase in phrases)


def _contact_stop_reply(first_name: str, greet: bool) -> str:
    prefix = _greeting(first_name) + " " if greet else ""
    return prefix + "Поняла. Больше не буду вас отвлекать."


def _is_simple_acknowledgement(text: str) -> bool:
    """Короткая реакция после уже назначенной встречи не требует ответа."""
    raw = text.strip().lower()
    if not raw:
        return True

    emoji_only = re.sub(
        r"[\s👍👌🙏❤️❤✅👏🙂😊🔥🎉💚💛💙💜🤝]+",
        "",
        raw,
    )
    if not emoji_only:
        return True

    normalized = re.sub(r"[^a-zа-яё0-9 ]+", " ", raw).strip()
    phrases = {
        "спасибо",
        "благодарю",
        "отлично",
        "хорошо",
        "супер",
        "договорились",
        "до встречи",
        "ок",
        "okay",
        "понятно",
        "ясно",
        "принято",
    }
    return normalized in phrases



def _parse_start(context: dict[str, Any]) -> datetime | None:
    date_value = context.get("requested_date")
    time_value = context.get("requested_time")
    tz_name = context.get("contact_timezone")
    if not (date_value and time_value and tz_name):
        return None
    try:
        local_date = date.fromisoformat(str(date_value))
        hh, mm = [int(part) for part in str(time_value).split(":", 1)]
        local = datetime.combine(local_date, dt_time(hh, mm), ZoneInfo(str(tz_name)))
        return local.astimezone(UTC)
    except Exception:
        return None


def _format_slot(start_utc: datetime, tz_name: str) -> str:
    local = start_utc.astimezone(ZoneInfo(tz_name))
    msk = start_utc.astimezone(MSK)
    if tz_name == "Europe/Moscow":
        return f"{msk:%d.%m.%Y} в {msk:%H:%M} МСК"
    return (
        f"{msk:%d.%m.%Y} в {msk:%H:%M} МСК. "
        f"Для вас это {local:%H:%M} по местному времени"
    )


def _find_three_slots(config: Config, owner_id: int, around_utc: datetime, contact_timezone: str) -> list[datetime]:
    result: list[datetime] = []
    start_day_msk = around_utc.astimezone(MSK).date()
    for day_offset in range(0, 8):
        day = start_day_msk + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        cursor = datetime.combine(day, dt_time(10, 0), MSK)
        end = datetime.combine(day, dt_time(20, 0), MSK)
        while cursor + timedelta(minutes=DURATION_MINUTES) <= end:
            start_utc = cursor.astimezone(UTC)
            end_utc = start_utc + timedelta(minutes=DURATION_MINUTES)
            local = start_utc.astimezone(ZoneInfo(contact_timezone))
            if 8 <= local.hour < 22 and start_utc > datetime.now(UTC) + timedelta(minutes=30):
                if _slot_free(config, owner_id, start_utc, end_utc):
                    result.append(start_utc)
                    if len(result) == 3:
                        return result
            cursor += timedelta(minutes=30)
    return result



def _normalize_intent_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _direct_material_request(text: str) -> bool:
    """Явная просьба человека прислать ссылку/материал без догадок по одному «да»."""
    value = _normalize_intent_text(text)
    if not value:
        return False
    direct_phrases = (
        "пришлите ссылку", "пришли ссылку", "дайте ссылку", "дай ссылку",
        "пришлите материал", "пришли материал", "отправьте материал", "отправь материал",
        "хочу посмотреть", "хочу увидеть", "покажите материал", "покажи материал",
        "где посмотреть", "можно ссылку", "скиньте ссылку", "скинь ссылку",
    )
    return any(phrase in value for phrase in direct_phrases)


def _explicit_material_consent(text: str) -> bool:
    """Согласие после того, как Неона уже предложила прислать материал."""
    value = _normalize_intent_text(text)
    if not value:
        return False
    compact = re.sub(r"[^a-zа-яё0-9]+", " ", value, flags=re.IGNORECASE).strip()
    exact = {
        "да", "давайте", "конечно", "можно", "хорошо", "ок", "okay", "yes",
        "интересно", "пришлите", "пришли", "отправьте", "отправь", "покажите", "покажи",
        "да пожалуйста", "да интересно", "конечно присылайте", "давайте посмотрю",
    }
    if compact in exact:
        return True
    phrases = (
        "да пришлите", "да пришли", "да отправьте", "да отправь", "да покажите",
        "да покажи", "можно посмотреть", "хочу посмотреть", "интересно посмотреть",
        "пришлите пожалуйста", "отправьте пожалуйста", "конечно пришлите",
    )
    return any(phrase in compact for phrase in phrases)


def _material_offer_intent(reply: str) -> bool:
    """Распознаёт только явное предложение Неоны прислать/показать материал."""
    value = _normalize_intent_text(reply)
    if not value:
        return False
    offer_words = ("пришлю", "могу прислать", "отправлю", "могу отправить", "могу показать")
    material_words = ("пример", "материал", "ссылк", "ролик", "видео", "канал")
    return any(word in value for word in offer_words) and any(word in value for word in material_words)


def _telegram_material_reply(first_name: str, greet: bool) -> str:
    prefix = _greeting(first_name) + " " if greet else ""
    return (
        prefix
        + "Вот материалы об Агентстве W, которые можно посмотреть в удобное время: "
        + TELEGRAM_AGENCY_MATERIAL_URL
        + ". Если что-то откликнется, напишете, что именно?"
    )

def _openai_general_reply(
    config: Config,
    owner_name: str,
    first_name: str,
    text: str,
    greet: bool,
    context: dict[str, Any] | None = None,
) -> str:
    greeting_rule = (
        f"Начни с «{_greeting(first_name)}»" if greet else "Не повторяй приветствие, если диалог уже начат."
    )

    context = dict(context or {})
    previous_reply = str(context.get("last_reply_text") or "").strip()

    # Короткий ответ вроде «Да» можно понять только вместе с предыдущим вопросом Неоны.
    # Поэтому всегда передаём модели последнюю реплику Неоны, если она сохранена.
    dialog_input = (
        "Последнее сообщение Неоны:\n"
        + (previous_reply if previous_reply else "(нет сохранённого предыдущего сообщения)")
        + "\n\nНовое сообщение человека:\n"
        + str(text or "")
        + "\n\nКлючевая проверка перед ответом:\n"
          "Определи текущий этап человека. Не предлагай действия следующего этапа, "
          "если человек их ещё не выбрал сам."
    )

    constitution = NEONA_CONSTITUTION or (
        "Слушай смысл последней реплики, не задавай лишних вопросов, "
        "не обещай невыполненных действий и не веди к встрече раньше времени."
    )
    knowledge = NEONA_KNOWLEDGE_BASE or (
        "Используй только подтверждённые факты об Агентстве W и не придумывай функции."
    )
    constitution = _personalize_neona_reference_text(constitution, owner_name)
    knowledge = _personalize_neona_reference_text(knowledge, owner_name)

    instructions = f"""
Ты Неона — ИИ-секретарь-референт {owner_name} в Агентстве W.
КРИТИЧНО: текущий владелец этого кабинета — {owner_name}. Не представляйся
секретарём Валентины и не предлагай встречу с Валентиной, если текущий владелец
не Валентина. Все упоминания владельца должны относиться именно к {owner_name}.
Пиши по-русски простым человеческим языком, без корпоративного жаргона.

{NEONA_DIALOG_CORE}

ВАЖНО: Конституция и база знаний ниже являются актуальными правилами Неоны.
Если старое общее правило или привычный скрипт им противоречит, следуй Конституции и базе знаний.

===== КОНСТИТУЦИЯ НЕОНЫ =====
{constitution}
===== КОНЕЦ КОНСТИТУЦИИ =====

===== БАЗА ЗНАНИЙ НЕОНЫ =====
{knowledge}
===== КОНЕЦ БАЗЫ ЗНАНИЙ =====

Операционные правила этого диалога:
- Сначала пойми, на что именно отвечает человек. Короткое «да/нет/хорошо» трактуй только относительно последнего сообщения Неоны, которое дано во входном контексте.
- Если человек исправляет одно слово, формулировку или факт в предыдущем сообщении Неоны, просто коротко признай поправку. Не превращай поправку в новую тему и не задавай вслед новый вопрос без необходимости.
- Не используй местоимения «он/она/оно/её/его/это», если из предыдущей фразы не совершенно ясно, к чему они относятся. Лучше повтори конкретное слово.
- Если человек спрашивает, пишет ли бот/ИИ, отвечай прозрачно: ты Неона, ИИ-секретарь-референт {owner_name}. Не оправдывайся и не продолжай продажу без интереса человека.
- Если человек говорит, что ему неинтересно общаться, просит не писать, говорит, что сообщения отвлекают или не по теме, не убеждай и не задавай вопросов. Коротко извинись/подтверди остановку и прекрати автоматический диалог.
- Сначала ответь на текущий вопрос или реши текущую задачу; только затем предлагай следующий шаг.
- Не превращай техническую проблему, просьбу о помощи или просьбу связать с владельцем в автоматическое приглашение на встречу.
- Если человек уже ясно сказал, чего хочет, не спрашивай то же самое другими словами.
- Не задавай вопрос просто ради продолжения беседы. Вопрос нужен только если без ответа нельзя выбрать следующий полезный шаг.
- Никогда не говори «проверила», «записала», «отправила», «передала», «добавила», «создала», если соответствующее действие технически не было реально выполнено.
- Если человек прямо просит Директора/владельца, не устраивай допрос. Ответь по существу и предложи только реально доступный способ перехода.
- Встречу предлагай только как естественный следующий шаг после явного интереса или прямой просьбы человека.
- Не убегай вперёд этапа человека. Если он только хочет понять, что такое Агентство W, не предлагай список кандидатов, приглашения, рабочий запуск или онбординг.
- Если встреча уже запрошена/согласуется, твоя задача — только довести согласование встречи до конца. Не добавляй новые предложения, продажи или рабочие действия.
- Если человек ещё не увидел ответ Директора, напомни мягко: «Директор уже ответил по встрече». Не пиши это как упрёк и не предлагай параллельно новые действия.
- Не упоминай спонсора, наставника, знакомых и третьих лиц, если они не нужны для текущего вопроса.
- Не используй фамилию собеседника в обращении. {greeting_rule}
- Ответ — обычно 1–3 коротких предложения; 4 только если без этого теряется смысл.
- Материалы Агентства W в Telegram: {TELEGRAM_AGENCY_MATERIAL_URL}. Никогда не давай ссылку без запроса в первом холодном касании. Если человек прямо просит ссылку/материал — можно дать. Если сама предлагаешь материал, сначала дождись отдельного согласия.
- Если человек занят, не толкай к встрече. Коротко признай это и предложи один маленький полезный следующий шаг только если он уместен.
- Если человеку интересно, сначала свяжи возможности Агентства W с его задачей. Не пересказывай рекламный текст и не обещай результат.
- Дату, время и формат встречи не выдумывай: календарная ветка согласует их отдельно.
""".strip()

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-5-mini",
            "instructions": instructions,
            "input": dialog_input,
            "store": False,
        },
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    parts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    answer = "\n".join(parts).strip()
    if not answer:
        raise DialogError("OpenAI не сформировал ответ.")
    return answer


def _update_context_from_message(context: dict[str, Any], text: str, message_dt: datetime) -> dict[str, Any]:
    context = dict(context or {})
    detected_tz = _detect_timezone(text)
    if detected_tz:
        context["contact_timezone"] = detected_tz
    detected_format = _detect_format(text)
    if detected_format:
        context["meeting_format"] = detected_format
    detected_times = _detect_times(text)
    if len(detected_times) > 1:
        # Человек уже дал выбор времени. Не заставляем его повторять то,
        # что он только что написал: сохраняем все варианты для проверки
        # календаря и убираем возможное старое одиночное значение.
        context["requested_time_options"] = detected_times
        context.pop("requested_time", None)
    elif len(detected_times) == 1:
        context["requested_time"] = detected_times[0]
        context.pop("requested_time_options", None)
    detected_date = _detect_date(text, message_dt, context.get("contact_timezone"))
    if detected_date:
        context["requested_date"] = detected_date
    return context


def _apply_owner_manual_scheduling_message(
    config: Config,
    owner_id: int,
    text: str,
    message_dt: datetime,
    stage: str,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Учитывает ручное сообщение владельца как часть согласования встречи.

    Владелец может сам написать, например, «11:00» после того, как кандидат
    предложил несколько вариантов. Раньше такое сообщение служило только
    fence-сигналом и его смысл терялся: следующий ответ кандидата уже нельзя
    было связать с выбранным временем. Теперь ручной выбор становится частью
    того же state machine, что и выбор Неоны.
    """
    current = dict(context or {})
    scheduling_stage = stage in {
        "invited_to_meeting",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "awaiting_slot_choice",
    }

    # Одиночное «11:00» имеет смысл как встреча только внутри уже начатого
    # согласования. Вне него не превращаем любое ручное время в календарь.
    if not scheduling_stage and not any(
        current.get(key)
        for key in (
            "requested_date",
            "requested_time",
            "requested_time_options",
            "proposed_start_at",
        )
    ) and not _meeting_intent(text):
        return stage, current

    updated = _update_context_from_message(current, text, message_dt)
    detected_time = _detect_time(text)
    detected_date = _detect_date(
        text,
        message_dt,
        updated.get("contact_timezone"),
    )

    # Если владелец выбрал один из ранее предложенных вариантов, сохраняем
    # именно его и снимаем список вариантов.
    if detected_time:
        updated["requested_time"] = detected_time
        updated.pop("requested_time_options", None)
        updated["owner_selected_time"] = detected_time
    if detected_date:
        updated["requested_date"] = detected_date
        updated["owner_selected_date"] = detected_date

    # Формат владелец тоже может уточнить вручную.
    detected_format = _detect_format(text)
    if detected_format:
        updated["meeting_format"] = detected_format

    start_utc = _parse_start(updated)
    if start_utc is not None and start_utc > datetime.now(UTC) + timedelta(minutes=5):
        updated["proposed_start_at"] = start_utc.isoformat()
        updated["owner_manual_schedule_pending_confirmation"] = True
        updated["owner_manual_schedule_selected_at"] = datetime.now(UTC).isoformat()
        # Даже если формат ещё не назван, оставляем awaiting_confirmation:
        # следующий ответ кандидата может одновременно подтвердить встречу и
        # уточнить формат («Отлично, созвонимся в WhatsApp»).
        return "awaiting_confirmation", updated

    return stage, updated


def _schedule_reply(
    config: Config,
    owner_id: int,
    owner_name: str,
    contact_id: int,
    first_name: str,
    username: str,
    text: str,
    message_dt: datetime,
    stage: str,
    context: dict[str, Any],
    greet: bool,
) -> tuple[str, str, dict[str, Any]]:
    context = _update_context_from_message(context, text, message_dt)
    prefix = _greeting(first_name) + " " if greet else ""

    if stage == "invited_to_meeting" and _is_no(text):
        return (
            prefix + "Хорошо. Тогда продолжим здесь, без встречи. Что вам сейчас интереснее всего узнать?",
            "idle",
            {},
        )

    # Подтверждение работает одинаково, независимо от того, кто выбрал слот:
    # сама Неона, кандидат или владелец, вручную вмешавшийся в Telegram.
    # owner_manual_schedule_pending_confirmation выставляется при ручном выборе
    # владельцем даты/времени и не даёт такому согласованию выпасть из календаря.
    if stage == "awaiting_confirmation" or bool(
        context.get("owner_manual_schedule_pending_confirmation")
    ):
        if _is_meeting_confirmation(text):
            proposed = context.get("proposed_start_at")
            tz_name = str(context.get("contact_timezone") or "")
            meeting_format = str(context.get("meeting_format") or "")
            if not (proposed and tz_name and meeting_format):
                stage = "collecting_meeting_details"
            else:
                start_utc = datetime.fromisoformat(str(proposed).replace("Z", "+00:00")).astimezone(UTC)
                end_utc = start_utc + timedelta(minutes=DURATION_MINUTES)
                if not _slot_free(config, owner_id, start_utc, end_utc):
                    context.pop("proposed_start_at", None)
                    stage = "collecting_meeting_details"
                    return (
                        prefix + "Пока мы подтверждали, это время стало занято. Напишите, пожалуйста, другой удобный день или время — я проверю его по календарю.",
                        stage,
                        context,
                    )
                zoom_link = ""
                zoom_note = ""
                if "zoom" in meeting_format.lower() or "зум" in meeting_format.lower():
                    zoom_link, zoom_note = _load_stagirite_zoom_link(
                        config,
                        owner_id,
                    )

                channel = str(
                    context.get("channel") or "telegram"
                ).strip().casefold()

                source_label = {
                    "vk": "Неона — VK диалог",
                    "instagram": "Неона — Instagram Direct",
                    "telegram": "Неона — Telegram диалог",
                }.get(channel, "Неона — Telegram диалог")

                notes_label = {
                    "vk": "Назначено Неоной после подтверждения человека во VK.",
                    "instagram": "Назначено Неоной после подтверждения человека в Instagram Direct.",
                    "telegram": "Назначено Неоной после подтверждения человека в Telegram.",
                }.get(
                    channel,
                    "Назначено Неоной после подтверждения человека в Telegram.",
                )

                created = _create_meeting(
                    config,
                    {
                        "owner_telegram_id": int(owner_id),
                        "owner_name": owner_name,
                        "contact_telegram_id": int(contact_id),
                        "contact_name": first_name or "Без имени",
                        "contact_username": username or None,
                        "contact_city": context.get("contact_city") or tz_name,
                        "contact_timezone": tz_name,
                        "start_at": start_utc.isoformat(),
                        "end_at": end_utc.isoformat(),
                        "meeting_format": meeting_format,
                        "meeting_link": zoom_link or None,
                        "status": "Подтверждена",
                        "notes": notes_label,
                        "source": source_label,
                    },
                )
                confirmed_start = datetime.fromisoformat(str(created["start_at"]).replace("Z", "+00:00")).astimezone(UTC)
                context["meeting_id"] = created.get("id")
                context["owner_manual_schedule_pending_confirmation"] = False
                context["meeting_confirmed_at"] = datetime.now(UTC).isoformat()
                stage = "scheduled"

                zoom_part = ""
                if zoom_link:
                    zoom_part = f" Ссылка Zoom: {zoom_link}."
                    if zoom_note:
                        zoom_part += f" {zoom_note}"

                return (
                    prefix
                    + f"Договорились! Встреча с {owner_name} назначена: {_format_slot(confirmed_start, tz_name)}. Формат — {meeting_format}. Встреча действительно внесена в календарь."
                    + zoom_part,
                    stage,
                    context,
                )
        if _is_no(text):
            context.pop("proposed_start_at", None)
            context["owner_manual_schedule_pending_confirmation"] = False
            stage = "collecting_meeting_details"
            return (
                prefix + "Хорошо. Напишите, пожалуйста, какой день и время вам удобнее.",
                stage,
                context,
            )

    if stage == "awaiting_slot_choice":
        choice = re.search(r"\b([123])\b", text)
        slots = context.get("offered_slots") or []
        if choice and isinstance(slots, list) and len(slots) >= int(choice.group(1)):
            selected = slots[int(choice.group(1)) - 1]
            context["proposed_start_at"] = selected
            start_utc = datetime.fromisoformat(str(selected).replace("Z", "+00:00")).astimezone(UTC)
            stage = "awaiting_confirmation"
            return (
                prefix + f"Выбрали {_format_slot(start_utc, str(context['contact_timezone']))}. Формат — {context['meeting_format']}. Подтверждаем?",
                stage,
                context,
            )

       # Спрашиваем только то, чего действительно не хватает.
    # Уже названные человеком данные повторно не запрашиваем.

    if not context.get("requested_date"):
        lead = (
            "Отлично. Тогда давайте подберём удобное время. "
            if stage == "invited_to_meeting"
            else ""
        )
        return (
            prefix + lead + "На какой день вам удобна встреча?",
            "collecting_meeting_details",
            context,
        )

    time_options = context.get("requested_time_options")
    if isinstance(time_options, list) and time_options:
        # Если человек сам предложил несколько вариантов, сначала сохраняем
        # недостающие параметры, а затем проверяем КАЖДЫЙ вариант по календарю.
        # Повторно спрашивать «На какое время вам удобно?» здесь нельзя.
        if not context.get("contact_timezone"):
            return (
                prefix + "Вы предложили несколько вариантов времени. По какому часовому поясу они указаны?",
                "collecting_meeting_details",
                context,
            )
        if not context.get("meeting_format"):
            return (
                prefix + "Вижу ваши варианты времени. Как вам удобнее созвониться — Telegram или WhatsApp?",
                "collecting_meeting_details",
                context,
            )

        tz_name = str(context["contact_timezone"])
        meeting_format = str(context["meeting_format"])
        try:
            local_date = date.fromisoformat(str(context["requested_date"]))
            contact_tz = ZoneInfo(tz_name)
        except Exception:
            return (
                prefix + "Не смогла точно определить дату. Напишите, пожалуйста, дату ещё раз.",
                "collecting_meeting_details",
                context,
            )

        valid_starts: list[datetime] = []
        for option in time_options:
            try:
                hh, mm = [int(part) for part in str(option).split(":", 1)]
                local_start = datetime.combine(local_date, dt_time(hh, mm), contact_tz)
                start_option_utc = local_start.astimezone(UTC)
            except Exception:
                continue
            if start_option_utc <= datetime.now(UTC) + timedelta(minutes=5):
                continue
            valid_starts.append(start_option_utc)
            if _slot_free(
                config,
                owner_id,
                start_option_utc,
                start_option_utc + timedelta(minutes=DURATION_MINUTES),
            ):
                context["requested_time"] = str(option)
                context["proposed_start_at"] = start_option_utc.isoformat()
                context.pop("requested_time_options", None)
                return (
                    prefix
                    + f"Я проверила ваши варианты по календарю. {_format_slot(start_option_utc, tz_name)} у {owner_name} свободно. Формат — {meeting_format}. Подтверждаем это время?",
                    "awaiting_confirmation",
                    context,
                )

        around_utc = valid_starts[0] if valid_starts else datetime.now(UTC) + timedelta(hours=1)
        slots = _find_three_slots(config, owner_id, around_utc, tz_name)
        if not slots:
            return (
                prefix + "Я проверила все предложенные вами варианты — они заняты. Напишите, пожалуйста, другой удобный день, и я проверю календарь.",
                "collecting_meeting_details",
                context,
            )
        context["offered_slots"] = [slot.isoformat() for slot in slots]
        context.pop("requested_time_options", None)
        options = "\n".join(
            f"{index}. {_format_slot(slot, tz_name)}"
            for index, slot in enumerate(slots, 1)
        )
        return (
            prefix
            + "Я проверила все предложенные вами варианты — они заняты. Вот ближайшие свободные:\n"
            + options
            + "\nНапишите номер подходящего варианта.",
            "awaiting_slot_choice",
            context,
        )

    if not context.get("requested_time"):
        return (
            prefix + "На какое время вам удобно?",
            "collecting_meeting_details",
            context,
        )

    if not context.get("contact_timezone"):
        return (
            prefix + "По какому часовому поясу указано время?",
            "collecting_meeting_details",
            context,
        )

    if not context.get("meeting_format"):
        return (
            prefix + "Как вам удобнее встретиться — Zoom, Telegram или WhatsApp?",
            "collecting_meeting_details",
            context,
        )

    start_utc = _parse_start(context)
    if start_utc is None:
        return prefix + "Не смогла точно определить время. Напишите, пожалуйста, дату и время ещё раз.", "collecting_meeting_details", context
    if start_utc <= datetime.now(UTC) + timedelta(minutes=5):
        return prefix + "Это время уже прошло или слишком близко. Напишите, пожалуйста, другое удобное время.", "collecting_meeting_details", context

    end_utc = start_utc + timedelta(minutes=DURATION_MINUTES)
    tz_name = str(context["contact_timezone"])
    meeting_format = str(context["meeting_format"])

    if _slot_free(config, owner_id, start_utc, end_utc):
        context["proposed_start_at"] = start_utc.isoformat()
        return (
            prefix
            + f"Я проверила календарь: {_format_slot(start_utc, tz_name)} у {owner_name} свободно. Формат — {meeting_format}. Подтверждаем это время?",
            "awaiting_confirmation",
            context,
        )

    slots = _find_three_slots(config, owner_id, start_utc, tz_name)
    if not slots:
        return (
            prefix + "Это время занято, а в ближайшем рабочем окне свободных вариантов пока не нашлось. Напишите другой удобный день — я проверю.",
            "collecting_meeting_details",
            context,
        )
    context["offered_slots"] = [slot.isoformat() for slot in slots]
    options = "\n".join(f"{index}. {_format_slot(slot, tz_name)}" for index, slot in enumerate(slots, 1))
    return (
        prefix + "Это время занято. Нашла ближайшие свободные варианты:\n" + options + "\nНапишите номер подходящего варианта.",
        "awaiting_slot_choice",
        context,
    )


def _process_message(
    config: Config,
    owner_id: int,
    owner_name: str,
    contact_id: int,
    first_name: str,
    username: str,
    text: str,
    message_dt: datetime,
    state: dict[str, Any],
) -> tuple[str, str, bool, dict[str, Any]]:
    stage = str(state.get("stage") or "idle")
    greeted = bool(state.get("greeted", False))
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    greet = not greeted

    # Жёсткий приоритет уважения к границе человека. Если он просит прекратить
    # общение или прямо говорит, что сообщения мешают, Неона не пытается
    # "спасти" диалог новым вопросом/продажей. Отправляется одно завершение,
    # после чего автоматический диалог закрывается.
    if _contact_stop_intent(text):
        closed_context = dict(context or {})
        closed_context.update(
            {
                "contact_dialog_closed": True,
                "contact_dialog_closed_reason": "explicit_stop",
                "contact_dialog_closed_at": datetime.now(UTC).isoformat(),
            }
        )
        return _contact_stop_reply(first_name, greet), "contact_closed", True, closed_context

    # После явного стопа Неона молчит. Учитываем и уже состоявшиеся диалоги,
    # где старая версия успела написать «больше не буду вас беспокоить», но ещё
    # не сохраняла специальный stage=contact_closed. Это закрывает текущий кейс
    # без необходимости вручную править запись в Supabase.
    previous_reply_text = _normalize_intent_text(
        str((context or {}).get("last_reply_text") or "")
    )
    legacy_closed_reply = any(
        phrase in previous_reply_text
        for phrase in (
            "больше не буду вас беспокоить",
            "больше не буду вас отвлекать",
            "не буду больше вас беспокоить",
        )
    )
    if (
        stage == "contact_closed"
        or bool(context.get("contact_dialog_closed"))
        or legacy_closed_reply
    ):
        if not _contact_reopen_intent(text):
            closed_context = dict(context or {})
            closed_context["contact_dialog_closed"] = True
            closed_context.setdefault("contact_dialog_closed_reason", "explicit_stop")
            closed_context.setdefault("contact_dialog_closed_at", datetime.now(UTC).isoformat())
            return "", "contact_closed", greeted, closed_context
        context = dict(context or {})
        context.pop("contact_dialog_closed", None)
        context.pop("contact_dialog_closed_reason", None)
        context["contact_dialog_reopened_at"] = datetime.now(UTC).isoformat()
        stage = "idle"

    # Если Неона в прошлом сообщении предложила прислать материал, одно короткое
    # «да/пришлите» достаточно. В остальных стадиях одиночное «да» не трактуем
    # как согласие на ссылку, чтобы не спутать его с подтверждением встречи.
    if stage == "awaiting_material_consent":
        if _explicit_material_consent(text):
            context = dict(context or {})
            context["material_sent"] = True
            context["material_url"] = TELEGRAM_AGENCY_MATERIAL_URL
            return _telegram_material_reply(first_name, greet), "material_sent", True, context
        if _is_no(text):
            return (
                (_greeting(first_name) + " " if greet else "")
                + "Хорошо, без материалов. Что сейчас было бы для вас полезнее всего узнать?",
                "idle",
                True,
                {},
            )

    # Явная просьба «пришлите ссылку/материал» работает и без промежуточной стадии.
    if _direct_material_request(text):
        context = dict(context or {})
        context["material_sent"] = True
        context["material_url"] = TELEGRAM_AGENCY_MATERIAL_URL
        return _telegram_material_reply(first_name, greet), "material_sent", True, context

    scheduling_stage = stage in {"invited_to_meeting", "collecting_meeting_details", "awaiting_confirmation", "awaiting_slot_choice"}
    if _meeting_intent(text) or scheduling_stage:
        reply, new_stage, context = _schedule_reply(
            config,
            owner_id,
            owner_name,
            contact_id,
            first_name,
            username,
            text,
            message_dt,
            stage,
            context,
            greet,
        )
    else:
        reply = _openai_general_reply(
            config,
            owner_name,
            first_name,
            text,
            greet,
            context,
        )
        if _meeting_intent(reply):
            new_stage = "invited_to_meeting"
        elif _material_offer_intent(reply):
            new_stage = "awaiting_material_consent"
            context = dict(context or {})
            context["material_offer_channel"] = "telegram"
        else:
            new_stage = stage

    return reply, new_stage, True, context


async def _verified_sent_message(client: TelegramClient, entity, contact_id: int, message_id: int):
    """Проверяет, что исходящее сообщение реально существует именно в нужном личном чате."""
    if not message_id:
        return None
    for attempt in range(3):
        try:
            message = await client.get_messages(entity, ids=int(message_id))
        except Exception:
            message = None

        if message is not None and bool(getattr(message, "out", False)):
            peer = getattr(message, "peer_id", None)
            peer_user_id = int(getattr(peer, "user_id", 0) or 0)
            if not peer_user_id or peer_user_id == int(contact_id):
                return message

        if attempt < 2:
            await asyncio.sleep(0.4)
    return None


async def sync_owner_once(
    owner_id: int,
    owner_name: str,
    *,
    initialize_new_dialogs: bool = True,
    force_full_scan: bool = False,
) -> dict[str, int]:
    _voice_diag_reset()
    """
    Проверяет новые личные входящие сообщения только от людей, которым из
    Агентства W уже было отправлено утверждённое первое сообщение.

    На первом обнаружении диалога создаёт безопасную точку отсчёта и не
    отвечает на старую переписку. Следующие новые сообщения обрабатывает.
    """
    config = load_config()
    allowed = _allowed_contacts(config, int(owner_id))
    stats = {
        "allowed": len(allowed),
        "initialized": 0,
        "processed": 0,
        "replied": 0,
        "partner_registered": 0,
        "partner_active": 0,
        "partner_handoffs": 0,
        "partner_suppressed": 0,
        "urgent_callbacks": 0,
        "errors": 0,
    }

    diag_this_run = int(owner_id) not in _NEONA_DIAG_PRINTED_OWNERS
    if diag_this_run:
        _NEONA_DIAG_PRINTED_OWNERS.add(int(owner_id))
        allowed_preview = [
            {
                "telegram_id": int(contact_id),
                "name": str(meta.get("recipient_name") or ""),
                "message_id": int(meta.get("message_id") or 0),
                "sent_at": str(meta.get("sent_at") or ""),
            }
            for contact_id, meta in allowed.items()
        ]
        print(
            f"[NeonaDiag] owner={int(owner_id)} "
            f"allowed_count={len(allowed)} allowed={allowed_preview}",
            flush=True,
        )

    if not allowed:
        return stats

    # Серверный стоп-сигнал для старой воронки Неоны. Загружаем один раз на
    # каждый цикл worker: регистрация/активация становится видна без догадок
    # по тексту переписки.
    try:
        partner_lifecycle = _load_partner_lifecycle_map(
            config,
            int(owner_id),
            set(allowed),
        )
    except Exception as exc:
        # Fail closed: если статус партнёрства проверить не удалось, Неона не
        # должна рисковать и продолжать продажный диалог вслепую.
        print(
            f"[NeonaPartnerGate] owner={int(owner_id)} lookup_error="
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        stats["errors"] += 1
        return stats

    matched_allowed_ids: set[int] = set()

    session = _get_telegram_session(config, int(owner_id))
    if not session:
        raise DialogError("Telegram-сессия владельца не найдена.")

    client = TelegramClient(StringSession(session), config.telegram_api_id, config.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise DialogError("Telegram-сессия владельца больше не авторизована.")
        me = await client.get_me()
        if int(me.id) != int(owner_id):
            raise DialogError("Подключена Telegram-сессия другого владельца.")

        async for dialog in client.iter_dialogs(limit=500):
            entity = dialog.entity
            contact_id = int(getattr(entity, "id", 0) or 0)
            if contact_id not in allowed or getattr(entity, "bot", False):
                continue
            matched_allowed_ids.add(contact_id)
            if not getattr(entity, "first_name", None) and not getattr(entity, "last_name", None):
                continue

            if not force_full_scan:
                dialog_dt = getattr(dialog, "date", None)
                if dialog_dt is not None:
                    try:
                        dialog_dt_utc = dialog_dt.astimezone(UTC)
                        if datetime.now(UTC) - dialog_dt_utc > timedelta(
                            hours=NEONA_RECENT_DIALOG_HOURS
                        ):
                            # Если человек ответит спустя неделю или месяц,
                            # Telegram обновит dialog.date, и диалог сразу снова
                            # попадёт в быструю проверку.
                            continue
                    except Exception:
                        pass

            state = _dialog_state(config, int(owner_id), contact_id)
            all_recent = []
            recent = []
            async for message in client.iter_messages(entity, limit=30):
                all_recent.append(message)
                if message.out:
                    continue

                kind = _telegram_message_kind(message)
                plain_text = str(getattr(message, "message", "") or "").strip()

                # Берём обычный текст, голос и аудио. Остальные пустые медиа
                # пока не включаем в диалог Неоны.
                if not plain_text and kind not in {"voice", "audio"}:
                    continue

                recent.append(message)
            all_recent.sort(key=lambda item: int(item.id))
            recent.sort(key=lambda item: int(item.id))
            latest_incoming_id = int(recent[-1].id) if recent else 0

            if diag_this_run:
                state_last_before = int((state or {}).get("last_incoming_message_id") or 0)
                print(
                    f"[NeonaDiag] owner={int(owner_id)} contact={contact_id} "
                    f"username=@{str(getattr(entity, 'username', '') or '')} "
                    f"name={str(getattr(entity, 'first_name', '') or '')!r} "
                    f"allowed_name={str(allowed[contact_id].get('recipient_name') or '')!r} "
                    f"first_message_id={int(allowed[contact_id].get('message_id') or 0)} "
                    f"state_exists={state is not None} "
                    f"state_last_before={state_last_before} "
                    f"latest_incoming_id={latest_incoming_id}",
                    flush=True,
                )

            # Восстановление отметки для уже обработанной просьбы позвонить.
            # Это нужно, например, если новая версия с календарной отметкой
            # установлена ПОСЛЕ того, как Неона уже ответила человеку.
            if state is not None and recent:
                latest_existing = recent[-1]
                latest_existing_text = str(
                    getattr(latest_existing, "message", "") or ""
                ).strip()
                state_context_for_callback = (
                    dict(state.get("context"))
                    if isinstance(state.get("context"), dict)
                    else {}
                )
                already_processed = int(
                    state.get("last_incoming_message_id") or 0
                ) >= int(getattr(latest_existing, "id", 0) or 0)
                if (
                    already_processed
                    and latest_existing_text
                    and _urgent_callback_intent(latest_existing_text)
                    and not state_context_for_callback.get("urgent_callback_id")
                ):
                    try:
                        callback_name = _first_name(
                            entity,
                            allowed[contact_id].get("recipient_name", ""),
                        )
                        callback_username = str(
                            getattr(entity, "username", "") or ""
                        )
                        _, callback_context = _ensure_urgent_callback_marker(
                            config,
                            owner_id=int(owner_id),
                            owner_name=owner_name,
                            contact_id=contact_id,
                            contact_name=callback_name,
                            username=callback_username,
                            message_dt=latest_existing.date.astimezone(UTC),
                            incoming_message_id=int(latest_existing.id),
                            incoming_text=latest_existing_text,
                            context=state_context_for_callback,
                        )
                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=int(
                                state.get("last_incoming_message_id") or 0
                            ),
                            stage=str(state.get("stage") or "idle"),
                            greeted=bool(state.get("greeted", False)),
                            context=callback_context,
                        )
                        state = {**state, "context": callback_context}
                        stats["urgent_callbacks"] += 1
                    except Exception as exc:
                        print(
                            "NEONA_URGENT_CALLBACK_BACKFILL_ERROR:",
                            f"owner={int(owner_id)} contact={contact_id} "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        stats["errors"] += 1

            if state is None:
                # Если точка отсчёта не была создана в момент отправки,
                # ориентируемся на время первого сообщения из sent_log:
                # старые входящие до него игнорируем, ответы после него обрабатываем.
                sent_at_raw = str(allowed[contact_id].get("sent_at") or "")
                sent_at_dt = None
                if sent_at_raw:
                    try:
                        sent_at_dt = datetime.fromisoformat(
                            sent_at_raw.replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except Exception:
                        sent_at_dt = None

                baseline_id = latest_incoming_id
                if sent_at_dt is not None:
                    old_incoming = [
                        message
                        for message in recent
                        if message.date.astimezone(UTC) <= sent_at_dt
                    ]
                    baseline_id = (
                        int(old_incoming[-1].id) if old_incoming else 0
                    )

                if initialize_new_dialogs:
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=baseline_id,
                        stage="idle",
                        greeted=False,
                        context={
                            "initialized_at": datetime.now(UTC).isoformat(),
                            "first_message_sent_at": sent_at_raw,
                            "activated_by": "sent_log_fallback",
                        },
                    )
                    stats["initialized"] += 1

                state = {
                    "last_incoming_message_id": baseline_id,
                    "stage": "idle",
                    "greeted": False,
                    "context": {
                        "first_message_sent_at": sent_at_raw,
                        "activated_by": "sent_log_fallback",
                    },
                }

            last_id = int(state.get("last_incoming_message_id") or 0)
            state_context = (
                state.get("context")
                if isinstance(state.get("context"), dict)
                else {}
            )

            # Если владелец сам написал человеку в Telegram, считаем, что он
            # перехватил разговор. Неона не должна отвечать на старые входящие,
            # которые были до этого ручного сообщения. Она ждёт только нового
            # сообщения человека после вмешательства владельца.
            previous_reply_id = int(state_context.get("last_reply_id") or 0)
            first_message_id = int(allowed[contact_id].get("message_id") or 0)
            owner_fence_id = int(state_context.get("owner_outgoing_fence_id") or 0)
            known_automatic_ids = {
                value for value in (previous_reply_id, first_message_id) if value
            }
            manual_outgoing = [
                message
                for message in all_recent
                if bool(getattr(message, "out", False))
                and int(message.id) > max(last_id, owner_fence_id)
                and int(message.id) not in known_automatic_ids
            ]
            if manual_outgoing:
                ordered_owner_messages = sorted(
                    manual_outgoing,
                    key=lambda message: int(message.id),
                )
                owner_message = ordered_owner_messages[-1]
                owner_fence_id = int(owner_message.id)
                handled_incoming_ids = [
                    int(message.id)
                    for message in recent
                    if int(message.id) <= owner_fence_id
                ]
                if handled_incoming_ids:
                    last_id = max(last_id, max(handled_incoming_ids))

                # Владелец может разбить выбор на несколько сообщений:
                # «завтра» -> «11:00» -> «WhatsApp». Применяем их по порядку.
                owner_stage = str(state.get("stage") or "idle")
                owner_text = ""
                for owner_part in ordered_owner_messages:
                    owner_part_text = str(
                        getattr(owner_part, "message", "") or ""
                    ).strip()
                    if not owner_part_text:
                        continue
                    owner_text = owner_part_text
                    owner_stage, state_context = _apply_owner_manual_scheduling_message(
                        config,
                        int(owner_id),
                        owner_part_text,
                        owner_part.date.astimezone(UTC),
                        owner_stage,
                        state_context,
                    )

                state_context = {
                    **state_context,
                    "owner_outgoing_fence_id": owner_fence_id,
                    "owner_outgoing_fence_at": datetime.now(UTC).isoformat(),
                    "owner_outgoing_scheduling_applied_id": owner_fence_id,
                    "owner_outgoing_text": owner_text[:500],
                }
                _save_dialog_state(
                    config,
                    int(owner_id),
                    contact_id,
                    last_incoming_id=last_id,
                    stage=owner_stage,
                    greeted=bool(state.get("greeted", False)),
                    context=state_context,
                )
                state = {
                    **state,
                    "last_incoming_message_id": last_id,
                    "stage": owner_stage,
                    "context": state_context,
                }

            # Backfill для диалогов, где владелец уже выбрал время ДО установки
            # этой версии. Старый код сохранил owner_outgoing_fence_id, но не
            # сохранил смысл ручного сообщения. Один раз восстанавливаем его.
            applied_owner_id = int(
                state_context.get("owner_outgoing_scheduling_applied_id") or 0
            )
            if owner_fence_id and applied_owner_id != owner_fence_id:
                fenced_owner_message = next(
                    (
                        message
                        for message in all_recent
                        if bool(getattr(message, "out", False))
                        and int(message.id) == owner_fence_id
                    ),
                    None,
                )
                if fenced_owner_message is not None:
                    fenced_text = str(
                        getattr(fenced_owner_message, "message", "") or ""
                    ).strip()
                    recovered_stage = str(state.get("stage") or "idle")
                    if fenced_text:
                        recovered_stage, state_context = _apply_owner_manual_scheduling_message(
                            config,
                            int(owner_id),
                            fenced_text,
                            fenced_owner_message.date.astimezone(UTC),
                            recovered_stage,
                            state_context,
                        )
                    state_context = {
                        **state_context,
                        "owner_outgoing_scheduling_applied_id": owner_fence_id,
                        "owner_outgoing_text": fenced_text[:500],
                        "owner_outgoing_backfilled_at": datetime.now(UTC).isoformat(),
                    }

                    # Если кандидат уже успел подтвердить выбранный владельцем
                    # слот, создаём пропущенную запись в календаре без повторного
                    # сообщения человеку. Это чинит уже состоявшийся сценарий.
                    already_seen_after_owner = [
                        message
                        for message in recent
                        if owner_fence_id < int(message.id) <= last_id
                    ]
                    if (
                        already_seen_after_owner
                        and bool(state_context.get("owner_manual_schedule_pending_confirmation"))
                        and not state_context.get("meeting_id")
                    ):
                        confirmation_message = already_seen_after_owner[-1]
                        confirmation_text = str(
                            getattr(confirmation_message, "message", "") or ""
                        ).strip()
                        if confirmation_text and _is_meeting_confirmation(confirmation_text):
                            try:
                                first_name_for_backfill = _first_name(
                                    entity,
                                    allowed[contact_id].get("recipient_name", ""),
                                )
                                username_for_backfill = str(
                                    getattr(entity, "username", "") or ""
                                )
                                _, recovered_stage, state_context = _schedule_reply(
                                    config,
                                    int(owner_id),
                                    owner_name,
                                    contact_id,
                                    first_name_for_backfill,
                                    username_for_backfill,
                                    confirmation_text,
                                    confirmation_message.date.astimezone(UTC),
                                    recovered_stage,
                                    state_context,
                                    False,
                                )
                                if recovered_stage == "scheduled":
                                    state_context["meeting_backfilled_after_owner_choice"] = True
                                    state_context["meeting_backfilled_at"] = datetime.now(UTC).isoformat()
                            except Exception as exc:
                                state_context["owner_schedule_backfill_error"] = (
                                    f"{type(exc).__name__}: {exc}"
                                )[:500]

                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=last_id,
                        stage=recovered_stage,
                        greeted=bool(state.get("greeted", False)),
                        context=state_context,
                    )
                    state = {
                        **state,
                        "stage": recovered_stage,
                        "context": state_context,
                    }

            new_messages = [
                message
                for message in recent
                if int(message.id) > max(last_id, owner_fence_id)
            ]

            # Явный ручной повтор от владельца имеет приоритет над baseline.
            # Это важно при гонке между Streamlit и фоновым worker: worker мог
            # успеть снова сохранить last_incoming_id, но marker остаётся в
            # context и гарантированно возвращает ровно одно сообщение в работу.
            manual_retry_id = int(
                state_context.get("manual_retry_message_id") or 0
            )
            if (
                not new_messages
                and manual_retry_id
                and manual_retry_id > owner_fence_id
            ):
                retry_message = next(
                    (
                        message
                        for message in recent
                        if not bool(getattr(message, "out", False))
                        and int(message.id) == manual_retry_id
                    ),
                    None,
                )
                if retry_message is not None:
                    new_messages = [retry_message]

            failed_id = int(
                state_context.get("last_processing_failed_id") or 0
            )
            failed_error = str(
                state_context.get("last_processing_error") or ""
            )
            recovered_once = int(
                state_context.get("fixed_code_retry_id") or 0
            )
            if (
                not new_messages
                and failed_id
                and failed_id == last_id
                and recovered_once != failed_id
                and failed_error.startswith(
                    "AttributeError: module 'neona_dialog_policy'"
                )
            ):
                attempts = (
                    dict(state_context.get("message_processing_attempts"))
                    if isinstance(
                        state_context.get("message_processing_attempts"),
                        dict,
                    )
                    else {}
                )
                attempts.pop(str(failed_id), None)
                state_context = {
                    **state_context,
                    "message_processing_attempts": attempts,
                    "fixed_code_retry_id": failed_id,
                    "fixed_code_retry_at": datetime.now(UTC).isoformat(),
                }
                recovery_message = next(
                    (
                        message
                        for message in recent
                        if not bool(getattr(message, "out", False))
                        and int(message.id) == failed_id
                    ),
                    None,
                )
                if recovery_message is not None:
                    new_messages = [recovery_message]

            # Общая страховка после технических сбоев:
            # если входящее уже помечено как обработанное из-за ошибки,
            # подтверждённого ответа Неоны нет и владелец сам не вмешивался,
            # один раз возвращаем это сообщение в работу автоматически.
            stale_failed_id = int(
                state_context.get("last_processing_failed_id") or 0
            )
            stale_recovery_done = int(
                state_context.get("auto_failed_recovery_id") or 0
            )
            last_reply_id_for_recovery = int(
                state_context.get("last_reply_id") or 0
            )
            last_reply_verified_for_recovery = bool(
                state_context.get("last_reply_verified")
            )
            if (
                not new_messages
                and stale_failed_id
                and stale_failed_id == last_id
                and stale_failed_id > owner_fence_id
                and stale_recovery_done != stale_failed_id
                and not last_reply_verified_for_recovery
                and (
                    not last_reply_id_for_recovery
                    or last_reply_id_for_recovery < stale_failed_id
                )
            ):
                stale_message = next(
                    (
                        message
                        for message in recent
                        if not bool(getattr(message, "out", False))
                        and int(message.id) == stale_failed_id
                    ),
                    None,
                )
                if stale_message is not None:
                    attempts = (
                        dict(state_context.get("message_processing_attempts"))
                        if isinstance(
                            state_context.get("message_processing_attempts"),
                            dict,
                        )
                        else {}
                    )
                    attempts.pop(str(stale_failed_id), None)
                    state_context = {
                        **state_context,
                        "message_processing_attempts": attempts,
                        "auto_failed_recovery_id": stale_failed_id,
                        "auto_failed_recovery_at": datetime.now(UTC).isoformat(),
                    }
                    new_messages = [stale_message]
                    print(
                        f"[NeonaRecovery] owner={int(owner_id)} "
                        f"contact={contact_id} message_id={stale_failed_id} "
                        "reason=stale_failed_without_verified_reply",
                        flush=True,
                    )

            # Самовосстановление Story-диалогов.
            # Бывает, что состояние уже записало последнее входящее как seen,
            # хотя после него в реальном Telegram-чате нет исходящего ответа.
            # Для диалогов, начатых ответом владельца на Story, это безопасно
            # проверяем по самой переписке и один раз возвращаем входящее в работу.
            story_reply_id = int(
                state_context.get("story_reply_message_id") or 0
            )
            story_recovery_done_id = int(
                state_context.get("story_silent_recovery_id") or 0
            )
            current_stage = str(state.get("stage") or "idle")
            if (
                not new_messages
                and str(state_context.get("activated_by") or "") == "telegram_story_reply"
                and last_id > 0
                and last_id > story_reply_id
                and last_id > owner_fence_id
                and story_recovery_done_id != last_id
                and current_stage not in {
                    "contact_closed",
                    "opted_out",
                    "partner_active",
                    "partner_registered",
                }
            ):
                latest_seen_incoming = next(
                    (
                        message
                        for message in recent
                        if not bool(getattr(message, "out", False))
                        and int(message.id) == last_id
                    ),
                    None,
                )
                real_outgoing_after_seen = any(
                    bool(getattr(message, "out", False))
                    and int(message.id) > last_id
                    for message in all_recent
                )
                if (
                    latest_seen_incoming is not None
                    and not real_outgoing_after_seen
                ):
                    attempts = (
                        dict(state_context.get("message_processing_attempts"))
                        if isinstance(
                            state_context.get("message_processing_attempts"),
                            dict,
                        )
                        else {}
                    )
                    attempts.pop(str(last_id), None)
                    state_context = {
                        **state_context,
                        "message_processing_attempts": attempts,
                        "story_silent_recovery_id": last_id,
                        "story_silent_recovery_at": datetime.now(UTC).isoformat(),
                    }
                    new_messages = [latest_seen_incoming]
                    print(
                        f"[NeonaRecovery] owner={int(owner_id)} "
                        f"contact={contact_id} message_id={last_id} "
                        "reason=story_seen_without_real_reply",
                        flush=True,
                    )

            # Точечное одноразовое восстановление Ильи после сбоя 08.10.
            # Массовый replay отключён; это конкретный диалог, подтверждённый
            # логами и владельцем как оставшийся без ответа.
            if (
                not new_messages
                and int(owner_id) == 1129658410
                and contact_id == 332006775
                and last_id == 43178
                and owner_fence_id < 43178
                and int(
                    state_context.get("ilya_recovery_20261009_id") or 0
                ) != 43178
            ):
                ilya_message = next(
                    (
                        message
                        for message in recent
                        if not bool(getattr(message, "out", False))
                        and int(message.id) == 43178
                    ),
                    None,
                )
                if ilya_message is not None:
                    attempts = (
                        dict(state_context.get("message_processing_attempts"))
                        if isinstance(
                            state_context.get("message_processing_attempts"),
                            dict,
                        )
                        else {}
                    )
                    attempts.pop("43178", None)
                    state_context = {
                        **state_context,
                        "message_processing_attempts": attempts,
                        "ilya_recovery_20261009_id": 43178,
                        "ilya_recovery_20261009_at": datetime.now(UTC).isoformat(),
                    }
                    new_messages = [ilya_message]
                    print(
                        "[NeonaRecovery] owner=1129658410 contact=332006775 "
                        "message_id=43178 reason=targeted_ilya_recovery",
                        flush=True,
                    )

            if diag_this_run:
                print(
                    f"[NeonaDiag] owner={int(owner_id)} contact={contact_id} "
                    f"last_id={last_id} owner_fence_id={owner_fence_id} "
                    f"new_message_ids={[int(message.id) for message in new_messages]}",
                    flush=True,
                )

            # ------------------------------------------------------------
            # ПАРТНЁРСКИЙ СТОП-КРАН.
            # Как только человек зарегистрирован в Агентстве W, старая воронка
            # «интерес -> встреча -> регистрация» для него навсегда закрывается.
            # Неона больше не запускает _process_message / sales prompt.
            # При первом новом сообщении после смены статуса она один раз
            # объясняет переход; дальше старый outreach-диалог молчит.
            # ------------------------------------------------------------
            lifecycle = partner_lifecycle.get(
                contact_id,
                {"state": "candidate"},
            )
            lifecycle_state = str(lifecycle.get("state") or "candidate")
            if lifecycle_state in {"registered", "active"}:
                if lifecycle_state == "active":
                    stats["partner_active"] += 1
                    partner_stage = "partner_active"
                else:
                    stats["partner_registered"] += 1
                    partner_stage = "partner_registered"

                partner_context = _partner_lifecycle_context(
                    state_context,
                    lifecycle,
                )

                # Даже без нового входящего сразу фиксируем новый статус в
                # agency_dialog_states — кандидатский сценарий уже закрыт.
                if str(state.get("stage") or "idle") != partner_stage:
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=last_id,
                        stage=partner_stage,
                        greeted=bool(state.get("greeted", False)),
                        context=partner_context,
                    )
                    state = {
                        **state,
                        "stage": partner_stage,
                        "context": partner_context,
                    }
                    state_context = partner_context

                if not new_messages:
                    continue

                stats["processed"] += len(new_messages)
                latest_partner_message = new_messages[-1]
                latest_partner_id = int(latest_partner_message.id)

                # Один раз объясняем смену режима. После этого старый диалог
                # кандидата больше автоматически не отвечает: сопровождение уже
                # идёт через партнёрский контур, Неолу и раздел «Команда».
                if not bool(partner_context.get("partner_handoff_sent")):
                    first_name = _first_name(
                        entity,
                        allowed[contact_id].get("recipient_name", ""),
                    )
                    handoff_reply = _partner_handoff_reply(
                        first_name,
                        lifecycle,
                    )
                    try:
                        sent = await client.send_message(
                            entity,
                            handoff_reply,
                            parse_mode=None,
                            link_preview=False,
                        )
                        verified_sent = await _verified_sent_message(
                            client,
                            entity,
                            contact_id,
                            int(sent.id),
                        )
                        if verified_sent is None:
                            raise DialogError(
                                "Telegram вернул ID переходного ответа, но "
                                "сообщение не найдено в нужном чате."
                            )

                        partner_context = {
                            **partner_context,
                            "partner_handoff_sent": True,
                            "partner_handoff_sent_at": datetime.now(UTC).isoformat(),
                            "last_reply_id": int(sent.id),
                            "last_reply_text": handoff_reply,
                            "last_reply_verified": True,
                        }
                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=latest_partner_id,
                            stage=partner_stage,
                            greeted=True,
                            context=partner_context,
                        )
                        stats["replied"] += 1
                        stats["partner_handoffs"] += 1
                    except Exception as exc:
                        if _flood_wait_seconds(exc):
                            raise
                        print(
                            f"[NeonaPartnerGate] owner={int(owner_id)} "
                            f"contact={contact_id} handoff_error="
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        stats["errors"] += 1
                    continue

                partner_context = {
                    **partner_context,
                    "partner_last_suppressed_incoming_id": latest_partner_id,
                    "partner_last_suppressed_at": datetime.now(UTC).isoformat(),
                }
                _save_dialog_state(
                    config,
                    int(owner_id),
                    contact_id,
                    last_incoming_id=latest_partner_id,
                    stage=partner_stage,
                    greeted=True,
                    context=partner_context,
                )
                stats["partner_suppressed"] += len(new_messages)
                continue

            # Проверяем сохранённый ответ. Если владелец удалил сообщение
            # Неоны, не восстанавливаем его автоматически: удаление считаем
            # осознанным вмешательством владельца.

            last_incoming_message = next(
                (message for message in recent if int(message.id) == last_id),
                None,
            )
            last_incoming_is_audio = bool(
                last_incoming_message is not None
                and _telegram_message_kind(last_incoming_message) in {"voice", "audio"}
            )
            if not new_messages and previous_reply_id and last_incoming_message is not None:
                previous_reply = await _verified_sent_message(
                    client,
                    entity,
                    contact_id,
                    previous_reply_id,
                )
                if previous_reply is None:
                    if last_incoming_is_audio:
                        _voice_diag_add(
                            "saved_reply_missing",
                            latest_message_id=last_id,
                            missing_reply_id=previous_reply_id,
                        )

                    repaired_context = {
                        **state_context,
                        "last_reply_id": 0,
                        "last_reply_verified": False,
                        "last_reply_missing_at": datetime.now(UTC).isoformat(),
                        "auto_resend_suppressed": True,
                    }
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=last_id,
                        stage=str(state.get("stage") or "idle"),
                        greeted=bool(state.get("greeted", False)),
                        context=repaired_context,
                    )
                    state_context = repaired_context
                    state = {**state, "context": repaired_context}
                    # Никогда не отправляем удалённый ответ повторно без нового
                    # входящего сообщения от человека.
                    continue

            # Старая тестовая версия могла ошибочно поставить baseline прямо
            # на первом ответе. Один раз подхватываем такой ответ, если Неона
            # ещё ни разу не отвечала в этом диалоге.
            if (
                not new_messages
                and not bool(state.get("greeted", False))
                and not bool(state_context.get("auto_resend_suppressed"))
                and not int(state_context.get("owner_outgoing_fence_id") or 0)
            ):
                state_context = (
                    state.get("context")
                    if isinstance(state.get("context"), dict)
                    else {}
                )
                if not state_context.get("last_reply_id"):
                    sent_at_raw = str(
                        allowed[contact_id].get("sent_at") or ""
                    )
                    try:
                        sent_at_dt = datetime.fromisoformat(
                            sent_at_raw.replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except Exception:
                        sent_at_dt = None
                    if sent_at_dt is not None:
                        after_first = [
                            message
                            for message in recent
                            if message.date.astimezone(UTC) > sent_at_dt
                        ]
                        if after_first:
                            latest_after_first = after_first[-1]
                            if int(latest_after_first.id) == last_id:
                                new_messages = [latest_after_first]
            if new_messages:
                stats["processed"] += len(new_messages)
                latest = new_messages[-1]
                incoming_parts: list[str] = []
                failed_voice_ids: list[int] = []
                working_context = dict(state_context)

                # Голосовое сообщение распознаём максимум ОДИН раз на message_id.
                # Результат сразу сохраняем в context до генерации ответа Неоны.
                for incoming_message in new_messages:
                    incoming_id = int(getattr(incoming_message, "id", 0) or 0)
                    kind = _telegram_message_kind(incoming_message)

                    if kind in {"voice", "audio"}:
                        memory_key = (
                            int(owner_id),
                            int(contact_id),
                            int(incoming_id),
                        )
                        cached = _voice_cache_get(
                            working_context,
                            incoming_id,
                        )
                        if cached is None:
                            memory_cached = _VOICE_TRANSCRIPTION_MEMORY_CACHE.get(
                                memory_key
                            )
                            cached = (
                                dict(memory_cached)
                                if isinstance(memory_cached, dict)
                                else None
                            )

                        if cached is not None:
                            if (
                                str(cached.get("status") or "") == "ok"
                                and str(cached.get("text") or "").strip()
                            ):
                                incoming_parts.append(
                                    str(cached.get("text") or "").strip()
                                )
                            else:
                                failed_voice_ids.append(incoming_id)
                            continue

                        try:
                            incoming_text = await _incoming_message_text(
                                config,
                                incoming_message,
                            )
                            if incoming_text:
                                cache_entry = {
                                    "status": "ok",
                                    "text": incoming_text,
                                    "error": "",
                                }
                                _VOICE_TRANSCRIPTION_MEMORY_CACHE[
                                    memory_key
                                ] = cache_entry
                                working_context = _voice_cache_put(
                                    working_context,
                                    incoming_id,
                                    status="ok",
                                    text=incoming_text,
                                )
                                incoming_parts.append(incoming_text)
                            else:
                                cache_entry = {
                                    "status": "failed",
                                    "text": "",
                                    "error": "empty_transcript",
                                }
                                _VOICE_TRANSCRIPTION_MEMORY_CACHE[
                                    memory_key
                                ] = cache_entry
                                working_context = _voice_cache_put(
                                    working_context,
                                    incoming_id,
                                    status="failed",
                                    error="empty_transcript",
                                )
                                failed_voice_ids.append(incoming_id)
                        except Exception as exc:
                            error_text = f"{type(exc).__name__}: {exc}"
                            _voice_diag_add(
                                "transcription_failed_once",
                                message_id=incoming_id,
                                error=error_text,
                            )
                            cache_entry = {
                                "status": "failed",
                                "text": "",
                                "error": error_text[:500],
                            }
                            _VOICE_TRANSCRIPTION_MEMORY_CACHE[
                                memory_key
                            ] = cache_entry
                            working_context = _voice_cache_put(
                                working_context,
                                incoming_id,
                                status="failed",
                                error=error_text,
                            )
                            failed_voice_ids.append(incoming_id)

                        # Сохраняем кэш НЕМЕДЛЕННО. Если генерация ответа или
                        # Telegram-отправка ниже упадут, повторной транскрибации
                        # на следующем 15-секундном цикле уже не будет.
                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=last_id,
                            stage=str(state.get("stage") or "idle"),
                            greeted=bool(state.get("greeted", False)),
                            context=working_context,
                        )
                        state_context = working_context
                        state = {**state, "context": state_context}
                        continue

                    plain_text = str(
                        getattr(incoming_message, "message", "") or ""
                    ).strip()
                    if plain_text:
                        incoming_parts.append(plain_text)

                combined_text = "\n".join(incoming_parts).strip()

                # Если единственное новое содержимое — нераспознанный voice,
                # не гоняем его по кругу. Один раз просим прислать заново/текстом
                # и помечаем входящее обработанным.
                if not combined_text:
                    if failed_voice_ids:
                        notice = (
                            "Не смогла разобрать голосовое сообщение. "
                            "Пожалуйста, отправьте его ещё раз или напишите "
                            "коротко текстом."
                        )
                        notice_id = 0
                        try:
                            sent_notice = await client.send_message(
                                entity,
                                notice,
                                parse_mode=None,
                                link_preview=False,
                            )
                            notice_id = int(getattr(sent_notice, "id", 0) or 0)
                        except Exception as exc:
                            if _flood_wait_seconds(exc):
                                raise
                            _voice_diag_add(
                                "voice_failure_notice_send_error",
                                latest_message_id=int(latest.id),
                                error=f"{type(exc).__name__}: {exc}",
                            )

                        final_context = {
                            **working_context,
                            "last_voice_failure_ids": failed_voice_ids[-10:],
                            "last_voice_failure_at": datetime.now(UTC).isoformat(),
                        }
                        if notice_id:
                            final_context["last_reply_id"] = notice_id
                            final_context["last_reply_text"] = notice

                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=int(latest.id),
                            stage=str(state.get("stage") or "idle"),
                            greeted=bool(state.get("greeted", False)),
                            context=final_context,
                        )
                        stats["errors"] += 1
                        continue

                    _voice_diag_add(
                        "incoming_text_empty",
                        latest_message_id=int(latest.id),
                    )
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=int(latest.id),
                        stage=str(state.get("stage") or "idle"),
                        greeted=bool(state.get("greeted", False)),
                        context=working_context,
                    )
                    stats["errors"] += 1
                    continue

                # Любую дальнейшую обработку одного и того же batch ограничиваем.
                # Это защищает не только аудио, но и gpt-5-mini от бесконечного
                # повтора, если после ответа API ломается отправка в Telegram.
                attempts_map = (
                    working_context.get("message_processing_attempts")
                    if isinstance(
                        working_context.get("message_processing_attempts"),
                        dict,
                    )
                    else {}
                )
                previous_attempts = int(
                    attempts_map.get(str(int(latest.id)), 0) or 0
                )
                if previous_attempts >= MAX_MESSAGE_PROCESSING_ATTEMPTS:
                    suppressed_context = {
                        **working_context,
                        "last_processing_suppressed_id": int(latest.id),
                        "last_processing_suppressed_at": datetime.now(UTC).isoformat(),
                    }
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=int(latest.id),
                        stage=str(state.get("stage") or "idle"),
                        greeted=bool(state.get("greeted", False)),
                        context=suppressed_context,
                    )
                    _voice_diag_add(
                        "processing_suppressed",
                        latest_message_id=int(latest.id),
                        attempts=previous_attempts,
                    )
                    stats["errors"] += 1
                    continue

                current_attempt = previous_attempts + 1
                working_context = _processing_attempts_put(
                    working_context,
                    int(latest.id),
                    current_attempt,
                )
                _save_dialog_state(
                    config,
                    int(owner_id),
                    contact_id,
                    last_incoming_id=last_id,
                    stage=str(state.get("stage") or "idle"),
                    greeted=bool(state.get("greeted", False)),
                    context=working_context,
                )
                state_context = working_context
                state = {**state, "context": state_context}

                try:
                    first_name = _first_name(
                        entity,
                        allowed[contact_id].get("recipient_name", ""),
                    )
                    username = str(getattr(entity, "username", "") or "")
                    reply, stage, greeted, context = _process_message(
                        config,
                        int(owner_id),
                        owner_name,
                        contact_id,
                        first_name,
                        username,
                        combined_text,
                        latest.date.astimezone(UTC),
                        state,
                    )
                    # Закрытый по просьбе человека диалог не должен отвечать
                    # автоматически на последующие сообщения. Мы всё равно
                    # отмечаем входящее обработанным, чтобы worker не возвращался
                    # к нему на каждом цикле.
                    if not reply:
                        context = _processing_attempts_remove(
                            context,
                            int(latest.id),
                        )
                        context.pop("manual_retry_message_id", None)
                        context.pop("manual_retry_requested_at", None)
                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=int(latest.id),
                            stage=stage,
                            greeted=greeted,
                            context=context,
                        )
                        stats["skipped"] = int(stats.get("skipped", 0)) + 1
                        continue

                    sent = await client.send_message(
                        entity,
                        reply,
                        parse_mode=None,
                        link_preview=False,
                    )
                    verified_sent = await _verified_sent_message(
                        client,
                        entity,
                        contact_id,
                        int(sent.id),
                    )
                    if verified_sent is None:
                        raise DialogError(
                            "Telegram вернул ID ответа, но сообщение не найдено "
                            "в нужном чате после отправки."
                        )

                    # Прямая просьба «пусть Валентина позвонит / позвоните мне»
                    # должна сразу стать видимой в календаре. Делаем это ПОСЛЕ
                    # подтверждённой отправки ответа Неоны и идемпотентно, чтобы
                    # повтор worker не создавал несколько одинаковых карточек.
                    if _urgent_callback_intent(combined_text):
                        try:
                            _, context = _ensure_urgent_callback_marker(
                                config,
                                owner_id=int(owner_id),
                                owner_name=owner_name,
                                contact_id=contact_id,
                                contact_name=first_name,
                                username=username,
                                message_dt=latest.date.astimezone(UTC),
                                incoming_message_id=int(latest.id),
                                incoming_text=combined_text,
                                context=context,
                            )
                            stats["urgent_callbacks"] += 1
                        except Exception as exc:
                            # Не ломаем живой Telegram-диалог из-за временной
                            # ошибки календаря: ответ человеку уже отправлен.
                            context = dict(context or {})
                            context["urgent_callback_calendar_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )[:500]
                            context["urgent_callback_calendar_error_at"] = (
                                datetime.now(UTC).isoformat()
                            )
                            print(
                                "NEONA_URGENT_CALLBACK_ERROR:",
                                f"owner={int(owner_id)} contact={contact_id} "
                                f"{type(exc).__name__}: {exc}",
                                flush=True,
                            )
                            stats["errors"] += 1

                    if any(
                        _telegram_message_kind(item) in {"voice", "audio"}
                        for item in new_messages
                    ):
                        _voice_diag_add(
                            "reply_sent_after_voice",
                            latest_message_id=int(latest.id),
                            reply_id=int(sent.id),
                            verified=True,
                        )

                    context = _processing_attempts_remove(
                        context,
                        int(latest.id),
                    )
                    context.pop("manual_retry_message_id", None)
                    context.pop("manual_retry_requested_at", None)
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=int(latest.id),
                        stage=stage,
                        greeted=greeted,
                        context={
                            **context,
                            "last_reply_id": int(sent.id),
                            "last_reply_text": reply,
                            "last_reply_verified": True,
                        },
                    )
                    stats["replied"] += 1
                except Exception as exc:
                    if _flood_wait_seconds(exc):
                        raise
                    print(
                        "[NeonaDialogError] "
                        f"owner={int(owner_id)} contact={contact_id} "
                        f"message_id={int(latest.id)} attempt={current_attempt} "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    _voice_diag_add(
                        "dialog_or_send_error",
                        latest_message_id=int(latest.id),
                        attempt=current_attempt,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    stats["errors"] += 1

                    if current_attempt >= MAX_MESSAGE_PROCESSING_ATTEMPTS:
                        # После 3 неудач прекращаем платный цикл. Человека не
                        # спамим автоматическими повторами; новый входящий текст
                        # снова запустит обычную обработку.
                        stopped_context = {
                            **working_context,
                            "last_processing_failed_id": int(latest.id),
                            "last_processing_failed_attempts": current_attempt,
                            "last_processing_failed_at": datetime.now(UTC).isoformat(),
                            "last_processing_error": (
                                f"{type(exc).__name__}: {exc}"
                            )[:500],
                        }
                        stopped_context.pop("manual_retry_message_id", None)
                        stopped_context.pop("manual_retry_requested_at", None)
                        _save_dialog_state(
                            config,
                            int(owner_id),
                            contact_id,
                            last_incoming_id=int(latest.id),
                            stage=str(state.get("stage") or "idle"),
                            greeted=bool(state.get("greeted", False)),
                            context=stopped_context,
                        )

        if diag_this_run:
            missing_allowed_ids = sorted(set(allowed) - matched_allowed_ids)
            print(
                f"[NeonaDiag] owner={int(owner_id)} "
                f"matched_allowed={len(matched_allowed_ids)}/{len(allowed)} "
                f"missing_allowed_ids={missing_allowed_ids} "
                f"stats={stats}",
                flush=True,
            )

        return stats
    finally:
        await client.disconnect()



def _message_mime_type(message) -> str:
    document = getattr(message, "document", None)
    return str(getattr(document, "mime_type", "") or "").lower()


def _telegram_message_kind(message) -> str:
    """Надёжно различает обычный текст и Telegram voice/audio."""

    mime_type = _message_mime_type(message)

    if bool(getattr(message, "voice", False)):
        return "voice"
    if bool(getattr(message, "audio", False)):
        return "audio"

    document = getattr(message, "document", None)
    if document is not None and mime_type.startswith("audio/"):
        return "audio"

    return "text"


def _audio_suffix(message) -> str:
    mime_type = _message_mime_type(message)

    if "ogg" in mime_type or "opus" in mime_type:
        return ".ogg"
    if "mpeg" in mime_type or "mp3" in mime_type:
        return ".mp3"
    if "mp4" in mime_type or "m4a" in mime_type:
        return ".m4a"
    if "wav" in mime_type:
        return ".wav"
    if "aac" in mime_type:
        return ".aac"
    if "flac" in mime_type:
        return ".flac"

    # Telegram voice обычно OGG/Opus.
    return ".ogg"


async def _download_audio_to_temp(message) -> Path | None:
    message_id = int(getattr(message, "id", 0) or 0)
    mime_type = _message_mime_type(message)
    suffix = _audio_suffix(message)
    _voice_diag_add(
        "voice_detected",
        message_id=message_id,
        mime_type=mime_type or "unknown",
        suffix=suffix,
        is_voice=bool(getattr(message, "voice", False)),
        is_audio=bool(getattr(message, "audio", False)),
    )
    try:
        audio_bytes = await message.download_media(file=bytes)
    except Exception as exc:
        _voice_diag_add(
            "download_error",
            message_id=message_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    if not audio_bytes:
        _voice_diag_add("download_empty", message_id=message_id)
        return None
    _voice_diag_add("download_ok", message_id=message_id, bytes=len(audio_bytes))
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(audio_bytes)
        temp_path = Path(temporary.name)
    _voice_diag_add(
        "tempfile_ok",
        message_id=message_id,
        suffix=temp_path.suffix,
        bytes=temp_path.stat().st_size if temp_path.exists() else 0,
    )
    return temp_path

def _transcribe_audio_with_model(
    config: Config,
    path: Path,
    model: str,
) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "audio/ogg"
    _voice_diag_add(
        "transcription_request",
        model=model,
        mime_type=mime_type,
        bytes=path.stat().st_size if path.exists() else 0,
    )
    with path.open("rb") as audio_file:
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            data={"model": model, "language": "ru", "response_format": "json"},
            files={"file": (path.name, audio_file, mime_type)},
            timeout=120,
        )
    if not response.ok:
        _voice_diag_add(
            "transcription_http_error",
            model=model,
            status_code=int(response.status_code),
            response_text=str(response.text or "")[:500],
        )
        response.raise_for_status()
    payload=response.json()
    transcript=str(payload.get("text") or "").strip()
    _voice_diag_add("transcription_ok", model=model, characters=len(transcript))
    return transcript

def _transcribe_audio(config: Config, path: Path) -> str:
    """Одна платная транскрибация без дорогого автоматического fallback."""

    # ВАЖНО: не переключаемся автоматически на whisper-1.
    # Один Telegram message_id должен иметь максимум одну попытку
    # распознавания, а результат/ошибка затем кэшируются в dialog context.
    return _transcribe_audio_with_model(
        config,
        path,
        "gpt-4o-mini-transcribe",
    )


async def _incoming_message_text(config: Config, message) -> str:
    """Возвращает текст обычного сообщения или транскрипцию voice/audio."""

    plain_text = str(getattr(message, "message", "") or "").strip()
    kind = _telegram_message_kind(message)

    if kind == "text":
        return plain_text

    if kind in {"voice", "audio"}:
        temp_path = await _download_audio_to_temp(message)
        if temp_path is None:
            return ""

        try:
            return _transcribe_audio(config, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    return plain_text


def run_sync_owner_once(owner_id: int, owner_name: str, *, initialize_new_dialogs: bool = True) -> dict[str, int]:
    return asyncio.run(sync_owner_once(owner_id, owner_name, initialize_new_dialogs=initialize_new_dialogs))


def _owners(config: Config) -> list[tuple[int, str]]:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/telegram_sessions",
        headers=_headers(config),
        params={"select": "telegram_id"},
        timeout=20,
    )
    response.raise_for_status()
    ids = []
    for row in response.json():
        try:
            ids.append(int(row.get("telegram_id")))
        except (TypeError, ValueError):
            pass
    if not ids:
        return []

    members = requests.get(
        f"{config.supabase_url}/rest/v1/agency_members",
        headers=_headers(config),
        params={"telegram_id": f"in.({','.join(str(item) for item in ids)})", "select": "telegram_id,first_name"},
        timeout=20,
    )
    members.raise_for_status()
    names = {}
    for row in members.json():
        try:
            names[int(row.get("telegram_id"))] = str(row.get("first_name") or "Владелец")
        except (TypeError, ValueError):
            pass
    return [(owner_id, names.get(owner_id, "Владелец")) for owner_id in ids]


def worker_forever(poll_seconds: int = NEONA_DEFAULT_POLL_SECONDS) -> None:
    config = load_config()
    safe_poll_seconds = max(NEONA_MIN_POLL_SECONDS, int(poll_seconds))
    flood_until_by_owner: dict[int, float] = {}
    print(
        f"Neona Telegram worker started; poll={safe_poll_seconds}s; "
        f"recent_window={NEONA_RECENT_DIALOG_HOURS}h",
        flush=True,
    )
    while True:
        try:
            for owner_id, owner_name in _owners(config):
                owner_id = int(owner_id)
                now_mono = time.monotonic()
                blocked_until = float(flood_until_by_owner.get(owner_id, 0) or 0)
                if blocked_until > now_mono:
                    continue
                try:
                    stats = asyncio.run(
                        sync_owner_once(
                            owner_id,
                            owner_name,
                            initialize_new_dialogs=True,
                            force_full_scan=False,
                        )
                    )
                    if stats["processed"] or stats["initialized"] or stats["errors"]:
                        print(f"owner={owner_id} stats={stats}", flush=True)
                except Exception as exc:
                    wait_seconds = _flood_wait_seconds(exc)
                    if wait_seconds:
                        # Не продолжаем стучаться в аккаунт во время FloodWait.
                        # Добавляем небольшой запас, чтобы не попасть в новый цикл
                        # в ту же секунду, когда ограничение формально закончится.
                        flood_until_by_owner[owner_id] = (
                            time.monotonic() + wait_seconds + 30
                        )
                        print(
                            f"[NeonaFloodWait] owner={owner_id} "
                            f"pause={wait_seconds + 30}s",
                            flush=True,
                        )
                        continue
                    print(f"owner={owner_id} error={exc}", flush=True)
        except Exception as exc:
            print(f"worker error={exc}", flush=True)
        time.sleep(safe_poll_seconds)


if __name__ == "__main__":
    # Безопасный резервный запуск. Частоту нельзя возвращать к 15–30 секундам:
    # это создаёт лишние Telegram/Supabase запросы и риск FloodWait.
    configured_poll = int(
        os.getenv("NEONA_POLL_SECONDS", str(NEONA_DEFAULT_POLL_SECONDS))
    )
    worker_forever(poll_seconds=configured_poll)
