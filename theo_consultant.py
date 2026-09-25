import json
import os
import re
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent

THEO_MODEL = os.getenv("THEO_MODEL", "gpt-5.6").strip() or "gpt-5.6"
CONSTITUTION_FILE = BASE_DIR / "THEO_CONSTITUTION.txt"
KNOWLEDGE_FILE = BASE_DIR / "THEO_KNOWLEDGE_BASE.txt"

ALLOWED_STAGES = {"consulting", "interested", "meeting_ready", "closed"}


UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")

DURATION_MINUTES = 30
BUFFER_MINUTES = 60
MIN_LEAD_MINUTES = 60

MEETING_FORMATS = ("Zoom", "Telegram", "WhatsApp")
ACTIVE_MEETING_STATES = {
    "collecting_meeting_details",
    "awaiting_slot_choice",
    "awaiting_confirmation",
}

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
    "готов", "готова", "все верно", "всё верно", "отлично", "замечательно",
}

NO_WORDS = {
    "нет", "не подходит", "неудобно", "другое время", "перенести",
}


THEO_FALLBACK_RULES = """
Ты — Тео, консультант Агентства W в VK-сообществе.

Твоя роль:
— содержательно отвечать на вопросы об Агентстве W;
— сначала отвечать на вопрос, а не превращать каждый ответ в вопрос;
— объяснять простым языком;
— показывать применимость Агентства W к ситуации человека только по известным данным;
— не выдумывать функции, цифры, цены, результаты и факты о человеке;
— не обещать доход, клиентов, партнёров или гарантированный результат;
— при реальном интересе мягко предложить короткую встречу с Директором,
  который пригласил человека в сообщество;
— никогда не говорить, что встреча уже назначена, если календарная запись
  технически не создана;
— если точного ответа нет, честно сказать, что подтверждённого ответа нет,
  и предложить уточнить у Директора.

Главная идея Агентства W:
«Мы возвращаем человеку время».

Агентство W — система специализированных ИИ-помощников:
Неония — анализ проекта, целевой аудитории и подбор подходящих людей.
Неона — первичное общение и создание интереса.
Тео — консультация и понимание возможностей Агентства W.
Неола — сопровождение партнёра после регистрации и активации.
Стагирит — координация и порядок всей системы.
Человек остаётся Директором и принимает ключевые решения.

Стиль:
спокойный, умный, доброжелательный, без рекламного пафоса.
Обычно 1–4 коротких абзаца.
Не задавай вопрос только ради продолжения разговора.
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY")
    return url, key


def _headers(prefer: str = "") -> dict[str, str]:
    _, key = _supabase_config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _sb_get(params: dict) -> list[dict]:
    url, _ = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/agency_theo_vk_dialogs",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_post(payload: dict) -> list[dict]:
    url, _ = _supabase_config()
    response = requests.post(
        f"{url}/rest/v1/agency_theo_vk_dialogs?on_conflict=vk_user_id",
        headers=_headers(
            "resolution=merge-duplicates,return=representation"
        ),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []



def _table_get(table: str, params: dict) -> list[dict]:
    url, _ = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _table_post(table: str, payload: dict, prefer: str = "return=representation") -> list[dict]:
    url, _ = _supabase_config()
    response = requests.post(
        f"{url}/rest/v1/{table}",
        headers=_headers(prefer),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _list_meetings(owner_id: int, start_utc: datetime, end_utc: datetime) -> list[dict]:
    return _table_get(
        "agency_meetings",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "start_at": f"lt.{end_utc.astimezone(UTC).isoformat()}",
            "end_at": f"gt.{start_utc.astimezone(UTC).isoformat()}",
            "status": "not.in.(Отменена,Перенесена)",
            "select": "*",
            "order": "start_at.asc",
        },
    )


def _recurring_blocks(owner_id: int, weekday: int) -> list[dict]:
    try:
        return _table_get(
            "agency_calendar_blocks",
            {
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "active": "eq.true",
                "weekday": f"eq.{int(weekday)}",
                "select": "weekday,start_time,end_time,active,title",
                "order": "start_time.asc",
            },
        )
    except Exception as exc:
        # Безопаснее считать слот недоступным, чем создать двойную встречу.
        print(
            "THEO_CALENDAR_BLOCKS_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return [{"_calendar_error": True}]


def _slot_free_with_buffer(owner_id: int, start_utc: datetime, end_utc: datetime) -> bool:
    start_utc = start_utc.astimezone(UTC)
    end_utc = end_utc.astimezone(UTC)

    if start_utc < datetime.now(UTC) + timedelta(minutes=MIN_LEAD_MINUTES):
        return False

    start_msk = start_utc.astimezone(MSK)
    end_msk = end_utc.astimezone(MSK)
    day_start = datetime.combine(start_msk.date(), dt_time(10, 0), MSK)
    day_end = datetime.combine(start_msk.date(), dt_time(20, 0), MSK)

    if start_msk < day_start or end_msk > day_end:
        return False

    expanded_start = start_utc - timedelta(minutes=BUFFER_MINUTES)
    expanded_end = end_utc + timedelta(minutes=BUFFER_MINUTES)

    if _list_meetings(owner_id, expanded_start, expanded_end):
        return False

    blocks = _recurring_blocks(owner_id, start_msk.weekday())
    if blocks and blocks[0].get("_calendar_error"):
        return False

    for block in blocks:
        if not bool(block.get("active", True)):
            continue
        try:
            raw_start = str(block.get("start_time") or "").strip().split(":")
            raw_end = str(block.get("end_time") or "").strip().split(":")
            if len(raw_start) < 2 or len(raw_end) < 2:
                continue
            block_start_clock = dt_time(int(raw_start[0]), int(raw_start[1]))
            block_end_clock = dt_time(int(raw_end[0]), int(raw_end[1]))
        except (TypeError, ValueError):
            continue

        block_start_msk = datetime.combine(start_msk.date(), block_start_clock, MSK)
        block_end_msk = datetime.combine(start_msk.date(), block_end_clock, MSK)

        if (
            expanded_start < block_end_msk.astimezone(UTC)
            and expanded_end > block_start_msk.astimezone(UTC)
        ):
            return False

    return True


def _load_zoom_link(owner_id: int) -> tuple[str, str]:
    try:
        rows = _table_get(
            "agency_stagirite_tasks",
            {
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "task_kind": "eq.settings",
                "select": "result,updated_at",
                "order": "updated_at.desc",
                "limit": 1,
            },
        )
        if not rows:
            return "", ""
        result = rows[0].get("result")
        if not isinstance(result, dict):
            return "", ""
        return (
            str(result.get("zoom_link") or "").strip(),
            str(result.get("zoom_note") or "").strip(),
        )
    except Exception as exc:
        print(
            "THEO_ZOOM_LINK_WARNING:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return "", ""


def _create_meeting(payload: dict) -> dict:
    rows = _table_post("agency_meetings", payload)
    if not rows:
        raise RuntimeError("Supabase не подтвердил создание встречи.")
    return rows[0]


def _detect_format(text: str) -> str | None:
    lowered = str(text or "").casefold()
    if "zoom" in lowered or "зум" in lowered:
        return "Zoom"
    if "whatsapp" in lowered or "ватсап" in lowered or "вацап" in lowered:
        return "WhatsApp"
    if "telegram" in lowered or "телеграм" in lowered:
        return "Telegram"
    return None


def _detect_timezone(text: str) -> str | None:
    raw = str(text or "")
    lowered = raw.casefold().strip()

    iana = re.search(r"\b[A-Za-z_]+/[A-Za-z_+-]+\b", raw)
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
    raw = str(text or "")
    lowered = raw.casefold()

    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", raw)
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    match = re.search(
        r"(?:^|\s)в\s+([01]?\d|2[0-3])\s*[-–—.]\s*([0-5]\d)\b(?![./-]\d)",
        lowered,
    )
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    match = re.search(
        r"\b([01]?\d|2[0-3])\s*[-–—.]\s*([0-5]\d)\s*"
        r"(?:мск|по\s+москве|москов\w*(?:\s+врем\w*)?)\b",
        lowered,
    )
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    match = re.search(
        r"(?:^|\s)(?:в\s+)?([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|ч)?(?:\s|$)",
        lowered,
    )
    if match:
        return f"{int(match.group(1)):02d}:00"

    return None



def _normalize_calendar_text(text: str) -> str:
    """Нормализует пользовательский текст для календарных дат."""
    value = str(text or "")
    value = value.replace("\u00a0", " ").replace("\u202f", " ")
    value = value.replace("ё", "е").casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _detect_date(text: str, message_dt: datetime, tz_name: str | None) -> str | None:
    raw = str(text or "")
    lowered = _normalize_calendar_text(raw)

    try:
        tz = ZoneInfo(tz_name) if tz_name else MSK
    except Exception:
        tz = MSK

    base = message_dt.astimezone(tz).date()

    # Самые частые естественные ответы после вопроса Тео:
    # "сегодня", "сегодня в 15:00", "завтра вечером", "послезавтра".
    if re.search(r"(?:^|\W)послезавтра(?:\W|$)", lowered):
        return (base + timedelta(days=2)).isoformat()
    if re.search(r"(?:^|\W)завтра(?:\W|$)", lowered):
        return (base + timedelta(days=1)).isoformat()
    if re.search(r"(?:^|\W)сегодня(?:\W|$)", lowered):
        return base.isoformat()

    match = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b",
        raw,
    )
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
        normalized_word = word.replace("ё", "е").casefold()
        if re.search(
            rf"(?:^|\W){re.escape(normalized_word)}(?:\W|$)",
            lowered,
        ):
            delta = (weekday - base.weekday()) % 7
            if delta == 0:
                delta = 7
            return (base + timedelta(days=delta)).isoformat()

    return None


def _meeting_intent(text: str) -> bool:
    lowered = str(text or "").casefold()
    if any(
        token in lowered
        for token in ("встреч", "созвон", "zoom", "зум", "поговорить", "поговорим", "связаться")
    ):
        return True

    has_date_hint = (
        any(token in lowered for token in ("сегодня", "завтра", "послезавтра"))
        or bool(re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", str(text or "")))
        or any(word in lowered for word in WEEKDAYS_RU)
    )
    return bool(_detect_time(text) and has_date_hint)


def _is_yes(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", str(text or "").casefold()).strip()
    tokens = set(lowered.split())
    return any(
        (" " in word and word in lowered) or (" " not in word and word in tokens)
        for word in YES_WORDS
    )


def _is_no(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9 ]+", " ", str(text or "").casefold()).strip()
    tokens = set(lowered.split())
    return any(
        (" " in word and word in lowered) or (" " not in word and word in tokens)
        for word in NO_WORDS
    )


def _update_meeting_context(context: dict, text: str, message_dt: datetime) -> dict:
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


def _parse_start(context: dict) -> datetime | None:
    date_value = context.get("requested_date")
    time_value = context.get("requested_time")
    tz_name = context.get("contact_timezone")

    if not (date_value and time_value and tz_name):
        return None

    try:
        local_date = date.fromisoformat(str(date_value))
        hh, mm = [int(part) for part in str(time_value).split(":", 1)]
        local = datetime.combine(
            local_date,
            dt_time(hh, mm),
            ZoneInfo(str(tz_name)),
        )
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
        f"Для вас это {local:%d.%m.%Y} в {local:%H:%M} ({tz_name})"
    )


def _find_three_slots(owner_id: int, around_utc: datetime, contact_timezone: str) -> list[datetime]:
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

            if 8 <= local.hour < 22 and _slot_free_with_buffer(owner_id, start_utc, end_utc):
                result.append(start_utc)
                if len(result) == 3:
                    return result

            cursor += timedelta(minutes=30)

    return result


def _schedule_reply(
    *,
    owner_id: int,
    owner_name: str,
    vk_user_id: int,
    visitor_first_name: str,
    incoming_text: str,
    meeting_state: str,
    meeting_context: dict,
    first_contact: bool,
) -> tuple[str, str, dict, str | None]:

    context = _update_meeting_context(
        meeting_context,
        incoming_text,
        datetime.now(UTC),
    )
    state = str(meeting_state or "collecting_meeting_details").strip()

    print(
        "THEO_CALENDAR_PARSE:",
        {
            "state": state,
            "incoming": incoming_text,
            "requested_date": context.get("requested_date"),
            "requested_time": context.get("requested_time"),
            "contact_timezone": context.get("contact_timezone"),
            "meeting_format": context.get("meeting_format"),
        },
        flush=True,
    )
    person = str(visitor_first_name or "").strip()
    intro = (
        "Я Тео, консультант Агентства W. Помогу подобрать удобное время. "
        if first_contact
        else ""
    )

    if state == "awaiting_confirmation":
        if _is_yes(incoming_text):
            proposed = context.get("proposed_start_at")
            tz_name = str(context.get("contact_timezone") or "")
            meeting_format = str(context.get("meeting_format") or "")

            if not (proposed and tz_name and meeting_format):
                state = "collecting_meeting_details"
            else:
                start_utc = datetime.fromisoformat(
                    str(proposed).replace("Z", "+00:00")
                ).astimezone(UTC)
                end_utc = start_utc + timedelta(minutes=DURATION_MINUTES)

                if not _slot_free_with_buffer(owner_id, start_utc, end_utc):
                    context.pop("proposed_start_at", None)
                    return (
                        "Пока мы подтверждали, это время стало недоступно. "
                        "Напишите, пожалуйста, другой удобный день или время — я проверю календарь.",
                        "collecting_meeting_details",
                        context,
                        None,
                    )

                zoom_link = ""
                zoom_note = ""
                if meeting_format == "Zoom":
                    zoom_link, zoom_note = _load_zoom_link(owner_id)

                created = _create_meeting(
                    {
                        "owner_telegram_id": int(owner_id),
                        "owner_name": owner_name,
                        "contact_name": person or "Посетитель VK",
                        "contact_username": None,
                        "contact_city": context.get("contact_city") or tz_name,
                        "contact_timezone": tz_name,
                        "start_at": start_utc.isoformat(),
                        "end_at": end_utc.isoformat(),
                        "meeting_format": meeting_format,
                        "meeting_link": zoom_link or None,
                        "status": "Подтверждена",
                        "notes": (
                            "Назначено Тео после подтверждения человека "
                            f"в VK-сообществе. VK user id: {int(vk_user_id)}."
                        ),
                        "source": "Тео — VK сообщество",
                    }
                )

                meeting_id = str(created.get("id") or "").strip() or None
                context["meeting_id"] = meeting_id
                context["confirmed_at"] = _now_iso()

                zoom_part = ""
                if zoom_link:
                    zoom_part = f" Ссылка Zoom: {zoom_link}."
                    if zoom_note:
                        zoom_part += f" {zoom_note}"

                return (
                    f"Договорились! Встреча с {owner_name} назначена: "
                    f"{_format_slot(start_utc, tz_name)}. "
                    f"Формат — {meeting_format}. "
                    "Встреча действительно внесена в календарь."
                    + zoom_part,
                    "scheduled",
                    context,
                    meeting_id,
                )

        if _is_no(incoming_text):
            context.pop("proposed_start_at", None)
            return (
                "Хорошо. Напишите, пожалуйста, какой день и время вам удобнее.",
                "collecting_meeting_details",
                context,
                None,
            )

    if state == "awaiting_slot_choice":
        choice = re.search(r"\b([123])\b", str(incoming_text or ""))
        slots = context.get("offered_slots") or []

        if choice and isinstance(slots, list) and len(slots) >= int(choice.group(1)):
            selected = slots[int(choice.group(1)) - 1]
            context["proposed_start_at"] = selected
            start_utc = datetime.fromisoformat(
                str(selected).replace("Z", "+00:00")
            ).astimezone(UTC)

            return (
                f"Выбрали {_format_slot(start_utc, str(context['contact_timezone']))}. "
                f"Формат — {context['meeting_format']}. Подтверждаем?",
                "awaiting_confirmation",
                context,
                None,
            )

        return (
            "Напишите, пожалуйста, номер подходящего варианта: 1, 2 или 3.",
            "awaiting_slot_choice",
            context,
            None,
        )

    if not context.get("requested_date"):
        return (
            intro + "На какой день вам удобна встреча?",
            "collecting_meeting_details",
            context,
            None,
        )

    if not context.get("requested_time"):
        return (
            intro + "На какое время вам удобно?",
            "collecting_meeting_details",
            context,
            None,
        )

    if not context.get("contact_timezone"):
        return (
            intro + "По какому часовому поясу указано время? Например: МСК или Berlin.",
            "collecting_meeting_details",
            context,
            None,
        )

    if not context.get("meeting_format"):
        return (
            intro + "Как вам удобнее встретиться — Zoom, Telegram или WhatsApp?",
            "collecting_meeting_details",
            context,
            None,
        )

    start_utc = _parse_start(context)
    if start_utc is None:
        return (
            "Не смог точно определить дату и время. "
            "Напишите, пожалуйста, их ещё раз.",
            "collecting_meeting_details",
            context,
            None,
        )

    end_utc = start_utc + timedelta(minutes=DURATION_MINUTES)
    tz_name = str(context["contact_timezone"])
    meeting_format = str(context["meeting_format"])

    if start_utc < datetime.now(UTC) + timedelta(minutes=MIN_LEAD_MINUTES):
        context.pop("proposed_start_at", None)
        return (
            "Это время уже слишком близко. Встречу нужно назначать минимум за час. "
            "Напишите, пожалуйста, другое удобное время.",
            "collecting_meeting_details",
            context,
            None,
        )

    start_msk = start_utc.astimezone(MSK)
    end_msk = end_utc.astimezone(MSK)
    day_start = datetime.combine(start_msk.date(), dt_time(10, 0), MSK)
    day_end = datetime.combine(start_msk.date(), dt_time(20, 0), MSK)

    if start_msk < day_start or end_msk > day_end:
        return (
            "Встречи Агентства W назначаются в рабочем окне 10:00–20:00 МСК. "
            "Напишите другое удобное время — я проверю его по календарю.",
            "collecting_meeting_details",
            context,
            None,
        )

    if _slot_free_with_buffer(owner_id, start_utc, end_utc):
        context["proposed_start_at"] = start_utc.isoformat()
        return (
            f"Я проверил календарь: {_format_slot(start_utc, tz_name)} "
            f"у {owner_name} свободно. Формат — {meeting_format}. "
            "Подтверждаем это время?",
            "awaiting_confirmation",
            context,
            None,
        )

    slots = _find_three_slots(owner_id, start_utc, tz_name)
    if not slots:
        return (
            "Это время недоступно, а в ближайшем рабочем окне свободных вариантов "
            "пока не нашлось. Напишите другой удобный день — я проверю.",
            "collecting_meeting_details",
            context,
            None,
        )

    context["offered_slots"] = [slot.isoformat() for slot in slots]
    options = "\n".join(
        f"{index}. {_format_slot(slot, tz_name)}"
        for index, slot in enumerate(slots, 1)
    )

    return (
        "Это время недоступно. Нашёл ближайшие свободные варианты:\n"
        + options
        + "\nНапишите номер подходящего варианта.",
        "awaiting_slot_choice",
        context,
        None,
    )

def _read_text(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(
            "THEO_KNOWLEDGE_READ_WARNING:",
            f"{path.name}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return ""


def _knowledge_bundle() -> str:
    constitution = _read_text(CONSTITUTION_FILE)
    knowledge = _read_text(KNOWLEDGE_FILE)

    parts = [THEO_FALLBACK_RULES]

    if constitution:
        parts.append(
            "\n\n===== КОНСТИТУЦИЯ ТЕО =====\n"
            + constitution[:30000]
        )

    if knowledge:
        parts.append(
            "\n\n===== БАЗА ЗНАНИЙ АГЕНТСТВА W =====\n"
            + knowledge[:60000]
        )

    return "".join(parts)


def _load_dialog(vk_user_id: int) -> dict | None:
    rows = _sb_get(
        {
            "vk_user_id": f"eq.{int(vk_user_id)}",
            "select": "*",
            "limit": 1,
        }
    )
    return rows[0] if rows else None


def _clean_history(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    result = []

    for item in value[-24:]:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()

        if role not in {"user", "assistant"} or not content:
            continue

        result.append(
            {
                "role": role,
                "content": content[:2500],
                "at": str(item.get("at") or "").strip() or None,
            }
        )

    return result


def _strip_json_fence(text: str) -> str:
    value = str(text or "").strip()

    value = re.sub(
        r"^```(?:json)?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*```$",
        "",
        value,
    )

    return value.strip()


def _parse_model_result(raw: str) -> dict:
    clean = _strip_json_fence(raw)

    try:
        data = json.loads(clean)
    except Exception:
        data = {"reply": clean}

    if not isinstance(data, dict):
        data = {"reply": str(clean)}

    reply = str(data.get("reply") or "").strip()
    stage = str(
        data.get("stage") or "consulting"
    ).strip()
    summary = str(
        data.get("summary") or ""
    ).strip()

    if stage not in ALLOWED_STAGES:
        stage = "consulting"

    if not reply:
        raise RuntimeError(
            "Theo returned an empty reply."
        )

    return {
        "reply": reply,
        "stage": stage,
        "summary": summary[:2000],
    }


def _build_instructions(
    owner_name: str,
    first_contact: bool,
) -> str:

    owner = (
        str(owner_name or "").strip()
        or "Директор"
    )

    if first_contact:
        intro_rule = (
            "Это первое сообщение Тео этому человеку. "
            "Представься один раз: "
            "«Я Тео, консультант Агентства W». "
        )
    else:
        intro_rule = (
            "Вы уже общались. "
            "Не представляйся заново без причины. "
        )

    return f"""
{_knowledge_bundle()}

===== ТЕКУЩАЯ ЗАДАЧА =====

Ты отвечаешь человеку в сообщениях официального VK-сообщества
Агентства W.

{intro_rule}

Директор, который закреплён за этим человеком:
{owner}.

Алгоритм ответа:

1. Сначала пойми, что именно человек спросил или сообщил.

2. Дай содержательный ответ на его вопрос.

3. Если уместно, покажи, как это относится к его ситуации.

4. Не задавай лишний вопрос.

5. Если человек явно заинтересован в демонстрации,
подключении, обсуждении своего проекта, условиях
или личной консультации —
предложи короткую встречу с Директором {owner}.

6. Не выдумывай свободное время и не говори,
что встреча внесена в календарь. Если человек согласился
на встречу, поставь stage=meeting_ready: после этого
отдельный календарный модуль Тео проверит реальную занятость.

7. При явном отказе уважительно заверши тему.

8. Если точного подтверждённого ответа нет —
не фантазируй.

Определи stage:

consulting —
обычная консультация.

interested —
человек проявил заметный интерес,
но встречу ещё не просит.

meeting_ready —
человек готов обсудить систему с Директором
или согласен на встречу.

closed —
человек отказался или попросил прекратить тему.

Верни ТОЛЬКО JSON без markdown:

{{
  "reply": "готовый ответ человеку",
  "stage": "consulting|interested|meeting_ready|closed",
  "summary": "краткая внутренняя сводка: интерес, задача, сомнения, что уже объяснено"
}}
""".strip()


def _model_reply(
    *,
    owner_name: str,
    visitor_first_name: str,
    incoming_text: str,
    history: list[dict],
    first_contact: bool,
) -> dict:

    api_key = os.getenv(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY"
        )

    client = OpenAI(
        api_key=api_key
    )

    conversation = []

    for item in history[-16:]:
        conversation.append(
            {
                "role": item["role"],
                "content": item["content"],
            }
        )

    person = str(
        visitor_first_name or ""
    ).strip()

    if person:
        current = (
            f"Имя посетителя: {person}\n"
            f"Сообщение: {incoming_text}"
        )
    else:
        current = (
            f"Сообщение посетителя: "
            f"{incoming_text}"
        )

    conversation.append(
        {
            "role": "user",
            "content": current,
        }
    )

    response = client.responses.create(
        model=THEO_MODEL,
        instructions=_build_instructions(
            owner_name,
            first_contact,
        ),
        input=conversation,
        max_output_tokens=700,
    )

    return _parse_model_result(
        response.output_text
    )


def process_vk_community_message(
    *,
    vk_user_id: int,
    vk_peer_id: int,
    owner: dict,
    visitor_first_name: str,
    incoming_text: str,
    message_id: str = "",
) -> dict:

    vk_user_id = int(vk_user_id)
    vk_peer_id = int(vk_peer_id)

    incoming_text = str(incoming_text or "").strip()
    message_id = str(message_id or "").strip()

    if not incoming_text:
        raise RuntimeError(
            "Theo requires a non-empty incoming message."
        )

    owner = dict(owner or {})
    owner_id = int(owner.get("telegram_id") or 0)

    if owner_id <= 0:
        raise RuntimeError(
            "Theo requires a valid Agency W owner."
        )

    owner_name = str(
        owner.get("first_name") or ""
    ).strip()

    owner_member_code = str(
        owner.get("member_code") or ""
    ).strip()

    existing = _load_dialog(vk_user_id)

    if existing and message_id:
        if (
            str(existing.get("last_vk_message_id") or "").strip()
            == message_id
        ):
            return {
                "reply": str(existing.get("last_reply_text") or "").strip(),
                "stage": str(existing.get("stage") or "consulting"),
                "summary": str(existing.get("summary") or "").strip(),
                "meeting_state": str(
                    existing.get("meeting_state") or "none"
                ).strip(),
                "meeting_id": (
                    str(existing.get("meeting_id") or "").strip()
                    or None
                ),
                "duplicate": True,
            }

    history = _clean_history(
        existing.get("dialogue_history")
        if existing
        else []
    )
    first_contact = not bool(history)

    meeting_state = str(
        existing.get("meeting_state")
        if existing
        else "none"
    ).strip() or "none"

    meeting_context = (
        existing.get("meeting_context")
        if existing
        and isinstance(existing.get("meeting_context"), dict)
        else {}
    )

    meeting_id = (
        str(existing.get("meeting_id") or "").strip()
        if existing
        else ""
    ) or None

    existing_stage = str(
        existing.get("stage") or "consulting"
        if existing
        else "consulting"
    ).strip()

    existing_summary = str(
        existing.get("summary") or ""
        if existing
        else ""
    ).strip()

    result: dict

    # Если календарный диалог уже начат, OpenAI больше не решает,
    # свободно ли время. Здесь работает только детерминированная логика.
    if meeting_state in ACTIVE_MEETING_STATES:
        reply, meeting_state, meeting_context, created_meeting_id = _schedule_reply(
            owner_id=owner_id,
            owner_name=owner_name,
            vk_user_id=vk_user_id,
            visitor_first_name=visitor_first_name,
            incoming_text=incoming_text,
            meeting_state=meeting_state,
            meeting_context=meeting_context,
            first_contact=first_contact,
        )

        if created_meeting_id:
            meeting_id = created_meeting_id

        result = {
            "reply": reply,
            "stage": "meeting_ready",
            "summary": existing_summary,
        }

    else:
        result = _model_reply(
            owner_name=owner_name,
            visitor_first_name=visitor_first_name,
            incoming_text=incoming_text,
            history=history,
            first_contact=first_contact,
        )

        # Как только Тео распознал реальное согласие на встречу,
        # он передаёт разговор своему календарному модулю.
        if (
            result["stage"] == "meeting_ready"
            and meeting_state != "scheduled"
        ):
            reply, meeting_state, meeting_context, created_meeting_id = _schedule_reply(
                owner_id=owner_id,
                owner_name=owner_name,
                vk_user_id=vk_user_id,
                visitor_first_name=visitor_first_name,
                incoming_text=incoming_text,
                meeting_state="collecting_meeting_details",
                meeting_context=meeting_context,
                first_contact=first_contact,
            )

            if created_meeting_id:
                meeting_id = created_meeting_id

            result["reply"] = reply

    now = _now_iso()

    history.append(
        {
            "role": "user",
            "content": incoming_text[:2500],
            "at": now,
        }
    )

    history.append(
        {
            "role": "assistant",
            "content": result["reply"][:2500],
            "at": now,
        }
    )

    history = history[-24:]

    payload = {
        "vk_user_id": vk_user_id,
        "vk_peer_id": vk_peer_id,
        "owner_telegram_id": owner_id,
        "owner_member_code": owner_member_code or None,
        "owner_name": owner_name or None,
        "visitor_first_name": (
            str(visitor_first_name or "").strip() or None
        ),
        "stage": result["stage"],
        "dialogue_history": history,
        "summary": result["summary"] or None,
        "last_incoming_text": incoming_text[:3000],
        "last_reply_text": result["reply"][:3000],
        "last_vk_message_id": message_id or None,
        "meeting_state": meeting_state,
        "meeting_context": meeting_context,
        "meeting_id": meeting_id,
        "last_seen_at": now,
        "updated_at": now,
    }

    if not existing:
        payload["first_seen_at"] = now

    _sb_post(payload)

    return {
        **result,
        "meeting_state": meeting_state,
        "meeting_id": meeting_id,
        "duplicate": False,
    }
