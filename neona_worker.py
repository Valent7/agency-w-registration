import json
import os
import re
from pathlib import Path
from typing import Any
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import requests

import neona_telegram_dialogs as nd


BRAIN_VERSION = "4.1"
APP_DIR = Path(__file__).resolve().parent
CORE_PATH = APP_DIR / "NEONA_CORE.md"

ORIGINAL_PROCESS_MESSAGE = nd._process_message
ORIGINAL_DETECT_TIMEZONE = nd._detect_timezone
ORIGINAL_DETECT_DATE = nd._detect_date
ORIGINAL_SCHEDULE_REPLY = nd._schedule_reply

MIN_PREP_MINUTES = int(os.getenv("NEONA_MIN_PREP_MINUTES", "120"))

STRICT_SCHEDULING_STAGES = {
    "awaiting_confirmation",
    "awaiting_slot_choice",
}

CONVERSATIONAL_SCHEDULING_STAGES = {
    "invited_to_meeting",
    "collecting_meeting_details",
}

PROFILE_FIELDS = {
    "work_or_project",
    "current_situation",
    "pain",
    "goal",
    "dream",
    "time_drains",
    "motivation",
    "partnership_values",
    "concerns",
    "important_people",
    "desired_change",
}

ROLE_CONFUSION_PATTERNS = (
    r"\bя\s+(?:начну|поищу|найду|подберу|подготовлю|соберу|пришлю)\b.{0,90}\b(?:кандидат|контакт|подборк|перв(?:ое|ых)\s+сообщен)",
    r"\b(?:подготовлю|пришлю)\b.{0,80}\b(?:через|в течение)\b.{0,30}\b(?:час|минут)",
    r"\bзадайте\b.{0,50}\b(?:критери|фильтр|параметр).{0,60}\b(?:поиск|кандидат|контакт)",
)

TECH_JARGON = (
    "скоринг",
    "воронка",
    "лид",
    "лидогенерац",
    "сегментац",
    "crm",
    "конверси",
    "операционн",
    "прогнать кейс",
    "ключевые результаты",
    "приемлем",
    "инициир",
)


def _read_core() -> str:
    try:
        text = CORE_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return (
        "Агентство W возвращает человеку время. Неона ведёт человеческий "
        "диалог и приводит заинтересованного человека к полезной встрече "
        "с владельцем кабинета, сохраняя честность, тактичность и уважение."
    )


NEONA_CORE = _read_core()


def _smart_detect_timezone(text: str) -> str | None:
    raw = str(text or "")
    lowered = raw.lower().strip()
    normalized = re.sub(r"[.,;:()]+", " ", lowered)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    moscow_markers = (
        "мск", "моск", "москва", "москве", "москов",
        "московск", "по моск", "по мск",
    )
    if any(marker in normalized for marker in moscow_markers):
        return "Europe/Moscow"

    berlin_markers = (
        "берлин", "германи", "немецк", "по берлину",
    )
    if any(marker in normalized for marker in berlin_markers):
        return "Europe/Berlin"

    return ORIGINAL_DETECT_TIMEZONE(raw)



RUS_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


def _smart_detect_date(text: str, message_dt: datetime, tz_name: str | None) -> str | None:
    """Понимает обычные русские даты: «10 августа», «в понедельник 10 августа»."""
    lowered = str(text or "").lower()
    tz = ZoneInfo(tz_name) if tz_name else nd.MSK
    base = message_dt.astimezone(tz).date()

    # Явная дата словами важнее дня недели и старого контекста.
    match = re.search(
        r"\b([0-3]?\d)(?:-?го)?\s+"
        r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|"
        r"июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|"
        r"ноябр\w*|декабр\w*)"
        r"(?:\s+(\d{4}))?\b",
        lowered,
    )
    if match:
        day = int(match.group(1))
        month_word = match.group(2)
        year = int(match.group(3)) if match.group(3) else base.year
        month = None
        for stem, value in RUS_MONTHS.items():
            if month_word.startswith(stem):
                month = value
                break
        if month:
            try:
                candidate = date(year, month, day)
                if not match.group(3) and candidate < base - timedelta(days=2):
                    candidate = date(year + 1, month, day)
                return candidate.isoformat()
            except ValueError:
                return None

    return ORIGINAL_DETECT_DATE(text, message_dt, tz_name)


def _reschedule_intent(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "перенес", "перенести", "перенос",
            "другое время", "другой день",
            "не подходит", "не устраивает",
            "неправильно выбрали", "не правильно выбрали",
            "давайте на", "лучше на", "мне нужно на",
        )
    )


def _has_explicit_new_datetime(text: str, message_dt: datetime, tz_name: str | None) -> bool:
    return bool(
        _smart_detect_date(text, message_dt, tz_name)
        or nd._detect_time(text)
    )


def _clear_old_datetime(context: dict[str, Any]) -> dict[str, Any]:
    context = dict(context or {})
    for key in ("requested_date", "requested_time", "proposed_start_at", "offered_slots"):
        context.pop(key, None)
    return context


def _round_up_half_hour(value: datetime) -> datetime:
    value = value.replace(second=0, microsecond=0)
    if value.minute in (0, 30):
        return value
    if value.minute < 30:
        return value.replace(minute=30)
    return value.replace(minute=0) + timedelta(hours=1)


def _find_safe_slots(
    config: nd.Config,
    owner_id: int,
    after_utc: datetime,
    contact_timezone: str,
    *,
    limit: int = 3,
) -> list[datetime]:
    """Ближайшие свободные варианты после времени, достаточного для подготовки."""
    result: list[datetime] = []
    cursor = _round_up_half_hour(after_utc.astimezone(nd.MSK))

    # Ищем до 14 дней вперёд, по 30 минут.
    for _ in range(14 * 48):
        if 10 <= cursor.hour < 20:
            start_utc = cursor.astimezone(nd.UTC)
            end_utc = start_utc + timedelta(minutes=nd.DURATION_MINUTES)
            local = start_utc.astimezone(ZoneInfo(contact_timezone))
            if 8 <= local.hour < 22 and nd._slot_free(config, owner_id, start_utc, end_utc):
                result.append(start_utc)
                if len(result) >= limit:
                    break
        cursor += timedelta(minutes=30)
    return result


def _friendly_slot(start_utc: datetime, tz_name: str) -> str:
    msk = start_utc.astimezone(nd.MSK)
    if tz_name == "Europe/Moscow":
        return f"{msk:%d.%m.%Y в %H:%M} по Москве"
    local = start_utc.astimezone(ZoneInfo(tz_name))
    return f"{msk:%d.%m.%Y в %H:%M} по Москве ({local:%H:%M} по вашему времени)"


def _format_options(slots: list[datetime], tz_name: str) -> str:
    return "\n".join(
        f"{index}. {_friendly_slot(slot, tz_name)}"
        for index, slot in enumerate(slots, 1)
    )


def _mark_meeting_rescheduled(config: nd.Config, meeting_id: Any) -> None:
    if not meeting_id:
        return
    response = requests.patch(
        f"{config.supabase_url}/rest/v1/agency_meetings",
        headers=nd._headers(config, "return=minimal"),
        params={"id": f"eq.{meeting_id}"},
        json={"status": "Перенесена"},
        timeout=20,
    )
    response.raise_for_status()


def _schedule_reply_v41(
    config: nd.Config,
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
    """Календарь 4.1: 2 часа на подготовку, перенос, новая дата, повторная проверка."""
    context = dict(context or {})
    prefix = nd._greeting(first_name) + " " if greet else ""
    tz_before = context.get("contact_timezone")

    # Уже назначенную встречу меняем только по явной просьбе человека.
    if stage == "scheduled":
        if not (_reschedule_intent(text) or _has_explicit_new_datetime(text, message_dt, tz_before)):
            return (
                prefix + "Хорошо. Встреча уже в календаре. Если захотите изменить время — просто напишите.",
                "scheduled",
                context,
            )
        old_id = context.get("meeting_id")
        if old_id:
            context["rescheduling_meeting_id"] = old_id
        context = _clear_old_datetime(context)
        stage = "collecting_meeting_details"

    # Если человек в процессе согласования назвал новую дату или время,
    # старая дата не должна «прилипать».
    elif stage in {"awaiting_confirmation", "awaiting_slot_choice"} and (
        _reschedule_intent(text)
        or _has_explicit_new_datetime(text, message_dt, tz_before)
    ) and not nd._is_yes(text):
        context = _clear_old_datetime(context)
        stage = "collecting_meeting_details"

    context = nd._update_context_from_message(context, text, message_dt)

    # Отказ от встречи.
    if stage == "invited_to_meeting" and nd._is_no(text):
        return (
            prefix + "Хорошо, без спешки. Продолжим здесь.",
            "idle",
            context,
        )

    # Человек выбирает один из предложенных вариантов.
    if stage == "awaiting_slot_choice":
        choice = re.search(r"\b([123])\b", text)
        slots = context.get("offered_slots") or []
        if choice and isinstance(slots, list) and len(slots) >= int(choice.group(1)):
            selected = slots[int(choice.group(1)) - 1]
            start_utc = datetime.fromisoformat(str(selected).replace("Z", "+00:00")).astimezone(nd.UTC)
            context["proposed_start_at"] = start_utc.isoformat()
            return (
                prefix + f"Тогда {_friendly_slot(start_utc, str(context['contact_timezone']))}. Подтверждаете?",
                "awaiting_confirmation",
                context,
            )

    # Подтверждение — обязательная повторная проверка прямо перед записью.
    if stage == "awaiting_confirmation" and nd._is_yes(text):
        proposed = context.get("proposed_start_at")
        tz_name = str(context.get("contact_timezone") or "")
        meeting_format = str(context.get("meeting_format") or "")
        if proposed and tz_name and meeting_format:
            start_utc = datetime.fromisoformat(str(proposed).replace("Z", "+00:00")).astimezone(nd.UTC)
            end_utc = start_utc + timedelta(minutes=nd.DURATION_MINUTES)

            # Даже между предложением и «да» слот мог заняться.
            if not nd._slot_free(config, owner_id, start_utc, end_utc):
                context.pop("proposed_start_at", None)
                safe_after = max(
                    datetime.now(nd.UTC) + timedelta(minutes=MIN_PREP_MINUTES),
                    start_utc,
                )
                slots = _find_safe_slots(config, owner_id, safe_after, tz_name)
                if slots:
                    context["offered_slots"] = [slot.isoformat() for slot in slots]
                    return (
                        prefix + "Это время уже занято. Вот ближайшие свободные варианты:\n"
                        + _format_options(slots, tz_name)
                        + "\nКакой вам удобнее?",
                        "awaiting_slot_choice",
                        context,
                    )
                return (
                    prefix + "Это время уже занято. Напишите другое удобное время, я сразу проверю.",
                    "collecting_meeting_details",
                    context,
                )

            created = nd._create_meeting(
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
                    "meeting_link": None,
                    "status": "Подтверждена",
                    "notes": "Назначено Неоной после подтверждения человека в Telegram.",
                    "source": "Неона — Telegram диалог",
                },
            )
            context["meeting_id"] = created.get("id")

            old_id = context.pop("rescheduling_meeting_id", None)
            if old_id and str(old_id) != str(context.get("meeting_id")):
                _mark_meeting_rescheduled(config, old_id)

            confirmed = datetime.fromisoformat(
                str(created["start_at"]).replace("Z", "+00:00")
            ).astimezone(nd.UTC)
            return (
                prefix + f"Готово. Встреча с {owner_name} записана на {_friendly_slot(confirmed, tz_name)}. "
                + f"Формат — {meeting_format}.",
                "scheduled",
                context,
            )

    if stage == "awaiting_confirmation" and nd._is_no(text):
        context.pop("proposed_start_at", None)
        return (
            prefix + "Хорошо. Какое другое время вам удобно?",
            "collecting_meeting_details",
            context,
        )

    # Собираем только недостающие данные.
    missing = []
    if not context.get("requested_date") or not context.get("requested_time"):
        missing.append("date_time")
    if not context.get("contact_timezone"):
        missing.append("timezone")
    if not context.get("meeting_format"):
        missing.append("format")

    if missing:
        questions = []
        if "date_time" in missing:
            questions.append("день и время")
        if "timezone" in missing:
            questions.append("город или часовой пояс")
        if "format" in missing:
            questions.append("Zoom, Telegram или WhatsApp")
        if len(questions) == 1:
            ask = questions[0]
        else:
            ask = ", ".join(questions[:-1]) + " и " + questions[-1]
        return (
            prefix + "Подскажите, пожалуйста, " + ask + ".",
            "collecting_meeting_details",
            context,
        )

    start_utc = nd._parse_start(context)
    if start_utc is None:
        return (
            prefix + "Не смогла точно понять время. Напишите, пожалуйста, дату и время ещё раз.",
            "collecting_meeting_details",
            context,
        )

    now_utc = datetime.now(nd.UTC)
    earliest = now_utc + timedelta(minutes=MIN_PREP_MINUTES)
    tz_name = str(context["contact_timezone"])
    meeting_format = str(context["meeting_format"])

    # Встреча слишком близко — честно говорим и сразу предлагаем реальное время.
    if start_utc < earliest:
        slots = _find_safe_slots(config, owner_id, earliest, tz_name)
        context.pop("proposed_start_at", None)
        if slots:
            context["offered_slots"] = [slot.isoformat() for slot in slots]
            return (
                prefix + f"Так быстро не получится — {owner_name} нужно немного времени подготовиться. "
                + "Вот ближайшие свободные варианты:\n"
                + _format_options(slots, tz_name)
                + "\nКакой вам удобнее?",
                "awaiting_slot_choice",
                context,
            )
        return (
            prefix + f"Так быстро не получится — {owner_name} нужно немного времени подготовиться. "
            + "Предложите время хотя бы через пару часов.",
            "collecting_meeting_details",
            context,
        )

    end_utc = start_utc + timedelta(minutes=nd.DURATION_MINUTES)

    # Первая проверка занятости.
    if nd._slot_free(config, owner_id, start_utc, end_utc):
        context["proposed_start_at"] = start_utc.isoformat()
        return (
            prefix + f"{_friendly_slot(start_utc, tz_name)} свободно. Формат — {meeting_format}. Подтверждаете?",
            "awaiting_confirmation",
            context,
        )

    slots = _find_safe_slots(config, owner_id, max(start_utc, earliest), tz_name)
    if slots:
        context["offered_slots"] = [slot.isoformat() for slot in slots]
        return (
            prefix + "Это время уже занято. Вот ближайшие свободные варианты:\n"
            + _format_options(slots, tz_name)
            + "\nКакой вам удобнее?",
            "awaiting_slot_choice",
            context,
        )

    return (
        prefix + "Это время занято. Напишите другое удобное время, я сразу проверю.",
        "collecting_meeting_details",
        context,
    )


def _message_has_meeting_details(text: str) -> bool:
    return bool(
        nd._detect_time(text)
        or _smart_detect_date(text, datetime.now(nd.UTC), _smart_detect_timezone(text))
        or _smart_detect_timezone(text)
        or nd._detect_format(text)
    )


def _explicit_meeting_commitment(text: str) -> bool:
    lowered = re.sub(r"[^a-zа-яё0-9:./ -]+", " ", str(text or "").lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()

    explicit_phrases = (
        "давайте встретимся",
        "давайте созвонимся",
        "давай встретимся",
        "давай созвонимся",
        "хочу встретиться",
        "хочу созвониться",
        "готов встретиться",
        "готова встретиться",
        "готов созвониться",
        "готова созвониться",
        "когда можно встретиться",
        "когда можно созвониться",
        "назначим встречу",
        "назначить встречу",
        "запишите меня",
        "давайте назначим",
        "можно встретиться",
    )
    if any(phrase in lowered for phrase in explicit_phrases):
        return True

    has_date = bool(
        re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", lowered)
        or any(word in lowered for word in ("сегодня", "завтра", "послезавтра"))
        or any(word in lowered for word in nd.WEEKDAYS_RU)
    )
    return bool(nd._detect_time(text) and has_date)


def _conversation_tail(context: dict[str, Any]) -> list[dict[str, str]]:
    tail = context.get("conversation_tail")
    if not isinstance(tail, list):
        return []
    clean = []
    for item in tail[-10:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            clean.append({"role": role, "content": content})
    return clean


def _project_passport(config: nd.Config, owner_id: int) -> str:
    """Берёт анализ проекта Неонии из рабочего состояния конкретного владельца."""
    try:
        workspace = nd._load_workspace(config, int(owner_id))
    except Exception:
        return ""

    if not isinstance(workspace, dict):
        return ""

    preferred_key = f"neonia_target_audience_passport_{int(owner_id)}"
    passport = workspace.get(preferred_key)

    if not isinstance(passport, dict):
        passport = None
        for key, value in workspace.items():
            if str(key).startswith("neonia_target_audience_passport_") and isinstance(value, dict):
                passport = value
                break

    if not isinstance(passport, dict):
        return ""

    analysis = str(passport.get("analysis") or "").strip()
    owner_note = str(passport.get("owner_note") or "").strip()

    parts = []
    if analysis:
        parts.append("АНАЛИЗ ПРОЕКТА НЕОНИЕЙ:\n" + analysis[:7000])
    if owner_note:
        parts.append("КОММЕНТАРИЙ ВЛАДЕЛЬЦА:\n" + owner_note[:1500])
    return "\n\n".join(parts)


def _extract_response_text(data: dict[str, Any]) -> str:
    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def _parse_json_answer(raw: str) -> dict[str, Any]:
    raw = str(raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {
            "reply": raw,
            "meeting_committed": False,
            "invited_to_meeting": False,
            "conversation_stage": "discover",
            "meeting_readiness": 0,
            "next_action": "listen",
            "person_updates": {},
        }


def _normalize_profile(context: dict[str, Any]) -> dict[str, str]:
    raw = context.get("person_profile")
    if not isinstance(raw, dict):
        return {}
    profile = {}
    for key, value in raw.items():
        if key in PROFILE_FIELDS:
            value = str(value or "").strip()
            if value:
                profile[key] = value
    return profile


def _merge_profile(context: dict[str, Any], updates: Any) -> dict[str, Any]:
    context = dict(context or {})
    profile = _normalize_profile(context)

    if isinstance(updates, dict):
        for key, value in updates.items():
            if key not in PROFILE_FIELDS:
                continue
            value = str(value or "").strip()
            if value:
                profile[key] = value[:900]

    context["person_profile"] = profile
    return context


def _remember_exchange(
    context: dict[str, Any],
    user_text: str,
    assistant_text: str,
) -> dict[str, Any]:
    context = dict(context or {})
    tail = _conversation_tail(context)
    tail.append({"role": "user", "content": str(user_text).strip()})
    if assistant_text:
        tail.append({"role": "assistant", "content": str(assistant_text).strip()})
    context["conversation_tail"] = tail[-10:]
    return context


def _migrate_context(context: dict[str, Any], stage: str) -> tuple[dict[str, Any], str]:
    """Старая ошибочная переписка не должна обучать новую Неону."""
    context = dict(context or {})
    if context.get("brain_version") == BRAIN_VERSION:
        return context, stage

    scheduling_keys = {
        key: context[key]
        for key in (
            "contact_timezone",
            "contact_city",
            "meeting_format",
            "requested_time",
            "requested_date",
            "proposed_start_at",
            "offered_slots",
            "meeting_id",
        )
        if context.get(key)
    }

    if stage == "scheduled":
        scheduling_keys["brain_version"] = BRAIN_VERSION
        return scheduling_keys, stage

    scheduling_keys["brain_version"] = BRAIN_VERSION
    # Не переносим старые ответы Неоны и старые гипотезы о человеке:
    # вчерашние ошибки не должны направлять новую версию.
    return scheduling_keys, "idle"


def _reply_has_role_confusion(reply: str) -> bool:
    lowered = str(reply or "").lower()
    for pattern in ROLE_CONFUSION_PATTERNS:
        if re.search(pattern, lowered, flags=re.S):
            return True
    return False


def _reply_is_too_technical(reply: str) -> bool:
    lowered = str(reply or "").lower()
    hard_jargon = (
        "операционная нагрузка",
        "прогнать кейс",
        "ключевые результаты",
        "приемлемый результат",
    )
    if any(token in lowered for token in hard_jargon):
        return True
    jargon_hits = sum(1 for token in TECH_JARGON if token in lowered)
    return jargon_hits >= 2


def _reply_is_too_long(reply: str) -> bool:
    text = str(reply or "").strip()
    return len(text) > 560 or text.count("\n") > 3


def _needs_repair(reply: str) -> bool:
    return (
        not str(reply or "").strip()
        or _reply_has_role_confusion(reply)
        or _reply_is_too_technical(reply)
        or _reply_is_too_long(reply)
    )


def _repair_reply(
    config: nd.Config,
    owner_name: str,
    incoming: str,
    draft: str,
) -> str:
    instructions = f"""
Ты редактор ответа Неоны — ИИ-помощницы {owner_name} в Агентстве W.

Перепиши черновик так, чтобы:
- Неона НЕ искала кандидатов и не обещала подборки: это работа Неонии;
- Неона не обещала выполнить действие позже или «через пару часов»;
- ответ был простым, живым, человеческим, обычно 1–3 коротких предложения;
- его с первого раза понял бы человек, далёкий от ИИ и бизнеса;
- можно спокойно использовать понятные слова: «ваш пример», «удобное время»,
  «рутина», «что вам сейчас важно»;
- избегай тяжёлых выражений вроде «операционная нагрузка», «прогнать кейс»,
  «ключевые результаты», «приемлемый результат»;
- сначала был дан ответ по смыслу последней реплики человека;
- если уместно, в конце был только один естественный вопрос о самом человеке;
- внутреннее направление — полезная встреча с {owner_name};
- центральная ценность Агентства W — «Мы возвращаем человеку время»;
- никакого давления и никакого корпоративного жаргона.

Верни только исправленный текст без пояснений.
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
            "input": (
                "Последняя реплика человека:\n"
                + str(incoming)
                + "\n\nЧерновик Неоны:\n"
                + str(draft)
            ),
            "store": False,
        },
        timeout=90,
    )
    response.raise_for_status()
    answer = _extract_response_text(response.json())
    return answer.strip() or str(draft).strip()


def _ai_director(
    config: nd.Config,
    owner_id: int,
    owner_name: str,
    first_name: str,
    text: str,
    context: dict[str, Any],
    *,
    greet: bool,
) -> dict[str, Any]:
    history = _conversation_tail(context)
    profile = _normalize_profile(context)
    project = _project_passport(config, owner_id)

    history_text = "\n".join(
        f"{'Собеседник' if item['role'] == 'user' else 'Неона'}: {item['content']}"
        for item in history
    ) or "Новой версии Неоны пока не передана достоверная история."

    profile_text = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "{}"

    greeting_rule = (
        f"Можно естественно поприветствовать человека по имени {first_name}, если это уместно."
        if greet and first_name
        else "Диалог уже идёт: не повторяй приветствие без причины."
    )

    project_block = project if project else (
        "Для этого владельца подробный паспорт проекта сейчас не найден. "
        "Не выдумывай сведения о проекте. На глубокие проектные вопросы "
        f"делай мост к встрече с {owner_name}."
    )

    instructions = f"""
Ты — ДИРЕКТОР РАЗГОВОРА и одновременно голос Неоны.
Сначала молча анализируешь ситуацию, затем создаёшь один живой ответ.

Ниже находится ЯДРО НЕОНЫ. Оно имеет приоритет над стилевыми импровизациями.

--- ЯДРО НЕОНЫ ---
{NEONA_CORE}
--- КОНЕЦ ЯДРА ---

КОНТЕКСТ ПРОЕКТА ВЛАДЕЛЬЦА:
{project_block}

ТЕКУЩАЯ РОЛЬ:
Ты Неона — ИИ-помощница {owner_name}.
Перед тобой уже выбранный для разговора человек.
Ты не Неония и не выполняешь поиск кандидатов.

ВНУТРЕННЯЯ ЛОГИКА КАЖДОГО ХОДА:
1. Пойми эмоциональный и смысловой тон последней реплики.
2. Реши, что сейчас полезнее:
   listen / discover / reflect / explain_value / deepen /
   bridge_to_owner / invite_meeting / respect_boundary.
3. Обнови понимание человека только фактами, которые он сам сообщил.
4. Оцени готовность к встрече от 0 до 100.
5. Сформулируй короткий человеческий ответ.
6. Не раскрывай человеку стадии, баллы, KPI, анализ и внутреннюю логику.

СТАДИИ:
- cold: человек пока почти не включился;
- warm: начал разговаривать и отвечать;
- discover: раскрывается его ситуация/цели/боли/мечты;
- value: он начинает видеть личную пользу;
- intrigued: хочет узнать больше;
- ready: встреча уже естественна;
- scheduling: человек согласился и идут дата/время/формат.

ВАЖНО:
- Ты управляешь разговором, но не допрашиваешь.
- Говори обычным человеческим языком: обычно 1–3 коротких предложения.
- Можно использовать понятные выражения: «ваш пример», «удобное время»,
  «рутина», «что вам сейчас важно».
- Не используй без необходимости тяжёлые офисные выражения вроде
  «операционная нагрузка», «прогнать кейс», «ключевые результаты»,
  «приемлемый результат».
- Если мысль можно сказать проще — всегда скажи проще.
- Обычно один вопрос за сообщение, и вопрос должен быть о человеке,
  а не о настройке системы.
- Не задавай вопрос только ради вопроса.
- Если уже понятна важная потребность и есть интерес, не продолжай
  бесконечно «диагностировать» — создавай мост к встрече.
- Глубокий вопрос о конкретном проекте или позиции владельца — прекрасный
  повод предложить короткий разговор с {owner_name}.
- Простое объясняй сама.
- Если человек сказал «слишком сложно», говори намного проще.
- Если человек поймал тебя на повторе или ошибке, можно коротко и
  доброжелательно признать это и сменить курс.
- Не обещай сделать что-то «позже», если у тебя нет такого инструмента.
- Нельзя обещать поиск или подборку кандидатов.
- «Мы возвращаем человеку время» — смысл, а не рекламный лозунг.
  Используй эту мысль естественно и не повторяй её механически.
- Не дави на страхи и слабости человека.
- Не используй уязвимости для принуждения к встрече.

{greeting_rule}

Верни ТОЛЬКО JSON:
{{
  "reply": "готовый ответ Неоны",
  "conversation_stage": "cold|warm|discover|value|intrigued|ready",
  "next_action": "listen|discover|reflect|explain_value|deepen|bridge_to_owner|invite_meeting|respect_boundary",
  "meeting_readiness": 0,
  "deep_owner_question": false,
  "meeting_committed": false,
  "invited_to_meeting": false,
  "person_updates": {{
    "work_or_project": "",
    "current_situation": "",
    "pain": "",
    "goal": "",
    "dream": "",
    "time_drains": "",
    "motivation": "",
    "partnership_values": "",
    "concerns": "",
    "important_people": "",
    "desired_change": ""
  }}
}}
""".strip()

    user_input = (
        "НАКОПЛЕННОЕ ПОНИМАНИЕ ЧЕЛОВЕКА:\n"
        + profile_text
        + "\n\nПОСЛЕДНИЕ РЕПЛИКИ:\n"
        + history_text
        + "\n\nНОВАЯ РЕПЛИКА ЧЕЛОВЕКА:\n"
        + str(text)
    )

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-5-mini",
            "instructions": instructions,
            "input": user_input,
            "store": False,
        },
        timeout=90,
    )
    response.raise_for_status()
    raw = _extract_response_text(response.json())
    if not raw:
        raise nd.DialogError("OpenAI не сформировал ответ.")
    plan = _parse_json_answer(raw)

    reply = str(plan.get("reply") or "").strip()
    if _needs_repair(reply):
        reply = _repair_reply(config, owner_name, text, reply)
        plan["reply"] = reply

    try:
        readiness = int(plan.get("meeting_readiness") or 0)
    except (TypeError, ValueError):
        readiness = 0
    plan["meeting_readiness"] = max(0, min(100, readiness))
    return plan


def _smart_process_message(
    config: nd.Config,
    owner_id: int,
    owner_name: str,
    contact_id: int,
    first_name: str,
    username: str,
    text: str,
    message_dt,
    state: dict[str, Any],
):
    stage = str(state.get("stage") or "idle")
    greeted = bool(state.get("greeted", False))
    raw_context = state.get("context") if isinstance(state.get("context"), dict) else {}
    context, migrated_stage = _migrate_context(raw_context, stage)
    # При обновлении 4.0 -> 4.1 не теряем уже назначенную встречу.
    if stage == "scheduled":
        migrated_stage = "scheduled"
    stage = migrated_stage
    greet = not greeted

    # Календарные этапы обрабатываются строгим кодом, а не ИИ.
    if stage in {
        "scheduled",
        "invited_to_meeting",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "awaiting_slot_choice",
    }:
        # После назначенной встречи обычная реплика не должна запускать перенос.
        if stage == "scheduled" and not (
            _reschedule_intent(text)
            or _has_explicit_new_datetime(text, message_dt, context.get("contact_timezone"))
        ):
            plan = _ai_director(
                config,
                owner_id,
                owner_name,
                first_name,
                text,
                context,
                greet=greet,
            )
            reply = str(plan.get("reply") or "").strip()
            if not reply:
                reply = "Хорошо. До встречи!"
            context = _merge_profile(context, plan.get("person_updates"))
            context["brain_version"] = BRAIN_VERSION
            context = _remember_exchange(context, text, reply)
            return reply, "scheduled", True, context

        reply, new_stage, context = _schedule_reply_v41(
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
        context["brain_version"] = BRAIN_VERSION
        context = _remember_exchange(context, text, reply)
        return reply, new_stage, True, context

    # Свободный человеческий диалог.
    context = nd._update_context_from_message(context, text, message_dt)
    context["brain_version"] = BRAIN_VERSION

    plan = _ai_director(
        config,
        owner_id,
        owner_name,
        first_name,
        text,
        context,
        greet=greet,
    )
    reply = str(plan.get("reply") or "").strip()
    if not reply:
        reply = "Что для вас сейчас в этой теме самое важное?"

    context = _merge_profile(context, plan.get("person_updates"))
    context["conversation_stage"] = str(plan.get("conversation_stage") or "discover")
    context["meeting_readiness"] = int(plan.get("meeting_readiness") or 0)
    context["next_action"] = str(plan.get("next_action") or "listen")

    committed = bool(plan.get("meeting_committed")) or _explicit_meeting_commitment(text)
    if committed:
        reply, new_stage, context = _schedule_reply_v41(
            config,
            owner_id,
            owner_name,
            contact_id,
            first_name,
            username,
            text,
            message_dt,
            "invited_to_meeting",
            context,
            greet,
        )
    else:
        new_stage = (
            "invited_to_meeting"
            if bool(plan.get("invited_to_meeting"))
            else "idle"
        )

    context["brain_version"] = BRAIN_VERSION
    context = _remember_exchange(context, text, reply)
    return reply, new_stage, True, context


# Патчим только поведение круглосуточного worker.
# Telegram, Supabase и календарь остаются в проверенном модуле.
nd._detect_timezone = _smart_detect_timezone
nd._detect_date = _smart_detect_date
nd._process_message = _smart_process_message


if __name__ == "__main__":
    nd.worker_forever(int(os.getenv("NEONA_POLL_SECONDS", "15")))
