from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from PIL import Image, UnidentifiedImageError

from agency_core import agency_core_prompt, mission_director_answer, mission_test_prompt

import agency_calendar
try:
    from team_center import (
        attach_structure_image_to_published_message,
        publish_structure_material,
        publish_structure_message,
        publish_structure_video,
        structure_member_ids,
    )
except ImportError:
    publish_structure_message = None
    publish_structure_material = None
    publish_structure_video = None
    attach_structure_image_to_published_message = None

    def structure_member_ids(owner_telegram_id: int) -> list[int]:
        return []

try:
    from agency_publisher import (
        list_publisher_destinations,
        publish_to_publisher_destinations,
    )
except ImportError:
    list_publisher_destinations = None
    publish_to_publisher_destinations = None

from workspace_persistence import persist_workspace_if_changed


UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")

STAGIRITE_CORE = agency_core_prompt(
    "Стагирит",
    "координировать Агентство по целям Директора, распределять работу и возвращать результат на решение",
)
CONTENT_MASTER_CORE = agency_core_prompt(
    "Мастер контента",
    "создавать понятный человеческий контент, который служит миссии Агентства и не подменяет факты рекламой",
)
MISSION_TEST_CORE = mission_test_prompt()

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
ФАКТЫ О ТЕКУЩЕЙ ВЕРСИИ АГЕНТСТВА W (23.08.2026):

ГОТОВО И МОЖНО ПОКАЗЫВАТЬ ПАРТНЁРАМ:
- КРИТИЧЕСКИ ВАЖНО: ежедневная рабочая пятёрка НЕ зависит от активной недельной цели.
  Если сохранённый пул Неонии уже есть, Стагирит готовит пятёрку на день независимо от недельной кампании.
  Никогда не говори, что сегодняшняя пятёрка появляется только «при активной недельной цели».
- Стагирит — заместитель Директора и координатор работы Агентства.
- Он принимает поручение как желаемый результат, а не как список кнопок.
- Недельная цель встреч ставится один раз: например минимум 3, желательно до 5.
- Для недельной цели Стагирит НЕ бронирует часы заранее.
- Сначала Неония готовит кандидатов, Директор выбирает людей,
  Неона ведёт ответы и выясняет удобный человеку день/время.
- Только после согласия человека Стагирит проверяет календарь,
  исключает накладки и встреча записывается.
- Неония для первого холодного обращения работает с реальными
  Telegram-контактами владельца и учитывает активность сегодня/вчера.
- Недельный резерв может быть до 50 контактов; ежедневно готовится
  рабочая пятёрка, а не весь резерв одновременно.
- Ежедневная рабочая пятёрка готовится из уже сохранённого пула Неонии независимо от недельной цели:
  владельцу не нужно каждый день вручную проходить Стагирит → Неония → Неона.
- Неона готовит персональное первое сообщение.
  Первую отправку обязательно утверждает владелец.
- После ответа человека Неона продолжает диалог и ведёт к осознанной встрече.
- Неола — голосовой наставник взрослого новичка: ведёт от первых шагов
  до первого партнёра и помогает повторять рабочую систему.
- Мастер контента работает под руководством Стагирита.
- Художник-иллюстратор работает под руководством Стагирита-арт-директора.
- Если первое сообщение Telegram не отправил, контакт не считается обработанным
  и может быть отложен/переведён на другой допустимый канал.
- Главный принцип Агентства: ИИ берёт на себя рутину,
  человек принимает решения, строит отношения и живёт свою жизнь.
- Девиз: «Мы создаём своё настоящее».

ВАЖНО:
- WhatsApp и другие дополнительные каналы находятся в дорожной карте
  и ещё не должны описываться как уже подключённые.
- Нельзя обещать гарантированные встречи, партнёров или доход.
- Нельзя писать, что Стагирит принимает человеческие решения вместо владельца.
- Нельзя описывать несуществующее действие как уже работающее.
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
                    "limit": 100,
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
    st.session_state[_task_key(owner_id)] = fallback[:100]
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



def _delete_task(owner_id: int, task_id: str) -> None:
    """Удаляет поручение из Supabase или локального fallback."""
    owner_id = int(owner_id)
    task_id = str(task_id or "").strip()

    if task_id and not task_id.startswith("local-") and _db_available():
        url, _ = _supabase_config()
        try:
            response = requests.delete(
                f"{url}/rest/v1/agency_stagirite_tasks",
                headers=_db_headers("return=minimal"),
                params={
                    "id": f"eq.{task_id}",
                    "owner_telegram_id": f"eq.{owner_id}",
                },
                timeout=20,
            )
            if response.ok:
                return
        except Exception:
            pass

    fallback = list(st.session_state.get(_task_key(owner_id), []))
    fallback = [
        item
        for item in fallback
        if str(item.get("id") or "") != task_id
    ]
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

        # Редакция «Хроники Агентства W».
        # Эти формулировки должны без уточнений идти
        # прямо в Мастера контента.
        "хроник",
        "выпуск",
        "новелл",
        "фельетон",
        "рассказ",
        "зарисовк",
        "притч",
    )
    content_requested = any(x in lowered for x in content_words)

    # Дополнительная страховка для естественных формулировок:
    # «напиши новый выпуск Хроник...», «сделай фельетон...»
    # не должны попадать в general даже при будущих изменениях словаря.
    creative_action = any(
        token in lowered
        for token in (
            "напиши новый выпуск",
            "напиши выпуск",
            "создай выпуск",
            "сделай выпуск",
            "напиши хроник",
            "новый выпуск хроник",
            "напиши новелл",
            "напиши фельетон",
            "напиши рассказ",
        )
    )
    content_requested = bool(
        content_requested
        or creative_action
    )

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



def _has_explicit_weekly_start(text: str) -> bool:
    """
    True только когда Директор действительно задал отдельную дату старта:
    «начать с 24 августа», «с будущего понедельника»,
    «начиная с 01.09.2026».

    Само выражение «на следующей неделе» для недельной кампании
    НЕ откладывает старт: это рабочая цель на ближайшие 7 дней.
    """
    lowered = str(text or "").lower().replace("ё", "е")

    # Явные глаголы/предлоги старта.
    start_prefix = r"(?:начать|запустить|стартовать|начиная|начиная\s+с|с)"

    # Числовые даты: 24.08, 24.08.2026, 24/08/2026, 24-08.
    numeric_date = (
        r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b"
    )

    months = (
        "января|февраля|марта|апреля|мая|июня|июля|"
        "августа|сентября|октября|ноября|декабря"
    )
    word_date = rf"\b\d{{1,2}}\s+(?:{months})\b"

    weekdays = (
        "понедельника|вторника|среды|четверга|пятницы|"
        "субботы|воскресенья"
    )
    future_weekday = rf"\b(?:будущего|следующего)\s+(?:{weekdays})\b"

    if re.search(
        rf"\b(?:начать|запустить|стартовать|начиная)(?:\s+работу)?\s+"
        rf"(?:с\s+)?(?:{numeric_date}|{word_date}|{future_weekday})",
        lowered,
    ):
        return True

    if re.search(
        rf"\bс\s+(?:{numeric_date}|{word_date}|{future_weekday})",
        lowered,
    ):
        return True

    return False

def _weekly_period(text: str) -> tuple[date, date]:
    """
    Недельная цель встреч — это активная кампания на ближайшие 7 дней.

    «На следующей неделе хочу 3–5 встреч» НЕ означает ждать следующего
    календарного понедельника. Стагирит начинает сегодня.

    Отложенный старт допускается только при явной формулировке:
    «начать с 24 августа», «с будущего понедельника» и т.п.
    """
    today = datetime.now(BERLIN).date()

    if _has_explicit_weekly_start(text):
        period = _target_period_from_text(text)
        if period:
            return period[0], period[-1]

        explicit_day = _target_date_from_text(text)
        if explicit_day and explicit_day >= today:
            return explicit_day, explicit_day + timedelta(days=6)

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



def _content_mode_from_assignment(assignment: str) -> str:
    lowered = str(assignment or "").lower().replace("ё", "е")

    if (
        "непричесанн" in lowered
        and "мысл" in lowered
    ):
        return "night_thought"

    if (
        "мысль дня" in lowered
        or "мысли дня" in lowered
    ):
        return "day_thought"

    if any(
        token in lowered
        for token in (
            "анонс",
            "приглаш",
            "zoom",
            "зум",
            "продающ",
        )
    ):
        return "announcement"

    if any(
        token in lowered
        for token in (
            "список",
            "тезис",
            "инструкц",
            "план ",
            "пункты",
            "чек-лист",
            "чеклист",
        )
    ):
        return "informational"

    return "story"


CHRONICLES_GENRES = (
    "мини-новелла",
    "фельетон",
    "короткий рассказ",
    "диалог",
    "журнальная зарисовка",
    "ироничное наблюдение",
    "маленькая притча",
    "письмо из недалёкого будущего",
    "сцена из виртуального офиса",
    "история одного решения",
)

CHRONICLES_HUMAN_SITUATIONS = (
    "утро предпринимателя, когда не хочется снова открывать десятки чатов",
    "вечер дома, когда работа раньше продолжала сидеть за столом вместе с семьёй",
    "прогулка, поездка или встреча с близкими, во время которой бизнес не останавливается",
    "новичок, которому неловко в третий раз задавать один и тот же вопрос",
    "директор, который раньше сам был секретарём, аналитиком, диспетчером и напоминалкой",
    "человек, который впервые за долгое время не боится что-то забыть",
    "маленькая рабочая проблема, которая раньше съедала целый час",
    "момент, когда вместо десятка действий человеку остаётся одно решение",
    "обычный выходной, в который работа перестаёт требовать постоянного присутствия",
    "конец дня без ощущения, что половина дел опять потерялась между чатами",
    "первый спокойный день взрослого новичка рядом с терпеливым наставником",
    "ситуация, когда важный разговор начинается без массовой рассылки и давления",
)

CHRONICLES_BANNED_CLICHES = (
    "Кружка остывает на столе",
    "Кофе остыл",
    "Представьте модельный рабочий день",
    "Утро ещё не решило, каким будет",
    "Пустое место в календаре",
    "Что бы вы сделали с лишним часом?",
    "Будущее уже наступило",
    "ИИ меняет мир",
)


CHRONICLES_CHARACTER_CARDS = {
    "Стагирит": (
        "ИИ-заместитель Директора Агентства W: принимает цель, "
        "координирует специалистов и следит за выполнением."
    ),
    "Неония": (
        "ИИ-аналитик Агентства W: помогает находить и разбирать "
        "подходящих людей для начала общения."
    ),
    "Неона": (
        "ИИ-секретарь-референт Агентства W: готовит первые сообщения "
        "и после ответа поддерживает человеческий диалог."
    ),
    "Неола": (
        "ИИ-наставник Агентства W: спокойно ведёт взрослого новичка "
        "от первых шагов к самостоятельной работе."
    ),
}

CHRONICLES_FIRST_READER_RULE = """
КАЖДЫЙ выпуск — самостоятельная точка входа в мир Агентства W.

Читатель может:
- никогда раньше не видеть ни одного выпуска;
- не знать, кто такие Неона, Неония, Неола и Стагирит;
- впервые слышать название «Агентство W».

Поэтому:
1. Нельзя писать так, будто читатель обязан помнить предыдущую серию.
2. При ПЕРВОМ упоминании внутреннего героя дай естественную короткую
   расшифровку роли прямо внутри фразы — 3–9 слов, без справочника.
   Пример принципа: «Неона, ИИ-секретарь Агентства W, ...».
3. Не вводи больше двух внутренних героев по имени в одном выпуске,
   если сюжет не требует большего.
4. Если название «Агентство W» появляется впервые, из контекста должно
   быть понятно, что это ИИ-команда, которая забирает бизнес-рутину.
5. Никаких фраз «как вы помните», «в прошлой серии», «снова Неона»
   без самостоятельного объяснения.
6. Объяснение роли — один лёгкий штрих. Не превращай рассказ в каталог агентов.
7. После прочтения человек без предыстории должен понимать:
   КТО действует, ЧТО произошло, КАКУЮ рутину сняло Агентство
   и ЧТО получил человек.
""".strip()


def _parse_editor_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}

    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*```$", "", text)

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _recent_chronicles_history(
    owner_id: int,
    limit: int = 12,
) -> list[dict[str, str]]:
    """
    Память редакции: какие способности, ситуации и жанры уже использовались.
    Никаких AI-вызовов.
    """
    tasks, _ = _load_tasks(int(owner_id))
    history: list[dict[str, str]] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue

        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        content = str(
            result.get("edited_content")
            or result.get("content")
            or ""
        ).strip()
        if not content:
            continue

        meta = (
            result.get("content_master")
            if isinstance(result.get("content_master"), dict)
            else {}
        )

        assignment_lower = str(
            task.get("assignment") or ""
        ).lower().replace("ё", "е")
        series_name = str(meta.get("series") or "").strip()

        # Для памяти сериала считаем именно «Хроники», а не все посты подряд.
        if (
            series_name
            and series_name != "Хроники Агентства W"
            and "хроник" not in assignment_lower
        ):
            continue

        history.append(
            {
                "assignment": str(
                    task.get("assignment") or ""
                )[:220],
                "genre": str(
                    meta.get("genre") or ""
                )[:80],
                "capability": str(
                    meta.get("capability") or ""
                )[:220],
                "human_situation": str(
                    meta.get("human_situation") or ""
                )[:220],
                "opening": content[:180],
            }
        )

        if len(history) >= max(1, int(limit)):
            break

    return history


def _chronicles_soft_cta_allowed(
    recent_history: list[dict[str, str]],
) -> bool:
    """
    Не превращаем сериал в рекламную ленту.
    Примерно каждый третий выпуск может иметь мягкий вход.
    """
    return (len(recent_history) + 1) % 3 == 0


def _chronicles_brief_fallback(
    assignment: str,
    recent_history: list[dict[str, str]],
) -> dict[str, Any]:
    used_genres = {
        str(item.get("genre") or "")
        for item in recent_history[:5]
    }

    genre = next(
        (
            value
            for value in CHRONICLES_GENRES
            if value not in used_genres
        ),
        CHRONICLES_GENRES[0],
    )

    index = len(recent_history) % len(
        CHRONICLES_HUMAN_SITUATIONS
    )

    return {
        "series": "Хроники Агентства W",
        "genre": genre,
        "capability": (
            "ИИ-команда забирает повторяющуюся рутину, "
            "а человек оставляет за собой решения и отношения."
        ),
        "truth_for_story": [
            (
                "Стагирит координирует работу специализированных "
                "агентов по цели Директора."
            )
        ],
        "human_situation": CHRONICLES_HUMAN_SITUATIONS[index],
        "human_problem": (
            "человек вынужден держать в голове слишком много "
            "повторяющихся мелких действий"
        ),
        "human_gain": (
            "освободившееся внимание, время и спокойствие "
            "для жизни и человеческих решений"
        ),
        "scene": (
            "одна конкретная бытовая сцена с живым взрослым человеком"
        ),
        "intrigue": (
            "показать необычное отсутствие привычной суеты, "
            "а причину раскрыть не сразу"
        ),
        "humor": (
            "лёгкая узнаваемая ирония над старым способом всё тащить самому"
        ),
        "ending": (
            "короткий образ: работа продолжается, а человек наконец живёт"
        ),
        "reader_entry": (
            "выпуск полностью понятен человеку, который впервые "
            "слышит об Агентстве W"
        ),
        "characters": [],
        "cta_mode": (
            "soft"
            if _chronicles_soft_cta_allowed(recent_history)
            else "none"
        ),
    }


def _looks_like_report(text: str) -> bool:
    lowered = str(text or "").lower().replace("ё", "е")
    dry = (
        "представьте модельный рабочий день",
        "недельная цель ставится",
        "система ролей и правил",
        "резерв держится",
        "реальные telegram-контакты",
        "рабочая пятерка",
        "рабочая «пятерка»",
        "функционал системы",
        "в рамках проекта",
        "алгоритм работы",
        "согласно критериям",
        "процесс устроен",
        "минимум три встречи",
        "до пятидесяти контактов",
        "активность «сегодня/вчера»",
        "наша система позволяет",
        "решение представляет собой",
    )

    hits = sum(
        1
        for phrase in dry
        if phrase in lowered
    )
    numbered = len(
        re.findall(
            r"(?m)^\s*\d+[.)]\s+",
            str(text or ""),
        )
    )
    bullets = len(
        re.findall(
            r"(?m)^\s*[-•]\s+",
            str(text or ""),
        )
    )

    return (
        hits >= 1
        or numbered >= 3
        or bullets >= 4
    )


def _has_bad_story_voice(text: str) -> bool:
    lowered = str(text or "").lower().replace("ё", "е")

    bad_patterns = (
        r"\bя смотрю на календар",
        r"\bя включаюсь\b",
        r"\bя закрываю ноутбук",
        r"\bговорю я\b",
        r"\bговорю я себе",
        r"\bсдвигаю\b.*\bвстреч",
        r"\bдвигаю\b.*\bсозвон",
        r"\bпереношу\b.*\bвстреч",
    )

    return any(
        re.search(pattern, lowered, flags=re.S)
        for pattern in bad_patterns
    )


def _starts_with_tired_cliche(text: str) -> bool:
    opening = (
        str(text or "")
        .strip()
        .lower()
        .replace("ё", "е")[:260]
    )

    return any(
        str(cliche)
        .lower()
        .replace("ё", "е")
        in opening
        for cliche in CHRONICLES_BANNED_CLICHES
    )


def _content_quality_score(
    review: dict[str, Any],
) -> int:
    scores = review.get("scores")
    if not isinstance(scores, dict):
        return 0

    keys = (
        "hook",
        "story",
        "intrigue",
        "humor",
        "humanity",
        "clarity",
        "desire",
        "trust",
        "truth",
    )

    values = []
    for key in keys:
        try:
            values.append(
                max(
                    0,
                    min(
                        10,
                        int(scores.get(key, 0)),
                    ),
                )
            )
        except (TypeError, ValueError):
            values.append(0)

    return (
        round(sum(values) / len(values))
        if values
        else 0
    )





def _rubric_strip_leading_icon(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(
        r"^(?:💡|☀️|☀)\s*",
        "",
        value,
        count=1,
    ).strip()
    return value


def _rubric_with_icon(
    text: str,
    icon: str,
    *,
    limit: int = 350,
) -> str:
    value = _rubric_strip_leading_icon(text)
    prefix = f"{icon} "
    room = max(1, int(limit) - len(prefix))

    if len(value) > room:
        clipped = value[:room + 1]
        sentence_ends = [
            clipped.rfind("."),
            clipped.rfind("!"),
            clipped.rfind("?"),
            clipped.rfind("…"),
        ]
        end = max(sentence_ends)
        if end >= min(80, room // 2):
            value = clipped[:end + 1].strip()
        else:
            space = clipped.rfind(" ", 0, room)
            if space > 0:
                value = clipped[:space].rstrip(" ,;:-") + "…"
            else:
                value = clipped[:room].rstrip(" ,;:-") + "…"

    return (prefix + value).strip()



def _night_thought_clean(text: str) -> str:
    value = _rubric_strip_leading_icon(text)

    # Убираем оформление, списки и случайный заголовок.
    value = re.sub(
        r"(?im)^#{1,6}\s*неприч[её]санн(?:ые|ая)?\s+мысл(?:и|ь).*?$",
        "",
        value,
    ).strip()
    value = re.sub(r"(?m)^\s*[-•*]\s*", "", value)
    value = re.sub(r"(?m)^\s*\d+[.)]\s*", "", value)
    value = re.sub(r"\n{2,}", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _night_thought_limit(text: str, limit: int = 350) -> str:
    value = _night_thought_clean(text)
    if len(value) <= limit:
        return value

    clipped = value[:limit + 1]
    # Предпочтительно закончить на последней законченной фразе.
    sentence_ends = [
        clipped.rfind("."),
        clipped.rfind("!"),
        clipped.rfind("?"),
        clipped.rfind("…"),
    ]
    end = max(sentence_ends)
    if end >= 120:
        return clipped[:end + 1].strip()

    # Иначе хотя бы не режем слово.
    space = clipped.rfind(" ", 0, limit)
    if space > 0:
        return clipped[:space].rstrip(" ,;:-") + "…"

    return clipped[:limit].rstrip(" ,;:-") + "…"


def _night_thought_is_good(text: str) -> bool:
    value = _night_thought_clean(text)
    lowered = value.lower().replace("ё", "е")

    if not value or len(value) > 350:
        return False

    # Это должна быть одна мысль, а не мини-рассказ или список.
    if len(re.findall(r"\b(он|она|марк|анна|сын|дочь|утром|вечером)\b", lowered)) >= 3:
        return False
    if len(re.findall(r"(?m)^\s*\d+[.)]", value)) > 0:
        return False
    if value.count("\n") > 1:
        return False

    banned = (
        "агентство w помогает",
        "стагирит уже",
        "неона уже",
        "вот такая мысль",
        "а что бы вы",
        "представьте",
    )
    if any(part in lowered for part in banned):
        return False

    return len(value) >= 45


def _generate_night_thought_content(
    ask_openai_fn,
    ask_claude_fn,
    assignment: str,
) -> dict[str, Any]:
    """
    Рубрика «Непричёсанные мысли на ночь».

    Не рассказ.
    Не хроника.
    Не рекламный пост.
    Один афористический удар <= 350 знаков.
    """
    prompt = f"""
Ты — автор рубрики «Непричёсанные мысли на ночь».

{CONTENT_MASTER_CORE}

Поручение Директора:
{assignment}

ФОРМА:
- ОДНА короткая мысль;
- 1–3 предложения;
- максимум 350 знаков с пробелами;
- лучше 120–280 знаков;
- без заголовка;
- без списка;
- без истории, героя, сцены и диалога;
- без объяснения собственной мысли после финальной фразы.

ИНТОНАЦИЯ:
интеллектуальный афоризм: парадокс, ирония, точность, неожиданная развязка.
По умолчанию — сухая умная афористическая традиция, но конкретное
поручение Директора может полностью сместить оптику: например, в сторону
бытовой сатиры, философской иронии, притчи, научного парадокса,
басенной мудрости или абсурдной псевдосерьёзности.

СВОБОДНАЯ ПАЛИТРА ОРИЕНТИРОВ:
- Поручение Директора может называть ЛЮБЫХ философов, мудрецов, учёных,
  писателей, психологов, сатириков, баснописцев, публицистов и других
  интеллектуальных или литературных ориентиров.
- Список НЕ закрытый. Не ограничивайся примерами, которые когда-либо
  встречались в инструкции.
- Если назван один ориентир — возьми его интеллектуальную оптику:
  темы, тип вопросов, характер наблюдения, уровень иронии, степень
  философской глубины и способ поворота мысли.
- Если названы несколько ориентиров — соедини их. Если Директор указал
  проценты или пропорции («60% Франкл, 40% Ницше») — соблюдай их по смыслу.
- Если Директор добавил уточнение («мягче», «едче», «больше науки»,
  «меньше морализаторства», «бытовая сатира») — это важнее общего шаблона.
- Не копируй цитаты, известные афоризмы, узнаваемые формулировки и
  характерные фразы конкретного автора.
- Не выдавай подражание за цитату и не приписывай человеку слова,
  которых он не говорил.
- Для современных или живущих авторов не копируй индивидуальный голос
  буквально: используй только общие интеллектуальные признаки,
  тематику и риторические качества.
- Если фамилия или личность неоднозначна и ты не уверен, не выдумывай
  биографию и идеи. Используй только признаки, явно заданные Директором,
  и общий жанр рубрики.
- Результат должен быть НОВОЙ самостоятельной мыслью Агентства W,
  а названные люди — только интеллектуальные линзы.

Для этой рубрики особенно хорошо работают ориентиры на сатиру,
афористику, басню, наблюдение за человеческими привычками и парадокс,
но Директор вправе назвать ЛЮБОЙ другой интеллектуальный ориентир.

ВАЖНО:
- не копируй и не перефразируй известные афоризмы;
- не имитируй дословно конкретного автора;
- никакой банальной мотивации;
- никаких «верь в себя», «живи сейчас», «ИИ меняет мир»;
- никакого рекламного упоминания Агентства W, если оно не нужно самой мысли;
- не упоминай Неону, Стагирита, агентов;
- не используй персонажей и имена;
- юмор сухой, умный, иногда едкий;
- после прочтения хочется остановиться на секунду и подумать.

Верни ТОЛЬКО сам текст мысли. Не ставь кавычки.
""".strip()

    raw = ""
    engine = "reserve"
    model = ""

    if callable(ask_claude_fn):
        try:
            result = ask_claude_fn(
                prompt,
                str(assignment or ""),
                max_tokens=500,
            )
            if (
                isinstance(result, dict)
                and result.get("ok") is True
                and str(result.get("text") or "").strip()
            ):
                raw = str(result.get("text") or "").strip()
                engine = "primary"
                model = str(result.get("model") or "")
        except Exception:
            raw = ""

    if not raw and callable(ask_openai_fn):
        try:
            raw = str(
                ask_openai_fn(
                    prompt,
                    str(assignment or ""),
                    uploaded_files=[],
                    use_web_search=False,
                )
                or ""
            ).strip()
            engine = "reserve"
        except Exception:
            raw = ""

    first = _night_thought_limit(raw)

    # Один короткий возврат автору, если он опять сделал рассказ/банальность.
    if (
        not _night_thought_is_good(first)
        and callable(ask_claude_fn)
    ):
        retry_prompt = f"""
Перепиши это в ОДИН афоризм рубрики «Непричёсанные мысли на ночь»:

{first or raw}

ЖЁСТКО:
- максимум 350 знаков с пробелами;
- 1–3 предложения;
- никаких героев, историй, сцен, списков;
- никакого Агентства W и ИИ;
- одна парадоксальная мысль;
- финальная фраза должна менять угол зрения;
- СОХРАНИ интеллектуальный/литературный ориентир, заданный Директором
  в исходном поручении, включая смесь авторов и указанные пропорции;
- не копируй цитаты и узнаваемые формулировки названных людей;
- только готовый текст.
""".strip()
        try:
            retry = ask_claude_fn(
                retry_prompt,
                str(assignment or ""),
                max_tokens=350,
            )
            if (
                isinstance(retry, dict)
                and retry.get("ok") is True
                and str(retry.get("text") or "").strip()
            ):
                first = _night_thought_limit(
                    str(retry.get("text") or "")
                )
                engine = "primary"
                model = str(retry.get("model") or model)
        except Exception:
            pass

    if not first:
        first = (
            "Мы так долго учились экономить время, "
            "что теперь не знаем, на что его потратить. "
            "Пожалуй, самая дорогая роскошь — не эффективность, а отсутствие необходимости быть эффективным."
        )
        first = _night_thought_limit(first)
        engine = "deterministic_fallback"

    first = _rubric_with_icon(
        first,
        "💡",
        limit=350,
    )

    return {
        "text": first,
        "master_engine": engine,
        "master_model": model,
        "quality_score": 0,
        "rewrite_used": False,
        "report_warning": False,
        "series": "Непричёсанные мысли на ночь",
        "genre": "афоризм",
        "capability": "",
        "human_situation": "",
        "human_gain": "",
        "reader_entry": "",
        "cta_mode": "none",
        "newcomer_reason": "",
        "night_thought": True,
        "character_count": len(first),
    }



def _day_thought_clean(text: str) -> str:
    value = _rubric_strip_leading_icon(text)
    value = re.sub(
        r"(?im)^#{1,6}\s*мысл(?:ь|и)\s+дня.*?$",
        "",
        value,
    ).strip()
    value = re.sub(r"(?m)^\s*[-•*]\s*", "", value)
    value = re.sub(r"(?m)^\s*\d+[.)]\s*", "", value)
    value = re.sub(r"\n{2,}", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _day_thought_limit(
    text: str,
    limit: int = 348,
) -> str:
    value = _day_thought_clean(text)
    if len(value) <= limit:
        return value

    clipped = value[:limit + 1]
    ends = [
        clipped.rfind("."),
        clipped.rfind("!"),
        clipped.rfind("?"),
        clipped.rfind("…"),
    ]
    end = max(ends)
    if end >= 80:
        return clipped[:end + 1].strip()

    space = clipped.rfind(" ", 0, limit)
    if space > 0:
        return clipped[:space].rstrip(" ,;:-") + "…"

    return clipped[:limit].rstrip(" ,;:-") + "…"


def _day_thought_is_good(text: str) -> bool:
    value = _day_thought_clean(text)
    lowered = value.lower().replace("ё", "е")

    if not value or len(value) > 348:
        return False
    if len(value) < 35:
        return False
    if value.count("\n") > 1:
        return False
    if re.search(r"(?m)^\s*\d+[.)]", value):
        return False

    banned = (
        "доброе утро",
        "успешного дня",
        "верь в себя",
        "все получится",
        "агентство w",
        "стагирит",
        "неона",
        "неола",
        "искусственный интеллект",
        "а что думаете вы",
    )
    if any(part in lowered for part in banned):
        return False

    return True


def _generate_day_thought_content(
    ask_openai_fn,
    ask_claude_fn,
    assignment: str,
) -> dict[str, Any]:
    """
    Рубрика «Мысль дня».

    Короткий самостоятельный афоризм.
    Всегда начинается с ☀️.
    """
    prompt = f"""
Ты — автор рубрики «Мысль дня».

{CONTENT_MASTER_CORE}

Поручение Директора:
{assignment}

ФОРМА:
- одна самостоятельная мысль;
- 1–3 коротких предложения;
- весь итог вместе со значком ☀️ — максимум 350 знаков;
- лучше 90–260 знаков;
- без заголовка;
- без списка;
- без истории, персонажей и длинного объяснения.

ХАРАКТЕР:
- точность;
- наблюдение за человеком и жизнью;
- философская, научная или психологическая глубина — если её просит Директор;
- умная сухая ирония там, где она уместна;
- парадокс или неожиданный поворот;
- никакой сладкой мотивации и открытки «хорошего дня».

СВОБОДНАЯ ПАЛИТРА ОРИЕНТИРОВ:
- Поручение Директора может называть ЛЮБЫХ философов, мудрецов, учёных,
  писателей, психологов, сатириков, баснописцев, публицистов и других
  интеллектуальных или литературных ориентиров.
- Список НЕ закрытый. Не ограничивайся примерами, которые когда-либо
  встречались в инструкции.
- Если назван один ориентир — возьми его интеллектуальную оптику:
  темы, тип вопросов, характер наблюдения, уровень иронии, степень
  философской глубины и способ поворота мысли.
- Если названы несколько ориентиров — соедини их. Если Директор указал
  проценты или пропорции («60% Франкл, 40% Ницше») — соблюдай их по смыслу.
- Если Директор добавил уточнение («мягче», «едче», «больше науки»,
  «меньше морализаторства», «бытовая сатира») — это важнее общего шаблона.
- Не копируй цитаты, известные афоризмы, узнаваемые формулировки и
  характерные фразы конкретного автора.
- Не выдавай подражание за цитату и не приписывай человеку слова,
  которых он не говорил.
- Для современных или живущих авторов не копируй индивидуальный голос
  буквально: используй только общие интеллектуальные признаки,
  тематику и риторические качества.
- Если фамилия или личность неоднозначна и ты не уверен, не выдумывай
  биографию и идеи. Используй только признаки, явно заданные Директором,
  и общий жанр рубрики.
- Результат должен быть НОВОЙ самостоятельной мыслью Агентства W,
  а названные люди — только интеллектуальные линзы.

Для «Мысли дня» особенно естественны философы, учёные, психологи,
мыслители и писатели, но это НЕ ограничение. Можно использовать любую
интеллектуальную линзу, которую назвал Директор.

Ориентир по уровню, НЕ для копирования:
- честолюбие любит вывески, а дело — тишину;
- лень умеет сокращать лишнее, но часто оставляет скуку;
- совет особенно легко давать тому, кому не придётся делать;
- честность ценна именно потому, что редко возвращается сдачей.

Не копируй и не перефразируй эти примеры.
Они показывают только плотность, точность и характер.

Можно использовать тире, двоеточие или точку с запятой,
если это делает мысль острее, но это не шаблон.

ВАЖНО:
- не копируй известные афоризмы;
- не имитируй дословно конкретного автора;
- не упоминай Агентство W, ИИ или агентов;
- не пиши «доброе утро», «успешного дня», «всё получится»;
- никаких призывов к действию;
- верни ТОЛЬКО сам афоризм, без ☀️: значок добавит система.
""".strip()

    raw = ""
    engine = "reserve"
    model = ""

    if callable(ask_claude_fn):
        try:
            result = ask_claude_fn(
                prompt,
                str(assignment or ""),
                max_tokens=450,
            )
            if (
                isinstance(result, dict)
                and result.get("ok") is True
                and str(result.get("text") or "").strip()
            ):
                raw = str(result.get("text") or "").strip()
                engine = "primary"
                model = str(result.get("model") or "")
        except Exception:
            raw = ""

    if not raw and callable(ask_openai_fn):
        try:
            raw = str(
                ask_openai_fn(
                    prompt,
                    str(assignment or ""),
                    uploaded_files=[],
                    use_web_search=False,
                )
                or ""
            ).strip()
            engine = "reserve"
        except Exception:
            raw = ""

    thought = _day_thought_limit(raw)

    if (
        not _day_thought_is_good(thought)
        and callable(ask_claude_fn)
    ):
        retry_prompt = f"""
Перепиши это в одну короткую «Мысль дня»:

{thought or raw}

ЖЁСТКО:
- без заголовка;
- максимум 348 знаков до добавления значка;
- 1–3 предложения;
- одна мысль;
- парадокс или точный финальный поворот;
- без истории, рекламы, ИИ и мотивационных лозунгов;
- СОХРАНИ интеллектуальный ориентир, заданный Директором в исходном
  поручении, включая смесь нескольких людей, проценты и уточнения;
- не копируй цитаты и узнаваемые формулировки названных людей;
- только готовый афоризм, без ☀️.
""".strip()
        try:
            retry = ask_claude_fn(
                retry_prompt,
                str(assignment or ""),
                max_tokens=320,
            )
            if (
                isinstance(retry, dict)
                and retry.get("ok") is True
                and str(retry.get("text") or "").strip()
            ):
                thought = _day_thought_limit(
                    str(retry.get("text") or "")
                )
                engine = "primary"
                model = str(retry.get("model") or model)
        except Exception:
            pass

    if not thought:
        thought = (
            "Срочность любит шум: так ей легче притвориться важностью. "
            "Настоящие дела обычно приходят без сирены."
        )
        engine = "deterministic_fallback"

    thought = _rubric_with_icon(
        thought,
        "☀️",
        limit=350,
    )

    return {
        "text": thought,
        "master_engine": engine,
        "master_model": model,
        "quality_score": 0,
        "rewrite_used": False,
        "report_warning": False,
        "series": "Мысль дня",
        "genre": "афоризм",
        "capability": "",
        "human_situation": "",
        "human_gain": "",
        "reader_entry": "",
        "cta_mode": "none",
        "newcomer_reason": "",
        "day_thought": True,
        "character_count": len(thought),
    }



def _announcement_time_hint(assignment: str) -> str:
    text = str(assignment or "")
    patterns = (
        r"\b(?:в|к)\s*(\d{1,2}[:.]\d{2}\s*(?:мск|москв\w*)?)",
        r"\b(\d{1,2}[:.]\d{2}\s*(?:мск|москв\w*)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return str(match.group(1)).replace(".", ":").strip()
    return ""


def _generate_announcement_content(
    ask_openai_fn,
    ask_claude_fn,
    owner_id: int,
    owner_name: str,
    assignment: str,
) -> dict[str, Any]:
    """
    Короткий надёжный маршрут для анонсов.

    Анонс НЕ проходит через художественный конвейер «Хроник»,
    потому что ему не нужны:
    - Банк жанров;
    - литературный критик;
    - проверка интриги;
    - художественное переписывание.

    Это уменьшает число AI-вызовов и число точек отказа.
    """
    settings = _load_stagirite_settings(int(owner_id))
    zoom_link = str(settings.get("zoom_link") or "").strip()
    time_hint = _announcement_time_hint(assignment)

    system_prompt = f"""
Ты — Мастер коротких анонсов Агентства W.

{CONTENT_MASTER_CORE}

Стагирит уже понял поручение Директора.

ПОРУЧЕНИЕ:
{assignment}

Имя Директора:
{owner_name}

Сохранённая постоянная Zoom-ссылка:
{zoom_link or "не сохранена"}

Время, явно указанное Директором:
{time_hint or "не найдено отдельным парсером — используй только то, что есть в поручении"}

Сделай готовый короткий анонс для людей.

ПРАВИЛА:
- 5–10 коротких строк;
- сразу понятно: что происходит, когда и зачем прийти;
- если тема дана Директором — сохрани её буквально по смыслу;
- если указано «сегодня» — не заменяй это другой датой;
- если указано время — не меняй его и не пересчитывай часовой пояс;
- не придумывай программу встречи, спикеров, подарки, результаты или обещания;
- не превращай анонс в художественную «Хронику»;
- тон живой, тёплый, уверенный, без канцелярита;
- допустим 1 лёгкий интригующий штрих;
- если Zoom-ссылка сохранена — поставь её в конце отдельной строкой;
- не упоминай внутреннюю техническую кухню Агентства;
- верни ТОЛЬКО готовый текст анонса.
""".strip()

    text = ""
    engine = "reserve"
    model = ""
    errors: list[str] = []

    # Claude — основной Мастер текста.
    if callable(ask_claude_fn):
        try:
            result = ask_claude_fn(
                system_prompt,
                str(assignment or ""),
                max_tokens=1400,
            )
            if (
                isinstance(result, dict)
                and result.get("ok") is True
                and str(result.get("text") or "").strip()
            ):
                text = str(result.get("text") or "").strip()
                engine = "primary"
                model = str(result.get("model") or "")
            elif isinstance(result, dict) and result.get("error"):
                errors.append(
                    "Claude: " + str(result.get("error"))
                )
        except Exception as exc:
            errors.append(
                f"Claude: {type(exc).__name__}: {exc}"
            )

    # OpenAI — резерв, только если Claude не дал текст.
    if not text and callable(ask_openai_fn):
        try:
            raw = ask_openai_fn(
                system_prompt,
                str(assignment or ""),
                uploaded_files=[],
                use_web_search=False,
            )
            candidate = str(raw or "").strip()
            if candidate and not candidate.startswith("Ошибка OpenAI:"):
                text = candidate
                engine = "reserve"
            elif candidate:
                errors.append(candidate)
        except Exception as exc:
            errors.append(
                f"OpenAI: {type(exc).__name__}: {exc}"
            )

    # Даже временная недоступность модели не должна лишать Директора
    # возможности быстро объявить уже назначенную встречу.
    if not text:
        lines = ["📣 Встречаемся сегодня в Zoom!"]
        lowered = str(assignment or "").lower().replace("ё", "е")

        if "новост" in lowered and "агентств" in lowered:
            lines.extend(
                [
                    "",
                    "Тема встречи: Новости Агентства W.",
                ]
            )

        if time_hint:
            lines.append(f"⏰ Начало: {time_hint}.")

        lines.extend(
            [
                "",
                "До встречи!",
            ]
        )

        if zoom_link:
            lines.extend(
                [
                    "",
                    f"🔗 Zoom: {zoom_link}",
                ]
            )

        text = "\n".join(lines).strip()
        engine = "deterministic_fallback"

    # На всякий случай гарантируем наличие сохранённой ссылки,
    # если Мастер её пропустил.
    if (
        zoom_link
        and zoom_link not in text
    ):
        text = (
            text.rstrip()
            + "\n\n"
            + f"🔗 Zoom: {zoom_link}"
        )

    return {
        "text": text,
        "master_engine": engine,
        "master_model": model,
        "quality_score": 0,
        "rewrite_used": False,
        "report_warning": False,
        "series": "",
        "genre": "анонс",
        "capability": "",
        "human_situation": "",
        "human_gain": "",
        "reader_entry": "",
        "cta_mode": "none",
        "newcomer_reason": "",
        "announcement": True,
        "announcement_time": time_hint,
        "generation_errors": errors,
    }


def _generate_content(
    ask_openai_fn,
    ask_claude_fn,
    owner_id: int,
    owner_name: str,
    assignment: str,
) -> dict[str, Any]:
    """
    Редакция «Хроники Агентства W».

    1. Банк правды — текущие реальные возможности.
    2. Банк человеческих ситуаций.
    3. Банк жанров.
    4. Стагирит выбирает новый угол, не повторяя недавние выпуски.
    5. Мастер контента пишет рассказ.
    6. Стагирит проверяет правду И притягательность для новичка.
    7. При провале Мастер переписывает один раз.
    """
    mode = _content_mode_from_assignment(assignment)

    # «Непричёсанные мысли на ночь» — отдельный сверхкороткий жанр.
    # Никогда не пропускаем его через «Хроники».
    if mode == "night_thought":
        return _generate_night_thought_content(
            ask_openai_fn,
            ask_claude_fn,
            assignment,
        )

    # «Мысль дня» — отдельный короткий жанр.
    # Всегда с ☀️, никогда не через «Хроники».
    if mode == "day_thought":
        return _generate_day_thought_content(
            ask_openai_fn,
            ask_claude_fn,
            assignment,
        )

    # Анонсы — отдельный быстрый контур.
    # Не гоняем Zoom-приглашение через литературную редакцию «Хроник».
    if mode == "announcement":
        return _generate_announcement_content(
            ask_openai_fn,
            ask_claude_fn,
            owner_id,
            owner_name,
            assignment,
        )

    facts = _agency_current_release_brief()
    recent = _recent_chronicles_history(
        owner_id,
        limit=12,
    )

    # --------------------------------------------------------
    # Не художественные задачи оставляем в том же конвейере,
    # но без требования сериальной драматургии.
    # --------------------------------------------------------
    series_mode = mode == "story"
    soft_cta_allowed = (
        _chronicles_soft_cta_allowed(recent)
        if series_mode
        else False
    )

    recent_text = json.dumps(
        recent[:8],
        ensure_ascii=False,
        indent=2,
    )

    genres_text = "\n".join(
        f"- {value}"
        for value in CHRONICLES_GENRES
    )
    situations_text = "\n".join(
        f"- {value}"
        for value in CHRONICLES_HUMAN_SITUATIONS
    )

    # --------------------------------------------------------
    # 1. Стагирит выбирает материал из Банка правды.
    # --------------------------------------------------------
    director_prompt = f"""
Ты — Стагирит, редакционный директор Агентства W.

{STAGIRITE_CORE}

Поручение Директора:
{assignment}

Режим:
{mode}

ЦЕЛЬ РЕДАКЦИИ:
Создавать бесконечную серию «Хроники Агентства W», после которой
человек не чувствует, что ему что-то продают, а думает:
«Вот бы и мне перестать тащить всё это на себе».

БАНК ПРАВДЫ — это единственные факты, которые можно считать
уже работающими:
{facts}

БАНК ЖАНРОВ:
{genres_text}

БАНК ЧЕЛОВЕЧЕСКИХ СИТУАЦИЙ:
{situations_text}

НЕДАВНИЕ ВЫПУСКИ:
{recent_text}

Твоя задача — выбрать НОВЫЙ угол истории.
Не повторяй недавний жанр, открывающую сцену и главную способность,
если есть другая честная возможность.

Новые возможности Агентства должны постепенно появляться в новых
историях, но только после того, как они попали в Банк правды.

ВАЖНО:
- выбери только ОДНУ главную способность Агентства;
- максимум 1–2 факта нужны самому рассказу;
- остальная техническая кухня остаётся за кадром;
- сначала человеческая жизнь, потом незаметно Агентство;
- герой — взрослый человек, не «система»;
- главная ценность: время, нервы, внимание, отношения, свобода выбора;
- юмор — наблюдательный, добрый, иногда чуть ехидный к старому способу работы;
- интрига должна рождаться из ситуации, а не из кликбейта;
- не использовать избитые начала:
  {", ".join(CHRONICLES_BANNED_CLICHES)};
- никаких гарантий дохода, встреч, партнёров;
- не придумывать ещё не работающие возможности;
- не объяснять архитектуру Агентства;
- не перечислять агентов один за другим.

ПРАВИЛО ПЕРВОГО ЧИТАТЕЛЯ:
{CHRONICLES_FIRST_READER_RULE}

КАРТОЧКИ ГЕРОЕВ — только для точности роли:
{json.dumps(CHRONICLES_CHARACTER_CARDS, ensure_ascii=False, indent=2)}

Выбери максимум 0–2 внутренних героя по имени.
Если герой не нужен сюжету — не вводи его только ради знакомства.

CTA:
{"В этом выпуске допустим ОДИН мягкий вход в самом конце: без давления, например предложить человеку посмотреть, какую его рутину уже можно передать Агентству." if soft_cta_allowed else "В этом выпуске НЕ должно быть призыва написать, купить, прийти или записаться. Финал работает только через желание узнать больше."}

Верни ТОЛЬКО JSON:
{{
  "series": "Хроники Агентства W",
  "genre": "...",
  "capability": "одна реальная способность Агентства человеческим языком",
  "truth_for_story": ["факт 1", "факт 2 при необходимости"],
  "human_situation": "...",
  "human_problem": "...",
  "human_gain": "...",
  "scene": "...",
  "intrigue": "...",
  "humor": "...",
  "ending": "...",
  "reader_entry": "как новый читатель поймёт Агентство без предыстории",
  "characters": [
    {{"name": "имя героя", "natural_intro": "короткое естественное представление роли"}}
  ],
  "cta_mode": "none" или "soft"
}}
""".strip()

    raw_brief = ask_openai_fn(
        director_prompt,
        assignment,
        uploaded_files=[],
        use_web_search=False,
    )
    brief = _parse_editor_json(raw_brief)

    if not brief:
        brief = _chronicles_brief_fallback(
            assignment,
            recent,
        )

    # Принудительно не даём модели самовольно включать CTA.
    brief["cta_mode"] = (
        "soft"
        if soft_cta_allowed
        and str(brief.get("cta_mode") or "") == "soft"
        else "none"
    )

    def write_with_master(
        system_prompt: str,
        user_text: str,
    ) -> tuple[str, dict[str, Any]]:
        claude_result = None

        if callable(ask_claude_fn):
            try:
                claude_result = ask_claude_fn(
                    system_prompt,
                    user_text,
                    max_tokens=4200,
                )
            except Exception:
                claude_result = None

        if (
            isinstance(claude_result, dict)
            and claude_result.get("ok") is True
            and str(
                claude_result.get("text") or ""
            ).strip()
        ):
            return (
                str(
                    claude_result["text"]
                ).strip(),
                {
                    "engine": "primary",
                    "model": str(
                        claude_result.get("model")
                        or ""
                    ),
                },
            )

        reserve = ask_openai_fn(
            system_prompt,
            user_text,
            uploaded_files=[],
            use_web_search=False,
        )

        return (
            str(reserve or "").strip(),
            {
                "engine": "reserve",
                "model": "",
            },
        )

    # --------------------------------------------------------
    # 2. Мастер НЕ видит весь Банк правды.
    # Только выбранный сюжет и 1–2 разрешённых факта.
    # --------------------------------------------------------
    master_prompt = f"""
Ты — Мастер контента редакции «Хроники Агентства W».

{CONTENT_MASTER_CORE}

Ты не технический копирайтер.
Ты автор короткой современной прозы для взрослых людей.

РЕДАКЦИОННАЯ КАРТОЧКА:
Жанр: {brief.get("genre", "короткий рассказ")}
Человеческая ситуация: {brief.get("human_situation", "")}
Проблема человека: {brief.get("human_problem", "")}
Что человек получает обратно: {brief.get("human_gain", "")}
Сцена: {brief.get("scene", "")}
Интрига: {brief.get("intrigue", "")}
Юмор: {brief.get("humor", "")}
Финальный образ: {brief.get("ending", "")}
Главная способность Агентства: {brief.get("capability", "")}
Как войти новому читателю: {brief.get("reader_entry", "")}
Герои и их естественное первое представление:
{json.dumps(brief.get("characters", []), ensure_ascii=False)}

ФАКТЫ, КОТОРЫЕ РАЗРЕШЕНО ПОКАЗАТЬ В ЭТОМ РАССКАЗЕ:
{json.dumps(brief.get("truth_for_story", []), ensure_ascii=False)}

ИСХОДНАЯ ПРОСЬБА ДИРЕКТОРА:
{assignment}

НАПИШИ ПУБЛИКАЦИЮ, КОТОРУЮ ХОЧЕТСЯ ДОЧИТАТЬ.

ЛИТЕРАТУРНЫЕ ПРАВИЛА:
- первые 2–3 строки открывают живую ситуацию;
- не объясняй Агентство до того, как читателю станет интересен человек;
- показывай пользу через изменение жизни;
- диалог, жест, бытовая деталь и наблюдение лучше абстрактного «эффективно»;
- допускается маленькая художественная условность,
  но фактические действия Агентства не искажай;
- не превращай агентов в список должностей;
- не делай всех героев идеальными;
- юмор должен быть узнаваемым, а не «шуткой ради шутки»;
- оставь немного воздуха: не разжёвывай мораль;
- никаких слов «воронка», «функционал», «экосистема» и «автоматизация»,
  если без них можно обойтись;
- не начинай с кофе/остывшей кружки/пустого календаря;
- не заканчивай банальным вопросом «а что бы вы сделали с лишним часом?»;
- не говори от первого лица ИИ или Стагирита без прямой просьбы.

ПРАВИЛО ПЕРВОГО ЧИТАТЕЛЯ — ОБЯЗАТЕЛЬНО:
{CHRONICLES_FIRST_READER_RULE}

Представление героя должно быть незаметным:
НЕ «Неона — это такой агент, который...»
А естественно внутри действия:
«Неона, ИИ-секретарь Агентства W, уже подготовила...»

Не надо каждый раз знакомить со всей командой.
Объясняй только тех, кто реально появился в этом выпуске.

СКВОЗНАЯ МЫСЛЬ СЕРИАЛА:
ИИ занимается рутиной не ради красивой технологии.
Он возвращает человеку время и нервы для семьи, отношений,
творчества, путешествий, развития и просто спокойной жизни.

CTA:
{"Только в самом конце можно сделать ОДНО мягкое приглашение без давления: предложить узнать, какую рутину человека уже можно передать Агентству W." if brief.get("cta_mode") == "soft" else "Призыва к действию нет. Пусть желание обратиться рождается из самой истории."}

Только готовый художественный текст.
Без слов «бриф», «карточка», «редакция», «оценка».
""".strip()

    draft, engine_meta = write_with_master(
        master_prompt,
        assignment,
    )

    # --------------------------------------------------------
    # 3. Стагирит проверяет не только красоту, но и МАГНИТ.
    # --------------------------------------------------------
    critic_prompt = f"""
Ты — Стагирит, главный редактор «Хроник Агентства W».

{STAGIRITE_CORE}

Поручение:
{assignment}

Редакционная карточка:
{json.dumps(brief, ensure_ascii=False)}

Готовый текст:
{draft}

Полный Банк правды:
{facts}

Главный вопрос:
После чтения потенциальный новичок должен не услышать рекламу,
а почувствовать: «Так можно было? Я тоже хочу вернуть себе время».

Оцени по 10:
hook — первая сцена удерживает;
story — это рассказ/фельетон/новелла, а не презентация;
intrigue — хочется узнать, чем закончится;
humor — есть естественная улыбка, когда жанр позволяет;
humanity — человек важнее технологии;
clarity — выпуск полностью понятен человеку без знания предыдущих серий;
desire — возникает желание примерить такую жизнь на себя;
trust — нет рекламной липкости и преувеличений;
truth — действия Агентства соответствуют Банку правды.

FATAL=true, если:
- придумана ещё не работающая возможность;
- техническая кухня стала сюжетом;
- пост похож на инструкцию/отчёт;
- обещается доход, встреча, партнёр или результат;
- перечислены функции агентов;
- назван Неона/Неония/Неола/Стагирит, но новый читатель не понимает,
  кто это и какую роль герой выполняет в данной сцене;
- текст ссылается на прошлый выпуск так, что без него теряется смысл;
- начало построено на избитом «кофе остыл/кружка остывает»;
- текст пытается продавать сильнее, чем рассказывать.

Верни ТОЛЬКО JSON:
{{
  "decision": "PASS" или "REWRITE",
  "fatal": true/false,
  "scores": {{
    "hook": 0,
    "story": 0,
    "intrigue": 0,
    "humor": 0,
    "humanity": 0,
    "clarity": 0,
    "desire": 0,
    "trust": 0,
    "truth": 0
  }},
  "why_newcomer_reads": "одно предложение",
  "revision_brief": "3–6 очень конкретных замечаний автору"
}}

PASS только если truth >= 9, clarity >= 9, desire >= 8, hook >= 8,
story >= 8, intrigue >= 8 и нет FATAL.
""".strip()

    raw_review = ask_openai_fn(
        critic_prompt,
        draft,
        uploaded_files=[],
        use_web_search=False,
    )
    review = _parse_editor_json(raw_review)
    score = _content_quality_score(review)

    scores = (
        review.get("scores")
        if isinstance(
            review.get("scores"),
            dict,
        )
        else {}
    )

    def score_value(key: str) -> int:
        try:
            return int(scores.get(key, 0))
        except (TypeError, ValueError):
            return 0

    needs_rewrite = (
        str(
            review.get("decision") or ""
        ).upper()
        != "PASS"
        or bool(review.get("fatal"))
        or score_value("truth") < 9
        or score_value("clarity") < 9
        or score_value("desire") < 8
        or score_value("hook") < 8
        or score_value("story") < 8
        or score_value("intrigue") < 8
        or _looks_like_report(draft)
        or _has_bad_story_voice(draft)
        or _starts_with_tired_cliche(draft)
    )

    final_text = draft
    rewrite_used = False

    # --------------------------------------------------------
    # 4. Один редакционный возврат автору.
    # --------------------------------------------------------
    if needs_rewrite:
        revision = str(
            review.get("revision_brief")
            or (
                "Перепиши как сильную человеческую историю. "
                "Убери объяснение системы, добавь живую ситуацию, "
                "интригу и узнаваемый юмор. Пусть желание узнать "
                "Агентство рождается из жизни героя, а не из рекламы."
            )
        ).strip()

        rewrite_prompt = f"""
Ты — Мастер контента «Хроник Агентства W».

{CONTENT_MASTER_CORE}

Первая версия не принята Стагиритом.

Редакционная карточка:
{json.dumps(brief, ensure_ascii=False)}

Первая версия:
{draft}

Замечания:
{revision}

Перепиши как НОВУЮ литературную версию, а не косметическую правку.

Обязательно:
- человек и его жизнь на первом плане;
- одна реальная способность Агентства проявляется в действии;
- читатель узнаёт собственную усталость от рутины;
- есть интрига;
- есть лёгкая улыбка, если ситуация позволяет;
- нет технической кухни;
- нет рекламной речи;
- нет выдуманных функций;
- выпуск понятен человеку, который впервые видит Агентство W;
- каждый названный внутренний герой естественно представлен при первом появлении;
- не нужно знать предыдущие выпуски, чтобы понять сюжет;
- финал оставляет желание узнать больше;
- не использовать «кофе остыл», «кружка остывает»,
  «пустой календарь» и вопрос про «лишний час».

CTA:
{"Допустим только один мягкий вход в самом конце." if brief.get("cta_mode") == "soft" else "CTA запрещён."}

Верни только готовый текст.
""".strip()

        rewritten, rewrite_meta = write_with_master(
            rewrite_prompt,
            assignment,
        )

        if rewritten:
            final_text = rewritten
            rewrite_used = True

            if (
                rewrite_meta.get("engine")
                == "primary"
            ):
                engine_meta = rewrite_meta

    return {
        "text": final_text,
        "master_engine": str(
            engine_meta.get("engine")
            or "reserve"
        ),
        "master_model": str(
            engine_meta.get("model")
            or ""
        ),
        "quality_score": int(score),
        "rewrite_used": bool(rewrite_used),
        "report_warning": bool(
            _looks_like_report(final_text)
            or _has_bad_story_voice(final_text)
            or _starts_with_tired_cliche(
                final_text
            )
        ),
        "series": str(
            brief.get("series")
            or (
                "Хроники Агентства W"
                if series_mode
                else ""
            )
        ),
        "genre": str(
            brief.get("genre")
            or ""
        ),
        "capability": str(
            brief.get("capability")
            or ""
        ),
        "human_situation": str(
            brief.get("human_situation")
            or ""
        ),
        "human_gain": str(
            brief.get("human_gain")
            or ""
        ),
        "reader_entry": str(
            brief.get("reader_entry")
            or ""
        ),
        "cta_mode": str(
            brief.get("cta_mode")
            or "none"
        ),
        "newcomer_reason": str(
            review.get("why_newcomer_reads")
            or ""
        ),
    }


STAGIRITE_EXECUTION_INTENTS = {
    "meetings",
    "content",
    "team",
    "general",
}


def _normalize_stagirite_intents(
    raw: Any,
    fallback: list[str],
) -> list[str]:
    values = raw if isinstance(raw, list) else []
    result: list[str] = []

    for value in values:
        clean = str(value or "").strip().lower()
        if clean not in STAGIRITE_EXECUTION_INTENTS:
            continue
        if clean not in result:
            result.append(clean)

    return result or list(fallback or ["general"])


def _needs_semantic_director(
    assignment: str,
    deterministic_intents: list[str],
) -> bool:
    """
    Не тратим ИИ на очевидную маршрутизацию.

    Семантический Замдиректор включается, когда:
    - старый словарь видит только general;
    - короткая человеческая фраза может иметь скрытый смысл;
    - речь идёт о структуре, но непонятно, нужно ли создать сообщение;
    - Директор просит подумать/разобраться/придумать решение.
    """
    lowered = str(assignment or "").lower().replace("ё", "е")
    if deterministic_intents == ["general"]:
        return True

    if (
        "team" in deterministic_intents
        and "content" not in deterministic_intents
    ):
        return True

    thinking_words = (
        "подумай",
        "разберись",
        "предложи",
        "придумай",
        "реши",
        "как лучше",
        "что делать",
        "помоги",
        "организуй",
        "сделай так",
    )
    if any(token in lowered for token in thinking_words):
        return True

    # Очень короткие поручения пожилого человека должны пониматься
    # по смыслу, а не требовать «правильных ключевых слов».
    words = [part for part in re.split(r"\s+", lowered) if part]
    return len(words) <= 7 and len(lowered) <= 110


def _understand_director_assignment(
    ask_openai_fn,
    assignment: str,
    deterministic_intents: list[str],
) -> dict[str, Any]:
    """
    Внутренний «мозг Замдиректора».

    Пользователь сообщает ЧТО хочет получить.
    Стагирит сам решает:
    - какой это результат;
    - кого подключить;
    - что поручить специалисту;
    - действительно ли нужен вопрос Директору.

    Вызов происходит только при новом явном поручении, не на idle/rerun.
    """
    fallback = {
        "goal": str(assignment or "").strip(),
        "intents": list(deterministic_intents or ["general"]),
        "execution_mode": (
            "answer"
            if deterministic_intents == ["general"]
            else "delegate"
        ),
        "internal_brief": str(assignment or "").strip(),
        "needs_clarification": False,
        "clarification_question": "",
        "requested_image": any(
            token in str(assignment or "").lower().replace("ё", "е")
            for token in (
                "иллюстрац",
                "картин",
                "изображен",
                "нарис",
                "обложк",
            )
        ),
        "steps": [],
    }

    if not callable(ask_openai_fn):
        return fallback

    facts = _agency_current_release_brief()

    prompt = f"""
Ты — Стагирит, универсальный ИИ-заместитель Директора Агентства W.

{STAGIRITE_CORE}

{MISSION_TEST_CORE}

Директор может быть человеком любого возраста и технической подготовки.
Он НЕ обязан:
- знать названия внутренних модулей;
- писать промты;
- перечислять шаги;
- понимать архитектуру Агентства;
- выбирать, кого из специалистов вызвать.

Его задача — коротко сказать, ЧТО он хочет.
Твоя задача — понять смысл и превратить его во внутреннее рабочее поручение.

ПОРУЧЕНИЕ ДИРЕКТОРА:
{assignment}

Старый технический маршрутизатор предположил:
{json.dumps(deterministic_intents, ensure_ascii=False)}

РЕАЛЬНО РАБОТАЮЩИЕ ВОЗМОЖНОСТИ АГЕНТСТВА:
{facts}

Доступные направления исполнения:
- meetings: цель по встречам, кандидаты, Неония, Неона, календарь;
- content: пост, рассказ, Хроники, анонс, текст, творческий материал;
- team: коммуникация/материал для структуры;
- general: анализ, решение, план, совет, исследование задачи Замдиректором.

ПРИНЦИПЫ:
1. Не спрашивай Директора «как это сделать». Это твоя работа.
2. Не требуй правильных терминов.
3. Уточняющий вопрос допустим ТОЛЬКО если без конкретного человеческого
   решения/данных невозможно выбрать безопасный или правильный результат.
4. Если данных достаточно для разумного исполнения — действуй.
5. Не придумывай техническую возможность, которой ещё нет.
6. Если просьба ясна, но для физического действия нет исполнительного модуля,
   не называй это «нужно уточнение»: execution_mode = "unsupported",
   а internal_brief объясняет, что именно нужно подключить.
7. Если попросили сообщить/написать что-то структуре, это обычно content + team.
8. Если попросили «подумай», «предложи», «разберись», «составь план» —
   это general + execution_mode "answer".
9. requested_image=true только если Директор действительно попросил изображение,
   иллюстрацию, картинку, обложку или рисунок.
10. Внутренний brief должен быть профессиональным и достаточным для исполнения, но не превращай
    простую идею в инженерный регламент без запроса Директора.
11. Если речь о миссии или оценке новой функции, используй только Тест миссии W.
    Не изобретай веса, проценты, KPI, юридические чек-листы, планы отката, kill switch
    и другие дополнительные методологии, если Директор прямо их не попросил.

Верни ТОЛЬКО JSON:
{{
  "goal": "какой конечный результат нужен Директору",
  "intents": ["meetings"|"content"|"team"|"general"],
  "execution_mode": "delegate"|"answer"|"clarify"|"unsupported",
  "internal_brief": "подробное внутреннее задание самому Стагириту/исполнителю",
  "needs_clarification": false,
  "clarification_question": "",
  "requested_image": false,
  "steps": [
    "внутренний шаг 1",
    "внутренний шаг 2"
  ]
}}
""".strip()

    try:
        raw = ask_openai_fn(
            prompt,
            str(assignment or ""),
            uploaded_files=[],
            use_web_search=False,
        )
        parsed = _parse_editor_json(raw)
    except Exception:
        parsed = {}

    if not parsed:
        return fallback

    parsed["intents"] = _normalize_stagirite_intents(
        parsed.get("intents"),
        deterministic_intents,
    )

    mode = str(
        parsed.get("execution_mode")
        or fallback["execution_mode"]
    ).strip().lower()
    if mode not in {
        "delegate",
        "answer",
        "clarify",
        "unsupported",
    }:
        mode = fallback["execution_mode"]
    parsed["execution_mode"] = mode

    parsed["goal"] = str(
        parsed.get("goal")
        or fallback["goal"]
    ).strip()

    parsed["internal_brief"] = str(
        parsed.get("internal_brief")
        or parsed["goal"]
        or fallback["internal_brief"]
    ).strip()

    parsed["needs_clarification"] = bool(
        parsed.get("needs_clarification")
        or mode == "clarify"
    )
    parsed["clarification_question"] = str(
        parsed.get("clarification_question")
        or ""
    ).strip()

    parsed["requested_image"] = bool(
        parsed.get("requested_image")
        or fallback["requested_image"]
    )

    steps = parsed.get("steps")
    parsed["steps"] = [
        str(item).strip()
        for item in steps
        if str(item).strip()
    ] if isinstance(steps, list) else []

    return parsed



def _is_core_mission_question(text: str) -> bool:
    """Распознаёт вопросы Директора о миссии/Конституции и фильтре новых функций."""
    lowered = str(text or "").lower().replace("ё", "е")
    mission_tokens = (
        "миссия агентства",
        "миссии агентства",
        "конституция агентства",
        "конституции агентства",
        "тест миссии",
        "по каким принципам",
        "принципы оценки новой функции",
        "оценивать новую функцию",
    )
    return any(token in lowered for token in mission_tokens)


def _execute_general_director_task(
    ask_openai_fn,
    assignment: str,
    understanding: dict[str, Any],
) -> str:
    """
    Стагирит сам выполняет интеллектуальные поручения, для которых
    не нужен отдельный технический модуль.
    """
    if _is_core_mission_question(assignment):
        return mission_director_answer()

    if not callable(ask_openai_fn):
        return (
            "Поручение понятно, но интеллектуальный исполнитель "
            "сейчас недоступен."
        )

    facts = _agency_current_release_brief()
    internal_brief = str(
        understanding.get("internal_brief")
        or assignment
    ).strip()

    prompt = f"""
Ты — Стагирит, универсальный заместитель Директора Агентства W.

{STAGIRITE_CORE}

{MISSION_TEST_CORE}

Исходное короткое поручение:
{assignment}

Ты уже понял его как:
{internal_brief}

Факты о текущих возможностях Агентства:
{facts}

Выполни интеллектуальную часть поручения сам:
- дай конкретный результат, а не рассуждение о том, как его можно сделать;
- пиши простым человеческим языком;
- не заставляй Директора разбираться во внутренней кухне;
- если часть задачи требует ещё не подключённого технического действия,
  честно отдели то, что уже сделал, от того, что пока нельзя выполнить;
- не объявляй действие выполненным без фактического исполнения;
- если можно принять разумное внутреннее решение самому — прими его;
- если поручение связано с новой функцией или идеей конкурента, оцени её только по пяти вопросам
  Теста миссии W и дай короткую рекомендацию;
- не добавляй свои веса, проценты, KPI, юридические чек-листы, планы отката, kill switch,
  execution_mode и другие внутренние технические процедуры, если Директор прямо их не запросил;
- ответ Директору должен быть настолько коротким, насколько позволяет задача.

Верни только готовый результат для Директора.
""".strip()

    try:
        answer = ask_openai_fn(
            prompt,
            str(assignment or ""),
            uploaded_files=[],
            use_web_search=False,
        )
    except Exception as exc:
        return f"Не удалось выполнить интеллектуальную часть: {type(exc).__name__}"

    return str(answer or "").strip()




def _prepare_uploaded_illustration(
    uploaded_file: Any,
    *,
    max_side: int = 2048,
    max_bytes: int = 12 * 1024 * 1024,
) -> dict[str, Any]:
    """
    Проверяет пользовательскую иллюстрацию и приводит её к PNG.

    Это локальная операция:
    - без OpenAI/Claude;
    - без генерации;
    - без отправки файла во внешний ИИ-сервис.
    """
    if uploaded_file is None:
        return {
            "ok": False,
            "error": "Файл не выбран.",
        }

    try:
        raw = uploaded_file.getvalue()
    except Exception:
        try:
            raw = bytes(uploaded_file)
        except Exception:
            raw = b""

    if not raw:
        return {
            "ok": False,
            "error": "Не удалось прочитать изображение.",
        }

    if len(raw) > max_bytes:
        return {
            "ok": False,
            "error": "Файл слишком большой. Максимум 12 МБ.",
        }

    try:
        with Image.open(BytesIO(raw)) as opened:
            opened.load()
            original_format = str(opened.format or "").upper()
            width, height = opened.size

            if width < 64 or height < 64:
                return {
                    "ok": False,
                    "error": "Изображение слишком маленькое.",
                }

            # Для публикаций достаточно 2048 px по длинной стороне.
            image = opened.copy()
            longest = max(image.size)
            resized = False
            if longest > max_side:
                ratio = max_side / float(longest)
                new_size = (
                    max(1, int(image.size[0] * ratio)),
                    max(1, int(image.size[1] * ratio)),
                )
                image = image.resize(
                    new_size,
                    Image.Resampling.LANCZOS,
                )
                resized = True

            # Telegram/Streamlit/Supabase получают единый предсказуемый PNG.
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")

            buffer = BytesIO()
            image.save(
                buffer,
                format="PNG",
                optimize=True,
            )
            png_bytes = buffer.getvalue()

        return {
            "ok": True,
            "image_bytes": png_bytes,
            "original_format": original_format,
            "original_width": int(width),
            "original_height": int(height),
            "width": int(image.size[0]),
            "height": int(image.size[1]),
            "resized": bool(resized),
        }

    except (UnidentifiedImageError, OSError, ValueError):
        return {
            "ok": False,
            "error": (
                "Не удалось открыть картинку. "
                "Используйте PNG, JPG/JPEG или WEBP."
            ),
        }



def _artist_image_bytes(value: Any) -> bytes:
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


def _artist_image_hash(value: Any) -> str:
    raw = _artist_image_bytes(value)
    return hashlib.sha256(raw).hexdigest() if raw else ""


def _telegram_publication_hash(
    text: str,
    image: Any | None,
    chat_ids: list[int],
    video: Any | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(str(text or "").strip().encode("utf-8"))
    digest.update(b"\0")
    raw = _artist_image_bytes(image) if image is not None else b""
    digest.update(raw)
    digest.update(b"\0")
    raw_video = _artist_image_bytes(video) if video is not None else b""
    digest.update(raw_video)
    digest.update(b"\0")
    digest.update(
        ",".join(str(int(x)) for x in sorted(set(chat_ids))).encode("ascii")
    )
    return digest.hexdigest()


MAX_PUBLISHER_VIDEO_BYTES = 300 * 1024 * 1024


def _prepare_uploaded_video(
    uploaded_file: Any,
    *,
    max_bytes: int = MAX_PUBLISHER_VIDEO_BYTES,
) -> dict[str, Any]:
    """
    Проверяет готовый ролик для публикации через Telegram Publisher.

    На этом этапе видео НЕ отправляется во внешний ИИ и НЕ перекодируется.
    Для публикации принимаем MP4 до 300 МБ через Local Telegram Bot API.
    """
    if uploaded_file is None:
        return {"ok": False, "error": "Файл не выбран."}

    try:
        raw = uploaded_file.getvalue()
    except Exception:
        try:
            raw = bytes(uploaded_file)
        except Exception:
            raw = b""

    if not raw:
        return {"ok": False, "error": "Не удалось прочитать видео."}

    name = str(getattr(uploaded_file, "name", "") or "").strip()
    mime_type = str(getattr(uploaded_file, "type", "") or "video/mp4").strip()

    if not name.lower().endswith(".mp4"):
        return {
            "ok": False,
            "error": "Для отправки в Telegram сейчас используйте ролик в формате MP4.",
        }

    if len(raw) > int(max_bytes):
        return {
            "ok": False,
            "error": (
                "Ролик слишком большой для текущего Publisher. "
                "Максимум 300 МБ."
            ),
        }

    return {
        "ok": True,
        "video_bytes": raw,
        "file_name": name or "agency_w_video.mp4",
        "mime_type": mime_type or "video/mp4",
        "size_bytes": len(raw),
        "size_mb": round(len(raw) / (1024 * 1024), 1),
    }


def _generate_video_announcement(
    ask_openai_fn,
    ask_claude_fn,
    *,
    owner_name: str,
    assignment: str,
    current_text: str,
    video_file_name: str,
    director_note: str = "",
) -> dict[str, Any]:
    """Стагирит координирует, а готовый анонс пишет Мастер контента."""
    note = str(director_note or "").strip()
    context_text = str(current_text or "").strip()

    system_prompt = f"""
Ты — Мастер контента Агентства W.

{CONTENT_MASTER_CORE}

Стагирит только координирует задачу и передал её тебе. ТЕКСТ ПИШЕШЬ ТЫ.

Нужно написать короткий живой анонс к готовому видеоролику для публикации
в Telegram-группах и каналах.

Имя владельца кабинета: {owner_name or 'не указано'}
Исходное поручение Директора:
{str(assignment or '').strip() or 'не указано'}

Имя видеофайла:
{str(video_file_name or '').strip() or 'ролик'}

Контекст текущего материала, если он есть:
{context_text[:5000] or 'нет дополнительного текста'}

Что Директор особенно просит учесть:
{note or 'дополнительных указаний нет'}

ПРАВИЛА:
- начни с человеческого смысла/сцены или сильного вопроса, а не с перечня функций;
- 4–8 коротких строк, удобно читать в Telegram;
- объясни, зачем человеку посмотреть ролик;
- если в исходных данных есть конкретные факты — используй их, но ничего не выдумывай;
- не пиши техническим отчётом и не перечисляй функции сухим списком;
- не обещай доход, гарантированный результат или несуществующие возможности;
- не пиши, что Стагирит создал анонс: Стагирит управляет, Мастер контента пишет;
- допустим один лёгкий интригующий штрих;
- без хэштегов, если Директор их отдельно не попросил;
- верни ТОЛЬКО готовый анонс, без комментариев и заголовков вроде «Вариант».
""".strip()

    text = ""
    engine = ""
    model = ""
    errors: list[str] = []

    if callable(ask_claude_fn):
        try:
            response = ask_claude_fn(
                system_prompt,
                note or str(assignment or "").strip(),
                max_tokens=1200,
            )
            if (
                isinstance(response, dict)
                and response.get("ok") is True
                and str(response.get("text") or "").strip()
            ):
                text = str(response.get("text") or "").strip()
                engine = "master_content"
                model = str(response.get("model") or "")
            elif isinstance(response, dict) and response.get("error"):
                errors.append("Claude: " + str(response.get("error")))
        except Exception as exc:
            errors.append(f"Claude: {type(exc).__name__}: {exc}")

    if not text and callable(ask_openai_fn):
        try:
            raw = ask_openai_fn(
                system_prompt,
                note or str(assignment or "").strip(),
                uploaded_files=[],
                use_web_search=False,
            )
            candidate = str(raw or "").strip()
            if candidate and not candidate.startswith("Ошибка OpenAI:"):
                text = candidate
                engine = "reserve"
            elif candidate:
                errors.append(candidate)
        except Exception as exc:
            errors.append(f"OpenAI: {type(exc).__name__}: {exc}")

    return {
        "ok": bool(text),
        "text": text,
        "engine": engine,
        "model": model,
        "error": "; ".join(errors) if errors else "",
    }


def _create_artist_direction(
    ask_openai_fn,
    assignment: str,
    content_text: str,
    content_meta: dict[str, Any] | None = None,
    user_change_request: str = "",
) -> str:
    """
    Стагирит как арт-директор сам пишет ОРИГИНАЛЬНЫЙ бриф
    для каждой конкретной истории.

    Постоянен только смысл:
    виртуальный офис работает → человек живёт.
    Человеческая сцена всегда извлекается из текущего текста.
    """
    meta = (
        dict(content_meta)
        if isinstance(content_meta, dict)
        else {}
    )

    fallback_scene = str(
        meta.get("human_situation")
        or meta.get("human_gain")
        or "конкретная человеческая сцена из текста"
    ).strip()

    if not callable(ask_openai_fn):
        return (
            "Создай кинематографичную иллюстрацию к данному тексту. "
            "Слева — премиальный виртуальный офис Агентства W в работе. "
            f"Справа — именно эта человеческая сцена: {fallback_scene}. "
            "Правая часть обязана соответствовать рассказу, а не использовать "
            "шаблонный завтрак, песочницу или семью, если их нет в тексте. "
            "Без текста и псевдотекста на изображении."
        )

    prompt = f"""
Ты — Стагирит, арт-директор Агентства W.

Директор НЕ должен писать промт Художнику.
Ты сам читаешь готовый текст и создаёшь профессиональное художественное
задание, которое меняется вместе с каждой историей.

ИСХОДНОЕ ПОРУЧЕНИЕ:
{assignment}

ГОТОВЫЙ ТЕКСТ:
{content_text}

МЕТАДАННЫЕ РЕДАКЦИИ:
{json.dumps(meta, ensure_ascii=False)}

ПОЖЕЛАНИЕ ДИРЕКТОРА К ПЕРЕРИСОВКЕ:
{user_change_request or "нет"}

ГЛАВНАЯ ФОРМУЛА ИЛЛЮСТРАЦИИ:
«Пока виртуальный офис Агентства W берёт на себя рутину,
человек получает обратно свою настоящую жизнь».

ПОСТОЯННАЯ ЧАСТЬ:
- премиальный виртуальный офис Агентства W;
- ощущение умной, спокойной, бесшумной работы;
- взрослые человеческие образы ИИ-агентов;
- Стагирит — взрослый мудрый мужчина-координатор, не статуя, не животное;
- Неония, Неона, Неола — взрослые женщины, если они действительно нужны сцене;
- не перечислять всех агентов ради заполнения кадра.

ПЕРЕМЕННАЯ ЧАСТЬ — САМОЕ ВАЖНОЕ:
1. Найди в ЭТОМ тексте момент, где особенно ясно видно,
   ЧТО человек получил обратно благодаря снятой рутине.
2. Именно этот момент должен стать человеческой сценой иллюстрации.
3. Если в рассказе женщина с ребёнком строит башню — рисуй эту сцену.
4. Если в следующем рассказе прогулка, ужин, путешествие, разговор,
   творчество или спокойная работа — рисуй уже ЭТУ сцену.
5. НИКОГДА не тащи песочницу, завтрак, ребёнка или семью из прошлой истории.
6. Ребёнок появляется только если он действительно есть в текущем тексте.
7. Визуально должно читаться: ИИ работает → человек живёт.

КОМПОЗИЦИЯ:
- предпочтительно две связанные реальности или единая сцена с ясным
  визуальным переходом;
- человеческая часть эмоционально важнее технической;
- связь можно показать мягким золотым светом/потоком;
- кинематографично, тепло, премиально, без рекламной инфографики.

ЗАПРЕЩЕНО:
- текст, подписи, лозунги и псевдотекст в изображении;
- совы, олени, животные-талисманы;
- случайные дети;
- золотые статуи/манекены вместо живых людей;
- рекламный плакат вместо художественной сцены;
- выдуманная человеческая ситуация, которой нет в рассказе.

Верни ТОЛЬКО готовый подробный промт Художнику.
Не объясняй свои решения.
""".strip()

    try:
        raw = ask_openai_fn(
            prompt,
            str(content_text or "")[:12000],
            uploaded_files=[],
            use_web_search=False,
        )
        direction = str(raw or "").strip()
    except Exception:
        direction = ""

    return direction or (
        "Создай кинематографичную иллюстрацию к текущему рассказу. "
        "Слева — виртуальный офис Агентства W в работе. "
        f"Справа — точная человеческая сцена из текста: {fallback_scene}. "
        "Не переносить визуальные детали из предыдущих историй. "
        "Без текста и псевдотекста."
    )


def _make_plan(
    intents: list[str],
    meeting_count: int,
    understanding: dict[str, Any] | None = None,
) -> list[str]:
    plan: list[str] = []

    semantic_steps = (
        understanding.get("steps")
        if isinstance(understanding, dict)
        else []
    )
    if isinstance(semantic_steps, list):
        for item in semantic_steps:
            clean = str(item or "").strip()
            if clean and clean not in plan:
                plan.append(clean)

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
            "Подготовить публикацию.",
            "Показать готовый материал Директору для правки и утверждения.",
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


def _process_assignment(
    owner_id: int,
    owner_name: str,
    assignment: str,
    ask_openai_fn,
    ask_claude_fn=None,
) -> dict[str, Any]:
    deterministic_intents = _detect_intents(assignment)

    if _needs_semantic_director(
        assignment,
        deterministic_intents,
    ):
        understanding = _understand_director_assignment(
            ask_openai_fn,
            assignment,
            deterministic_intents,
        )
    else:
        understanding = {
            "goal": str(assignment or "").strip(),
            "intents": list(deterministic_intents),
            "execution_mode": (
                "answer"
                if deterministic_intents == ["general"]
                else "delegate"
            ),
            "internal_brief": str(assignment or "").strip(),
            "needs_clarification": False,
            "clarification_question": "",
            "requested_image": any(
                token
                in str(assignment or "").lower().replace("ё", "е")
                for token in (
                    "иллюстрац",
                    "картин",
                    "изображен",
                    "нарис",
                    "обложк",
                )
            ),
            "steps": [],
        }

    intents = _normalize_stagirite_intents(
        understanding.get("intents"),
        deterministic_intents,
    )
    meeting_count = _meeting_count_from_text(assignment)
    result: dict[str, Any] = {
        "intents": intents,
        "meeting_count": meeting_count,
        "stagirite_understanding": {
            "goal": str(
                understanding.get("goal")
                or assignment
            ).strip(),
            "execution_mode": str(
                understanding.get("execution_mode")
                or "delegate"
            ).strip(),
            "internal_brief": str(
                understanding.get("internal_brief")
                or assignment
            ).strip(),
            "needs_clarification": bool(
                understanding.get("needs_clarification")
            ),
            "clarification_question": str(
                understanding.get("clarification_question")
                or ""
            ).strip(),
            "requested_image": bool(
                understanding.get("requested_image")
            ),
        },
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
            content_assignment = str(assignment or "").strip()
            internal_brief = str(
                understanding.get("internal_brief")
                or ""
            ).strip()
            if (
                internal_brief
                and internal_brief != content_assignment
            ):
                content_assignment = (
                    content_assignment
                    + "\n\n"
                    + "Внутреннее понимание Стагирита (не показывать читателю): "
                    + internal_brief
                )

            content_pack = _generate_content(
                ask_openai_fn,
                ask_claude_fn,
                owner_id,
                owner_name,
                content_assignment,
            )
            content = (
                str(content_pack.get("text") or "").strip()
                if isinstance(content_pack, dict)
                else str(content_pack or "").strip()
            )
            if content.startswith("Ошибка OpenAI:"):
                result["content_error"] = content
                print(
                    "[STAGIRITE_CONTENT_ERROR] "
                    + content
                )
                if status != "Ошибка":
                    status = "Ошибка"
            else:
                result["content"] = content
                if isinstance(content_pack, dict):
                    result["content_master"] = {
                        "engine": str(
                            content_pack.get("master_engine") or "reserve"
                        ),
                        "model": str(
                            content_pack.get("master_model") or ""
                        ),
                        "quality_score": int(
                            content_pack.get("quality_score") or 0
                        ),
                        "rewrite_used": bool(
                            content_pack.get("rewrite_used")
                        ),
                        "report_warning": bool(
                            content_pack.get("report_warning")
                        ),
                        "series": str(
                            content_pack.get("series") or ""
                        ),
                        "genre": str(
                            content_pack.get("genre") or ""
                        ),
                        "capability": str(
                            content_pack.get("capability") or ""
                        ),
                        "human_situation": str(
                            content_pack.get("human_situation") or ""
                        ),
                        "human_gain": str(
                            content_pack.get("human_gain") or ""
                        ),
                        "reader_entry": str(
                            content_pack.get("reader_entry") or ""
                        ),
                        "cta_mode": str(
                            content_pack.get("cta_mode") or "none"
                        ),
                        "newcomer_reason": str(
                            content_pack.get("newcomer_reason") or ""
                        ),
                        "announcement": bool(
                            content_pack.get("announcement")
                        ),
                        "announcement_time": str(
                            content_pack.get("announcement_time") or ""
                        ),
                        "night_thought": bool(
                            content_pack.get("night_thought")
                        ),
                        "character_count": int(
                            content_pack.get("character_count") or 0
                        ),
                    }
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            result["content_error"] = error_text
            print(
                "[STAGIRITE_CONTENT_ERROR] "
                + error_text
            )
            if status != "Ошибка":
                status = "Ошибка"

    if intents == ["general"]:
        execution_mode = str(
            understanding.get("execution_mode")
            or "answer"
        ).strip().lower()

        if (
            bool(understanding.get("needs_clarification"))
            or execution_mode == "clarify"
        ):
            question = str(
                understanding.get("clarification_question")
                or "Какой конечный результат для вас предпочтительнее?"
            ).strip()
            result["note"] = question
            status = "Нужно уточнение"

        elif execution_mode == "unsupported":
            result["note"] = (
                str(
                    understanding.get("internal_brief")
                    or ""
                ).strip()
                or (
                    "Поручение понятно, но для физического выполнения "
                    "нужен ещё не подключённый исполнительный модуль."
                )
            )
            status = "Нужен исполнительный модуль"

        else:
            result["general_answer"] = _execute_general_director_task(
                ask_openai_fn,
                assignment,
                understanding,
            )
            status = "Готово к утверждению"

    return {
        "assignment": assignment,
        "task_kind": "+".join(intents),
        "status": status,
        "plan": {
            "steps": _make_plan(
                intents,
                meeting_count,
                understanding=understanding,
            )
        },
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
        "Нужен исполнительный модуль": "🧩",
        "Нужен ваш выбор": "🟡",
        "Утверждено": "✅",
        "Выполнено": "✅",
        "В работе": "🔵",
        "Минимум выполнен": "🟢",
        "Ошибка": "🔴",
    }.get(status, "⚙️")




def _normalize_int_ids(values) -> list[int]:
    normalized: list[int] = []
    for value in values or []:
        try:
            contact_id = int(value)
        except (TypeError, ValueError):
            continue
        if contact_id not in normalized:
            normalized.append(contact_id)
    return normalized


def _active_weekly_meeting_task(owner_id: int) -> dict[str, Any] | None:
    """
    Находит или восстанавливает активную недельную цель встреч.

    ВАЖНО:
    Старые версии Агентства могли сохранить поручение с другим task_kind
    или статусом. Поэтому технические метки больше НЕ являются источником
    истины.

    Источник истины:
    1) result.weekly_goal, если он есть;
    2) либо текст поручения, если в нём явно есть «недел...» + «встреч...».

    Если недельная цель есть, но старая техническая метка неверна —
    Стагирит сам чинит запись и продолжает работу.
    """
    owner_id = int(owner_id)
    today = datetime.now(BERLIN).date()
    tasks, _ = _load_tasks(owner_id)

    candidates: list[dict[str, Any]] = []

    for raw in tasks:
        if not isinstance(raw, dict):
            continue

        raw_task = dict(raw)
        assignment_text = str(raw_task.get("assignment") or "").strip()
        assignment_lower = assignment_text.lower().replace("ё", "е")

        result = (
            dict(raw_task.get("result"))
            if isinstance(raw_task.get("result"), dict)
            else {}
        )

        goal = (
            dict(result.get("weekly_goal"))
            if isinstance(result.get("weekly_goal"), dict)
            else {}
        )

        # ------------------------------------------------------
        # MIGRATION 1:
        # Вчерашнее поручение могло быть создано старой версией
        # без weekly_goal, но сам текст однозначно говорит о неделе встреч.
        # Восстанавливаем структуру цели автоматически.
        # ------------------------------------------------------
        looks_like_weekly_meeting = (
            "недел" in assignment_lower
            and (
                "встреч" in assignment_lower
                or "созвон" in assignment_lower
                or "zoom" in assignment_lower
                or "зум" in assignment_lower
            )
        )

        if not goal and looks_like_weekly_meeting:
            minimum, desired = _weekly_goal_from_text(assignment_text)
            goal = {
                "minimum": minimum,
                "desired": desired,
                "period_start": today.isoformat(),
                "period_end": (today + timedelta(days=6)).isoformat(),
                "reserve_target": 50,
                "daily_target": 5,
                "weekly_pool_ids": [],
                "daily_batches": {},
                "restored_at": datetime.now(UTC).isoformat(),
                "restored_reason": "legacy_weekly_meeting_assignment",
            }
            result["meeting_count"] = minimum
            result["weekly_goal"] = goal

            task_id = str(raw_task.get("id") or "")
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "task_kind": "meetings",
                        "status": "В работе",
                        "result": result,
                    },
                )

            raw_task["task_kind"] = "meetings"
            raw_task["status"] = "В работе"
            raw_task["result"] = result

        if not goal:
            continue

        # ------------------------------------------------------
        # MIGRATION 2:
        # Если weekly_goal существует, но task_kind/status старые —
        # не отбрасываем задачу. Исправляем техническую оболочку.
        # ------------------------------------------------------
        task_id = str(raw_task.get("id") or "")
        task_kind = str(raw_task.get("task_kind") or "")
        raw_status = str(raw_task.get("status") or "")

        shell_changes: dict[str, Any] = {}
        if "meetings" not in task_kind:
            shell_changes["task_kind"] = "meetings"

        # Даже старое «Выполнено» не закрывает кампанию само по себе.
        # Реальное завершение ниже проверяется по desired/scheduled и периоду.
        if raw_status in {"Выполнено", "Ошибка", "Готово к утверждению"}:
            shell_changes["status"] = "В работе"

        if shell_changes and task_id:
            _update_task(owner_id, task_id, shell_changes)
            raw_task.update(shell_changes)

        # ------------------------------------------------------
        # MIGRATION 3:
        # Старая трактовка «следующей недели» как будущего понедельника.
        # Без явной даты старта переносим кампанию на сегодня + 6 дней.
        # ------------------------------------------------------
        try:
            period_start = date.fromisoformat(
                str(goal.get("period_start") or "")
            )
            period_end = date.fromisoformat(
                str(goal.get("period_end") or "")
            )
        except ValueError:
            period_start = today
            period_end = today + timedelta(days=6)

        if (
            period_start > today
            and not _has_explicit_weekly_start(assignment_text)
        ):
            period_start = today
            period_end = today + timedelta(days=6)

            goal["period_start"] = period_start.isoformat()
            goal["period_end"] = period_end.isoformat()
            goal["period_rebased_at"] = datetime.now(UTC).isoformat()
            goal["period_rebased_reason"] = (
                "weekly_goal_starts_immediately"
            )
            result["weekly_goal"] = goal
            raw_task["result"] = result

            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "task_kind": "meetings",
                        "status": "В работе",
                        "result": result,
                    },
                )

        # Истёкшая старая кампания сама не воскресает.
        if today > period_end:
            continue

        # Если старая дата начала почему-то позади — это нормально.
        # Цель активна пока сегодня внутри периода.
        if today < period_start:
            continue

        # ------------------------------------------------------
        # Реальный прогресс. Только он решает, закончена ли цель.
        # ------------------------------------------------------
        try:
            refreshed_result, refreshed_status = _refresh_meeting_task(
                owner_id,
                raw_task,
            )
        except Exception:
            refreshed_result = result
            refreshed_status = "В работе"

        desired = int(goal.get("desired") or 5)
        progress_summary = (
            refreshed_result.get("progress_summary")
            if isinstance(
                refreshed_result.get("progress_summary"),
                dict,
            )
            else {}
        )
        scheduled = int(progress_summary.get("scheduled", 0) or 0)

        # Цель действительно закончена только когда максимум достигнут.
        if scheduled >= desired:
            if task_id:
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "status": "Выполнено",
                        "result": refreshed_result,
                    },
                )
            continue

        task = dict(raw_task)
        task["task_kind"] = "meetings"
        task["status"] = (
            refreshed_status
            if refreshed_status not in {"Выполнено", "Ошибка"}
            else "В работе"
        )
        task["result"] = refreshed_result

        if task_id:
            _update_task(
                owner_id,
                task_id,
                {
                    "task_kind": "meetings",
                    "status": task["status"],
                    "result": refreshed_result,
                },
            )

        candidates.append(task)

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: str(
            item.get("created_at")
            or item.get("updated_at")
            or ""
        ),
        reverse=True,
    )
    return candidates[0]



DAILY_CANDIDATE_TASK_KIND = "daily_candidates"
DAILY_CANDIDATE_ASSIGNMENT = "Ежедневная рабочая пятёрка Стагирита"


def _daily_candidate_task(owner_id: int) -> dict[str, Any] | None:
    """Постоянная фоновая задача Стагирита для ежедневной рабочей пятёрки.

    Она не зависит от недельной цели встреч. Неония нужна владельцу, чтобы
    однажды получить/обновить общий пул людей. После этого Стагирит ежедневно
    работает с уже сохранённым пулом и показывает результат пользователю сам.
    """
    owner_id = int(owner_id)
    tasks, _ = _load_tasks(owner_id)

    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        if (
            str(raw.get("task_kind") or "") == DAILY_CANDIDATE_TASK_KIND
            or isinstance(result.get("daily_candidate_feed"), dict)
        ):
            return dict(raw)

    now = datetime.now(UTC).isoformat()
    task = {
        "assignment": DAILY_CANDIDATE_ASSIGNMENT,
        "task_kind": DAILY_CANDIDATE_TASK_KIND,
        "status": "В работе",
        "plan": {
            "steps": [
                "Использовать уже найденный пул контактов/кандидатов.",
                "Подготовить рабочую пятёрку на новый день.",
                "Передать выбранных владельцем людей Неоне.",
            ]
        },
        "result": {
            "daily_candidate_feed": {
                "daily_target": 5,
                "reserve_target": 50,
                "daily_batches": {},
                "created_at": now,
            },
            "selected_candidate_ids": [],
        },
        "created_at": now,
    }
    _save_task(owner_id, task)

    tasks, _ = _load_tasks(owner_id)
    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        if (
            str(raw.get("task_kind") or "") == DAILY_CANDIDATE_TASK_KIND
            or isinstance(result.get("daily_candidate_feed"), dict)
        ):
            return dict(raw)
    return None


def _candidate_feed_container(
    result: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    weekly = result.get("weekly_goal")
    if isinstance(weekly, dict) and weekly:
        return "weekly_goal", dict(weekly)

    daily = result.get("daily_candidate_feed")
    if isinstance(daily, dict) and daily:
        return "daily_candidate_feed", dict(daily)

    return "daily_candidate_feed", {
        "daily_target": 5,
        "reserve_target": 50,
        "daily_batches": {},
    }


def get_recent_daily_candidates_for_stagirite(
    owner_id: int,
    days: int = 2,
) -> list[dict[str, Any]]:
    """Возвращает сегодняшнюю и предыдущие рабочие пятёрки Стагирита."""
    owner_id = int(owner_id)
    days = max(1, min(14, int(days or 2)))
    today = datetime.now(BERLIN).date()
    wanted = [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]
    best: dict[str, dict[str, Any]] = {}

    tasks, _ = _load_tasks(owner_id)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        for container_name in ("daily_candidate_feed", "weekly_goal"):
            container = result.get(container_name)
            if not isinstance(container, dict):
                continue
            batches = container.get("daily_batches")
            if not isinstance(batches, dict):
                continue
            for day_key in wanted:
                info = batches.get(day_key)
                if not isinstance(info, dict):
                    continue
                current = best.get(day_key)
                prepared_at = str(info.get("prepared_at") or "")
                current_at = str((current or {}).get("prepared_at") or "")
                if current is None or prepared_at >= current_at:
                    best[day_key] = {
                        "date": day_key,
                        "candidate_ids": _normalize_int_ids(
                            info.get("candidate_ids", [])
                        ),
                        "approved_ids": _normalize_int_ids(
                            info.get("approved_ids", [])
                        ),
                        "prepared_at": prepared_at,
                        "source": str(info.get("source") or container_name),
                    }

    return [best[day_key] for day_key in wanted if day_key in best]


def _shown_candidate_ids_for_stagirite(
    owner_id: int,
    *,
    exclude_day: str = "",
) -> set[int]:
    """
    Все кандидаты, которых Стагирит уже показывал Директору раньше.

    Историю берём из сохранённых daily_batches во всех задачах Стагирита,
    поэтому ротация переживает rerun, новый день и переход между
    недельной целью и постоянной ежедневной пятёркой.
    """
    owner_id = int(owner_id)
    exclude_day = str(exclude_day or "").strip()
    shown: set[int] = set()

    tasks, _ = _load_tasks(owner_id)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = (
            task.get("result")
            if isinstance(task.get("result"), dict)
            else {}
        )
        for container_name in ("daily_candidate_feed", "weekly_goal"):
            container = result.get(container_name)
            if not isinstance(container, dict):
                continue
            batches = container.get("daily_batches")
            if not isinstance(batches, dict):
                continue
            for day_key, info in batches.items():
                if exclude_day and str(day_key) == exclude_day:
                    continue
                if not isinstance(info, dict):
                    continue
                shown.update(
                    _normalize_int_ids(info.get("candidate_ids", []))
                )
                shown.update(
                    _normalize_int_ids(info.get("approved_ids", []))
                )

    return shown


def ensure_weekly_candidates_for_neona(
    owner_id: int,
    prepare_candidates_fn=None,
) -> dict[str, Any]:
    """
    Ежедневная рабочая пятёрка Стагирита → Неона.

    Неония нужна владельцу только для получения/обновления общего пула людей.
    После появления пула Стагирит сам готовит ежедневную пятёрку.

    ВАЖНО:
    - не требует ежедневного захода в Неонию;
    - при первом открытии «Моего дня» или Неоны в новый день сам готовит текущую пятёрку;
    - повторный rerun в тот же день не создаёт новую пятёрку;
    - людей ещё НЕ считает выбранными владельцем.
    """
    owner_id = int(owner_id)
    task = _active_weekly_meeting_task(owner_id)
    if not task:
        task = _daily_candidate_task(owner_id)
    if not task:
        return {
            "ok": False,
            "active": False,
            "candidate_ids": [],
            "message": "Стагирит пока не смог открыть ежедневную рабочую пятёрку.",
        }

    result = (
        dict(task.get("result"))
        if isinstance(task.get("result"), dict)
        else {}
    )
    container_key, goal = _candidate_feed_container(result)

    minimum = int(goal.get("minimum") or 0)
    desired = int(goal.get("desired") or 5)
    reserve_target = int(goal.get("reserve_target") or 50)
    daily_target = int(goal.get("daily_target") or 5)

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

    candidate_ids = _normalize_int_ids(
        today_info.get("candidate_ids", [])
    )
    approved_ids = _normalize_int_ids(
        today_info.get("approved_ids", [])
    )

    # До исправления ротации сегодняшняя пятёрка могла повторять вчерашнюю.
    # Если Директор ещё никого из неё не утвердил, один раз считаем такую
    # запись устаревшей и пересобираем её из НОВЫХ людей.
    previously_shown = _shown_candidate_ids_for_stagirite(
        owner_id,
        exclude_day=today_key,
    )
    stale_repeat = bool(
        candidate_ids
        and not approved_ids
        and any(cid in previously_shown for cid in candidate_ids)
    )
    if stale_repeat:
        candidate_ids = []
        today_info = {}

    # Сегодняшняя новая пятёрка уже существует — повторный rerun её не меняет.
    if candidate_ids:
        return {
            "ok": True,
            "active": True,
            "task_id": str(task.get("id") or ""),
            "candidate_ids": candidate_ids,
            "approved_ids": approved_ids,
            "period_start": str(goal.get("period_start") or ""),
            "period_end": str(goal.get("period_end") or ""),
            "minimum": minimum,
            "desired": desired,
            "daily_target": daily_target,
            "prepared_at": str(today_info.get("prepared_at") or ""),
            "message": "",
        }

    if not callable(prepare_candidates_fn):
        return {
            "ok": False,
            "active": True,
            "task_id": str(task.get("id") or ""),
            "candidate_ids": [],
            "approved_ids": approved_ids,
            "message": "Неония пока недоступна для автоматической подготовки.",
        }

    selected_all = _normalize_int_ids(
        result.get("selected_candidate_ids", [])
    )

    # Главное правило ротации: вчера показан — сегодня место не занимает,
    # даже если Директор его не выбирал. Решение «не выбрала» тоже учитываем.
    excluded_for_today = set(selected_all)
    excluded_for_today.update(previously_shown)

    try:
        prepared = prepare_candidates_fn(
            owner_id,
            desired_count=daily_target,
            reserve_target=reserve_target,
            exclude_ids=sorted(excluded_for_today),
        )
    except TypeError:
        # Совместимость с коротким промежутком обновления файлов.
        prepared = prepare_candidates_fn(
            owner_id,
            desired_count=daily_target,
        )
    except Exception as exc:
        return {
            "ok": False,
            "active": True,
            "task_id": str(task.get("id") or ""),
            "candidate_ids": [],
            "approved_ids": approved_ids,
            "message": f"Не удалось подготовить сегодняшних кандидатов: {exc}",
        }

    if not isinstance(prepared, dict):
        prepared = {}

    candidate_ids = []
    for contact_id in _normalize_int_ids(
        prepared.get("candidate_ids", [])
    ):
        if contact_id in excluded_for_today:
            continue
        candidate_ids.append(contact_id)
        if len(candidate_ids) >= daily_target:
            break

    reserve_ids = _normalize_int_ids(
        prepared.get("reserve_ids", [])
    )[:reserve_target]

    goal["weekly_pool_ids"] = reserve_ids
    today_info = {
        "candidate_ids": candidate_ids,
        "approved_ids": approved_ids,
        "prepared_at": datetime.now(UTC).isoformat(),
        "source": "stagirite_daily_bridge",
    }
    daily_batches[today_key] = today_info
    goal["daily_batches"] = daily_batches
    goal["last_daily_prepare_date"] = today_key
    result[container_key] = goal

    task_id = str(task.get("id") or "")
    if task_id:
        _update_task(
            owner_id,
            task_id,
            {
                "status": "В работе",
                "result": result,
            },
        )

    return {
        "ok": True,
        "active": True,
        "task_id": task_id,
        "candidate_ids": candidate_ids,
        "approved_ids": approved_ids,
        "period_start": str(goal.get("period_start") or ""),
        "period_end": str(goal.get("period_end") or ""),
        "minimum": minimum,
        "desired": desired,
        "daily_target": daily_target,
        "prepared_at": str(today_info.get("prepared_at") or ""),
        "available_reserve": int(
            prepared.get("available_reserve")
            or len(reserve_ids)
        ),
        "message": str(prepared.get("message") or ""),
    }


def accept_weekly_candidates_for_neona(
    owner_id: int,
    task_id: str,
    selected_ids: list[int],
) -> dict[str, Any]:
    """
    Владелец выбирает людей уже у Неоны.
    Только здесь кандидаты становятся фактически выбранными.
    """
    owner_id = int(owner_id)
    task_id = str(task_id or "").strip()
    chosen = _normalize_int_ids(selected_ids)

    if not task_id or not chosen:
        return {"ok": False, "selected_ids": []}

    tasks, _ = _load_tasks(owner_id)
    task = next(
        (
            item
            for item in tasks
            if str(item.get("id") or "") == task_id
        ),
        None,
    )
    if not isinstance(task, dict):
        return {"ok": False, "selected_ids": []}

    result = (
        dict(task.get("result"))
        if isinstance(task.get("result"), dict)
        else {}
    )
    container_key, goal = _candidate_feed_container(result)
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

    offered = set(
        _normalize_int_ids(today_info.get("candidate_ids", []))
    )
    chosen = [
        contact_id
        for contact_id in chosen
        if contact_id in offered
    ]
    if not chosen:
        return {"ok": False, "selected_ids": []}

    approved = _normalize_int_ids(
        today_info.get("approved_ids", [])
    )
    for contact_id in chosen:
        if contact_id not in approved:
            approved.append(contact_id)
    today_info["approved_ids"] = approved
    today_info["approved_at"] = datetime.now(UTC).isoformat()
    daily_batches[today_key] = today_info
    goal["daily_batches"] = daily_batches
    result[container_key] = goal

    all_selected = _normalize_int_ids(
        result.get("selected_candidate_ids", [])
    )
    for contact_id in chosen:
        if contact_id not in all_selected:
            all_selected.append(contact_id)
    result["selected_candidate_ids"] = all_selected

    progress = (
        dict(result.get("contact_progress"))
        if isinstance(result.get("contact_progress"), dict)
        else {}
    )
    for contact_id in chosen:
        previous = (
            dict(progress.get(str(contact_id)))
            if isinstance(progress.get(str(contact_id)), dict)
            else {}
        )
        previous["status"] = str(
            previous.get("status")
            or "awaiting_first_message"
        )
        progress[str(contact_id)] = previous
    result["contact_progress"] = progress

    _save_stagirite_candidate_selection(owner_id, chosen)
    _update_task(
        owner_id,
        task_id,
        {
            "status": "В работе",
            "result": result,
        },
    )

    return {
        "ok": True,
        "selected_ids": chosen,
        "approved_ids": approved,
    }



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

    previously_shown = _shown_candidate_ids_for_stagirite(
        owner_id,
        exclude_day=today_key,
    )
    current_ids = _normalize_int_ids(today_info.get("candidate_ids", []))
    current_approved = _normalize_int_ids(today_info.get("approved_ids", []))
    if (
        current_ids
        and not current_approved
        and any(cid in previously_shown for cid in current_ids)
    ):
        # Миграция старой повторяющейся пятёрки: если её ещё не утвердили,
        # сегодня сразу заменяем на свежую.
        today_info = {}

    excluded_for_today = set(selected_all)
    excluded_for_today.update(previously_shown)

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
                    exclude_ids=sorted(excluded_for_today),
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
                if cid in excluded_for_today or cid in candidate_ids:
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
    owner_name: str = "",
    ask_openai_fn=None,
    ask_claude_fn=None,
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
        st.markdown("## 🪄 Готовый материал")

        task_id = str(task.get("id") or task.get("created_at") or "content")
        draft_key = f"stagirite_content_draft_{task_id}"
        image_state_key = f"stagirite_artist_image_{task_id}"
        image_meta_key = f"stagirite_artist_meta_{task_id}"
        video_state_key = f"stagirite_video_{task_id}"
        video_meta_key = f"stagirite_video_meta_{task_id}"
        has_current_image = bool(
            st.session_state.get(image_state_key)
        )
        current_image_for_publish = st.session_state.get(
            image_state_key
        )
        has_current_video = bool(
            st.session_state.get(video_state_key)
        )
        telegram_destinations: list[dict[str, Any]] = []
        if callable(list_publisher_destinations):
            try:
                telegram_destinations = list_publisher_destinations(
                    owner_id
                )
            except Exception:
                telegram_destinations = []

        saved_text = str(
            result.get("edited_content")
            or result.get("content")
            or ""
        )

        # Кнопка «Переписать» не может менять значение уже созданного
        # st.text_area в том же проходе Streamlit. Поэтому новый текст
        # передаём через отдельный служебный ключ и применяем ТОЛЬКО
        # на следующем rerun — до создания виджета.
        pending_draft_key = (
            f"stagirite_content_pending_draft_{task_id}"
        )
        pending_text = st.session_state.pop(
            pending_draft_key,
            None,
        )
        if pending_text is not None:
            st.session_state[draft_key] = str(pending_text)
        elif draft_key not in st.session_state:
            st.session_state[draft_key] = saved_text

        draft = st.text_area(
            "Текст публикации",
            key=draft_key,
            height=260,
        )


        is_approved = bool(result.get("content_approved"))
        published_at = str(result.get("published_at") or "").strip()
        published_count = int(result.get("published_count") or 0)

        c1, c2, c3 = st.columns(3)

        if c1.button(
            "✏️ Сохранить правки",
            key=f"stagirite_save_content_{task_id}",
            use_container_width=True,
        ):
            updated = dict(result)
            updated["edited_content"] = str(draft).strip()
            updated["content_approved"] = False
            updated.pop("content_approved_at", None)
            updated.pop("published_at", None)
            updated.pop("published_count", None)
            updated.pop("image_published_at", None)
            updated.pop("image_published_count", None)
            updated.pop("image_published_hash", None)
            updated.pop("image_media_id", None)
            updated.pop("telegram_published_at", None)
            updated.pop("telegram_published_hash", None)
            updated.pop("telegram_published_count", None)
            updated.pop("telegram_publish_results", None)
            _update_task(
                owner_id,
                task_id,
                {
                    "result": updated,
                    "status": "Нужно решение владельца",
                },
            )
            st.success("Правки сохранены.")
            st.rerun()

        can_rewrite = (
            callable(ask_openai_fn)
            and str(task.get("assignment") or "").strip()
        )
        if c2.button(
            "🔄 Переписать",
            key=f"stagirite_rewrite_content_{task_id}",
            use_container_width=True,
            disabled=not can_rewrite,
        ):
            with st.spinner("Стагирит возвращает материал Мастеру на новую версию..."):
                content_pack = _generate_content(
                    ask_openai_fn,
                    ask_claude_fn,
                    owner_id,
                    owner_name,
                    str(task.get("assignment") or "").strip(),
                )

            new_text = (
                str(content_pack.get("text") or "").strip()
                if isinstance(content_pack, dict)
                else str(content_pack or "").strip()
            )

            if not new_text:
                st.warning("Новая версия не получилась. Старый текст оставлен.")
            else:
                updated = dict(result)
                updated["content"] = new_text
                updated.pop("edited_content", None)
                updated["content_approved"] = False
                updated.pop("content_approved_at", None)
                updated.pop("published_at", None)
                updated.pop("published_count", None)
                updated.pop("image_published_at", None)
                updated.pop("image_published_count", None)
                updated.pop("image_published_hash", None)
                updated.pop("image_media_id", None)
                updated.pop("telegram_published_at", None)
                updated.pop("telegram_published_hash", None)
                updated.pop("telegram_published_count", None)
                updated.pop("telegram_publish_results", None)

                if isinstance(content_pack, dict):
                    updated["content_master"] = {
                        "engine": str(
                            content_pack.get("master_engine") or "reserve"
                        ),
                        "model": str(
                            content_pack.get("master_model") or ""
                        ),
                        "quality_score": int(
                            content_pack.get("quality_score") or 0
                        ),
                        "rewrite_used": bool(
                            content_pack.get("rewrite_used")
                        ),
                        "report_warning": bool(
                            content_pack.get("report_warning")
                        ),
                        "series": str(
                            content_pack.get("series") or ""
                        ),
                        "genre": str(
                            content_pack.get("genre") or ""
                        ),
                        "capability": str(
                            content_pack.get("capability") or ""
                        ),
                        "human_situation": str(
                            content_pack.get("human_situation") or ""
                        ),
                        "human_gain": str(
                            content_pack.get("human_gain") or ""
                        ),
                        "reader_entry": str(
                            content_pack.get("reader_entry") or ""
                        ),
                        "cta_mode": str(
                            content_pack.get("cta_mode") or "none"
                        ),
                        "newcomer_reason": str(
                            content_pack.get("newcomer_reason") or ""
                        ),
                    }

                _update_task(
                    owner_id,
                    task_id,
                    {
                        "result": updated,
                        "status": "Нужно решение владельца",
                    },
                )
                st.session_state[
                    f"stagirite_content_pending_draft_{task_id}"
                ] = new_text
                st.rerun()

        delete_flag_key = f"stagirite_delete_content_confirm_{task_id}"
        if c3.button(
            "🗑 Удалить",
            key=f"stagirite_delete_content_{task_id}",
            use_container_width=True,
        ):
            st.session_state[delete_flag_key] = True
            st.rerun()

        if st.session_state.get(delete_flag_key):
            st.warning(
                "Удалить этот материал вместе с поручением? "
                "Он исчезнет из текущей работы Стагирита."
            )
            d1, d2 = st.columns(2)
            if d1.button(
                "Да, удалить",
                key=f"stagirite_delete_content_yes_{task_id}",
                use_container_width=True,
            ):
                _delete_task(owner_id, task_id)
                st.session_state.pop(
                    f"stagirite_content_draft_{task_id}",
                    None,
                )
                st.session_state.pop(delete_flag_key, None)
                st.rerun()

            if d2.button(
                "Отмена",
                key=f"stagirite_delete_content_no_{task_id}",
                use_container_width=True,
            ):
                st.session_state.pop(delete_flag_key, None)
                st.rerun()

        if st.button(
            "✅ Утвердить материал",
            key=f"stagirite_approve_content_{task_id}",
            use_container_width=True,
            type="primary",
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
                updated.pop("image_published_at", None)
                updated.pop("image_published_count", None)
                updated.pop("image_published_hash", None)
                updated.pop("image_media_id", None)
                updated.pop("telegram_published_at", None)
                updated.pop("telegram_published_hash", None)
                updated.pop("telegram_published_count", None)
                updated.pop("telegram_publish_results", None)
                _update_task(
                    owner_id,
                    task_id,
                    {
                        "result": updated,
                        "status": "Готово к публикации",
                    },
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
                    f"📣 Размещено во внутренних сообщениях: {published_count} получател(я/ей)."
                )
                st.caption(
                    "Сообщение размещено непосредственно каждому зарегистрированному "
                    "человеку вашей нижестоящей структуры. Это внутренняя доставка "
                    "Агентства W, а не подтверждение Telegram-доставки."
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

                if has_current_image:
                    st.info(
                        "Иллюстрация уже готова. Ниже можно разместить "
                        "пост и картинку вместе одной кнопкой."
                    )
                elif st.button(
                    f"📣 Разместить всей структуре ({recipients_count})",
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
                            subject="Сообщение Агентства W",
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
                        updated["published_delivery"] = "internal_structure"
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

            # Telegram-площадки владельца. Если иллюстрация уже готова,
            # кнопка будет ниже непосредственно под иллюстрацией.
            if telegram_destinations and not has_current_image:
                tg_ids = [
                    int(item.get("chat_id"))
                    for item in telegram_destinations
                    if item.get("chat_id") is not None
                ]
                tg_hash = _telegram_publication_hash(
                    str(result.get("edited_content") or draft).strip(),
                    None,
                    tg_ids,
                )
                tg_already = bool(
                    str(result.get("telegram_published_hash") or "") == tg_hash
                )
                if tg_already:
                    st.success(
                        "📡 Telegram принял эту версию материала: "
                        f"{int(result.get('telegram_published_count') or 0)} "
                        "площадк(а/и)."
                    )
                elif st.button(
                    f"📡 Опубликовать во всех моих Telegram-площадках ({len(tg_ids)})",
                    key=f"stagirite_publish_telegram_text_{task_id}",
                    use_container_width=True,
                    type="primary",
                    disabled=publish_to_publisher_destinations is None,
                ):
                    try:
                        tg_result = publish_to_publisher_destinations(
                            owner_id,
                            str(result.get("edited_content") or draft).strip(),
                            destination_ids=tg_ids,
                        )
                        updated = dict(result)
                        updated["telegram_published_at"] = datetime.now(UTC).isoformat()
                        updated["telegram_published_hash"] = tg_hash
                        updated["telegram_published_count"] = int(tg_result.get("accepted") or 0)
                        updated["telegram_publish_results"] = tg_result.get("results") or []
                        _update_task(owner_id, task_id, {"result": updated})
                        st.rerun()
                    except Exception as exc:
                        st.error(
                            "Не удалось передать материал в Telegram. "
                            f"{type(exc).__name__}: {exc}"
                        )


        understanding = (
            result.get("stagirite_understanding")
            if isinstance(result.get("stagirite_understanding"), dict)
            else {}
        )
        if bool(understanding.get("requested_image")):
            st.success(
                "🎨 Стагирит понял: к материалу нужна иллюстрация. "
                "Промт Художнику он подготовит сам."
            )

        st.markdown("### 📎 Готовые медиа")
        st.caption(
            "Можно загрузить свою картинку или готовый MP4-ролик. "
            "Художник отвечает только за иллюстрации; анонс к ролику пишет "
            "Мастер контента."
        )

        # --------------------------------------------------------
        # Директор может принести готовую картинку или ролик.
        # --------------------------------------------------------
        upload_key = f"stagirite_artist_upload_{task_id}"
        video_upload_key = f"stagirite_video_upload_{task_id}"
        media_upload_cols = st.columns(2)
        with media_upload_cols[0]:
            uploaded_illustration = st.file_uploader(
                "🖼️ Загрузить картинку",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=False,
                key=upload_key,
                help=(
                    "Если подходящая картинка уже найдена или создана "
                    "в другом сервисе, загрузите её сюда. "
                    "Поддерживаются PNG, JPG/JPEG и WEBP до 12 МБ."
                ),
            )
        with media_upload_cols[1]:
            uploaded_video = st.file_uploader(
                "🎬 Загрузить ролик",
                type=["mp4"],
                accept_multiple_files=False,
                key=video_upload_key,
                help=(
                    "Готовый MP4-ролик для публикации в ваших Telegram-площадках. "
                    "Текущий лимит — 300 МБ."
                ),
            )

        if uploaded_illustration is not None:
            try:
                st.image(
                    uploaded_illustration,
                    caption="Предпросмотр вашей иллюстрации",
                    use_container_width=True,
                )
            except Exception:
                pass

            if st.button(
                "✅ Использовать эту иллюстрацию",
                key=f"stagirite_artist_use_upload_{task_id}",
                use_container_width=True,
                type="primary",
            ):
                prepared_upload = _prepare_uploaded_illustration(
                    uploaded_illustration
                )
                if not prepared_upload.get("ok"):
                    st.error(
                        str(
                            prepared_upload.get("error")
                            or "Не удалось загрузить иллюстрацию."
                        )
                    )
                else:
                    st.session_state[image_state_key] = (
                        prepared_upload["image_bytes"]
                    )
                    st.session_state[image_meta_key] = {
                        "source": "uploaded",
                        "source_text": str(draft).strip(),
                        "original_name": str(
                            getattr(
                                uploaded_illustration,
                                "name",
                                "",
                            )
                            or ""
                        ),
                        "original_format": str(
                            prepared_upload.get("original_format")
                            or ""
                        ),
                        "original_width": int(
                            prepared_upload.get("original_width")
                            or 0
                        ),
                        "original_height": int(
                            prepared_upload.get("original_height")
                            or 0
                        ),
                        "width": int(
                            prepared_upload.get("width")
                            or 0
                        ),
                        "height": int(
                            prepared_upload.get("height")
                            or 0
                        ),
                        "resized": bool(
                            prepared_upload.get("resized")
                        ),
                        "created_at": datetime.now(UTC).isoformat(),
                        "quality_passed": False,
                        "auto_redrawn": False,
                    }
                    st.success(
                        "Ваша иллюстрация принята. "
                        "Теперь её можно публиковать вместе с постом."
                    )
                    st.rerun()

        if uploaded_video is not None:
            prepared_video = _prepare_uploaded_video(uploaded_video)
            if prepared_video.get("ok"):
                st.video(uploaded_video)
                st.caption(
                    "🎬 "
                    + str(prepared_video.get("file_name") or "Ролик")
                    + " · "
                    + str(prepared_video.get("size_mb") or 0)
                    + " МБ"
                )
                if st.button(
                    "✅ Использовать этот ролик",
                    key=f"stagirite_video_use_upload_{task_id}",
                    use_container_width=True,
                    type="primary",
                ):
                    st.session_state[video_state_key] = prepared_video["video_bytes"]
                    st.session_state[video_meta_key] = {
                        "source": "uploaded",
                        "file_name": str(prepared_video.get("file_name") or "agency_w_video.mp4"),
                        "mime_type": str(prepared_video.get("mime_type") or "video/mp4"),
                        "size_bytes": int(prepared_video.get("size_bytes") or 0),
                        "size_mb": float(prepared_video.get("size_mb") or 0),
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                    updated = dict(result)
                    for key in (
                        "video_announcement",
                        "video_announcement_approved",
                        "video_announcement_approved_at",
                        "video_announcement_engine",
                        "video_structure_published_at",
                        "video_structure_published_hash",
                        "video_structure_published_count",
                        "video_structure_media_id",
                        "video_telegram_published_at",
                        "video_telegram_published_hash",
                        "video_telegram_published_count",
                        "video_telegram_publish_results",
                    ):
                        updated.pop(key, None)
                    _update_task(owner_id, task_id, {"result": updated})
                    st.success("Ролик принят. Теперь Мастер контента может написать к нему анонс.")
                    st.rerun()
            else:
                st.error(str(prepared_video.get("error") or "Не удалось подготовить ролик."))

        st.markdown("### 🎨 Художник-иллюстратор")
        st.caption(
            "Если нужна новая иллюстрация, вам не надо придумывать промт. "
            "Стагирит как арт-директор ставит Художнику задачу по смыслу текста."
        )

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
            image_source = str(
                current_meta.get("source") or "artist"
            ).strip()

            st.image(
                current_image,
                caption=(
                    "Ваша загруженная иллюстрация"
                    if image_source == "uploaded"
                    else "Иллюстрация Художника"
                ),
                use_container_width=True,
            )

            if image_source == "uploaded":
                st.caption(
                    "📤 Эта картинка загружена Директором. "
                    "Она используется в публикации так же, "
                    "как иллюстрация Художника."
                )
                if current_meta.get("resized"):
                    st.caption(
                        "Изображение было аккуратно уменьшено "
                        "для удобной публикации."
                    )

            if current_meta.get("auto_redrawn"):
                st.caption(
                    "🧐 Стагирит отклонил первую версию и сам отправил "
                    "Художнику на одну доработку."
                )
            elif current_meta.get("quality_passed"):
                st.caption(
                    "✅ Стагирит проверил: иллюстрация соответствует смыслу поста."
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

            # ----------------------------------------------------
            # Публикация иллюстрации партнёрам.
            # Если пост уже размещён — дополняем СУЩЕСТВУЮЩИЕ
            # сообщения картинкой, не дублируя текст.
            # Если пост ещё не размещён — отправляем текст + картинку
            # как одно внутреннее сообщение.
            # ----------------------------------------------------
            current_image_hash = _artist_image_hash(
                current_image
            )
            published_image_hash = str(
                result.get("image_published_hash")
                or ""
            ).strip()
            image_published_count = int(
                result.get("image_published_count")
                or 0
            )
            image_already_published = bool(
                current_image_hash
                and published_image_hash
                and current_image_hash == published_image_hash
            )

            image_recipients = structure_member_ids(
                owner_id
            )
            image_recipients_count = len(
                image_recipients
            )

            if image_already_published:
                st.success(
                    "🖼️ Иллюстрация размещена во внутренних сообщениях: "
                    f"{image_published_count} получател(я/ей)."
                )
            elif published_image_hash and current_image_hash:
                st.warning(
                    "Эта версия иллюстрации ещё не размещена: "
                    "после предыдущей публикации картинка была изменена."
                )

            can_publish_image = bool(
                is_approved
                and not source_changed
                and image_recipients_count > 0
                and publish_structure_material is not None
            )

            if not is_approved:
                st.caption(
                    "Сначала утвердите текст материала — затем его "
                    "можно разместить вместе с иллюстрацией."
                )
            elif source_changed:
                st.caption(
                    "Сначала перерисуйте иллюстрацию под текущую версию текста."
                )
            elif image_recipients_count == 0:
                st.caption(
                    "В структуре пока нет зарегистрированных получателей."
                )
            elif not image_already_published:
                already_text_published = bool(
                    str(result.get("published_at") or "").strip()
                )

                image_button_label = (
                    f"📣 Добавить иллюстрацию к посту у всей структуры "
                    f"({image_recipients_count})"
                    if already_text_published
                    else
                    f"📣 Разместить пост с иллюстрацией всей структуре "
                    f"({image_recipients_count})"
                )

                if st.button(
                    image_button_label,
                    key=f"stagirite_publish_image_{task_id}",
                    use_container_width=True,
                    type="primary",
                    disabled=not can_publish_image,
                ):
                    try:
                        image_file_name = (
                            "agency_w_illustration_"
                            + datetime.now(BERLIN).strftime(
                                "%Y%m%d_%H%M%S"
                            )
                            + ".png"
                        )
                        clean_post_text = str(
                            result.get("edited_content")
                            or draft
                        ).strip()

                        if already_text_published:
                            if (
                                attach_structure_image_to_published_message
                                is None
                            ):
                                raise RuntimeError(
                                    "Модуль прикрепления изображения не подключён."
                                )

                            publish_result = (
                                attach_structure_image_to_published_message(
                                    owner_id,
                                    clean_post_text,
                                    current_image,
                                    subject="Сообщение Агентства W",
                                    published_at=str(
                                        result.get("published_at")
                                        or ""
                                    ),
                                    file_name=image_file_name,
                                )
                            )
                        else:
                            publish_result = publish_structure_material(
                                owner_id,
                                clean_post_text,
                                image_bytes=current_image,
                                subject="Сообщение Агентства W",
                                zoom_url=str(
                                    result.get("zoom_link")
                                    or _load_stagirite_settings(
                                        owner_id
                                    ).get("zoom_link")
                                    or ""
                                ).strip(),
                                file_name=image_file_name,
                            )

                        count = int(
                            publish_result.get("count")
                            or 0
                        )
                        media_id = str(
                            publish_result.get("media_id")
                            or ""
                        ).strip()
                        publish_mode = str(
                            publish_result.get("mode")
                            or ""
                        ).strip()

                        updated = dict(result)
                        updated["edited_content"] = clean_post_text
                        updated["content_approved"] = True
                        updated["image_published_at"] = (
                            datetime.now(UTC).isoformat()
                        )
                        updated["image_published_count"] = count
                        updated["image_published_hash"] = (
                            current_image_hash
                        )
                        updated["image_media_id"] = media_id
                        updated["image_published_mode"] = (
                            publish_mode
                        )

                        if not already_text_published:
                            updated["published_at"] = (
                                datetime.now(UTC).isoformat()
                            )
                            updated["published_count"] = count
                            updated["published_delivery"] = (
                                "internal_structure_with_image"
                            )

                        _update_task(
                            owner_id,
                            task_id,
                            {
                                "result": updated,
                                "status": "Выполнено",
                            },
                        )
                        st.rerun()

                    except Exception as exc:
                        st.error(
                            "Не удалось разместить иллюстрацию "
                            "во внутренних сообщениях. "
                            f"{type(exc).__name__}: {exc}"
                        )

            # ----------------------------------------------------
            # Telegram-площадки: одна кнопка, все подключённые площадки.
            # Статус означает только успешный ответ Telegram Bot API.
            # ----------------------------------------------------
            if (
                is_approved
                and not source_changed
                and telegram_destinations
            ):
                tg_ids = [
                    int(item.get("chat_id"))
                    for item in telegram_destinations
                    if item.get("chat_id") is not None
                ]
                clean_tg_text = str(
                    result.get("edited_content") or draft
                ).strip()
                tg_hash = _telegram_publication_hash(
                    clean_tg_text,
                    current_image,
                    tg_ids,
                )
                tg_already = bool(
                    str(result.get("telegram_published_hash") or "") == tg_hash
                )

                if tg_already:
                    st.success(
                        "📡 Telegram принял пост с иллюстрацией: "
                        f"{int(result.get('telegram_published_count') or 0)} "
                        "площадк(а/и)."
                    )
                elif st.button(
                    f"📡 Опубликовать пост с иллюстрацией во всех моих Telegram-площадках ({len(tg_ids)})",
                    key=f"stagirite_publish_telegram_image_{task_id}",
                    use_container_width=True,
                    type="primary",
                    disabled=publish_to_publisher_destinations is None,
                ):
                    try:
                        tg_result = publish_to_publisher_destinations(
                            owner_id,
                            clean_tg_text,
                            image_bytes=current_image,
                            destination_ids=tg_ids,
                        )
                        updated = dict(result)
                        updated["telegram_published_at"] = datetime.now(UTC).isoformat()
                        updated["telegram_published_hash"] = tg_hash
                        updated["telegram_published_count"] = int(
                            tg_result.get("accepted") or 0
                        )
                        updated["telegram_publish_results"] = (
                            tg_result.get("results") or []
                        )
                        _update_task(
                            owner_id,
                            task_id,
                            {"result": updated},
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(
                            "Не удалось передать пост в Telegram-площадки. "
                            f"{type(exc).__name__}: {exc}"
                        )

            change_request = st.text_input(
                "Что изменить в сцене?",
                placeholder=(
                    "Можно написать совсем просто: «сделай теплее», "
                    "«меньше офиса», «героиня старше». Стагирит сам "
                    "превратит это в бриф Художнику."
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
                        artist_direction = _create_artist_direction(
                            ask_openai_fn,
                            str(task.get("assignment") or ""),
                            str(draft).strip(),
                            result.get("content_master")
                            if isinstance(result.get("content_master"), dict)
                            else {},
                            user_change_request=str(
                                change_request or ""
                            ).strip(),
                        )
                        generated = generate_image_fn(
                            str(draft).strip(),
                            change_request=artist_direction,
                            size=chosen_size,
                        )
                    if generated.get("ok"):
                        st.session_state[image_state_key] = generated["image_bytes"]
                        st.session_state[image_meta_key] = {
                            "source": "artist",
                            "source_text": str(draft).strip(),
                            "size": chosen_size,
                            "change_request": str(change_request or "").strip(),
                            "artist_direction": str(
                                artist_direction or ""
                            ).strip(),
                            "created_at": datetime.now(UTC).isoformat(),
                            "quality_passed": bool(
                                generated.get("quality_passed")
                            ),
                            "quality_review": str(
                                generated.get("quality_review") or ""
                            ),
                            "auto_redrawn": bool(
                                generated.get("auto_redrawn")
                            ),
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
                        artist_direction = _create_artist_direction(
                            ask_openai_fn,
                            str(task.get("assignment") or ""),
                            str(draft).strip(),
                            result.get("content_master")
                            if isinstance(result.get("content_master"), dict)
                            else {},
                        )
                        generated = generate_image_fn(
                            str(draft).strip(),
                            change_request=artist_direction,
                            size=chosen_size,
                        )
                    if generated.get("ok"):
                        st.session_state[image_state_key] = generated["image_bytes"]
                        st.session_state[image_meta_key] = {
                            "source": "artist",
                            "source_text": str(draft).strip(),
                            "size": chosen_size,
                            "change_request": "",
                            "artist_direction": str(
                                artist_direction or ""
                            ).strip(),
                            "created_at": datetime.now(UTC).isoformat(),
                            "quality_passed": bool(
                                generated.get("quality_passed")
                            ),
                            "quality_review": str(
                                generated.get("quality_review") or ""
                            ),
                            "auto_redrawn": bool(
                                generated.get("auto_redrawn")
                            ),
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
            "Иллюстрацию можно создать у Художника или загрузить готовую. "
            "Оба варианта публикуются одинаково. "
            "Внутренняя публикация и Telegram-доставка "
            "по-прежнему считаются разными каналами."
        )

        # --------------------------------------------------------
        # Готовый ролик: Стагирит управляет процессом,
        # Мастер контента пишет анонс. После утверждения ролик можно
        # отправить и внутренней структуре, и во внешние Telegram-площадки.
        # --------------------------------------------------------
        current_video = st.session_state.get(video_state_key)
        video_meta = st.session_state.get(video_meta_key, {})
        if not isinstance(video_meta, dict):
            video_meta = {}

        if current_video:
            st.divider()
            st.markdown("### 🎬 Ролик для структуры и площадок")
            st.caption(
                "Стагирит здесь только управляет процессом. "
                "Анонс к ролику пишет Мастер контента. После утверждения ролик "
                "можно отправить партнёрам внутри Агентства и во внешние Telegram-площадки."
            )
            st.video(current_video)
            video_name = str(
                video_meta.get("file_name") or "agency_w_video.mp4"
            ).strip()
            video_size_mb = float(video_meta.get("size_mb") or 0)
            st.caption(f"{video_name} · {video_size_mb:.1f} МБ")

            director_video_note = st.text_area(
                "Что Мастеру контента важно знать о ролике — необязательно",
                placeholder=(
                    "Например: это короткий Reels об Агентстве W; "
                    "главный смысл — ИИ-команда возвращает человеку время."
                ),
                key=f"stagirite_video_note_{task_id}",
                height=90,
            )

            if st.button(
                "✍️ Мастер контента: написать анонс",
                key=f"stagirite_video_make_announcement_{task_id}",
                use_container_width=True,
                type="primary",
                disabled=(not callable(ask_claude_fn) and not callable(ask_openai_fn)),
            ):
                with st.spinner(
                    "Стагирит передал задачу Мастеру контента. Готовим анонс..."
                ):
                    announcement_pack = _generate_video_announcement(
                        ask_openai_fn,
                        ask_claude_fn,
                        owner_name=owner_name,
                        assignment=str(task.get("assignment") or ""),
                        current_text=str(draft or ""),
                        video_file_name=video_name,
                        director_note=str(director_video_note or ""),
                    )
                if announcement_pack.get("ok"):
                    announcement_text = str(
                        announcement_pack.get("text") or ""
                    ).strip()
                    updated = dict(result)
                    updated["video_announcement"] = announcement_text
                    updated["video_announcement_approved"] = False
                    updated["video_announcement_engine"] = str(
                        announcement_pack.get("engine") or ""
                    )
                    updated["video_announcement_model"] = str(
                        announcement_pack.get("model") or ""
                    )
                    updated.pop("video_announcement_approved_at", None)
                    updated.pop("video_structure_published_at", None)
                    updated.pop("video_structure_published_hash", None)
                    updated.pop("video_structure_published_count", None)
                    updated.pop("video_structure_media_id", None)
                    updated.pop("video_telegram_published_at", None)
                    updated.pop("video_telegram_published_hash", None)
                    updated.pop("video_telegram_published_count", None)
                    updated.pop("video_telegram_publish_results", None)
                    _update_task(owner_id, task_id, {"result": updated})
                    st.session_state[
                        f"stagirite_video_announcement_pending_{task_id}"
                    ] = announcement_text
                    st.rerun()
                else:
                    st.error(
                        "Мастер контента не смог подготовить анонс. "
                        + str(announcement_pack.get("error") or "Попробуйте ещё раз.")
                    )

            video_announcement_key = f"stagirite_video_announcement_{task_id}"
            pending_video_announcement = st.session_state.pop(
                f"stagirite_video_announcement_pending_{task_id}",
                None,
            )
            saved_video_announcement = str(
                result.get("video_announcement") or ""
            ).strip()
            if pending_video_announcement is not None:
                st.session_state[video_announcement_key] = str(
                    pending_video_announcement
                )
            elif video_announcement_key not in st.session_state:
                st.session_state[video_announcement_key] = saved_video_announcement

            video_announcement = st.text_area(
                "Анонс к ролику",
                key=video_announcement_key,
                height=180,
                placeholder=(
                    "Нажмите «Мастер контента: написать анонс» "
                    "или напишите свой текст."
                ),
            )

            announcement_approved = bool(
                result.get("video_announcement_approved")
            ) and bool(saved_video_announcement) and (
                str(video_announcement or "").strip() == saved_video_announcement
            )

            if bool(result.get("video_announcement_approved")) and not announcement_approved:
                st.warning(
                    "Анонс был изменён после утверждения. "
                    "Утвердите новую версию перед публикацией."
                )

            if st.button(
                "✅ Утвердить анонс к ролику",
                key=f"stagirite_video_approve_announcement_{task_id}",
                use_container_width=True,
                disabled=not str(video_announcement or "").strip(),
            ):
                clean_announcement = str(video_announcement or "").strip()
                updated = dict(result)
                updated["video_announcement"] = clean_announcement
                updated["video_announcement_approved"] = True
                updated["video_announcement_approved_at"] = datetime.now(UTC).isoformat()
                updated.pop("video_structure_published_at", None)
                updated.pop("video_structure_published_hash", None)
                updated.pop("video_structure_published_count", None)
                updated.pop("video_structure_media_id", None)
                updated.pop("video_telegram_published_at", None)
                updated.pop("video_telegram_published_hash", None)
                updated.pop("video_telegram_published_count", None)
                updated.pop("video_telegram_publish_results", None)
                _update_task(owner_id, task_id, {"result": updated})
                st.rerun()

            if announcement_approved:
                st.success("✅ Анонс к ролику утверждён владельцем.")

            clean_video_announcement = str(
                video_announcement or ""
            ).strip()

            # ----------------------------------------------------
            # Внутренняя структура Агентства W.
            # Ролик + анонс получают все нижестоящие партнёры.
            # ----------------------------------------------------
            video_structure_ids = [
                int(x) for x in structure_member_ids(owner_id)
                if int(x) != int(owner_id)
            ]
            video_structure_hash = _telegram_publication_hash(
                clean_video_announcement,
                None,
                video_structure_ids,
                video=current_video,
            ) if video_structure_ids else ""
            video_structure_already = bool(
                video_structure_hash
                and str(result.get("video_structure_published_hash") or "")
                == video_structure_hash
            )

            if not video_structure_ids:
                st.caption(
                    "Во внутренней структуре пока нет зарегистрированных получателей."
                )
            elif video_structure_already:
                st.success(
                    "📣 Ролик с анонсом уже размещён внутри Агентства: "
                    f"{int(result.get('video_structure_published_count') or 0)} партнёр(а/ов)."
                )
            elif st.button(
                (
                    "📣 Отправить ролик с анонсом всей структуре "
                    f"({len(video_structure_ids)})"
                ),
                key=f"stagirite_video_publish_structure_{task_id}",
                use_container_width=True,
                type="primary",
                disabled=(
                    publish_structure_video is None
                    or not announcement_approved
                ),
            ):
                try:
                    with st.spinner(
                        "Размещаем ролик с анонсом внутри Агентства W..."
                    ):
                        structure_result = publish_structure_video(
                            owner_id,
                            clean_video_announcement,
                            current_video,
                            subject="🎬 Ролик Агентства W",
                            mime_type=str(
                                video_meta.get("mime_type") or "video/mp4"
                            ),
                            file_name=video_name,
                        )
                    updated = dict(result)
                    updated["video_structure_published_at"] = datetime.now(UTC).isoformat()
                    updated["video_structure_published_hash"] = video_structure_hash
                    updated["video_structure_published_count"] = int(
                        structure_result.get("count") or 0
                    )
                    updated["video_structure_media_id"] = str(
                        structure_result.get("media_id") or ""
                    )
                    _update_task(owner_id, task_id, {"result": updated})
                    st.rerun()
                except Exception as exc:
                    st.error(
                        "Не удалось разместить ролик внутри структуры. "
                        f"{type(exc).__name__}: {exc}"
                    )

            st.caption(
                "Внутренняя структура Агентства и Telegram-площадки — два независимых канала: "
                "можно использовать любой из них или оба."
            )

            if not telegram_destinations:
                st.info(
                    "Сначала подключите Telegram-группы или каналы в разделе "
                    "«Команда → Мои площадки»."
                )
            else:
                video_dest_by_id = {
                    int(item.get("chat_id")): item
                    for item in telegram_destinations
                    if item.get("chat_id") is not None
                }
                all_video_destination_ids = list(video_dest_by_id.keys())
                selected_video_destination_ids = st.multiselect(
                    "Куда отправить ролик",
                    options=all_video_destination_ids,
                    default=all_video_destination_ids,
                    format_func=lambda chat_id: str(
                        video_dest_by_id.get(chat_id, {}).get("chat_title")
                        or "Telegram-площадка"
                    ),
                    key=f"stagirite_video_destinations_{task_id}",
                )

                selected_video_destination_ids = [
                    int(x) for x in selected_video_destination_ids
                ]
                video_tg_hash = _telegram_publication_hash(
                    clean_video_announcement,
                    None,
                    selected_video_destination_ids,
                    video=current_video,
                )
                published_count = int(
                    result.get("video_telegram_published_count") or 0
                )
                video_already_published = bool(
                    video_tg_hash
                    and str(result.get("video_telegram_published_hash") or "")
                    == video_tg_hash
                    and published_count > 0
                    and published_count == len(selected_video_destination_ids)
                )

                if video_already_published:
                    st.success(
                        "📡 Telegram уже принял эту версию ролика: "
                        f"{int(result.get('video_telegram_published_count') or 0)} "
                        "площадк(а/и)."
                    )
                elif st.button(
                    (
                        "📡 Отправить ролик с анонсом одним махом "
                        f"({len(selected_video_destination_ids)})"
                    ),
                    key=f"stagirite_video_publish_telegram_{task_id}",
                    use_container_width=True,
                    type="primary",
                    disabled=(
                        publish_to_publisher_destinations is None
                        or not announcement_approved
                        or not selected_video_destination_ids
                    ),
                ):
                    try:
                        with st.spinner(
                            "Publisher отправляет ролик в выбранные Telegram-площадки..."
                        ):
                            tg_result = publish_to_publisher_destinations(
                                owner_id,
                                clean_video_announcement,
                                video_bytes=current_video,
                                video_file_name=video_name,
                                video_mime_type=str(
                                    video_meta.get("mime_type") or "video/mp4"
                                ),
                                destination_ids=selected_video_destination_ids,
                            )
                        accepted = int(tg_result.get("accepted") or 0)
                        requested = int(
                            tg_result.get("requested")
                            or len(selected_video_destination_ids)
                        )
                        failed = int(tg_result.get("failed") or 0)
                        publish_results = tg_result.get("results") or []

                        # Ноль отправок НИКОГДА не считается публикацией.
                        # То же самое для частичной доставки: сначала показываем
                        # реальную ошибку, а не зелёный статус «Telegram принял».
                        if accepted <= 0 or failed > 0 or accepted < requested:
                            details = []
                            for item in publish_results:
                                if not isinstance(item, dict) or item.get("ok") is True:
                                    continue
                                title = str(
                                    item.get("title") or "Telegram-площадка"
                                ).strip()
                                error = str(
                                    item.get("error") or "неизвестная ошибка"
                                ).strip()
                                details.append(f"{title}: {error}")
                            detail_text = "; ".join(details[:3])
                            summary = (
                                f"Telegram доставил ролик в {accepted} из {requested} площадок."
                            )
                            if detail_text:
                                summary += f" Причина: {detail_text}"
                            raise RuntimeError(summary)

                        updated = dict(result)
                        updated["video_telegram_published_at"] = datetime.now(UTC).isoformat()
                        updated["video_telegram_published_hash"] = video_tg_hash
                        updated["video_telegram_published_count"] = accepted
                        updated["video_telegram_publish_results"] = publish_results
                        _update_task(owner_id, task_id, {"result": updated})
                        st.rerun()
                    except Exception as exc:
                        st.error(
                            "Не удалось отправить ролик в Telegram-площадки. "
                            f"{type(exc).__name__}: {exc}"
                        )

            st.caption(
                "Ролик хранится только в текущей сессии до публикации. "
                "Мастер контента получает текстовый контекст и ваше описание, "
                "сам видеофайл в ИИ не отправляется."
            )

    general_answer = str(
        result.get("general_answer")
        or ""
    ).strip()
    if general_answer:
        st.markdown("### 🧭 Решение Стагирита")
        st.write(general_answer)

    note = str(result.get("note") or "").strip()
    if note and not result.get("content_error"):
        status_now = str(task.get("status") or "")
        if status_now == "Нужно уточнение":
            st.info(note)
        elif status_now == "Нужен исполнительный модуль":
            st.warning(note)
        else:
            st.write(note)

    if result.get("content_error"):
        st.error("Не удалось подготовить материал.")
        with st.expander("🔧 Техническая причина"):
            st.code(
                str(result.get("content_error") or "")
            )

    if result.get("note"):
        st.info(str(result["note"]))

def render_stagirite_center(
    owner_telegram_id: int,
    owner_name: str,
    ask_openai_fn,
    ask_claude_fn=None,
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
                ask_claude_fn=ask_claude_fn,
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
    visible_tasks = [
        item for item in tasks
        if str(item.get("task_kind") or "") != DAILY_CANDIDATE_TASK_KIND
    ]

    st.markdown("### 📋 Текущее поручение")
    if not visible_tasks:
        st.info("Поручений пока нет.")
        return

    # Системная ежедневная пятёрка не занимает место пользовательского поручения.
    # Показываем только последнее обычное поручение полностью.
    task = visible_tasks[0]
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

        is_content_task = (
            "content" in task_kind
            or isinstance(task_result.get("content_master"), dict)
            or bool(task_result.get("content"))
        )

        if is_content_task:
            st.caption("Поручение Директора")
            if len(assignment_text) <= 260:
                st.write(assignment_text)
            else:
                st.write(assignment_text[:240].rstrip() + "…")
                with st.expander("Показать исходное поручение полностью"):
                    st.write(assignment_text)
        else:
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
            owner_name=owner_name,
            ask_openai_fn=ask_openai_fn,
            ask_claude_fn=ask_claude_fn,
            prepare_candidates_fn=prepare_candidates_fn,
            generate_image_fn=generate_image_fn,
        )

        if (
            "meetings" not in task_kind
            and not is_content_task
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

    previous = visible_tasks[1:]
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

