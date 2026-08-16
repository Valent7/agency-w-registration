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
                return (rows if isinstance(rows, list) else []), True
            if response.status_code not in {400, 404}:
                response.raise_for_status()
        except Exception:
            pass

    return list(st.session_state.get(_task_key(owner_id), [])), False


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
    """Понимает «одна встреча» как 1, а неопределённое «встречи» — как 2."""
    lowered = str(text or "").lower()
    explicit = _extract_number_before(lowered, r"встреч", default=0)
    if explicit:
        return explicit
    if re.search(r"\bвстреч(?:а|у)\b", lowered):
        return 1
    return 2


def _detect_intents(text: str) -> list[str]:
    lowered = text.lower()
    intents: list[str] = []

    if any(x in lowered for x in ("встреч", "созвон", "календар", "свободн")):
        intents.append("meetings")
    if any(x in lowered for x in (
        "пост", "анонс", "контент", "публикац", "иллюстрац",
        "картин", "продвиж", "текст для чата", "сообщение команде",
    )):
        intents.append("content")
    if any(x in lowered for x in ("команд", "структур", "партнёр", "партнер")):
        intents.append("team")

    return intents or ["general"]


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
    start_day = explicit_day or datetime.now(MSK).date()

    days = [explicit_day] if explicit_day else [
        start_day + timedelta(days=offset) for offset in range(0, 14)
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

    return {
        "found": len(labels) >= count,
        "day": best_day.isoformat(),
        "slots": labels,
        "message": (
            f"Нашёл день, где можно подготовить {count} встреч(и)."
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
    """Последние подготовленные Неонией кандидаты для выбора прямо у Стагирита."""
    owner_id = int(owner_id)
    candidates = st.session_state.get(f"neonia_candidates_{owner_id}", [])
    if not isinstance(candidates, list):
        return []

    usable: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
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
    return f"{name}{username_part} · {activity} · интерес: {interest}"


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
            counts["scheduled"] += 1
            item.update(
                {
                    "status": "meeting_scheduled",
                    "meeting_id": meeting.get("id"),
                    "meeting_start_at": meeting.get("start_at"),
                    "meeting_format": meeting.get("meeting_format"),
                }
            )
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



def _generate_content(ask_openai_fn, owner_name: str, assignment: str) -> str:
    instructions = f"""
Ты — Стагирит, заместитель Директора Агентства W.
Директор: {owner_name}.

Твоя задача сейчас — организовать создание контента по поручению Директора.

Философия Агентства W:
- ИИ берёт на себя рутину, человек принимает решения и строит отношения.
- Главная ценность — возвращать человеку время.
- Девиз: «Мы создаём своё настоящее».
- Не обещай функций, которых ещё нет.
- Не называй Агентство просто чат-ботом.
- Текст должен быть человеческим, ясным и без рекламного крика.

Подготовь ровно то, что попросил Директор.
Если нужны посты/анонсы — дай готовые тексты.
Если нужны иллюстрации — НЕ выдумывай, что картинка уже создана; дай короткое техническое задание для визуального агента под каждый материал.
Если запрос на несколько материалов — раздели их понятными заголовками.
Не добавляй длинных объяснений о своей работе.
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
            "Проверить календарь без обращения к OpenAI.",
            f"Найти свободный день и {meeting_count} подходящих окна с часовым буфером.",
            "Проверить, есть ли уже подготовленные кандидаты Неонии.",
            "Передать выбранных владельцем людей Неоне для диалога и согласования встречи.",
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
            result["meetings"] = _find_meeting_day(owner_id, assignment, meeting_count)
            result["candidates"] = _candidate_snapshot(owner_id)
            status = "Нужно решение владельца"
        except Exception as exc:
            result["meetings"] = {
                "found": False,
                "message": f"Не удалось прочитать календарь: {exc}",
                "slots": [],
            }
            status = "Ошибка"

    if "content" in intents:
        try:
            content = _generate_content(ask_openai_fn, owner_name, assignment)
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
        "Ошибка": "🔴",
    }.get(status, "⚙️")


def _render_result(task: dict[str, Any], owner_id: int) -> None:
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

    meetings = result.get("meetings")
    if isinstance(meetings, dict):
        st.markdown("**📅 Свободное время**")
        st.write(str(meetings.get("message") or ""))
        slots = meetings.get("slots") or []
        for idx, slot in enumerate(slots, start=1):
            st.markdown(
                f"**{idx}. {slot.get('msk', '')}**  \n"
                f"{slot.get('berlin', '')}"
            )

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
                    elif state == "dialogue":
                        line = "💬 ответил(а), Неона ведёт диалог"
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
                    "По одному из контактов ответа нет уже больше суток. "
                    "Неона повторно ему не пишет. Можно выбрать резервного кандидата."
                )

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

        if slots and selected_count < needed_count:
            pool = _meeting_candidate_pool(owner_id, limit=10)
            if pool:
                st.markdown("**👥 Выберите людей для работы Неоны**")
                st.caption(
                    f"Для цели «{needed_count} встречи» можно выбрать от "
                    f"{needed_count} до 5 человек. Это единственный шаг, "
                    "который Стагирит оставляет вам: решение, с кем начинать разговор."
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
                    disabled=len(chosen_ids) < needed_count,
                    key=f"stagirite_confirm_people_{task.get('id', task.get('created_at'))}",
                    use_container_width=True,
                ):
                    _save_stagirite_candidate_selection(owner_id, chosen_ids)
                    task_id = str(task.get("id") or "")
                    if task_id:
                        updated_result = dict(result)
                        updated_result["selected_candidate_ids"] = [int(x) for x in chosen_ids]
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
        st.markdown("**✍️ Готовый материал**")
        st.markdown(str(result["content"]))

    if result.get("content_error"):
        st.error("Не удалось подготовить материал. Попробуйте ещё раз чуть позже.")

    if result.get("note"):
        st.info(str(result["note"]))

def render_stagirite_center(owner_telegram_id: int, owner_name: str, ask_openai_fn) -> None:
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
                "Например: «На 17 августа подготовь одну встречу» "
                "или «Сделай два поста и анонс Агентства W»."
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

        _render_result(task, owner_id)

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

