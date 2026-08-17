from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
import streamlit as st

import agency_calendar
try:
    from team_center import publish_structure_message, structure_member_ids
except ImportError:
    publish_structure_message = None

    def structure_member_ids(owner_telegram_id: int) -> list[int]:
        return []

from workspace_persistence import persist_workspace_if_changed


UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")

WORK_START = dt_time(10, 0)
WORK_END = dt_time(20, 0)
MEETING_MINUTES = 30
BUFFER_MINUTES = 60


def _supabase_config() -> tuple[str, str]:
    return (
        str(st.secrets.get("SUPABASE_URL") or "").rstrip("/"),
        str(st.secrets.get("SUPABASE_SECRET_KEY") or ""),
    )


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


def _task_key(owner_id: int) -> str:
    return f"stagirite_tasks_fallback_{int(owner_id)}"


def _db_available() -> bool:
    url, key = _supabase_config()
    return bool(url and key)



def _settings_key(owner_id: int) -> str:
    return f"stagirite_settings_{int(owner_id)}"


def _load_stagirite_settings(owner_id: int) -> dict[str, Any]:
    owner_id = int(owner_id)

    if _db_available():
        url, _ = _supabase_config()
        try:
            response = requests.get(
                f"{url}/rest/v1/agency_stagirite_tasks",
                headers=_db_headers(),
                params={
                    "owner_telegram_id": f"eq.{owner_id}",
                    "task_kind": "eq.settings",
                    "select": "id,result,updated_at",
                    "order": "updated_at.desc",
                    "limit": 1,
                },
                timeout=20,
            )
            if response.ok:
                rows = response.json()
                if isinstance(rows, list) and rows:
                    result = rows[0].get("result")
                    if isinstance(result, dict):
                        st.session_state[_settings_key(owner_id)] = dict(result)
                        return dict(result)
        except Exception:
            pass

    return dict(
        st.session_state.get(
            _settings_key(owner_id),
            {},
        )
        or {}
    )


def _save_stagirite_settings(owner_id: int, settings: dict[str, Any]) -> None:
    owner_id = int(owner_id)
    clean = {
        "zoom_link": str(settings.get("zoom_link") or "").strip(),
        "zoom_note": str(settings.get("zoom_note") or "").strip(),
    }
    st.session_state[_settings_key(owner_id)] = clean

    if not _db_available():
        return

    url, _ = _supabase_config()
    try:
        lookup = requests.get(
            f"{url}/rest/v1/agency_stagirite_tasks",
            headers=_db_headers(),
            params={
                "owner_telegram_id": f"eq.{owner_id}",
                "task_kind": "eq.settings",
                "select": "id",
                "order": "updated_at.desc",
                "limit": 1,
            },
            timeout=20,
        )
        rows = lookup.json() if lookup.ok else []

        payload = {
            "owner_telegram_id": owner_id,
            "assignment": "Системные настройки Стагирита",
            "task_kind": "settings",
            "status": "Настройки",
            "plan": {},
            "result": clean,
            "updated_at": datetime.now(UTC).isoformat(),
        }

        if isinstance(rows, list) and rows:
            setting_id = str(rows[0].get("id") or "")
            if setting_id:
                response = requests.patch(
                    f"{url}/rest/v1/agency_stagirite_tasks",
                    headers=_db_headers("return=minimal"),
                    params={"id": f"eq.{setting_id}"},
                    json=payload,
                    timeout=20,
                )
                response.raise_for_status()
                return

        payload["created_at"] = datetime.now(UTC).isoformat()
        response = requests.post(
            f"{url}/rest/v1/agency_stagirite_tasks",
            headers=_db_headers("return=minimal"),
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        # Ссылка остаётся хотя бы в текущей сессии.
        return


def _valid_zoom_link(value: str) -> bool:
    value = str(value or "").strip().lower()
    return (
        not value
        or (
            value.startswith("https://")
            and "zoom" in value
        )
    )


def _agency_current_release_brief() -> str:
    """Факты, которые Стагириту разрешено использовать в контенте."""
    return """
ФАКТЫ О ТЕКУЩЕЙ ВЕРСИИ АГЕНТСТВА W (релиз 16.08.2026):

ГОТОВО И МОЖНО ПОКАЗЫВАТЬ ПАРТНЁРАМ:
- Подключён Стагирит — заместитель Директора и координатор работы Агентства.
- Стагирит принимает поручение как желаемый результат, а не только как нажатие кнопки.
- Он понимает обычные формулировки о встречах, Zoom, кандидатах и контенте.
- Он проверяет календарь и находит свободное время для встречи.
- Он передаёт работу Неонии и Неоне и отслеживает цель до назначенной встречи.
- Неония для холодного списка проверяет Telegram-активность и предлагает только тех,
  кто был онлайн сегодня или вчера.
- Неония не предлагает для нового первого сообщения людей с явным отказом,
  уже начатым диалогом или слишком недавним обращением.
- Неона готовит персональное первое сообщение. Перед первой отправкой решение
  и утверждение остаются за владельцем кабинета.
- После ответа человека Неона продолжает диалог по контексту и ведёт к
  осознанной встрече.
- Стагирит умеет готовить посты, анонсы и сообщения по поручению владельца.
- Главный принцип Агентства: ИИ берёт на себя рутину, человек принимает решения
  и строит отношения.
- Девиз: «Мы создаём своё настоящее».

ВАЖНО:
- WhatsApp и другие дополнительные каналы находятся в дорожной карте и ещё
  не должны описываться как уже подключённые.
- Нельзя обещать, что Агентство гарантирует встречи, партнёров или доход.
- Нельзя писать, что Стагирит сам принимает человеческие решения вместо владельца.
""".strip()


def _load_tasks(owner_id: int) -> tuple[list[dict[str, Any]], bool]:
    """Возвращает поручения. При отсутствии SQL-таблицы использует session_state."""
    if _db_available():
        url, _ = _supabase_config()
        try:
            response = requests.get(
                f"{url}/rest/v1/agency_stagirite_tasks",
                headers=_db_headers(),
                params={
                    "owner_telegram_id": f"eq.{int(owner_id)}",
                    "select": "*",
                    "order": "created_at.desc",
                    "limit": 30,
                },
                timeout=20,
            )
            if response.ok:
                rows = response.json()
                rows = rows if isinstance(rows, list) else []
                rows = [
                    row
                    for row in rows
                    if str(row.get("task_kind") or "") != "settings"
                ]
                return rows, True
            if response.status_code not in {400, 404}:
                response.raise_for_status()
        except Exception:
            pass

    fallback_rows = list(st.session_state.get(_task_key(owner_id), []))
    fallback_rows = [
        row
        for row in fallback_rows
        if str(row.get("task_kind") or "") != "settings"
    ]
    return fallback_rows, False


def _save_task(owner_id: int, task: dict[str, Any]) -> bool:
    now = datetime.now(UTC).isoformat()
    payload = {
        "owner_telegram_id": int(owner_id),
        "assignment": str(task.get("assignment") or ""),
        "task_kind": str(task.get("task_kind") or "general"),
        "status": str(task.get("status") or "planned"),
        "plan": task.get("plan") or {},
        "result": task.get("result") or {},
        "created_at": str(task.get("created_at") or now),
        "updated_at": now,
    }

    if _db_available():
        url, _ = _supabase_config()
        try:
            response = requests.post(
                f"{url}/rest/v1/agency_stagirite_tasks",
                headers=_db_headers("return=representation"),
                json=payload,
                timeout=20,
            )
            if response.ok:
                return True
            if response.status_code not in {400, 404}:
                response.raise_for_status()
        except Exception:
            pass

    fallback = list(st.session_state.get(_task_key(owner_id), []))
    payload["id"] = f"local-{hashlib.sha1((payload['assignment'] + now).encode()).hexdigest()[:12]}"
    fallback.insert(0, payload)
    st.session_state[_task_key(owner_id)] = fallback[:30]
    return False


def _update_task(owner_id: int, task_id: str, changes: dict[str, Any]) -> None:
    changes = {**changes, "updated_at": datetime.now(UTC).isoformat()}
    if task_id and not str(task_id).startswith("local-") and _db_available():
        url, _ = _supabase_config()
        try:
            response = requests.patch(
                f"{url}/rest/v1/agency_stagirite_tasks",
                headers=_db_headers("return=minimal"),
                params={"id": f"eq.{task_id}"},
                json=changes,
                timeout=20,
            )
            if response.ok:
                return
        except Exception:
            pass

    fallback = list(st.session_state.get(_task_key(owner_id), []))
    for item in fallback:
        if str(item.get("id")) == str(task_id):
            item.update(changes)
            break
    st.session_state[_task_key(owner_id)] = fallback


def _extract_number_before(text: str, stem: str, default: int = 2) -> int:
    lowered = text.lower()
    match = re.search(rf"\b(\d+)\s+[^.\n]{{0,18}}{stem}", lowered)
    if match:
        return max(1, min(10, int(match.group(1))))

    words = {
        "одну": 1, "один": 1, "одна": 1,
        "две": 2, "два": 2,
        "три": 3,
        "четыре": 4,
        "пять": 5,
    }
    for word, value in words.items():
        if re.search(rf"\b{word}\b[^.\n]{{0,18}}{stem}", lowered):
            return value
    return default


def _meeting_count_from_text(text: str) -> int:
    """Понимает естественные формулировки цели по встречам/Zoom."""
    lowered = str(text or "").lower().replace("ё", "е")

    explicit = _extract_number_before(
        lowered,
        r"(?:встреч|созвон|zoom|зум)",
        default=0,
    )
    if explicit:
        return explicit

    # «Хотя бы один из них вышел в Zoom» = цель одна встреча.
    if re.search(
        r"\bхотя\s+бы\s+(?:один|одна|1)\b",
        lowered,
    ):
        return 1
    if re.search(r"\bодин\s+из\s+(?:них|кандидат)", lowered):
        return 1

    if any(
        token in lowered
        for token in (
            "разговор в зум",
            "разговор в zoom",
            "в зуме",
            "в zoom",
            "zoom-встреч",
            "зум-встреч",
        )
    ):
        return 1

    if re.search(r"\bвстреч(?:а|у)\b", lowered):
        return 1

    return 2


def _detect_intents(text: str) -> list[str]:
    """
    Различает:
    - «подготовь анонс Zoom-встречи» -> только контент;
    - «организуй/назначь Zoom-встречу» -> календарь + кандидаты + Неона.
    """
    lowered = str(text or "").lower().replace("ё", "е")
    intents: list[str] = []

    content_words = (
        "пост",
        "анонс",
        "контент",
        "публикац",
        "иллюстрац",
        "картин",
        "продвиж",
        "текст для чата",
        "сообщение команде",
        "объявлен",
        "приглашение для",
        "напиши текст",
        "подготовь текст",
    )
    content_requested = any(x in lowered for x in content_words)

    # Явные глаголы, означающие, что Директор действительно хочет
    # ОРГАНИЗОВАТЬ встречу, а не просто написать о ней.
    meeting_action_words = (
        "организуй встреч",
        "организовать встреч",
        "назначь встреч",
        "назначить встреч",
        "согласуй встреч",
        "согласовать встреч",
        "подготовь встреч",
        "подготовить встреч",
        "найди время",
        "найти время",
        "найди свобод",
        "найти свобод",
        "подбери кандидат",
        "подобрать кандидат",
        "найди кандидат",
        "найти кандидат",
        "доведи до встреч",
        "довести до встреч",
        "мне нужна встреч",
        "мне нужны встреч",
        "хочу встреч",
        "нужна zoom-встреч",
        "нужна зум-встреч",
        "нужны zoom-встреч",
        "нужны зум-встреч",
    )
    meeting_action_requested = any(
        x in lowered
        for x in meeting_action_words
    )

    meeting_context_words = (
        "встреч",
        "созвон",
        "календар",
        "свободн",
        "zoom",
        "зум",
        "видеозвон",
        "видеовстреч",
        "разговор в зуме",
        "разговор в zoom",
    )

    candidate_words = (
        "кандидат",
        "подобрать людей",
        "подбери людей",
        "найти людей",
        "выбрать людей",
        "контакт",
    )

    # Если попросили анонс/пост о Zoom-встрече, упоминание Zoom — это
    # содержание текста. Не запускаем календарь и Неонию.
    if meeting_action_requested:
        intents.append("meetings")
    elif not content_requested and any(
        x in lowered
        for x in meeting_context_words
    ):
        intents.append("meetings")
    elif (
        not content_requested
        and any(x in lowered for x in candidate_words)
        and any(
            x in lowered
            for x in (
                "разговор",
                "пообщ",
                "переговор",
                "выйти на связь",
            )
        )
    ):
        intents.append("meetings")

    if content_requested:
        intents.append("content")

    if any(
        x in lowered
        for x in ("команд", "структур", "партнёр", "партнер")
    ):
        intents.append("team")

    return intents or ["general"]


def _is_weekly_meeting_assignment(text: str) -> bool:
    lowered = str(text or "").lower().replace("ё", "е")
    return "недел" in lowered


def _weekly_goal_from_text(text: str) -> tuple[int, int]:
    """
    Возвращает (минимум, желаемый максимум).
    «от 3 до 5» -> (3, 5)
    «минимум 3» / «хотя бы три» -> (3, 5)
    «3 встречи за неделю» -> (3, 5)
    """
    lowered = str(text or "").lower().replace("ё", "е")

    word_nums = {
        "один": 1, "одна": 1, "одну": 1,
        "два": 2, "две": 2,
        "три": 3,
        "четыре": 4,
        "пять": 5,
        "шесть": 6,
        "семь": 7,
    }

    match = re.search(r"\bот\s+(\d+)\s+до\s+(\d+)\b", lowered)
    if match:
        low = max(1, min(10, int(match.group(1))))
        high = max(low, min(10, int(match.group(2))))
        return low, high

    for low_word, low_value in word_nums.items():
        for high_word, high_value in word_nums.items():
            if re.search(
                rf"\bот\s+{low_word}\s+до\s+{high_word}\b",
                lowered,
            ):
                return low_value, max(low_value, high_value)

    explicit = _meeting_count_from_text(lowered)
    low = max(1, min(10, int(explicit or 3)))

    # Для недельной цели одно число трактуем как минимум,
    # а желаемый потолок — до пяти встреч.
    return low, max(low, 5)


def _weekly_period(text: str) -> tuple[date, date]:
    period = _target_period_from_text(text)
    today = datetime.now(BERLIN).date()

    if period:
        return period[0], period[-1]

    # Если сказано просто «за неделю» — текущие 7 дней от сегодня.
    return today, today + timedelta(days=6)


def _meetings_for_period(
    owner_id: int,
    start_day_iso: str,
    end_day_iso: str,
    contact_ids: list[int],
) -> list[dict[str, Any]]:
    if not start_day_iso or not end_day_iso or not contact_ids:
        return []
    try:
        start_day = date.fromisoformat(str(start_day_iso))
        end_day = date.fromisoformat(str(end_day_iso))
    except Exception:
        return []

    start_msk = datetime.combine(start_day, dt_time.min, tzinfo=MSK)
    end_msk = datetime.combine(
        end_day + timedelta(days=1),
        dt_time.min,
        tzinfo=MSK,
    )

    try:
        rows = agency_calendar.list_meetings(
            int(owner_id),
            start_msk.astimezone(UTC),
            end_msk.astimezone(UTC),
        )
    except Exception:
        return []

    wanted = {int(value) for value in contact_ids}
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            contact_id = int(row.get("contact_telegram_id"))
        except (TypeError, ValueError):
            continue
        if contact_id not in wanted:
            continue
        if row.get("status") in {"Отменена", "Перенесена"}:
            continue
        result.append(row)
    return result


def _target_period_from_text(text: str) -> list[date] | None:
    """Календарные диапазоны из обычной речи."""
    lowered = str(text or "").lower().replace("ё", "е")
    today = datetime.now(MSK).date()

    if (
        ("следующ" in lowered or "будущ" in lowered)
        and "недел" in lowered
    ):
        days_to_monday = (7 - today.weekday()) % 7
        if days_to_monday == 0:
            days_to_monday = 7
        monday = today + timedelta(days=days_to_monday)
        return [monday + timedelta(days=offset) for offset in range(7)]

    if (
        ("этой недел" in lowered)
        or ("текущ" in lowered and "недел" in lowered)
    ):
        days_to_sunday = 6 - today.weekday()
        return [
            today + timedelta(days=offset)
            for offset in range(days_to_sunday + 1)
        ]

    return None


def _target_date_from_text(text: str) -> date | None:
    lowered = text.lower()
    today = datetime.now(MSK).date()
    if "послезавтра" in lowered:
        return today + timedelta(days=2)
    if "завтра" in lowered:
        return today + timedelta(days=1)
    if "сегодня" in lowered:
        return today

    weekdays = {
        "понедельник": 0, "понедельника": 0,
        "вторник": 1, "вторника": 1,
        "среда": 2, "среду": 2,
        "четверг": 3, "четверга": 3,
        "пятница": 4, "пятницу": 4,
        "суббота": 5, "субботу": 5,
        "воскресенье": 6,
    }
    for word, weekday in weekdays.items():
        if word in lowered:
            delta = (weekday - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)

    match = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", lowered)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _active_meetings_for_day(owner_id: int, day: date) -> list[dict[str, Any]]:
    start_msk = datetime.combine(day, dt_time.min, tzinfo=MSK)
    end_msk = start_msk + timedelta(days=1)
    return [
        item for item in agency_calendar.list_meetings(
            int(owner_id),
            start_msk.astimezone(UTC),
            end_msk.astimezone(UTC),
        )
        if item.get("status") not in {"Отменена", "Перенесена"}
    ]


def _free_slots_for_day(owner_id: int, day: date, needed: int) -> list[datetime]:
    meetings = _active_meetings_for_day(owner_id, day)
    existing = [
        (_parse_utc(item["start_at"]), _parse_utc(item["end_at"]))
        for item in meetings
        if item.get("start_at") and item.get("end_at")
    ]

    now_utc = datetime.now(UTC)
    cursor = datetime.combine(day, WORK_START, tzinfo=MSK)
    day_end = datetime.combine(day, WORK_END, tzinfo=MSK)
    chosen: list[datetime] = []

    while cursor + timedelta(minutes=MEETING_MINUTES) <= day_end:
        start_utc = cursor.astimezone(UTC)
        end_utc = start_utc + timedelta(minutes=MEETING_MINUTES)

        if start_utc >= now_utc + timedelta(minutes=60):
            blocked = False
            for ex_start, ex_end in existing:
                if (
                    start_utc < ex_end + timedelta(minutes=BUFFER_MINUTES)
                    and end_utc > ex_start - timedelta(minutes=BUFFER_MINUTES)
                ):
                    blocked = True
                    break

            if not blocked:
                for other in chosen:
                    other_end = other + timedelta(minutes=MEETING_MINUTES)
                    if (
                        start_utc < other_end + timedelta(minutes=BUFFER_MINUTES)
                        and end_utc > other - timedelta(minutes=BUFFER_MINUTES)
                    ):
                        blocked = True
                        break

            if not blocked:
                chosen.append(start_utc)
                if len(chosen) >= needed:
                    return chosen

        cursor += timedelta(minutes=30)

    return chosen


def _find_meeting_day(owner_id: int, text: str, count: int) -> dict[str, Any]:
    explicit_day = _target_date_from_text(text)
    target_period = _target_period_from_text(text)
    start_day = explicit_day or datetime.now(MSK).date()

    if explicit_day:
        days = [explicit_day]
    elif target_period:
        days = target_period
    else:
        days = [
            start_day + timedelta(days=offset)
            for offset in range(0, 14)
        ]

    best_day = None
    best_slots: list[datetime] = []

    for day in [d for d in days if d is not None]:
        slots = _free_slots_for_day(owner_id, day, count)
        if len(slots) > len(best_slots):
            best_day, best_slots = day, slots
        if len(slots) >= count:
            best_day, best_slots = day, slots
            break

    if best_day is None:
        return {
            "found": False,
            "message": "В ближайшие две недели не удалось найти подходящий свободный день.",
            "slots": [],
        }

    labels = []
    for start_utc in best_slots[:count]:
        msk = start_utc.astimezone(MSK)
        berlin = start_utc.astimezone(BERLIN)
        labels.append({
            "start_at": start_utc.isoformat(),
            "msk": msk.strftime("%d.%m.%Y · %H:%M МСК"),
            "berlin": berlin.strftime("%d.%m.%Y · %H:%M Германия"),
        })

    lowered = str(text or "").lower()
    meeting_word = "Zoom-встречу" if ("zoom" in lowered or "зум" in lowered) else "встречу"
    if count > 1:
        meeting_word = "Zoom-встречи" if ("zoom" in lowered or "зум" in lowered) else "встречи"

    return {
        "found": len(labels) >= count,
        "day": best_day.isoformat(),
        "slots": labels,
        "message": (
            f"Нашёл подходящий день и время для {count} {meeting_word}."
            if len(labels) >= count
            else f"На лучшем найденном дне свободно только {len(labels)} подходящих окон."
        ),
    }


def _candidate_snapshot(owner_id: int) -> dict[str, int]:
    """Живое состояние выбора Неонии."""
    owner_id = int(owner_id)
    candidates = st.session_state.get(f"neonia_candidates_{owner_id}", [])
    contacts = st.session_state.get(f"neonia_telegram_contacts_{owner_id}", [])

    raw_selected = st.session_state.get(
        f"neonia_selected_candidates_{owner_id}",
        [],
    )
    selected_ids: list[int] = []
    for value in raw_selected if isinstance(raw_selected, list) else []:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized not in selected_ids:
            selected_ids.append(normalized)

    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("status") not in {
                "Выбран владельцем",
                "Сообщение подготовлено",
                "Сообщение отредактировано",
                "Первое сообщение утверждено",
                "Отправлено",
            }:
                continue
            try:
                candidate_id = int(candidate.get("telegram_id"))
            except (TypeError, ValueError):
                continue
            if candidate_id not in selected_ids:
                selected_ids.append(candidate_id)

    pending_ids: list[int] = []
    prefix = f"owner_select_candidate_{owner_id}_"
    for key, value in st.session_state.items():
        if not str(key).startswith(prefix) or not bool(value):
            continue
        try:
            candidate_id = int(str(key)[len(prefix):])
        except (TypeError, ValueError):
            continue
        if candidate_id not in selected_ids and candidate_id not in pending_ids:
            pending_ids.append(candidate_id)

    return {
        "contacts": len(contacts) if isinstance(contacts, list) else 0,
        "candidates": (
            sum(
                1
                for item in candidates
                if isinstance(item, dict)
                and item.get("activity_eligible") is True
                and item.get("work_state") == "available"
                and not bool(item.get("selection_blocked"))
            )
            if isinstance(candidates, list)
            else 0
        ),
        "selected": len(selected_ids),
        "checked_pending": len(pending_ids),
    }



def _meeting_candidate_pool(owner_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Кандидаты только из реальных Telegram-контактов владельца."""
    owner_id = int(owner_id)
    candidates = st.session_state.get(f"neonia_candidates_{owner_id}", [])
    contacts = st.session_state.get(f"neonia_telegram_contacts_{owner_id}", [])
    if not isinstance(candidates, list):
        return []

    owner_contact_ids: set[int] = set()
    for contact in contacts if isinstance(contacts, list) else []:
        try:
            owner_contact_ids.add(int(contact.get("telegram_id")))
        except (TypeError, ValueError, AttributeError):
            continue

    usable: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        if contact_id not in owner_contact_ids:
            continue
        if not str(item.get("name") or "").strip():
            continue
        if item.get("status") == "Отправлено":
            continue
        if item.get("activity_eligible") is not True:
            continue
        if item.get("work_state") != "available":
            continue
        if bool(item.get("selection_blocked")):
            continue
        usable.append({**item, "telegram_id": contact_id})

    # Текущая десятка Неонии находится в конце накопленного списка.
    return usable[-max(1, int(limit)):]


def _candidate_label(candidate: dict[str, Any]) -> str:
    name = str(candidate.get("name") or "Кандидат")
    username = str(candidate.get("username") or "").strip()
    username_part = f" · @{username}" if username else ""
    interest = str(candidate.get("potential_interest") or "неясно")
    activity = str(
        candidate.get("telegram_activity_label")
        or "активность подтверждена"
    )
    return f"{name}{username_part} · в контактах · {activity} · интерес: {interest}"


def _save_stagirite_candidate_selection(
    owner_id: int,
    selected_ids: list[int],
) -> None:
    """Сохраняет человеческий выбор и передаёт его в общий рабочий контекст."""
    owner_id = int(owner_id)
    normalized: list[int] = []
    for value in selected_ids:
        try:
            contact_id = int(value)
        except (TypeError, ValueError):
            continue
        if contact_id not in normalized:
            normalized.append(contact_id)

    st.session_state[f"neonia_selected_candidates_{owner_id}"] = normalized

    candidates_key = f"neonia_candidates_{owner_id}"
    candidates = st.session_state.get(candidates_key, [])
    if isinstance(candidates, list):
        selected_set = set(normalized)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                candidate_id = int(candidate.get("telegram_id"))
            except (TypeError, ValueError):
                continue
            if candidate_id in selected_set and candidate.get("status") not in {
                "Сообщение подготовлено",
                "Сообщение отредактировано",
                "Первое сообщение утверждено",
                "Отправлено",
            }:
                candidate["status"] = "Выбран владельцем"
        st.session_state[candidates_key] = candidates

    persist_workspace_if_changed(owner_id, force=True)


def _dialog_states_for_contacts(
    owner_id: int,
    contact_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not contact_ids or not _db_available():
        return {}

    url, _ = _supabase_config()
    normalized = sorted({int(value) for value in contact_ids})
    try:
        response = requests.get(
            f"{url}/rest/v1/agency_dialog_states",
            headers=_db_headers(),
            params={
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "contact_telegram_id": (
                    "in.(" + ",".join(str(value) for value in normalized) + ")"
                ),
                "select": (
                    "contact_telegram_id,last_incoming_message_id,stage,"
                    "context,updated_at"
                ),
            },
            timeout=20,
        )
        if not response.ok:
            return {}
        rows = response.json()
        result = {}
        for row in rows if isinstance(rows, list) else []:
            try:
                contact_id = int(row.get("contact_telegram_id"))
            except (TypeError, ValueError):
                continue
            result[contact_id] = row
        return result
    except Exception:
        return {}


def _meetings_for_task(
    owner_id: int,
    target_day_iso: str,
    contact_ids: list[int],
) -> list[dict[str, Any]]:
    if not target_day_iso or not contact_ids:
        return []
    try:
        target_day = date.fromisoformat(str(target_day_iso))
    except Exception:
        return []

    start_msk = datetime.combine(target_day, dt_time.min, tzinfo=MSK)
    end_msk = start_msk + timedelta(days=1)
    try:
        rows = agency_calendar.list_meetings(
            int(owner_id),
            start_msk.astimezone(UTC),
            end_msk.astimezone(UTC),
        )
    except Exception:
        return []

    wanted = {int(value) for value in contact_ids}
    result = []
    for row in rows:
        try:
            contact_id = int(row.get("contact_telegram_id"))
        except (TypeError, ValueError):
            continue
        if contact_id not in wanted:
            continue
        if row.get("status") in {"Отменена", "Перенесена"}:
            continue
        result.append(row)
    return result


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _candidate_name_lookup(owner_id: int) -> dict[int, str]:
    candidates = st.session_state.get(
        f"neonia_candidates_{int(owner_id)}",
        [],
    )
    result: dict[int, str] = {}
    if not isinstance(candidates, list):
        return result
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        result[contact_id] = str(item.get("name") or "Кандидат")
    return result


def register_first_message_for_stagirite(
    owner_id: int,
    contact_id: int,
    *,
    sent_at: str,
    message_id: int | str = 0,
    baseline_incoming_id: int = 0,
) -> None:
    """Привязывает отправку Неоны к активному поручению Стагирита."""
    owner_id = int(owner_id)
    contact_id = int(contact_id)
    tasks, _ = _load_tasks(owner_id)

    for task in tasks:
        if "meetings" not in str(task.get("task_kind") or ""):
            continue
        if str(task.get("status") or "") in {"Выполнено", "Ошибка"}:
            continue

        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        selected = []
        for value in (
            result.get("selected_candidate_ids", [])
            if isinstance(result.get("selected_candidate_ids"), list)
            else []
        ):
            try:
                selected.append(int(value))
            except (TypeError, ValueError):
                continue
        if contact_id not in selected:
            continue

        progress = (
            result.get("contact_progress")
            if isinstance(result.get("contact_progress"), dict)
            else {}
        )
        progress = dict(progress)
        progress[str(contact_id)] = {
            **(
                progress.get(str(contact_id))
                if isinstance(progress.get(str(contact_id)), dict)
                else {}
            ),
            "first_message_sent_at": str(sent_at or ""),
            "telegram_message_id": int(message_id or 0),
            "baseline_incoming_id": int(baseline_incoming_id or 0),
        }

        updated_result = dict(result)
        updated_result["contact_progress"] = progress
        _update_task(
            owner_id,
            str(task.get("id") or ""),
            {"status": "В работе", "result": updated_result},
        )
        return



def register_first_message_failure_for_stagirite(
    owner_id: int,
    contact_id: int,
    *,
    reason: str,
) -> None:
    """Фиксирует, что выбранного человека не удалось запустить в работу."""
    owner_id = int(owner_id)
    contact_id = int(contact_id)
    tasks, _ = _load_tasks(owner_id)

    for task in tasks:
        if "meetings" not in str(task.get("task_kind") or ""):
            continue
        if str(task.get("status") or "") in {"Выполнено", "Ошибка"}:
            continue

        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        selected = []
        for value in (
            result.get("selected_candidate_ids", [])
            if isinstance(result.get("selected_candidate_ids"), list)
            else []
        ):
            try:
                selected.append(int(value))
            except (TypeError, ValueError):
                continue
        if contact_id not in selected:
            continue

        progress = (
            dict(result.get("contact_progress"))
            if isinstance(result.get("contact_progress"), dict)
            else {}
        )
        progress[str(contact_id)] = {
            **(
                progress.get(str(contact_id))
                if isinstance(progress.get(str(contact_id)), dict)
                else {}
            ),
            "status": "send_failed",
            "send_failed_at": datetime.now(UTC).isoformat(),
            "send_failure_reason": str(reason or "Отправить сообщение не удалось"),
        }

        updated_result = dict(result)
        updated_result["contact_progress"] = progress
        _update_task(
            owner_id,
            str(task.get("id") or ""),
            {"status": "В работе", "result": updated_result},
        )
        return



def get_first_message_failures_for_stagirite(owner_id: int) -> list[dict[str, Any]]:
    """Возвращает факты неудачной первой отправки из активных задач встреч."""
    owner_id = int(owner_id)
    tasks, _ = _load_tasks(owner_id)
    found: dict[int, dict[str, Any]] = {}

    for task in tasks:
        if "meetings" not in str(task.get("task_kind") or ""):
            continue
        if str(task.get("status") or "") in {"Выполнено", "Ошибка"}:
            continue
        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        progress = (
            result.get("contact_progress")
            if isinstance(result.get("contact_progress"), dict)
            else {}
        )
        for raw_id, item in progress.items():
            if (
                not isinstance(item, dict)
                or str(item.get("status") or "") != "send_failed"
            ):
                continue
            try:
                contact_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            record = {
                "telegram_id": contact_id,
                "failed_at": str(item.get("send_failed_at") or ""),
                "reason": str(
                    item.get("send_failure_reason")
                    or "Отправить сообщение не удалось"
                ),
                "task_id": str(task.get("id") or ""),
                "assignment": str(task.get("assignment") or ""),
            }
            previous = found.get(contact_id)
            if (
                previous is None
                or record["failed_at"]
                > str(previous.get("failed_at") or "")
            ):
                found[contact_id] = record

    return sorted(
        found.values(),
        key=lambda item: str(item.get("failed_at") or ""),
        reverse=True,
    )


def mark_first_message_retry_for_stagirite(
    owner_id: int,
    contact_id: int,
) -> None:
    """Возвращает send_failed в ожидание нового решения владельца."""
    owner_id = int(owner_id)
    contact_id = int(contact_id)
    tasks, _ = _load_tasks(owner_id)

    for task in tasks:
        if "meetings" not in str(task.get("task_kind") or ""):
            continue
        if str(task.get("status") or "") in {"Выполнено", "Ошибка"}:
            continue

        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        progress = (
            dict(result.get("contact_progress"))
            if isinstance(result.get("contact_progress"), dict)
            else {}
        )
        item = progress.get(str(contact_id))
        if (
            not isinstance(item, dict)
            or str(item.get("status") or "") != "send_failed"
        ):
            continue

        item = dict(item)
        item["status"] = "awaiting_first_message"
        item.pop("send_failed_at", None)
        item.pop("send_failure_reason", None)
        progress[str(contact_id)] = item

        updated_result = dict(result)
        updated_result["contact_progress"] = progress
        _update_task(
            owner_id,
            str(task.get("id") or ""),
            {"status": "В работе", "result": updated_result},
        )
        return

def _refresh_meeting_task(
    owner_id: int,
    task: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Собирает фактический прогресс без OpenAI."""
    result = (
        dict(task.get("result"))
        if isinstance(task.get("result"), dict)
        else {}
    )
    selected_ids: list[int] = []
    for value in (
        result.get("selected_candidate_ids", [])
        if isinstance(result.get("selected_candidate_ids"), list)
        else []
    ):
        try:
            contact_id = int(value)
        except (TypeError, ValueError):
            continue
        if contact_id not in selected_ids:
            selected_ids.append(contact_id)

    if not selected_ids:
        return result, str(task.get("status") or "")

    progress = (
        dict(result.get("contact_progress"))
        if isinstance(result.get("contact_progress"), dict)
        else {}
    )
    states = _dialog_states_for_contacts(owner_id, selected_ids)

    weekly_goal = (
        result.get("weekly_goal")
        if isinstance(result.get("weekly_goal"), dict)
        else {}
    )
    if weekly_goal:
        meetings = _meetings_for_period(
            owner_id,
            str(weekly_goal.get("period_start") or ""),
            str(weekly_goal.get("period_end") or ""),
            selected_ids,
        )
    else:
        meeting_block = (
            result.get("meetings")
            if isinstance(result.get("meetings"), dict)
            else {}
        )
        target_day = str(meeting_block.get("day") or "")
        meetings = _meetings_for_task(owner_id, target_day, selected_ids)
    meeting_by_contact = {}
    for meeting in meetings:
        try:
            meeting_by_contact[int(meeting.get("contact_telegram_id"))] = meeting
        except (TypeError, ValueError):
            continue

    now = datetime.now(UTC)
    counts = {
        "selected": len(selected_ids),
        "sent": 0,
        "waiting": 0,
        "dialogue": 0,
        "scheduled": 0,
        "needs_reserve": 0,
    }

    for contact_id in selected_ids:
        key = str(contact_id)
        item = (
            dict(progress.get(key))
            if isinstance(progress.get(key), dict)
            else {}
        )
        meeting = meeting_by_contact.get(contact_id)
        state = states.get(contact_id, {})

        if meeting:
            zoom_link = str(
                result.get("zoom_link")
                or _load_stagirite_settings(owner_id).get("zoom_link")
                or ""
            ).strip()
            meeting_format = str(meeting.get("meeting_format") or "")
            if (
                zoom_link
                and "zoom" in meeting_format.lower()
                and not str(meeting.get("meeting_link") or "").strip()
                and meeting.get("id")
            ):
                try:
                    agency_calendar.update_meeting(
                        str(meeting["id"]),
                        {"meeting_link": zoom_link},
                    )
                    meeting["meeting_link"] = zoom_link
                except Exception:
                    pass

            counts["scheduled"] += 1
            item.update(
                {
                    "status": "meeting_scheduled",
                    "meeting_id": meeting.get("id"),
                    "meeting_start_at": meeting.get("start_at"),
                    "meeting_format": meeting.get("meeting_format"),
                    "meeting_link": meeting.get("meeting_link"),
                }
            )
            progress[key] = item
            continue

        if str(item.get("status") or "") == "send_failed":
            counts["needs_reserve"] += 1
            progress[key] = item
            continue

        sent_at = _parse_iso_datetime(item.get("first_message_sent_at"))
        if sent_at is None:
            item["status"] = "awaiting_first_message"
            progress[key] = item
            continue

        counts["sent"] += 1
        baseline = int(item.get("baseline_incoming_id") or 0)
        try:
            last_incoming = int(state.get("last_incoming_message_id") or 0)
        except (TypeError, ValueError):
            last_incoming = 0

        stage = str(state.get("stage") or "idle")
        replied = last_incoming > baseline

        if stage == "scheduled":
            # Если таблица календаря ещё не успела прочитаться, не теряем факт.
            counts["scheduled"] += 1
            item["status"] = "meeting_scheduled"
        elif replied:
            counts["dialogue"] += 1
            item["status"] = "dialogue"
            item["dialogue_stage"] = stage
        else:
            counts["waiting"] += 1
            item["status"] = "waiting_reply"
            if now - sent_at >= timedelta(hours=24):
                item["status"] = "waiting_over_24h"
                counts["needs_reserve"] += 1

        progress[key] = item

    result["contact_progress"] = progress
    result["progress_summary"] = counts

    if weekly_goal:
        minimum = int(weekly_goal.get("minimum") or result.get("meeting_count") or 1)
        desired = int(weekly_goal.get("desired") or minimum)
        result["meeting_count"] = minimum
        result["weekly_goal"]["scheduled"] = int(counts["scheduled"])
        if counts["scheduled"] >= desired:
            status = "Выполнено"
        elif counts["scheduled"] >= minimum:
            status = "Минимум выполнен"
        else:
            status = "В работе"
    else:
        needed = int(result.get("meeting_count", 1) or 1)
        status = "Выполнено" if counts["scheduled"] >= needed else "В работе"
    return result, status


def _append_reserve_candidate(
    owner_id: int,
    task: dict[str, Any],
    contact_id: int,
) -> None:
    result = (
        dict(task.get("result"))
        if isinstance(task.get("result"), dict)
        else {}
    )
    selected = []
    for value in (
        result.get("selected_candidate_ids", [])
        if isinstance(result.get("selected_candidate_ids"), list)
        else []
    ):
        try:
            selected.append(int(value))
        except (TypeError, ValueError):
            continue

    contact_id = int(contact_id)
    if contact_id not in selected:
        selected.append(contact_id)

    result["selected_candidate_ids"] = selected
    _save_stagirite_candidate_selection(owner_id, selected)
    _update_task(
        int(owner_id),
        str(task.get("id") or ""),
        {"status": "В работе", "result": result},
    )



def _generate_content(
    ask_openai_fn,
    owner_id: int,
    owner_name: str,
    assignment: str,
) -> str:
    """
    Стагирит выступает редакционным директором.
    Он не пересылает Мастеру контента поручение буквально,
    а внутри одного AI-вызова превращает его в сильный творческий бриф.
    """
    settings = _load_stagirite_settings(owner_id)
    zoom_link = str(settings.get("zoom_link") or "").strip()
    zoom_note = str(settings.get("zoom_note") or "").strip()

    zoom_context = (
        f"Сохранённая ссылка Zoom: {zoom_link}\n"
        f"Дополнение: {zoom_note or 'нет'}"
        if zoom_link
        else (
            "Ссылка Zoom пока не сохранена. Если она нужна, используй "
            "[ССЫЛКА ZOOM], но никогда не выдумывай ссылку."
        )
    )

    instructions = f"""
Ты работаешь в Агентстве W сразу в двух внутренних ролях.

РОЛЬ 1 — СТАГИРИТ.
Ты — мудрый заместитель Директора и редакционный директор.
Директор может поставить очень простое поручение:
«напиши об одном дне Агентства»,
«сделай пост про Неону»,
«анонс встречи завтра»,
«расскажи, что у нас нового».

Ты НИКОГДА не передаёшь это поручение Мастеру контента буквально.
Сначала молча понимаешь:
- кто будет это читать;
- что должно остановить взгляд;
- какую эмоцию надо вызвать;
- какой образ позволит человеку увидеть пользу, а не читать список функций;
- что человек должен подумать или захотеть после поста.

После этого ты МОЛЧА формируешь профессиональный творческий бриф
для Мастера контента.

РОЛЬ 2 — МАСТЕР КОНТЕНТА.
Получив внутренний бриф Стагирита, ты пишешь только готовую публикацию.

Директор: {owner_name}.

{_agency_current_release_brief()}

ZOOM:
{zoom_context}
Используй Zoom-ссылку ТОЛЬКО если Директор прямо попросил анонс встречи,
приглашение, Zoom-пост или указал, что публикация должна вести на встречу.
В обычный пост ссылку и приглашение не вставляй.

ГЛАВНЫЙ РЕДАКЦИОННЫЙ ПРИНЦИП:
Люди читают не функции. Люди читают жизнь, в которой узнают себя.

ТВОЯ ПЛАНКА:
Публикация должна быть такой, чтобы человек начал читать из любопытства,
продолжил из удовольствия, а закончил с мыслью или улыбкой.
Если текст просто «понятный и полезный» — этого недостаточно.
Он должен быть ЖИВЫМ.

Поэтому вместо:
«Неония анализирует контакты, Неона ведёт диалоги,
Стагирит координирует работу»

предпочитай:
- сцену;
- человека;
- время суток;
- маленькое событие;
- узнаваемую бытовую деталь;
- характер;
- диалог;
- мягкий юмор;
- контраст «как было / как стало»;
- образ будущей жизни человека.

ПОСТ ДОЛЖЕН БЫТЬ ПОХОЖ НА МАЛЕНЬКИЙ ФИЛЬМ,
если тема это позволяет.

Например, тема «Один день в Агентстве W» естественно требует:
- движения дня от утра к вечеру;
- агентов как действующих персонажей;
- лёгкой персонификации;
- конкретных сцен;
- параллели: Агентство работает — человек живёт своей жизнью;
- финала, который собирает весь смысл в одну фразу.

Но НЕ копируй один и тот же сюжет для всех тем.
Стагирит каждый раз сам выбирает лучший жанр:
- история;
- мини-сцена;
- наблюдение;
- диалог;
- контраст;
- метафора;
- короткая притча;
- интрига;
- эмоциональный анонс.

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА МЫШЛЕНИЯ СТАГИРИТА
(не показывать пользователю):
1. Что здесь самое интересное для читателя?
2. Где человек узнает себя?
3. Как показать пользу через жизнь, а не через техническое описание?
4. Какая первая строка заставит читать дальше?
5. Какая последняя строка останется в памяти?

ОТДЕЛЬНО ПРО ВНИМАНИЕ:
Первая фраза не должна быть формальной.
Запрещённые начала:
- «Сегодня мы хотим рассказать...»
- «Агентство W представляет...»
- «В рамках развития проекта...»
- «Мы реализовали новый функционал...»

Хорошая первая фраза может быть:
- неожиданной;
- визуальной;
- чуть ироничной;
- парадоксальной;
- сразу помещать читателя внутрь события.

ЮМОР:
Допустим и желателен, когда подходит теме.
Юмор умный, добрый, наблюдательный.
Не превращай Агентство в цирк.
Одна точная улыбка сильнее пяти шуток.

ПОЛЬЗА:
Не объясняй «что умеет функция».
Показывай:
- что человеку больше не приходится делать;
- какое решение стало проще;
- какое время вернулось;
- где ему всё ещё нужно быть человеком:
  выбрать, поговорить, встретиться, построить отношения.

ПРАВДА:
Художественность не даёт права выдумывать функции.
Разрешено оживлять реальные процессы сценой и образом.
Запрещено утверждать, что уже работает то, чего нет в паспорте версии.
Будущие функции обозначай как будущие.

ОСОБО ВАЖНО:
- Стагирит не должен сам заранее назначать человеку время встречи.
- Для недельных встреч сначала кандидат и согласие, потом день/время,
  затем календарь.
- Неония работает с реальными Telegram-контактами владельца для первого
  холодного обращения и учитывает активность.
- Первое сообщение утверждает владелец.
- Неона ведёт диалог после ответа.
- Неола — наставник новичка, а не универсальный секретарь.
- Художник создаёт иллюстрацию только по отдельной команде владельца.
- Не обещай гарантированные встречи, партнёров, доход или абсолютную
  автономность без участия человека.

ДЛИНА:
Не делай текст коротким только ради краткости.
История может быть длиннее обычного поста, если она держит внимание.
Главный критерий — ни одного скучного абзаца.
Если Директор прямо задал лимит, соблюдай его.

ДЛЯ АНОНСА:
Это не отчёт.
Это причина прийти.
Схема:
крючок → интрига → 2–3 сильных изменения для человека →
что покажем вживую → приглашение → Zoom.

ДЛЯ ОБЫЧНОГО ПОСТА:
Не стремись всё продать.
Если Директор попросил просто «пост», «историю», «один день», «расскажи про...»
— это НЕ анонс и НЕ реклама.
Не добавляй Zoom, ссылку, приглашение на встречу, «приходите», «узнайте больше»
или другой призыв к действию, если Директор сам этого не просил.

Иногда лучший маркетинг — история, после которой читатель сам задаёт вопрос:
«А как это работает у меня?»

ЖЁСТКИЙ СТАНДАРТ ЖИВОГО ПОСТА:
- первая строка должна создавать любопытство, картинку или лёгкое напряжение;
- начни со сцены, реплики, детали, неожиданного наблюдения или маленького конфликта;
- в первых двух абзацах НЕ объясняй продукт;
- функции Агентства показывай только через действие персонажей;
- не перечисляй подряд больше одной-двух функций;
- каждый абзац должен либо двигать сцену, либо давать улыбку, либо открывать новую мысль;
- если абзац можно без потери заменить строкой из презентации — перепиши его;
- добавляй конкретные бытовые детали: кофе остыл, телефон молчит, утки требуют хлеб,
  человек опаздывает на автобус, агент уже закончил задачу — только когда это уместно;
- юмор строится на наблюдении и контрасте, а не на шутке ради шутки;
- допускай лёгкую самоиронию Агентства и агентов;
- оставляй читателю маленькую недосказанность;
- финал должен быть короче основной истории и сильнее её по смыслу.

ПРОВЕРКА ПЕРЕД ОТВЕТОМ — МОЛЧА:
1. Это похоже на рассказ человека или на инструкцию к программе?
   Если на инструкцию — перепиши.
2. Есть ли в первых трёх строках причина читать четвёртую?
   Если нет — перепиши начало.
3. Можно ли хотя бы один раз улыбнуться?
   Если нет и тема позволяет юмор — добавь живое наблюдение.
4. Видит ли читатель сцену глазами?
   Если нет — добавь конкретику.
5. Не начал ли текст продавать то, что Директор не просил продавать?
   Если да — убери продажу.
6. Есть ли хотя бы одна фраза, которую хочется переслать или процитировать?
   Если нет — усили финал.

ФИНАЛ:
Сильный финал — это не повтор текста.
Это мысль, реплика или короткий поворот, который хочется запомнить.
Допустим девиз:
«Мы создаём своё настоящее».
Но не вставляй его механически в каждый пост.

В ОТВЕТЕ:
ТОЛЬКО готовая публикация.
Не показывай:
- внутренний бриф;
- анализ;
- план;
- объяснение своих решений;
- список функций;
если Директор сам не попросил список.
""".strip()

    return ask_openai_fn(
        instructions,
        assignment,
        uploaded_files=[],
        use_web_search=False,
    )

def _make_plan(intents: list[str], meeting_count: int) -> list[str]:
    plan: list[str] = []
    if "meetings" in intents:
        plan.extend([
            "Проверить календарь.",
            f"Найти свободный день и время для цели: {meeting_count} встреч(а).",
            "Взять активных и пригодных кандидатов Неонии.",
            "Дать владельцу выбрать людей, с которыми начинать работу.",
            "Передать выбранных людей Неоне и контролировать результат до назначенной встречи.",
        ])
    if "content" in intents:
        plan.extend([
            "Сформировать требуемые тексты одним запросом к ИИ.",
            "Если нужна иллюстрация — подготовить визуальное задание.",
            "Показать результат Директору на утверждение до публикации.",
        ])
    if "team" in intents and "content" not in intents:
        plan.append("Проверить, относится ли поручение к структуре или коммуникации команды.")
    if intents == ["general"]:
        plan.extend([
            "Понять ожидаемый результат.",
            "Использовать уже имеющиеся данные Агентства.",
            "Обратиться к ИИ только если без него нельзя выполнить поручение.",
        ])
    return plan


def _process_assignment(owner_id: int, owner_name: str, assignment: str, ask_openai_fn) -> dict[str, Any]:
    intents = _detect_intents(assignment)
    meeting_count = _meeting_count_from_text(assignment)
    result: dict[str, Any] = {
        "intents": intents,
        "meeting_count": meeting_count,
    }
    status = "Готово к утверждению"

    if "meetings" in intents:
        try:
            settings = _load_stagirite_settings(owner_id)
            zoom_link = str(settings.get("zoom_link") or "").strip()
            if zoom_link:
                result["zoom_link"] = zoom_link
                result["zoom_note"] = str(
                    settings.get("zoom_note") or ""
                ).strip()

            if _is_weekly_meeting_assignment(assignment):
                minimum, desired = _weekly_goal_from_text(assignment)
                period_start, period_end = _weekly_period(assignment)
                result["meeting_count"] = minimum
                result["weekly_goal"] = {
                    "minimum": minimum,
                    "desired": desired,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "reserve_target": 50,
                    "daily_target": 5,
                    "weekly_pool_ids": [],
                    "daily_batches": {},
                }
                result["candidates"] = _candidate_snapshot(owner_id)
                status = "В работе"
            else:
                # Разовая встреча с конкретной датой по-прежнему может
                # использовать старый сценарий поиска свободного окна.
                result["meetings"] = _find_meeting_day(
                    owner_id,
                    assignment,
                    meeting_count,
                )
                result["candidates"] = _candidate_snapshot(owner_id)
                status = "Нужно решение владельца"
        except Exception as exc:
            result["meetings"] = {
                "found": False,
                "message": f"Не удалось обработать поручение о встречах: {exc}",
                "slots": [],
            }
            status = "Ошибка"

    if "content" in intents:
        try:
            content = _generate_content(
                ask_openai_fn,
                owner_id,
                owner_name,
                assignment,
            )
            if str(content).startswith("Ошибка OpenAI:"):
                result["content_error"] = content
                if status != "Ошибка":
                    status = "Ошибка"
            else:
                result["content"] = content
        except Exception as exc:
            result["content_error"] = f"{type(exc).__name__}: {exc}"
            if status != "Ошибка":
                status = "Ошибка"

    if intents == ["general"]:
        result["note"] = (
            "Поручение сохранено. Для этой формулировки Стагириту пока требуется "
            "уточнение или подключение дополнительного исполнительного модуля."
        )
        status = "Нужно уточнение"

    return {
        "assignment": assignment,
        "task_kind": "+".join(intents),
        "status": status,
        "plan": {"steps": _make_plan(intents, meeting_count)},
        "result": result,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _transcribe_audio(audio_bytes: bytes, filename: str = "stagirite-command.wav") -> str:
    api_key = str(st.secrets.get("OPENAI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден.")

    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    cache_key = f"stagirite_voice_transcript_{audio_hash}"
    cached = st.session_state.get(cache_key)
    if cached:
        return str(cached)

    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={"model": "gpt-4o-mini-transcribe", "language": "ru"},
        files={"file": (filename, audio_bytes, "audio/wav")},
        timeout=120,
    )
    response.raise_for_status()
    transcript = str(response.json().get("text") or "").strip()
    st.session_state[cache_key] = transcript
    return transcript


def _status_icon(status: str) -> str:
    return {
        "Готово к утверждению": "🟢",
        "Нужно решение владельца": "🟡",
        "Нужно уточнение": "🟡",
        "Нужен ваш выбор": "🟡",
        "Утверждено": "✅",
        "Выполнено": "✅",
        "В работе": "🔵",
        "Минимум выполнен": "🟢",
        "Ошибка": "🔴",
    }.get(status, "⚙️")



def _render_weekly_meeting_goal(
    task: dict[str, Any],
    owner_id: int,
    prepare_candidates_fn=None,
) -> None:
    result = (
        dict(task.get("result"))
        if isinstance(task.get("result"), dict)
        else {}
    )
    goal = (
        dict(result.get("weekly_goal"))
        if isinstance(result.get("weekly_goal"), dict)
        else {}
    )
    if not goal:
        return

    minimum = int(goal.get("minimum") or 3)
    desired = int(goal.get("desired") or max(minimum, 5))
    reserve_target = int(goal.get("reserve_target") or 50)
    daily_target = int(goal.get("daily_target") or 5)
    period_start = str(goal.get("period_start") or "")
    period_end = str(goal.get("period_end") or "")

    st.markdown("### 🎯 Недельная цель встреч")
    st.markdown(
        f"**{period_start} — {period_end}: минимум {minimum}, желательно {desired} встреч.**"
    )
    st.caption(
        "Стагирит не бронирует время заранее. Сначала Неона получает согласие "
        "человека и выясняет удобный день/время. Только после согласования "
        "встреча появляется в календаре."
    )

    selected_all: list[int] = []
    for value in (
        result.get("selected_candidate_ids", [])
        if isinstance(result.get("selected_candidate_ids"), list)
        else []
    ):
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid not in selected_all:
            selected_all.append(cid)

    summary = (
        result.get("progress_summary")
        if isinstance(result.get("progress_summary"), dict)
        else {}
    )
    st.markdown(
        "**Ход недели:** "
        f"первые сообщения — {summary.get('sent', 0)} · "
        f"ждём ответ — {summary.get('waiting', 0)} · "
        f"в диалоге — {summary.get('dialogue', 0)} · "
        f"встреч назначено — {summary.get('scheduled', 0)}"
    )

    if int(summary.get("scheduled", 0) or 0) >= desired:
        st.success(
            f"✅ Недельная цель выполнена полностью: "
            f"{summary.get('scheduled', 0)} встреч."
        )
    elif int(summary.get("scheduled", 0) or 0) >= minimum:
        st.success(
            f"✅ Минимальная цель выполнена. Уже назначено "
            f"{summary.get('scheduled', 0)}; можно двигаться к {desired}."
        )

    today_key = datetime.now(BERLIN).date().isoformat()
    daily_batches = (
        dict(goal.get("daily_batches"))
        if isinstance(goal.get("daily_batches"), dict)
        else {}
    )
    today_info = (
        dict(daily_batches.get(today_key))
        if isinstance(daily_batches.get(today_key), dict)
        else {}
    )

    # Формируем сегодняшнюю пятёрку только один раз в день.
    if not today_info.get("candidate_ids") and prepare_candidates_fn is not None:
        with st.spinner(
            "Стагирит просит Неонию обновить недельный резерв и подготовить сегодняшних кандидатов..."
        ):
            try:
                prepared = prepare_candidates_fn(
                    owner_id,
                    desired_count=daily_target,
                    reserve_target=reserve_target,
                    exclude_ids=selected_all,
                )
            except TypeError:
                # Совместимость на коротком промежутке обновления файлов.
                prepared = prepare_candidates_fn(
                    owner_id,
                    desired_count=daily_target,
                )
            except Exception:
                prepared = {
                    "ok": False,
                    "candidate_ids": [],
                    "reserve_ids": [],
                }

        if isinstance(prepared, dict):
            reserve_ids: list[int] = []
            for raw in prepared.get("reserve_ids", []) or []:
                try:
                    cid = int(raw)
                except (TypeError, ValueError):
                    continue
                if cid not in reserve_ids:
                    reserve_ids.append(cid)

            candidate_ids: list[int] = []
            for raw in prepared.get("candidate_ids", []) or []:
                try:
                    cid = int(raw)
                except (TypeError, ValueError):
                    continue
                if cid in selected_all or cid in candidate_ids:
                    continue
                candidate_ids.append(cid)

            # Недельный резерв динамический: каждый день Неония перепроверяет
            # активность и оставляет только реальных Telegram-контактов.
            goal["weekly_pool_ids"] = reserve_ids[:reserve_target]
            today_info = {
                "candidate_ids": candidate_ids[:daily_target],
                "approved_ids": [],
                "prepared_at": datetime.now(UTC).isoformat(),
            }
            daily_batches[today_key] = today_info
            goal["daily_batches"] = daily_batches
            result["weekly_goal"] = goal

            task_id = str(task.get("id") or "")
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {"status": "В работе", "result": result},
                )
                task["result"] = result

    weekly_pool_ids = [
        int(value)
        for value in goal.get("weekly_pool_ids", [])
        if str(value).lstrip("-").isdigit()
    ]
    st.caption(
        f"🧰 Недельный резерв: {len(weekly_pool_ids)} из {reserve_target} "
        "активных контактов Telegram."
    )
    if len(weekly_pool_ids) < reserve_target:
        st.caption(
            "Если активных и пригодных контактов меньше 50, Стагирит не "
            "додумывает людей — работает с теми, кого реально нашла Неония."
        )

    today_ids = []
    for value in today_info.get("candidate_ids", []) or []:
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid not in today_ids:
            today_ids.append(cid)

    approved_today = []
    for value in today_info.get("approved_ids", []) or []:
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid not in approved_today:
            approved_today.append(cid)

    candidates = _meeting_candidate_pool(owner_id, limit=200)
    by_id = {
        int(item["telegram_id"]): item
        for item in candidates
        if int(item.get("telegram_id")) in today_ids
    }

    if approved_today:
        st.success(
            f"✅ Сегодня передано Неоне: {len(approved_today)} человек(а). "
            "Неона готовит персональные первые сообщения; отправка — только "
            "после вашего утверждения."
        )

    remaining_today = [
        cid for cid in today_ids
        if cid not in approved_today and cid in by_id
    ]

    if remaining_today:
        st.markdown(f"### 👥 Сегодняшние кандидаты — до {daily_target}")
        st.caption(
            "Все люди ниже находятся в ваших Telegram-контактах и были "
            "активны сегодня или вчера. Выберите до пяти и передайте Неоне."
        )

        chosen_ids = st.multiselect(
            "Кого взять сегодня в работу",
            options=remaining_today,
            format_func=lambda cid: _candidate_label(by_id[cid]),
            max_selections=daily_target,
            key=f"stagirite_weekly_choice_{task.get('id', task.get('created_at'))}_{today_key}",
        )

        if chosen_ids:
            with st.expander("Коротко о выбранных"):
                for cid in chosen_ids:
                    candidate = by_id[cid]
                    st.markdown(f"**{candidate.get('name', 'Кандидат')}**")
                    st.caption(
                        f"{candidate.get('telegram_activity_label', '')} · "
                        f"интерес: {candidate.get('potential_interest', 'неясно')} · "
                        f"теплота: {candidate.get('warmth', 'неясно')}"
                    )

        if st.button(
            "✅ Утвердить сегодняшних и передать Неоне",
            type="primary",
            disabled=not chosen_ids,
            key=f"stagirite_weekly_confirm_{task.get('id', task.get('created_at'))}_{today_key}",
            use_container_width=True,
        ):
            chosen = [int(x) for x in chosen_ids]
            _save_stagirite_candidate_selection(owner_id, chosen)

            all_selected = list(selected_all)
            for cid in chosen:
                if cid not in all_selected:
                    all_selected.append(cid)

            approved = list(approved_today)
            for cid in chosen:
                if cid not in approved:
                    approved.append(cid)

            today_info["approved_ids"] = approved
            daily_batches[today_key] = today_info
            goal["daily_batches"] = daily_batches
            result["weekly_goal"] = goal
            result["selected_candidate_ids"] = all_selected

            progress = (
                dict(result.get("contact_progress"))
                if isinstance(result.get("contact_progress"), dict)
                else {}
            )
            for cid in chosen:
                progress.setdefault(
                    str(cid),
                    {"status": "awaiting_first_message"},
                )
            result["contact_progress"] = progress

            task_id = str(task.get("id") or "")
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {"status": "В работе", "result": result},
                )
            st.session_state["stagirite_open_agent"] = "Неона"
            st.rerun()

    elif not approved_today:
        st.info(
            "Сегодня Неония не нашла достаточного количества новых пригодных "
            "кандидатов среди активных Telegram-контактов."
        )

    # Показываем человеческий прогресс только по уже взятым в работу людям.
    progress = (
        result.get("contact_progress")
        if isinstance(result.get("contact_progress"), dict)
        else {}
    )
    if selected_all:
        names = _candidate_name_lookup(owner_id)
        with st.expander("👥 Ход работы по людям"):
            for cid in selected_all:
                item = (
                    progress.get(str(cid))
                    if isinstance(progress.get(str(cid)), dict)
                    else {}
                )
                state = str(item.get("status") or "awaiting_first_message")
                name = names.get(cid, f"Контакт {cid}")

                if state == "meeting_scheduled":
                    line = "✅ встреча назначена"
                    when = _parse_iso_datetime(item.get("meeting_start_at"))
                    if when is not None:
                        line += f" · {when.astimezone(BERLIN):%d.%m %H:%M} Германия"
                elif state == "dialogue":
                    line = "💬 Неона ведёт диалог"
                elif state == "send_failed":
                    line = "⚠️ Telegram не отправил — нужен другой кандидат или канал"
                elif state == "waiting_over_24h":
                    line = "🕒 ответа больше суток нет"
                elif state == "waiting_reply":
                    line = "✉️ сообщение отправлено, ждём ответ"
                else:
                    line = "📝 первое сообщение ещё не отправлено"

                st.write(f"**{name}** — {line}")


def _render_result(
    task: dict[str, Any],
    owner_id: int,
    prepare_candidates_fn=None,
    generate_image_fn=None,
) -> None:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}

    if "meetings" in str(task.get("task_kind") or ""):
        refreshed_result, refreshed_status = _refresh_meeting_task(
            owner_id,
            task,
        )
        if (
            refreshed_result != result
            or refreshed_status != str(task.get("status") or "")
        ):
            task_id = str(task.get("id") or "")
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "status": refreshed_status,
                        "result": refreshed_result,
                    },
                )
            task["status"] = refreshed_status
            task["result"] = refreshed_result
        result = refreshed_result

    weekly_mode = isinstance(result.get("weekly_goal"), dict)
    if weekly_mode:
        _render_weekly_meeting_goal(
            task,
            owner_id,
            prepare_candidates_fn=prepare_candidates_fn,
        )
        # renderer мог сохранить обновлённый result
        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else result
        )

    meetings = result.get("meetings")
    if (not weekly_mode) and isinstance(meetings, dict):
        st.markdown("**📅 Свободное время**")
        st.write(str(meetings.get("message") or ""))
        slots = meetings.get("slots") or []
        for idx, slot in enumerate(slots, start=1):
            st.markdown(
                f"**{idx}. {slot.get('msk', '')}**  \n"
                f"{slot.get('berlin', '')}"
            )

        zoom_link = str(result.get("zoom_link") or "").strip()
        if zoom_link:
            st.markdown(f"**🔗 Zoom:** {zoom_link}")

        summary = (
            result.get("progress_summary")
            if isinstance(result.get("progress_summary"), dict)
            else {}
        )
        if summary:
            needed = int(result.get("meeting_count", 1) or 1)
            st.markdown(
                "**🎯 Цель:** "
                f"{needed} встреч(а) · "
                f"отправлено: {summary.get('sent', 0)} · "
                f"ждём ответ: {summary.get('waiting', 0)} · "
                f"диалог: {summary.get('dialogue', 0)} · "
                f"назначено: {summary.get('scheduled', 0)}"
            )

            # Человеческий прогресс по каждому выбранному контакту.
            progress = (
                result.get("contact_progress")
                if isinstance(result.get("contact_progress"), dict)
                else {}
            )
            names = _candidate_name_lookup(owner_id)
            selected_ids_for_progress = []
            for raw_id in (
                result.get("selected_candidate_ids", [])
                if isinstance(result.get("selected_candidate_ids"), list)
                else []
            ):
                try:
                    cid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if cid not in selected_ids_for_progress:
                    selected_ids_for_progress.append(cid)

            if selected_ids_for_progress:
                st.markdown("**👥 Ход работы по людям**")
                for cid in selected_ids_for_progress:
                    item = (
                        progress.get(str(cid))
                        if isinstance(progress.get(str(cid)), dict)
                        else {}
                    )
                    name = names.get(cid, f"Контакт {cid}")
                    state = str(item.get("status") or "awaiting_first_message")

                    if state == "meeting_scheduled":
                        line = "✅ встреча назначена"
                        meeting_start = str(item.get("meeting_start_at") or "").strip()
                        if meeting_start:
                            parsed = _parse_iso_datetime(meeting_start)
                            if parsed is not None:
                                local = parsed.astimezone(BERLIN)
                                line += f" · {local:%d.%m.%Y %H:%M} Германия"
                        meeting_link = str(item.get("meeting_link") or "").strip()
                        if meeting_link:
                            line += f" · Zoom: {meeting_link}"
                    elif state == "dialogue":
                        line = "💬 ответил(а), Неона ведёт диалог"
                    elif state == "send_failed":
                        line = "⚠️ первое сообщение не отправлено — нужен другой кандидат или канал"
                    elif state == "waiting_over_24h":
                        line = "🕒 сообщение отправлено, ответа больше суток нет"
                    elif state == "waiting_reply":
                        line = "✉️ сообщение отправлено, ждём ответ"
                    else:
                        line = "📝 сообщение ещё не отправлено"

                    st.write(f"**{name}** — {line}")

            if int(summary.get("scheduled", 0) or 0) >= needed:
                st.success(
                    f"✅ Цель достигнута: назначено "
                    f"{summary.get('scheduled', 0)} из {needed} встреч."
                )
            elif int(summary.get("needs_reserve", 0) or 0) > 0:
                st.warning(
                    "По одному из контактов нужен резерв: либо первое сообщение "
                    "не удалось отправить, либо ответа нет больше суток. "
                    "Стагирит продолжает искать следующий вариант."
                )

        # Стагирит сам просит Неонию добрать рабочий пул до 5 человек.
        # Владелец не должен вручную ходить в Неонию ради следующей партии.
        existing_pool = _meeting_candidate_pool(owner_id, limit=10)
        auto_prepare_result = None
        if (
            slots
            and prepare_candidates_fn is not None
            and len(existing_pool) < 5
        ):
            with st.spinner("Стагирит просит Неонию подобрать ещё активных кандидатов..."):
                try:
                    auto_prepare_result = prepare_candidates_fn(
                        owner_id,
                        desired_count=5,
                    )
                except Exception:
                    auto_prepare_result = {
                        "ok": False,
                        "message": "Не удалось автоматически продолжить подбор.",
                    }

        snapshot = _candidate_snapshot(owner_id)
        task_selected_ids = []
        for value in result.get("selected_candidate_ids", []) if isinstance(result.get("selected_candidate_ids"), list) else []:
            try:
                normalized = int(value)
            except (TypeError, ValueError):
                continue
            if normalized not in task_selected_ids:
                task_selected_ids.append(normalized)
        selected_count = len(task_selected_ids)
        needed_count = int(result.get("meeting_count", 2) or 2)

        st.caption(
            f"Неония подготовила кандидатов: {snapshot.get('candidates', 0)} · "
            f"выбрано для этого поручения: {selected_count}"
        )
        if isinstance(auto_prepare_result, dict):
            added = int(auto_prepare_result.get("added", 0) or 0)
            checked = int(auto_prepare_result.get("checked", 0) or 0)
            if added > 0:
                st.caption(
                    f"Стагирит сам продолжил поиск: добавлено ещё {added} "
                    f"кандидат(а), просмотрено контактов: {checked}."
                )
            elif auto_prepare_result.get("exhausted"):
                st.caption(
                    "Неония сама продолжила поиск, но больше пригодных активных "
                    "кандидатов сейчас не нашла."
                )

        if slots and selected_count < needed_count:
            pool = _meeting_candidate_pool(owner_id, limit=10)
            if pool:
                st.markdown("**👥 Выберите людей для работы Неоны**")
                st.caption(
                    f"Цель — {needed_count} встреч(а). Можно начать даже с одного "
                    "подходящего человека: Неона сразу возьмёт его в работу, "
                    "а Стагирит продолжит контролировать цель и при необходимости "
                    "предложит следующих кандидатов. Выберите от 1 до 5."
                )

                by_id = {int(item["telegram_id"]): item for item in pool}
                current_selected = [cid for cid in task_selected_ids if cid in by_id]

                chosen_ids = st.multiselect(
                    "Кандидаты Неонии",
                    options=list(by_id.keys()),
                    default=current_selected,
                    format_func=lambda cid: _candidate_label(by_id[cid]),
                    max_selections=5,
                    key=f"stagirite_candidate_choice_{task.get('id', task.get('created_at'))}",
                )

                if chosen_ids:
                    with st.expander("Коротко о выбранных"):
                        for cid in chosen_ids:
                            candidate = by_id[cid]
                            st.markdown(f"**{candidate.get('name', 'Кандидат')}**")
                            st.write(
                                "Telegram: "
                                f"{candidate.get('telegram_activity_label', 'активен')} · "
                                "интерес: "
                                f"{candidate.get('potential_interest', 'неясно')} · "
                                "теплота: "
                                f"{candidate.get('warmth', 'неясно')}"
                            )
                            obstacles = candidate.get("obstacles") or []
                            if obstacles:
                                st.caption(
                                    "На что обратить внимание: "
                                    + "; ".join(str(x) for x in obstacles)
                                )
                            portrait = str(
                                candidate.get("short_portrait") or ""
                            ).strip()
                            if portrait:
                                st.caption(portrait)

                if st.button(
                    "✅ Выбрать и передать Неоне",
                    type="primary",
                    disabled=len(chosen_ids) < 1,
                    key=f"stagirite_confirm_people_{task.get('id', task.get('created_at'))}",
                    use_container_width=True,
                ):
                    _save_stagirite_candidate_selection(owner_id, chosen_ids)
                    task_id = str(task.get("id") or "")
                    if task_id:
                        updated_result = dict(result)
                        updated_result["selected_candidate_ids"] = [int(x) for x in chosen_ids]
                        updated_result["selection_note"] = (
                            f"Выбрано {len(chosen_ids)} из цели "
                            f"{needed_count} встреч(а). Работа начата."
                        )
                        _update_task(
                            owner_id,
                            task_id,
                            {"status": "В работе", "result": updated_result},
                        )
                    st.session_state["stagirite_open_agent"] = "Неона"
                    st.rerun()
            else:
                st.info(
                    "Свободное время найдено. Неонии пока нужно подготовить "
                    "кандидатов для этого поручения."
                )
                if st.button(
                    "➡️ Открыть Неонию",
                    key=f"stagirite_to_neonia_{task.get('id', task.get('created_at'))}",
                    use_container_width=True,
                ):
                    st.session_state["stagirite_open_agent"] = "Неония"
                    st.rerun()

        elif slots and selected_count >= needed_count:
            st.success(
                f"Время найдено, вы выбрали {selected_count} человек(а). "
                "Следующий этап — Неона готовит персональные сообщения и ведёт диалоги."
            )
            if st.button(
                "➡️ Продолжить с Неоной",
                key=f"stagirite_to_neona_{task.get('id', task.get('created_at'))}",
                use_container_width=True,
            ):
                st.session_state["stagirite_open_agent"] = "Неона"
                st.rerun()

        summary = (
            result.get("progress_summary")
            if isinstance(result.get("progress_summary"), dict)
            else {}
        )
        if int(summary.get("needs_reserve", 0) or 0) > 0:
            current_ids = set(task_selected_ids)
            reserve_pool = [
                item
                for item in _meeting_candidate_pool(owner_id, limit=10)
                if int(item.get("telegram_id")) not in current_ids
            ]
            if reserve_pool:
                st.markdown("**🛟 Резервный кандидат**")
                reserve_by_id = {
                    int(item["telegram_id"]): item
                    for item in reserve_pool
                }
                reserve_id = st.selectbox(
                    "Кого добавить в работу",
                    options=list(reserve_by_id.keys()),
                    format_func=lambda cid: _candidate_label(
                        reserve_by_id[cid]
                    ),
                    key=(
                        "stagirite_reserve_"
                        f"{task.get('id', task.get('created_at'))}"
                    ),
                )
                if st.button(
                    "➕ Добавить резервного кандидата",
                    key=(
                        "stagirite_add_reserve_"
                        f"{task.get('id', task.get('created_at'))}"
                    ),
                    use_container_width=True,
                ):
                    _append_reserve_candidate(
                        owner_id,
                        task,
                        int(reserve_id),
                    )
                    st.session_state["stagirite_open_agent"] = "Неона"
                    st.rerun()

    if result.get("content"):
        st.divider()
        st.markdown("## 🪄 Мастер контента")
        st.caption(
            "Стагирит сначала превратил ваше поручение в творческий бриф, "
            "а Мастер контента написал публикацию. "
            "Черновик → ваша правка → утверждение → публикация."
        )

        task_id = str(task.get("id") or task.get("created_at") or "content")
        draft_key = f"stagirite_content_draft_{task_id}"

        saved_text = str(
            result.get("edited_content")
            or result.get("content")
            or ""
        )
        if draft_key not in st.session_state:
            st.session_state[draft_key] = saved_text

        draft = st.text_area(
            "Готовый анонс / пост",
            key=draft_key,
            height=260,
        )

        st.caption(
            "Проверьте три вещи: хочется ли читать после первой строки, "
            "видна ли живая сцена, есть ли мысль или улыбка в финале."
        )

        is_approved = bool(result.get("content_approved"))
        published_at = str(result.get("published_at") or "").strip()
        published_count = int(result.get("published_count") or 0)

        c1, c2 = st.columns(2)

        if c1.button(
            "💾 Сохранить правки",
            key=f"stagirite_save_content_{task_id}",
            use_container_width=True,
        ):
            updated = dict(result)
            updated["edited_content"] = str(draft).strip()
            updated["content_approved"] = False
            # Любая правка создаёт новую версию: её нужно снова утвердить.
            updated.pop("published_at", None)
            updated.pop("published_count", None)
            _update_task(
                owner_id,
                task_id,
                {"result": updated},
            )
            st.success("Правки сохранены. Теперь материал можно утвердить.")
            st.rerun()

        if c2.button(
            "✅ Утвердить материал",
            key=f"stagirite_approve_content_{task_id}",
            use_container_width=True,
        ):
            clean = str(draft).strip()
            if not clean:
                st.warning("Материал пустой.")
            else:
                updated = dict(result)
                updated["edited_content"] = clean
                updated["content_approved"] = True
                updated["content_approved_at"] = datetime.now(UTC).isoformat()
                updated.pop("published_at", None)
                updated.pop("published_count", None)
                _update_task(
                    owner_id,
                    task_id,
                    {"result": updated, "status": "Готово к публикации"},
                )
                st.rerun()

        if is_approved:
            st.success("✅ Материал утверждён владельцем.")

            recipients = structure_member_ids(owner_id)
            recipients_count = len(recipients)
            zoom_link = str(
                result.get("zoom_link")
                or _load_stagirite_settings(owner_id).get("zoom_link")
                or ""
            ).strip()

            if published_at:
                st.success(
                    f"📣 Опубликовано всей структуре: {published_count} получател(я/ей)."
                )
                st.caption(
                    "Партнёры видят материал в разделе «Сообщения» Агентства W. "
                    "Им не нужно пересылать его дальше по цепочке."
                )
            elif recipients_count == 0:
                st.info(
                    "В вашей структуре пока нет зарегистрированных получателей "
                    "для внутренней публикации."
                )
            else:
                st.caption(
                    f"Получатели: вся ваша нижестоящая структура — "
                    f"{recipients_count} человек(а), на любой глубине."
                )
                if st.button(
                    f"📣 Отправить всей структуре ({recipients_count})",
                    key=f"stagirite_publish_structure_{task_id}",
                    use_container_width=True,
                    type="primary",
                    disabled=publish_structure_message is None,
                ):
                    try:
                        count = publish_structure_message(
                            owner_id,
                            str(
                                result.get("edited_content")
                                or draft
                            ).strip(),
                            subject="Анонс Агентства W",
                            zoom_url=zoom_link,
                        )
                        updated = dict(result)
                        updated["edited_content"] = str(
                            result.get("edited_content")
                            or draft
                        ).strip()
                        updated["content_approved"] = True
                        updated["published_at"] = datetime.now(UTC).isoformat()
                        updated["published_count"] = int(count)
                        _update_task(
                            owner_id,
                            task_id,
                            {"result": updated, "status": "Выполнено"},
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(
                            "Не удалось опубликовать материал всей структуре. "
                            f"{type(exc).__name__}: {exc}"
                        )


        st.markdown("### 🎨 Художник-иллюстратор")
        st.caption(
            "Стагирит сначала превращает смысл поста в режиссёрский бриф, "
            "а Художник рисует уже по этому брифу. "
            "Изображение создаётся только по вашей команде."
        )

        image_state_key = f"stagirite_artist_image_{task_id}"
        image_meta_key = f"stagirite_artist_meta_{task_id}"
        change_key = f"stagirite_artist_change_{task_id}"

        format_label = st.selectbox(
            "Формат иллюстрации",
            options=[
                "Квадрат — пост",
                "Вертикальный — сторис / мобильный пост",
                "Горизонтальный — анонс / обложка",
            ],
            key=f"stagirite_artist_format_{task_id}",
        )
        size_map = {
            "Квадрат — пост": "1024x1024",
            "Вертикальный — сторис / мобильный пост": "1024x1536",
            "Горизонтальный — анонс / обложка": "1536x1024",
        }
        chosen_size = size_map[format_label]

        current_image = st.session_state.get(image_state_key)
        current_meta = st.session_state.get(image_meta_key, {})
        if not isinstance(current_meta, dict):
            current_meta = {}

        if current_image:
            st.image(
                current_image,
                caption="Иллюстрация Художника",
                use_container_width=True,
            )

            source_changed = (
                str(current_meta.get("source_text") or "").strip()
                != str(draft or "").strip()
            )
            if source_changed:
                st.warning(
                    "После создания картинки текст был изменён. "
                    "При необходимости перерисуйте иллюстрацию под новую версию."
                )

            st.download_button(
                "⬇️ Сохранить PNG",
                data=current_image,
                file_name=(
                    "agency_w_illustration_"
                    + datetime.now(BERLIN).strftime("%Y%m%d")
                    + ".png"
                ),
                mime="image/png",
                key=f"stagirite_artist_download_{task_id}",
                use_container_width=True,
            )

            change_request = st.text_input(
                "Что изменить в сцене?",
                placeholder=(
                    "Например: слева виртуальный офис, справа завтрак директора "
                    "со взрослым сыном; Неола работает со взрослым новичком…"
                ),
                key=change_key,
            )

            if st.button(
                "🔄 Перерисовать",
                key=f"stagirite_artist_redraw_{task_id}",
                use_container_width=True,
                disabled=generate_image_fn is None,
            ):
                if generate_image_fn is None:
                    st.warning("Художник ещё не подключён.")
                else:
                    with st.spinner(
                        "Стагирит уточняет бриф, Художник создаёт новую версию..."
                    ):
                        generated = generate_image_fn(
                            str(draft).strip(),
                            change_request=change_request,
                            size=chosen_size,
                        )
                    if generated.get("ok"):
                        st.session_state[image_state_key] = generated["image_bytes"]
                        st.session_state[image_meta_key] = {
                            "source_text": str(draft).strip(),
                            "size": chosen_size,
                            "change_request": str(change_request or "").strip(),
                            "created_at": datetime.now(UTC).isoformat(),
                        }
                        st.rerun()
                    else:
                        st.error(
                            str(
                                generated.get("error")
                                or "Не удалось создать иллюстрацию."
                            )
                        )

        else:
            st.info(
                "Пока есть только текст. Когда он вас устраивает, "
                "попросите Художника создать к нему иллюстрацию."
            )
            if st.button(
                "🎨 Создать иллюстрацию",
                key=f"stagirite_artist_create_{task_id}",
                use_container_width=True,
                type="primary",
                disabled=generate_image_fn is None,
            ):
                if generate_image_fn is None:
                    st.warning("Художник ещё не подключён.")
                else:
                    with st.spinner(
                        "Стагирит ставит художественную задачу, Художник рисует..."
                    ):
                        generated = generate_image_fn(
                            str(draft).strip(),
                            size=chosen_size,
                        )
                    if generated.get("ok"):
                        st.session_state[image_state_key] = generated["image_bytes"]
                        st.session_state[image_meta_key] = {
                            "source_text": str(draft).strip(),
                            "size": chosen_size,
                            "change_request": "",
                            "created_at": datetime.now(UTC).isoformat(),
                        }
                        st.rerun()
                    else:
                        st.error(
                            str(
                                generated.get("error")
                                or "Не удалось создать иллюстрацию."
                            )
                        )

        st.caption(
            "Сейчас изображение можно сохранить как PNG и использовать вместе "
            "с постом. Автоматическую доставку самой картинки по внутренней "
            "рассылке подключим отдельным шагом после проверки Художника."
        )

    if result.get("content_error"):
        st.error("Не удалось подготовить материал. Попробуйте ещё раз чуть позже.")

    if result.get("note"):
        st.info(str(result["note"]))

def render_stagirite_center(
    owner_telegram_id: int,
    owner_name: str,
    ask_openai_fn,
    prepare_candidates_fn=None,
    generate_image_fn=None,
) -> None:
    owner_id = int(owner_telegram_id)
    assignment_key = f"stagirite_assignment_{owner_id}"
    clear_key = f"stagirite_clear_assignment_{owner_id}"

    # Меняем состояние текстового поля только ДО создания виджета Streamlit.
    if st.session_state.pop(clear_key, False):
        st.session_state[assignment_key] = ""
    pending_voice = str(st.session_state.pop(f"stagirite_voice_pending_{owner_id}", "") or "").strip()
    if pending_voice:
        st.session_state[assignment_key] = pending_voice

    st.markdown("### 🧭 Стагирит")
    st.caption(
        "Заместитель Директора. Скажите, какой результат нужен — "
        "Стагирит организует работу Агентства."
    )
    st.caption(
        "Контент: Мастер текста → Художник → правка → утверждение → публикация."
    )

    settings = _load_stagirite_settings(owner_id)
    with st.expander("🔗 Zoom для встреч и анонсов"):
        st.caption(
            "Сохраните постоянную ссылку один раз. Стагирит сможет вставлять "
            "её в анонсы, а Неона — передавать человеку при подтверждённой Zoom-встрече."
        )
        zoom_link_value = st.text_input(
            "Постоянная ссылка Zoom",
            value=str(settings.get("zoom_link") or ""),
            placeholder="https://....zoom.us/j/...",
            key=f"stagirite_zoom_link_input_{owner_id}",
        )
        zoom_note_value = st.text_input(
            "Дополнение (необязательно)",
            value=str(settings.get("zoom_note") or ""),
            placeholder="Например: код доступа или короткая подпись",
            key=f"stagirite_zoom_note_input_{owner_id}",
        )
        if st.button(
            "💾 Сохранить Zoom",
            key=f"stagirite_save_zoom_{owner_id}",
            use_container_width=True,
        ):
            if not _valid_zoom_link(zoom_link_value):
                st.error("Проверьте ссылку Zoom: она должна начинаться с https://.")
            else:
                _save_stagirite_settings(
                    owner_id,
                    {
                        "zoom_link": zoom_link_value,
                        "zoom_note": zoom_note_value,
                    },
                )
                st.success("✅ Zoom сохранён. Стагирит будет использовать его в работе.")

    def submit_assignment(clean_text: str) -> None:
        clean_text = str(clean_text or "").strip()
        if not clean_text:
            st.warning("Скажите Стагириту, какой результат вам нужен.")
            return
        with st.spinner("Стагирит выполняет поручение..."):
            task = _process_assignment(
                owner_id,
                owner_name,
                clean_text,
                ask_openai_fn,
            )
            _save_task(owner_id, task)
            st.session_state[f"stagirite_last_task_{owner_id}"] = task
        st.session_state[clear_key] = True
        st.rerun()

    with st.container(border=True):
        st.markdown("**🎯 Новое поручение**")
        assignment = st.text_area(
            "Поручение Стагириту",
            placeholder=(
                "Например: «Подбери несколько кандидатов, чтобы на следующей "
                "неделе состоялась хотя бы одна Zoom-встреча» или "
                "«Сделай два поста и анонс Агентства W»."
            ),
            height=105,
            key=assignment_key,
        )

        if st.button(
            "▶️ Выполнить поручение",
            key=f"stagirite_run_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            submit_assignment(assignment)

        with st.expander("🎙 Сказать поручение голосом"):
            st.caption(
                "Запишите поручение и остановите запись — Стагирит примет его автоматически."
            )
            if hasattr(st, "audio_input"):
                audio = st.audio_input(
                    "Скажите поручение",
                    key=f"stagirite_audio_{owner_id}",
                    label_visibility="collapsed",
                )
                if audio is not None:
                    audio_bytes = audio.getvalue()
                    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
                    processed_key = f"stagirite_voice_processed_{owner_id}"
                    if st.session_state.get(processed_key) != audio_hash:
                        # Сначала запоминаем hash: rerun не создаст дубликат поручения.
                        st.session_state[processed_key] = audio_hash
                        try:
                            with st.spinner("Стагирит слушает..."):
                                transcript = _transcribe_audio(
                                    audio_bytes,
                                    getattr(audio, "name", "stagirite-command.wav"),
                                )
                            transcript = str(transcript or "").strip()
                            if transcript:
                                st.info(f"Вы сказали: {transcript}")
                                submit_assignment(transcript)
                            else:
                                st.warning("Не удалось разобрать речь. Попробуйте ещё раз.")
                        except Exception:
                            st.error("Не удалось распознать поручение. Попробуйте ещё раз.")
            else:
                st.caption("Можно дать поручение текстом выше.")

    tasks, persistent = _load_tasks(owner_id)

    st.markdown("### 📋 Текущее поручение")
    if not tasks:
        st.info("Поручений пока нет.")
        return

    # Показываем только последнее поручение полностью. Старые тесты не растягивают страницу.
    task = tasks[0]
    status = str(task.get("status") or "planned")
    assignment_text = str(task.get("assignment") or "").strip()
    task_id = str(task.get("id") or task.get("created_at") or "")
    created = str(task.get("created_at") or "")
    task_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    task_kind = str(task.get("task_kind") or "")

    display_status = status
    if "meetings" in task_kind:
        refreshed_result, refreshed_status = _refresh_meeting_task(
            owner_id,
            task,
        )
        task_result = refreshed_result
        if (
            refreshed_result != task.get("result")
            or refreshed_status != status
        ):
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "status": refreshed_status,
                        "result": refreshed_result,
                    },
                )
            task["result"] = refreshed_result
            task["status"] = refreshed_status
            status = refreshed_status

        needed = int(task_result.get("meeting_count", 1) or 1)
        summary = (
            task_result.get("progress_summary")
            if isinstance(task_result.get("progress_summary"), dict)
            else {}
        )
        scheduled = int(summary.get("scheduled", 0) or 0)
        selected_ids = task_result.get("selected_candidate_ids", [])
        selected_count = len(selected_ids) if isinstance(selected_ids, list) else 0

        if scheduled >= needed:
            display_status = "Выполнено"
        elif selected_count >= 1:
            display_status = "В работе"
        elif (task_result.get("meetings") or {}).get("slots"):
            display_status = "Нужен ваш выбор"

    with st.container(border=True):
        st.markdown(f"#### {_status_icon(display_status)} {display_status}")
        st.write(assignment_text)
        if created:
            try:
                parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
                st.caption(parsed.astimezone(BERLIN).strftime("%d.%m.%Y · %H:%M"))
            except Exception:
                pass

        _render_result(
            task,
            owner_id,
            prepare_candidates_fn=prepare_candidates_fn,
            generate_image_fn=generate_image_fn,
        )

        if (
            "meetings" not in task_kind
            and status in {"Готово к утверждению", "Нужно решение владельца"}
        ):
            c1, c2 = st.columns(2)
            if c1.button(
                "✅ Утвердить",
                key=f"stagirite_approve_{task_id}",
                use_container_width=True,
            ):
                _update_task(owner_id, task_id, {"status": "Утверждено"})
                st.rerun()
            if c2.button(
                "✅ Считать выполненным",
                key=f"stagirite_done_{task_id}",
                use_container_width=True,
            ):
                _update_task(owner_id, task_id, {"status": "Выполнено"})
                st.rerun()

    previous = tasks[1:]
    if previous:
        with st.expander(f"🗂 История поручений · {len(previous)}"):
            for old in previous[:20]:
                old_status = str(old.get("status") or "")
                old_assignment = str(old.get("assignment") or "").strip()
                old_created = str(old.get("created_at") or "")
                when = ""
                if old_created:
                    try:
                        parsed = datetime.fromisoformat(old_created.replace("Z", "+00:00"))
                        when = parsed.astimezone(BERLIN).strftime("%d.%m · %H:%M")
                    except Exception:
                        pass
                suffix = f" · {when}" if when else ""
                st.markdown(
                    f"**{_status_icon(old_status)} {old_status}**{suffix}  \n"
                    f"{old_assignment}"
                )

