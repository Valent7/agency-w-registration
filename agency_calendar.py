"""Внутренний календарь встреч Агентства W.

Единый стандарт времени — МСК. В базе время хранится в UTC,
а в интерфейсе показывается одновременно по МСК и по местному
часовому поясу собеседника.
"""

from __future__ import annotations

import calendar as month_calendar
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import streamlit as st


UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")

MEETING_FORMATS = ("Zoom", "Telegram", "WhatsApp")
MEETING_STATUSES = (
    "Ожидает подтверждения",
    "Подтверждена",
    "Перенесена",
    "Отменена",
    "Состоялась",
)

COMMON_TIMEZONES = {
    "Москва — МСК": "Europe/Moscow",
    "Германия / Центральная Европа": "Europe/Berlin",
    "Казахстан — Алматы / Астана": "Asia/Almaty",
    "Киргизия — Бишкек": "Asia/Bishkek",
    "Узбекистан — Ташкент": "Asia/Tashkent",
    "Беларусь — Минск": "Europe/Minsk",
    "Украина — Киев": "Europe/Kyiv",
    "Армения — Ереван": "Asia/Yerevan",
    "Грузия — Тбилиси": "Asia/Tbilisi",
    "Азербайджан — Баку": "Asia/Baku",
    "Другая часовая зона": "",
}

MONTH_NAMES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

WEEKDAY_NAMES = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)


class CalendarStorageError(RuntimeError):
    """Ошибка чтения или записи календаря."""


def _supabase_headers() -> dict[str, str]:
    secret_key = str(st.secrets.get("SUPABASE_SECRET_KEY", "")).strip()
    if not secret_key:
        raise CalendarStorageError(
            "SUPABASE_SECRET_KEY не найден в Streamlit Secrets."
        )

    return {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }


def _supabase_url() -> str:
    base_url = str(st.secrets.get("SUPABASE_URL", "")).strip().rstrip("/")
    if not base_url:
        raise CalendarStorageError(
            "SUPABASE_URL не найден в Streamlit Secrets."
        )
    return base_url


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def list_meetings(
    owner_telegram_id: int,
    range_start: datetime,
    range_end: datetime,
) -> list[dict[str, Any]]:
    """Возвращает встречи владельца, пересекающие указанный период."""

    response = requests.get(
        f"{_supabase_url()}/rest/v1/agency_meetings",
        headers=_supabase_headers(),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "start_at": f"lt.{_utc_iso(range_end)}",
            "end_at": f"gt.{_utc_iso(range_start)}",
            "select": "*",
            "order": "start_at.asc",
        },
        timeout=30,
    )

    if response.status_code >= 400:
        details = response.text[:500]
        if response.status_code in {404, 400} and "agency_meetings" in details:
            raise CalendarStorageError(
                "Таблица agency_meetings ещё не создана в Supabase."
            )
        raise CalendarStorageError(
            f"Не удалось загрузить встречи: {details}"
        )

    rows = response.json()
    return rows if isinstance(rows, list) else []


def create_meeting(payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{_supabase_url()}/rest/v1/agency_meetings",
        headers={
            **_supabase_headers(),
            "Prefer": "return=representation",
        },
        json=payload,
        timeout=30,
    )

    if response.status_code >= 400:
        raise CalendarStorageError(
            f"Не удалось сохранить встречу: {response.text[:500]}"
        )

    rows = response.json()
    if not rows:
        raise CalendarStorageError(
            "Supabase не вернул сохранённую встречу."
        )
    return rows[0]


def update_meeting(meeting_id: str, changes: dict[str, Any]) -> None:
    response = requests.patch(
        f"{_supabase_url()}/rest/v1/agency_meetings",
        headers={
            **_supabase_headers(),
            "Prefer": "return=minimal",
        },
        params={"id": f"eq.{meeting_id}"},
        json={
            **changes,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        timeout=30,
    )

    if response.status_code >= 400:
        raise CalendarStorageError(
            f"Не удалось обновить встречу: {response.text[:500]}"
        )




def _parse_clock(value: Any) -> dt_time:
    """Разбирает время из Supabase TIME (например 17:00:00)."""
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) < 2:
        raise ValueError(f"Некорректное время: {raw}")
    return dt_time(int(parts[0]), int(parts[1]))


def list_recurring_blocks(owner_telegram_id: int) -> list[dict[str, Any]]:
    """Возвращает постоянную занятость владельца по дням недели."""
    response = requests.get(
        f"{_supabase_url()}/rest/v1/agency_calendar_blocks",
        headers=_supabase_headers(),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "active": "eq.true",
            "select": "*",
            "order": "weekday.asc,start_time.asc",
        },
        timeout=30,
    )
    if response.status_code == 404:
        raise CalendarStorageError(
            "Таблица agency_calendar_blocks ещё не создана в Supabase."
        )
    if response.status_code >= 400:
        raise CalendarStorageError(
            f"Не удалось загрузить постоянную занятость: {response.text[:500]}"
        )
    rows = response.json()
    return rows if isinstance(rows, list) else []


def save_recurring_blocks(
    owner_telegram_id: int,
    *,
    weekdays: list[int],
    title: str,
    start_time: dt_time,
    end_time: dt_time,
    meeting_format: str,
    meeting_link: str = "",
    notes: str = "",
) -> None:
    if not weekdays:
        raise CalendarStorageError("Выберите хотя бы один день недели.")
    if end_time <= start_time:
        raise CalendarStorageError("Окончание должно быть позже начала.")

    payload = [
        {
            "owner_telegram_id": int(owner_telegram_id),
            "title": title.strip() or "Занято",
            "weekday": int(weekday),
            "start_time": start_time.strftime("%H:%M:%S"),
            "end_time": end_time.strftime("%H:%M:%S"),
            "timezone": "Europe/Moscow",
            "meeting_format": meeting_format or None,
            "meeting_link": meeting_link.strip() or None,
            "notes": notes.strip() or None,
            "active": True,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for weekday in sorted(set(int(item) for item in weekdays))
    ]

    response = requests.post(
        (
            f"{_supabase_url()}/rest/v1/agency_calendar_blocks"
            "?on_conflict=owner_telegram_id,weekday,start_time,end_time,title"
        ),
        headers={
            **_supabase_headers(),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise CalendarStorageError(
            f"Не удалось сохранить постоянную занятость: {response.text[:500]}"
        )


def delete_recurring_block(block_id: str) -> None:
    response = requests.delete(
        f"{_supabase_url()}/rest/v1/agency_calendar_blocks",
        headers={
            **_supabase_headers(),
            "Prefer": "return=minimal",
        },
        params={"id": f"eq.{block_id}"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise CalendarStorageError(
            f"Не удалось удалить постоянную занятость: {response.text[:500]}"
        )


def _has_recurring_conflict(
    start_utc: datetime,
    end_utc: datetime,
    blocks: list[dict[str, Any]],
) -> bool:
    """Проверяет пересечение со стандартной повторяющейся занятостью по МСК."""
    start_msk = start_utc.astimezone(MSK)
    end_msk = end_utc.astimezone(MSK)
    if start_msk.date() != end_msk.date():
        # Рабочие встречи Агентства короткие; для перехода через полночь
        # считаем слот занятым консервативно.
        return True

    for block in blocks:
        if not bool(block.get("active", True)):
            continue
        try:
            weekday = int(block.get("weekday"))
            block_start = _parse_clock(block.get("start_time"))
            block_end = _parse_clock(block.get("end_time"))
        except (TypeError, ValueError):
            continue
        if weekday != start_msk.weekday():
            continue
        candidate_start = start_msk.timetz().replace(tzinfo=None)
        candidate_end = end_msk.timetz().replace(tzinfo=None)
        if candidate_start < block_end and candidate_end > block_start:
            return True
    return False


def _period_for_msk_day(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dt_time.min, tzinfo=MSK)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _meeting_datetimes(meeting: dict[str, Any]) -> tuple[datetime, datetime]:
    return (
        _parse_datetime(meeting["start_at"]),
        _parse_datetime(meeting["end_at"]),
    )


def _meeting_is_active(meeting: dict[str, Any]) -> bool:
    return meeting.get("status") not in {"Отменена", "Перенесена"}


def _has_conflict(
    start_utc: datetime,
    end_utc: datetime,
    meetings: list[dict[str, Any]],
) -> bool:
    for meeting in meetings:
        if not _meeting_is_active(meeting):
            continue
        existing_start, existing_end = _meeting_datetimes(meeting)
        if start_utc < existing_end and end_utc > existing_start:
            return True
    return False


def _slot_label(
    start_utc: datetime,
    duration_minutes: int,
    contact_tz: ZoneInfo,
    contact_city: str,
) -> str:
    start_msk = start_utc.astimezone(MSK)
    start_local = start_utc.astimezone(contact_tz)
    end_msk = (start_utc + timedelta(minutes=duration_minutes)).astimezone(MSK)
    local_label = contact_city.strip() or contact_tz.key
    return (
        f"{WEEKDAY_NAMES[start_msk.weekday()]}, "
        f"{start_msk.strftime('%d.%m.%Y')} · "
        f"{start_msk.strftime('%H:%M')}–{end_msk.strftime('%H:%M')} МСК · "
        f"для человека: {start_local.strftime('%H:%M')} ({local_label})"
    )


def find_available_slots(
    owner_telegram_id: int,
    search_start_date: date,
    days_ahead: int,
    work_start: dt_time,
    work_end: dt_time,
    duration_minutes: int,
    contact_timezone: str,
    local_start_hour: int,
    local_end_hour: int,
    include_weekends: bool,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Находит первые свободные слоты с учётом местного времени человека."""

    try:
        contact_tz = ZoneInfo(contact_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "Часовая зона не распознана. Укажите, например, Europe/Berlin."
        ) from exc

    range_start_msk = datetime.combine(
        search_start_date,
        dt_time.min,
        tzinfo=MSK,
    )
    range_end_msk = range_start_msk + timedelta(days=max(1, days_ahead))
    meetings = list_meetings(
        owner_telegram_id,
        range_start_msk.astimezone(UTC),
        range_end_msk.astimezone(UTC),
    )
    blocks = list_recurring_blocks(owner_telegram_id)

    slots: list[dict[str, Any]] = []
    now_utc = datetime.now(UTC)

    for day_offset in range(max(1, days_ahead)):
        slot_day = search_start_date + timedelta(days=day_offset)
        if not include_weekends and slot_day.weekday() >= 5:
            continue

        cursor_msk = datetime.combine(slot_day, work_start, tzinfo=MSK)
        day_end_msk = datetime.combine(slot_day, work_end, tzinfo=MSK)

        while cursor_msk + timedelta(minutes=duration_minutes) <= day_end_msk:
            start_utc = cursor_msk.astimezone(UTC)
            end_utc = start_utc + timedelta(minutes=duration_minutes)
            local_start = start_utc.astimezone(contact_tz)
            local_end = end_utc.astimezone(contact_tz)

            local_is_comfortable = (
                local_start.date() == local_end.date()
                and local_start_hour <= local_start.hour
                and (
                    local_end.hour < local_end_hour
                    or (
                        local_end.hour == local_end_hour
                        and local_end.minute == 0
                    )
                )
            )

            if (
                start_utc > now_utc
                and local_is_comfortable
                and not _has_conflict(start_utc, end_utc, meetings)
                and not _has_recurring_conflict(start_utc, end_utc, blocks)
            ):
                slots.append(
                    {
                        "start_at": _utc_iso(start_utc),
                        "end_at": _utc_iso(end_utc),
                    }
                )
                if len(slots) >= limit:
                    return slots

            cursor_msk += timedelta(minutes=30)

    return slots


def _meeting_status_icon(status: str) -> str:
    return {
        "Ожидает подтверждения": "🟡",
        "Подтверждена": "🟢",
        "Перенесена": "🔵",
        "Отменена": "⚫",
        "Состоялась": "✅",
    }.get(status, "📅")


def _render_meeting_card(meeting: dict[str, Any], key_prefix: str) -> None:
    start_utc, end_utc = _meeting_datetimes(meeting)
    start_msk = start_utc.astimezone(MSK)
    end_msk = end_utc.astimezone(MSK)

    timezone_name = str(meeting.get("contact_timezone") or "Europe/Moscow")
    try:
        contact_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        contact_tz = MSK

    local_start = start_utc.astimezone(contact_tz)
    local_end = end_utc.astimezone(contact_tz)
    person_name = str(meeting.get("contact_name") or "Без имени")
    city = str(meeting.get("contact_city") or timezone_name)
    status = str(meeting.get("status") or "Подтверждена")
    meeting_format = str(meeting.get("meeting_format") or "Не указан")
    meeting_id = str(meeting.get("id"))

    with st.container(border=True):
        st.markdown(
            f"### {start_msk.strftime('%H:%M')} МСК · {person_name}"
        )
        st.write(
            f"{_meeting_status_icon(status)} **{status}** · "
            f"{meeting_format} · "
            f"для человека: {local_start.strftime('%H:%M')}–"
            f"{local_end.strftime('%H:%M')} ({city})"
        )

        username = str(meeting.get("contact_username") or "").strip()
        if username:
            st.caption(f"Telegram: @{username.lstrip('@')}")

        notes = str(meeting.get("notes") or "").strip()
        if notes:
            st.caption(notes)

        meeting_link = str(meeting.get("meeting_link") or "").strip()
        if meeting_link:
            st.link_button(
                f"Открыть {meeting_format}",
                meeting_link,
                width="stretch",
            )

        action_columns = st.columns(3)

        if status == "Ожидает подтверждения":
            if action_columns[0].button(
                "✅ Подтвердить",
                key=f"{key_prefix}_confirm_{meeting_id}",
                width="stretch",
            ):
                update_meeting(meeting_id, {"status": "Подтверждена"})
                st.rerun()
        elif status == "Подтверждена":
            if action_columns[0].button(
                "✅ Состоялась",
                key=f"{key_prefix}_done_{meeting_id}",
                width="stretch",
            ):
                update_meeting(meeting_id, {"status": "Состоялась"})
                st.rerun()

        if status not in {"Отменена", "Состоялась"}:
            if action_columns[1].button(
                "↪️ Перенести",
                key=f"{key_prefix}_move_{meeting_id}",
                width="stretch",
            ):
                update_meeting(meeting_id, {"status": "Перенесена"})
                st.info(
                    "Старая встреча отмечена как перенесённая. "
                    "Создайте новый подтверждённый вариант во вкладке «Назначить»."
                )
                st.rerun()

            if action_columns[2].button(
                "Отменить",
                key=f"{key_prefix}_cancel_{meeting_id}",
                width="stretch",
            ):
                update_meeting(meeting_id, {"status": "Отменена"})
                st.rerun()


def _render_recurring_block_card(block: dict[str, Any], key_prefix: str) -> None:
    title = str(block.get("title") or "Занято")
    start_label = str(block.get("start_time") or "")[:5]
    end_label = str(block.get("end_time") or "")[:5]
    meeting_format = str(block.get("meeting_format") or "").strip()
    link = str(block.get("meeting_link") or "").strip()
    notes = str(block.get("notes") or "").strip()
    with st.container(border=True):
        st.markdown(f"### 🔒 {start_label}–{end_label} МСК · {title}")
        if meeting_format:
            st.write(f"**Постоянная занятость** · {meeting_format}")
        else:
            st.write("**Постоянная занятость**")
        if notes:
            st.caption(notes)
        if link:
            st.link_button(
                f"Открыть {meeting_format or 'ссылку'}",
                link,
                width="stretch",
            )


def _render_day_meetings(
    owner_telegram_id: int,
    day: date,
    key_prefix: str,
) -> None:
    range_start, range_end = _period_for_msk_day(day)
    meetings = list_meetings(owner_telegram_id, range_start, range_end)
    blocks = [
        block for block in list_recurring_blocks(owner_telegram_id)
        if int(block.get("weekday", -1)) == day.weekday()
    ]

    st.markdown(
        f"#### {WEEKDAY_NAMES[day.weekday()]}, {day.strftime('%d.%m.%Y')}"
    )

    if not meetings and not blocks:
        st.caption("На этот день встреч и постоянной занятости нет.")
        return

    for block in blocks:
        _render_recurring_block_card(block, key_prefix)

    for meeting in meetings:
        _render_meeting_card(meeting, key_prefix)


def _render_calendar_view(owner_telegram_id: int) -> None:
    view = st.segmented_control(
        "Вид календаря",
        ["День", "Неделя", "Месяц"],
        default="Неделя",
        required=True,
        key="agency_calendar_view",
    )

    focus_key = "agency_calendar_focus_date"
    if focus_key not in st.session_state:
        st.session_state[focus_key] = datetime.now(MSK).date()

    navigation = st.columns([1, 4, 1])
    step = 1 if view == "День" else 7 if view == "Неделя" else 31

    if navigation[0].button("◀", key="calendar_previous", width="stretch"):
        current = st.session_state[focus_key]
        if view == "Месяц":
            first = current.replace(day=1)
            previous = first - timedelta(days=1)
            st.session_state[focus_key] = previous.replace(day=1)
        else:
            st.session_state[focus_key] = current - timedelta(days=step)
        st.rerun()

    selected_date = navigation[1].date_input(
        "Дата",
        value=st.session_state[focus_key],
        label_visibility="collapsed",
        key="calendar_date_picker",
    )
    if selected_date != st.session_state[focus_key]:
        st.session_state[focus_key] = selected_date
        st.rerun()

    if navigation[2].button("▶", key="calendar_next", width="stretch"):
        current = st.session_state[focus_key]
        if view == "Месяц":
            next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
            st.session_state[focus_key] = next_month
        else:
            st.session_state[focus_key] = current + timedelta(days=step)
        st.rerun()

    focus_date = st.session_state[focus_key]

    if view == "День":
        _render_day_meetings(owner_telegram_id, focus_date, "day")
        return

    if view == "Неделя":
        monday = focus_date - timedelta(days=focus_date.weekday())
        sunday = monday + timedelta(days=6)
        st.markdown(
            f"#### Неделя: {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m.%Y')}"
        )
        for offset in range(7):
            current_day = monday + timedelta(days=offset)
            with st.expander(
                f"{WEEKDAY_NAMES[current_day.weekday()]} · "
                f"{current_day.strftime('%d.%m')}",
                expanded=current_day == datetime.now(MSK).date(),
            ):
                _render_day_meetings(
                    owner_telegram_id,
                    current_day,
                    f"week_{offset}",
                )
        return

    year = focus_date.year
    month = focus_date.month
    month_start = datetime(year, month, 1, tzinfo=MSK)
    next_month = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    meetings = list_meetings(
        owner_telegram_id,
        month_start.astimezone(UTC),
        next_month.astimezone(UTC),
    )

    meetings_by_day: dict[date, list[dict[str, Any]]] = {}
    for meeting in meetings:
        meeting_day = _parse_datetime(meeting["start_at"]).astimezone(MSK).date()
        meetings_by_day.setdefault(meeting_day, []).append(meeting)

    st.markdown(f"#### {MONTH_NAMES[month]} {year}")
    headers = st.columns(7)
    for index, label in enumerate(("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")):
        headers[index].markdown(f"**{label}**")

    month_weeks = month_calendar.Calendar(firstweekday=0).monthdatescalendar(
        year,
        month,
    )
    for week_index, week in enumerate(month_weeks):
        columns = st.columns(7)
        for day_index, current_day in enumerate(week):
            count = len(meetings_by_day.get(current_day, []))
            if current_day.month != month:
                columns[day_index].caption(str(current_day.day))
            elif count:
                columns[day_index].markdown(
                    f"**{current_day.day}**  \n📅 {count}"
                )
            else:
                columns[day_index].markdown(f"**{current_day.day}**")

    active_days = sorted(meetings_by_day)
    if not active_days:
        st.caption("В этом месяце встреч пока нет.")
        return

    st.markdown("#### Встречи месяца")
    for day_index, active_day in enumerate(active_days):
        with st.expander(
            f"{WEEKDAY_NAMES[active_day.weekday()]}, "
            f"{active_day.strftime('%d.%m.%Y')} · "
            f"{len(meetings_by_day[active_day])} встреч(и)",
        ):
            for meeting in meetings_by_day[active_day]:
                _render_meeting_card(
                    meeting,
                    f"month_{day_index}",
                )



def _render_recurring_blocks(owner_telegram_id: int) -> None:
    st.markdown("### 🔒 Постоянная занятость")
    st.caption(
        "Эти интервалы повторяются каждую неделю и считаются занятыми "
        "и для календаря, и для Неоны. Время задаётся по МСК."
    )

    weekday_options = list(range(7))
    selected_weekdays = st.multiselect(
        "Дни недели",
        options=weekday_options,
        default=[0, 1, 2, 3, 4],
        format_func=lambda item: WEEKDAY_NAMES[item],
        key="calendar_block_weekdays",
    )
    title = st.text_input(
        "Название",
        value="Командная встреча",
        key="calendar_block_title",
    )
    time_columns = st.columns(2)
    block_start = time_columns[0].time_input(
        "Начало по МСК",
        value=dt_time(17, 0),
        step=1800,
        key="calendar_block_start",
    )
    block_end = time_columns[1].time_input(
        "Окончание по МСК",
        value=dt_time(18, 0),
        step=1800,
        key="calendar_block_end",
    )
    block_format = st.selectbox(
        "Формат",
        MEETING_FORMATS,
        key="calendar_block_format",
    )
    block_link = st.text_input(
        "Постоянная ссылка — необязательно",
        placeholder="Например, постоянная ссылка Zoom",
        key="calendar_block_link",
    )
    block_notes = st.text_area(
        "Заметка — необязательно",
        placeholder="Например: ежедневная командная встреча",
        key="calendar_block_notes",
    )

    if st.button(
        "💾 Сохранить постоянную занятость",
        type="primary",
        width="stretch",
        key="calendar_save_recurring_block",
    ):
        try:
            save_recurring_blocks(
                owner_telegram_id,
                weekdays=selected_weekdays,
                title=title,
                start_time=block_start,
                end_time=block_end,
                meeting_format=block_format,
                meeting_link=block_link,
                notes=block_notes,
            )
            st.success("Постоянная занятость сохранена.")
            st.rerun()
        except CalendarStorageError as exc:
            st.error(str(exc))

    st.divider()
    try:
        blocks = list_recurring_blocks(owner_telegram_id)
    except CalendarStorageError as exc:
        st.error(str(exc))
        return

    if not blocks:
        st.caption("Постоянной занятости пока нет.")
        return

    st.markdown("#### Уже сохранено")
    for block in blocks:
        block_id = str(block.get("id") or "")
        weekday = int(block.get("weekday", 0))
        title_label = str(block.get("title") or "Занято")
        start_label = str(block.get("start_time") or "")[:5]
        end_label = str(block.get("end_time") or "")[:5]
        with st.container(border=True):
            st.write(
                f"**{WEEKDAY_NAMES[weekday]} · {start_label}–{end_label} МСК** — "
                f"{title_label} · {block.get('meeting_format') or 'без формата'}"
            )
            if st.button(
                "Удалить",
                key=f"calendar_delete_block_{block_id}",
            ):
                try:
                    delete_recurring_block(block_id)
                    st.rerun()
                except CalendarStorageError as exc:
                    st.error(str(exc))


def _render_new_meeting(owner_telegram_id: int, owner_name: str) -> None:
    st.markdown("### Подбор свободного времени")
    st.caption(
        "Календарь хранит время по МСК, а человеку показывает его местное "
        "время на конкретную дату с учётом летнего и зимнего времени."
    )

    contact_name = st.text_input(
        "Имя человека",
        key="calendar_contact_name",
    )
    contact_username = st.text_input(
        "Telegram username — необязательно",
        placeholder="username без @",
        key="calendar_contact_username",
    )
    contact_city = st.text_input(
        "Город или страна человека",
        placeholder="Например: Берлин, Германия",
        key="calendar_contact_city",
    )

    timezone_choice = st.selectbox(
        "Часовой пояс человека",
        list(COMMON_TIMEZONES),
        key="calendar_timezone_choice",
    )
    contact_timezone = COMMON_TIMEZONES[timezone_choice]
    if not contact_timezone:
        contact_timezone = st.text_input(
            "Введите часовую зону",
            placeholder="Например: America/New_York",
            key="calendar_custom_timezone",
        ).strip()

    search_columns = st.columns(3)
    search_start_date = search_columns[0].date_input(
        "Искать начиная с",
        value=datetime.now(MSK).date(),
        min_value=datetime.now(MSK).date(),
        key="calendar_search_start_date",
    )
    days_ahead = search_columns[1].selectbox(
        "Период поиска",
        [3, 7, 14, 21, 30],
        index=2,
        format_func=lambda value: f"{value} дней",
        key="calendar_days_ahead",
    )
    duration_minutes = search_columns[2].selectbox(
        "Длительность",
        [15, 30, 45, 60],
        index=1,
        format_func=lambda value: f"{value} минут",
        key="calendar_duration",
    )

    work_columns = st.columns(2)
    work_start = work_columns[0].time_input(
        "Начало встреч по МСК",
        value=dt_time(10, 0),
        step=1800,
        key="calendar_work_start",
    )
    work_end = work_columns[1].time_input(
        "Окончание встреч по МСК",
        value=dt_time(20, 0),
        step=1800,
        key="calendar_work_end",
    )

    local_columns = st.columns(2)
    local_start_hour = local_columns[0].selectbox(
        "Не раньше по местному времени",
        list(range(7, 15)),
        index=2,
        format_func=lambda hour: f"{hour:02d}:00",
        key="calendar_local_start",
    )
    local_end_hour = local_columns[1].selectbox(
        "Не позже по местному времени",
        list(range(17, 24)),
        index=4,
        format_func=lambda hour: f"{hour:02d}:00",
        key="calendar_local_end",
    )
    include_weekends = st.checkbox(
        "Предлагать субботу и воскресенье",
        value=False,
        key="calendar_include_weekends",
    )

    slots_key = f"calendar_suggested_slots_{owner_telegram_id}"
    slot_context_key = f"calendar_slot_context_{owner_telegram_id}"

    if st.button(
        "🔎 Найти 3 свободных варианта",
        type="primary",
        width="stretch",
        key="calendar_find_slots",
    ):
        if not contact_name.strip():
            st.warning("Сначала укажите имя человека.")
        elif not contact_timezone:
            st.warning("Укажите часовой пояс человека.")
        elif work_start >= work_end:
            st.warning("Окончание рабочего окна должно быть позже начала.")
        elif local_start_hour >= local_end_hour:
            st.warning("Местное окончание должно быть позже начала.")
        else:
            try:
                slots = find_available_slots(
                    owner_telegram_id=owner_telegram_id,
                    search_start_date=search_start_date,
                    days_ahead=days_ahead,
                    work_start=work_start,
                    work_end=work_end,
                    duration_minutes=duration_minutes,
                    contact_timezone=contact_timezone,
                    local_start_hour=local_start_hour,
                    local_end_hour=local_end_hour,
                    include_weekends=include_weekends,
                    limit=3,
                )
                st.session_state[slots_key] = slots
                st.session_state[slot_context_key] = {
                    "contact_name": contact_name.strip(),
                    "contact_username": contact_username.strip().lstrip("@"),
                    "contact_city": contact_city.strip(),
                    "contact_timezone": contact_timezone,
                    "duration_minutes": duration_minutes,
                }
                if not slots:
                    st.warning(
                        "В выбранном периоде не найдено трёх удобных свободных "
                        "вариантов. Расширьте период или рабочее окно."
                    )
                else:
                    st.success("Свободные варианты найдены.")
            except (CalendarStorageError, ValueError) as exc:
                st.error(str(exc))

    slots = st.session_state.get(slots_key, [])
    context = st.session_state.get(slot_context_key, {})

    if not slots:
        return

    try:
        contact_tz = ZoneInfo(context["contact_timezone"])
    except (KeyError, ZoneInfoNotFoundError):
        st.warning("Часовая зона изменилась. Выполните поиск времени заново.")
        return

    labels = {
        index: _slot_label(
            _parse_datetime(slot["start_at"]),
            int(context["duration_minutes"]),
            contact_tz,
            str(context.get("contact_city") or ""),
        )
        for index, slot in enumerate(slots)
    }

    selected_index = st.radio(
        "Предложите человеку один из вариантов",
        options=list(labels),
        format_func=lambda index: labels[index],
        key="calendar_selected_slot",
    )
    meeting_format = st.selectbox(
        "Формат встречи",
        MEETING_FORMATS,
        key="calendar_meeting_format",
    )
    meeting_link = st.text_input(
        "Ссылка на встречу — необязательно",
        placeholder="Ссылка Zoom, Telegram или WhatsApp",
        key="calendar_meeting_link",
    )
    notes = st.text_area(
        "Короткая заметка — необязательно",
        placeholder="Что важно обсудить",
        key="calendar_meeting_notes",
    )

    confirmed = st.checkbox(
        "Человек подтвердил этот день, время и формат",
        key="calendar_person_confirmed",
    )

    if st.button(
        "✅ Записать подтверждённую встречу",
        type="primary",
        width="stretch",
        disabled=not confirmed,
        key="calendar_create_meeting",
    ):
        selected_slot = slots[selected_index]
        start_utc = _parse_datetime(selected_slot["start_at"])
        end_utc = _parse_datetime(selected_slot["end_at"])

        try:
            latest_meetings = list_meetings(
                owner_telegram_id,
                start_utc - timedelta(minutes=1),
                end_utc + timedelta(minutes=1),
            )
            latest_blocks = list_recurring_blocks(owner_telegram_id)
            if (
                _has_conflict(start_utc, end_utc, latest_meetings)
                or _has_recurring_conflict(start_utc, end_utc, latest_blocks)
            ):
                st.error(
                    "Этот интервал уже занят. Нажмите «Найти 3 свободных "
                    "варианта» ещё раз."
                )
                return

            created = create_meeting(
                {
                    "owner_telegram_id": int(owner_telegram_id),
                    "owner_name": owner_name,
                    "contact_name": context["contact_name"],
                    "contact_username": context.get("contact_username") or None,
                    "contact_city": context.get("contact_city") or None,
                    "contact_timezone": context["contact_timezone"],
                    "start_at": _utc_iso(start_utc),
                    "end_at": _utc_iso(end_utc),
                    "meeting_format": meeting_format,
                    "meeting_link": meeting_link.strip() or None,
                    "status": "Подтверждена",
                    "notes": notes.strip() or None,
                    "source": "Внутренний календарь Агентства W",
                }
            )
            st.session_state.pop(slots_key, None)
            st.session_state.pop(slot_context_key, None)
            st.success(
                "Встреча записана в календарь: "
                + _slot_label(
                    _parse_datetime(created["start_at"]),
                    int(context["duration_minutes"]),
                    contact_tz,
                    str(context.get("contact_city") or ""),
                )
            )
        except CalendarStorageError as exc:
            st.error(str(exc))


def render_agency_calendar(owner_telegram_id: int, owner_name: str) -> None:
    """Полный раздел внутреннего календаря Агентства W."""

    st.markdown("### 📅 Календарь встреч")
    st.caption(
        "Единый стандарт — московское время. Для каждого человека "
        "местное время рассчитывается заново на дату встречи."
    )

    try:
        now = datetime.now(UTC)
        list_meetings(
            owner_telegram_id,
            now - timedelta(minutes=1),
            now + timedelta(minutes=1),
        )
    except CalendarStorageError as exc:
        st.error(str(exc))
        st.info(
            "Откройте Supabase → SQL Editor и выполните файл "
            "agency_meetings.sql из комплекта обновления. После этого "
            "вернитесь на сайт и обновите страницу."
        )
        return

    calendar_tab, create_tab, recurring_tab = st.tabs(
        ["Календарь", "Назначить встречу", "Постоянная занятость"]
    )
    with calendar_tab:
        _render_calendar_view(owner_telegram_id)
    with create_tab:
        _render_new_meeting(owner_telegram_id, owner_name)
    with recurring_tab:
        _render_recurring_blocks(owner_telegram_id)


def render_today_meetings_compact(owner_telegram_id: int) -> None:
    """Кратко показывает сегодняшние встречи и постоянную занятость на «Мой день»."""

    today_msk = datetime.now(MSK).date()
    range_start, range_end = _period_for_msk_day(today_msk)

    try:
        meetings = list_meetings(owner_telegram_id, range_start, range_end)
        blocks = [
            block for block in list_recurring_blocks(owner_telegram_id)
            if int(block.get("weekday", -1)) == today_msk.weekday()
        ]
    except CalendarStorageError:
        st.caption("Календарь ещё не подключён полностью.")
        return

    active = [meeting for meeting in meetings if _meeting_is_active(meeting)]
    if not active and not blocks:
        st.caption("На сегодня встреч нет.")
        return

    rows_shown = 0
    for block in blocks:
        if rows_shown >= 5:
            break
        st.write(
            f"**{str(block.get('start_time') or '')[:5]} МСК** — "
            f"{block.get('title') or 'Занято'} · "
            f"{block.get('meeting_format') or 'Постоянная занятость'} · 🔒"
        )
        rows_shown += 1

    for meeting in active:
        if rows_shown >= 5:
            break
        start_utc, _ = _meeting_datetimes(meeting)
        start_msk = start_utc.astimezone(MSK)
        st.write(
            f"**{start_msk.strftime('%H:%M')} МСК** — "
            f"{meeting.get('contact_name', 'Без имени')} · "
            f"{meeting.get('meeting_format', 'Формат не указан')} · "
            f"{meeting.get('status', '')}"
        )
        rows_shown += 1

    extra = len(active) + len(blocks) - rows_shown
    if extra > 0:
        st.caption(f"И ещё событий: {extra}")

