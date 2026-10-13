"""Shared daily motto and calendar brief for Neola (no UI/network side effects)."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
MOSCOW = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

DAILY_MOODS = (
    "Сегодня важно сделать один конкретный шаг, который приблизит вас к цели.",
    "Не стремитесь сделать всё сразу. Главное — двигаться вперёд каждый день.",
    "Своё настоящее мы создаём решениями, которые принимаем сегодня.",
    "Спокойствие, ясность и последовательность сильнее суеты.",
    "Сегодня хороший день, чтобы завершить то, что давно откладывалось.",
    "Большой результат начинается с одного простого действия.",
    "Сосредоточьтесь не на трудностях, а на следующем возможном шаге.",
    "Каждый новый человек — это новая история, а не просто контакт.",
    "Сегодня выбирайте действия, которые создают результат, а не занятость.",
    "Уверенность появляется не до действия, а после первых сделанных шагов.",
    "Не сравнивайте своё начало с чужим результатом. Продолжайте свой путь.",
    "Сегодня достаточно стать немного сильнее, чем вы были вчера.",
    "Возможности замечает тот, кто готов действовать.",
    "Ваш опыт — это ваша сила. Технологии помогают её масштабировать.",
)

INACTIVE_STATUSES = frozenset((
    "отменена", "перенесена", "состоялась", "завершена",
    "отменено", "перенесено", "cancelled", "canceled",
    "completed", "done",
))


def daily_mood_for_date(when: date) -> str:
    return DAILY_MOODS[when.toordinal() % len(DAILY_MOODS)]


def _parse_start(raw: object) -> datetime | None:
    try:
        if isinstance(raw, datetime):
            value = raw
        else:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def upcoming_meetings(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Upcoming meetings on the current *Moscow* calendar day, sorted by time.

    Past, cancelled, moved and completed appointments must never be announced.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    today = current.astimezone(MOSCOW).date()
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().casefold() in INACTIVE_STATUSES:
            continue
        start = _parse_start(row.get("start_at"))
        if start is None or start < current or start.astimezone(MOSCOW).date() != today:
            continue
        name = re.sub(r"\s+", " ", str(row.get("contact_name") or "").strip())[:65]
        meeting_format = re.sub(r"\s+", " ", str(row.get("meeting_format") or "").strip())[:35]
        result.append({
            "id": str(row.get("id") or f"{start.isoformat()}:{name}"),
            "start_utc": start,
            "start_msk": start.astimezone(MOSCOW),
            "name": name or "собеседником",
            "format": meeting_format,
        })
    return sorted(result, key=lambda item: item["start_utc"])


def _count_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "встреча"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "встречи"
    return "встреч"


def _minute_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "минуту"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "минуты"
    return "минут"


def spoken_meeting_time(start_msk: datetime) -> str:
    return f"{start_msk.hour} часов {start_msk.minute:02d} минут по московскому времени"


def build_daily_brief(
    owner_name: str,
    *,
    motto: str,
    meetings: list[dict] | None = None,
    calendar_available: bool = True,
    now: datetime | None = None,
) -> str:
    now = (now or datetime.now(UTC)).astimezone(BERLIN)
    hello = "Доброе утро" if now.hour < 12 else ("Добрый день" if now.hour < 18 else "Добрый вечер")
    name = re.sub(r"\s+", " ", str(owner_name or "").strip())[:45]
    parts = [f"{hello}{', ' + name if name else ''}.", f"Настрой дня: {motto}"]
    agenda = meetings or []
    if not calendar_available:
        parts.append("Не удалось проверить календарь. Пожалуйста, посмотрите встречи в разделе «Календарь».")
    elif agenda:
        parts.append(f"Сегодня впереди {len(agenda)} {_count_word(len(agenda))}.")
        for appointment in agenda[:4]:
            parts.append(
                f"В {spoken_meeting_time(appointment['start_msk'])} — "
                f"встреча с {appointment['name']}"
                + (f", формат {appointment['format']}." if appointment["format"] else ".")
            )
        if len(agenda) > 4:
            parts.append(f"Ещё {len(agenda) - 4} встреч смотрите в календаре.")
    else:
        parts.append("На сегодня предстоящих встреч в календаре нет.")

    parts.append(
        "Главный рабочий ориентир — пять новых кандидатов и осмысленные диалоги. "
        "Если понадобится моя помощь, я рядом."
    )
    return " ".join(parts)


def next_hour_reminder(
    agenda: list[dict],
    *,
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """Return a stable meeting ID and one-hour reminder, never a past event."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for meeting in agenda:
        until = (meeting["start_utc"] - current).total_seconds()
        if 0 < until <= 3600:
            minutes = max(1, math.ceil(until / 60))
            text = (
                f"Напоминание от Неолы. Через {minutes} {_minute_word(minutes)} "
                f"у вас встреча с {meeting['name']}. "
                f"Начало в {spoken_meeting_time(meeting['start_msk'])}."
            )
            if meeting["format"]:
                text += f" Формат — {meeting['format']}."
            return meeting["id"], text
    return None
