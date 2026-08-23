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

try:
    import streamlit as st
except Exception:  # pragma: no cover - standalone worker mode
    st = None

UTC = timezone.utc

_LAST_VOICE_DIAGNOSTICS: list[dict] = []

def _voice_diag_reset() -> None:
    _LAST_VOICE_DIAGNOSTICS.clear()

def _voice_diag_add(stage: str, **data) -> None:
    item = {"stage": stage, "at": datetime.now(UTC).isoformat()}
    item.update(data)
    _LAST_VOICE_DIAGNOSTICS.append(item)

def get_last_voice_diagnostics() -> list[dict]:
    return list(_LAST_VOICE_DIAGNOSTICS)

MSK = ZoneInfo("Europe/Moscow")
DURATION_MINUTES = 30

MEETING_FORMATS = ("Zoom", "Telegram", "WhatsApp")

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


def _allowed_contacts(config: Config, owner_id: int) -> dict[int, dict[str, Any]]:
    workspace = _load_workspace(config, owner_id)
    allowed: dict[int, dict[str, Any]] = {}
    for event in workspace.get("sent_log", []) if isinstance(workspace.get("sent_log"), list) else []:
        if not isinstance(event, dict) or event.get("kind") != "first_message":
            continue
        try:
            contact_id = int(event.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        allowed[contact_id] = {
            "sent_at": str(event.get("sent_at") or ""),
            "recipient_name": str(event.get("recipient_name") or ""),
        }
    return allowed



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


def _first_name(entity: Any, fallback: str = "") -> str:
    name = str(getattr(entity, "first_name", "") or "").strip()
    if not name and fallback:
        name = str(fallback).strip().split()[0]
    return name[:80]


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


def _detect_time(text: str) -> str | None:
    # Двоеточие — однозначный формат времени. Точку считаем временем только
    # после предлога «в», чтобы дата 07.08 не превращалась в 07:08.
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    match = re.search(r"(?:^|\s)в\s+([01]?\d|2[0-3])\.([0-5]\d)\b", text.lower())
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    match = re.search(r"(?:^|\s)(?:в\s+)?([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|ч)?(?:\s|$)", text.lower())
    if match:
        return f"{int(match.group(1)):02d}:00"
    return None


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
    lowered = text.lower()
    if any(token in lowered for token in ("встреч", "созвон", "zoom", "зум", "поговорить", "поговорим", "связаться")):
        return True
    has_date_hint = any(token in lowered for token in ("сегодня", "завтра", "послезавтра")) or bool(
        re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
    ) or any(word in lowered for word in WEEKDAYS_RU)
    return bool(_detect_time(text) and has_date_hint)


def _is_yes(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    tokens = set(lowered.split())
    return any((" " in word and word in lowered) or (" " not in word and word in tokens) for word in YES_WORDS)


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


def _openai_general_reply(config: Config, owner_name: str, first_name: str, text: str, greet: bool) -> str:
    greeting_rule = (
        f"Начни с «{_greeting(first_name)}»" if greet else "Не повторяй приветствие, если диалог уже начат."
    )
    instructions = f"""
Ты Неона — виртуальная помощница {owner_name}. Пиши по-русски простым человеческим языком, без корпоративного жаргона.

{NEONA_DIALOG_CORE}

Главная задача — заинтересовать человека реальными возможностями Агентства W и постепенно привести к осознанной встрече с {owner_name}.
Факты, которые уже реально доступны: команда ИИ-помощников помогает владельцу искать подходящих людей в его Telegram-контактах и чатах, анализировать кандидатов и готовить персональное первое сообщение. Окончательный выбор и утверждение первого сообщения делает человек.
Никогда не называй ИИ-помощников ботами. Не выдумывай функций. Не говори «проверила», «записала», «отправила», «создала», если техническое действие не было реально выполнено.
Не используй фамилию собеседника в обращении. {greeting_rule}
Ответ — 1–4 коротких предложения.
Если человек прямо говорит, что ему интересно, не пересказывай заново первое сообщение. Дай один конкретный и правдивый повод увидеть систему вживую: {owner_name} может показать на реальном примере, как команда уже ищет подходящих людей и готовит персональные первые сообщения.
После этого мягко подведи к короткой встрече с {owner_name} и задай один простой вопрос о готовности встретиться.
Не назначай дату, время и формат самостоятельно — их нужно отдельно согласовать с человеком.
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
            "input": text,
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
    detected_time = _detect_time(text)
    if detected_time:
        context["requested_time"] = detected_time
    detected_date = _detect_date(text, message_dt, context.get("contact_timezone"))
    if detected_date:
        context["requested_date"] = detected_date
    return context


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

    if stage == "awaiting_confirmation":
        if _is_yes(text):
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
                        "notes": "Назначено Неоной после подтверждения человека в Telegram.",
                        "source": "Неона — Telegram диалог",
                    },
                )
                confirmed_start = datetime.fromisoformat(str(created["start_at"]).replace("Z", "+00:00")).astimezone(UTC)
                context["meeting_id"] = created.get("id")
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

    missing = []
    if not context.get("requested_date") or not context.get("requested_time"):
        missing.append("date_time")
    if not context.get("contact_timezone"):
        missing.append("timezone")
    if not context.get("meeting_format"):
        missing.append("format")

    if missing:
        questions: list[str] = []
        if "date_time" in missing:
            questions.append("на какой день и время вам удобна встреча")
        if "timezone" in missing:
            questions.append("по какому часовому поясу указано время")
        if "format" in missing:
            questions.append("как вам удобнее встретиться — Zoom, Telegram или WhatsApp")
        if len(questions) == 1:
            ask = questions[0]
        else:
            ask = "; и ".join(questions)
        return prefix + "Подскажите, пожалуйста, " + ask + ".", "collecting_meeting_details", context

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
        reply = _openai_general_reply(config, owner_name, first_name, text, greet)
        new_stage = "invited_to_meeting" if _meeting_intent(reply) else stage

    return reply, new_stage, True, context


async def sync_owner_once(owner_id: int, owner_name: str, *, initialize_new_dialogs: bool = True) -> dict[str, int]:
    _voice_diag_reset()
    """
    Проверяет новые личные входящие сообщения только от людей, которым из
    Агентства W уже было отправлено утверждённое первое сообщение.

    На первом обнаружении диалога создаёт безопасную точку отсчёта и не
    отвечает на старую переписку. Следующие новые сообщения обрабатывает.
    """
    config = load_config()
    allowed = _allowed_contacts(config, int(owner_id))
    stats = {"allowed": len(allowed), "initialized": 0, "processed": 0, "replied": 0, "errors": 0}
    if not allowed:
        return stats

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
            if not getattr(entity, "first_name", None) and not getattr(entity, "last_name", None):
                continue

            state = _dialog_state(config, int(owner_id), contact_id)
            recent = []
            async for message in client.iter_messages(entity, limit=30):
                if message.out:
                    continue

                kind = _telegram_message_kind(message)
                plain_text = str(getattr(message, "message", "") or "").strip()

                # Берём обычный текст, голос и аудио. Остальные пустые медиа
                # пока не включаем в диалог Неоны.
                if not plain_text and kind not in {"voice", "audio"}:
                    continue

                recent.append(message)
            recent.sort(key=lambda item: int(item.id))
            latest_incoming_id = int(recent[-1].id) if recent else 0

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
            new_messages = [
                message for message in recent if int(message.id) > last_id
            ]

            # Старая тестовая версия могла ошибочно поставить baseline прямо
            # на первом ответе. Один раз подхватываем такой ответ, если Неона
            # ещё ни разу не отвечала в этом диалоге.
            if not new_messages and not bool(state.get("greeted", False)):
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
                incoming_parts = []
                for incoming_message in new_messages:
                    incoming_text = await _incoming_message_text(
                        config,
                        incoming_message,
                    )
                    if incoming_text:
                        incoming_parts.append(incoming_text)

                combined_text = "\n".join(incoming_parts).strip()
                if not combined_text:
                    _voice_diag_add("incoming_text_empty", latest_message_id=int(latest.id))
                    # Если аудио не удалось распознать, не помечаем его обработанным:
                    # следующий запуск сможет попробовать снова.
                    stats["errors"] += 1
                    continue

                try:
                    first_name = _first_name(entity, allowed[contact_id].get("recipient_name", ""))
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
                    sent = await client.send_message(entity, reply, parse_mode=None, link_preview=False)
                    if any(_telegram_message_kind(item) in {"voice", "audio"} for item in new_messages):
                        _voice_diag_add(
                            "reply_sent_after_voice",
                            latest_message_id=int(latest.id),
                            reply_id=int(sent.id),
                        )
                    _save_dialog_state(
                        config,
                        int(owner_id),
                        contact_id,
                        last_incoming_id=int(latest.id),
                        stage=stage,
                        greeted=greeted,
                        context={**context, "last_reply_id": int(sent.id)},
                    )
                    stats["replied"] += 1
                except Exception as exc:
                    _voice_diag_add(
                        "dialog_or_send_error",
                        latest_message_id=int(latest.id),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    stats["errors"] += 1
                    # При ошибке не помечаем сообщения обработанными, чтобы
                    # следующая попытка могла повторить безопасно.
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
    """Современная транскрибация с запасным проверенным вариантом."""

    try:
        transcript = _transcribe_audio_with_model(
            config,
            path,
            "gpt-4o-mini-transcribe",
        )
        if transcript:
            return transcript
    except Exception as exc:
        _voice_diag_add(
            "primary_transcription_exception",
            model="gpt-4o-mini-transcribe",
            error=f"{type(exc).__name__}: {exc}",
        )
    _voice_diag_add("fallback_started", model="whisper-1")
    # Старый рабочий контур Агентства использовал whisper-1.
    return _transcribe_audio_with_model(
        config,
        path,
        "whisper-1",
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


def worker_forever(poll_seconds: int = 15) -> None:
    config = load_config()
    print("Neona Telegram worker started", flush=True)
    while True:
        try:
            for owner_id, owner_name in _owners(config):
                try:
                    stats = asyncio.run(sync_owner_once(owner_id, owner_name, initialize_new_dialogs=True))
                    if stats["processed"] or stats["initialized"] or stats["errors"]:
                        print(f"owner={owner_id} stats={stats}", flush=True)
                except Exception as exc:
                    print(f"owner={owner_id} error={exc}", flush=True)
        except Exception as exc:
            print(f"worker error={exc}", flush=True)
        time.sleep(max(5, int(poll_seconds)))


if __name__ == "__main__":
    # Постоянный рабочий цикл Неоны.
    # Интервал 15 секунд достаточно быстрый для живого диалога
    # и не вызывает OpenAI, если новых сообщений нет.
    worker_forever(poll_seconds=15)
