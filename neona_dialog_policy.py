from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests
import neona_telegram_dialogs as core
import neona_objections as objections
import neona_memory as memory


BUFFER_MINUTES = 60
MIN_LEAD_MINUTES = 60


def _slot_free_with_buffer(config, owner_id, start_utc, end_utc):
    """Свободный слот с окном 10:00–20:00 МСК, буфером и постоянной занятостью."""

    start_utc = start_utc.astimezone(core.UTC)
    end_utc = end_utc.astimezone(core.UTC)

    # Не назначаем встречу менее чем через час.
    if start_utc < datetime.now(core.UTC) + timedelta(minutes=MIN_LEAD_MINUTES):
        return False

    # Рабочее окно владельца: 10:00–20:00 МСК.
    start_msk = start_utc.astimezone(core.MSK)
    end_msk = end_utc.astimezone(core.MSK)
    day_start = datetime.combine(start_msk.date(), dt_time(10, 0), core.MSK)
    day_end = datetime.combine(start_msk.date(), dt_time(20, 0), core.MSK)

    if start_msk < day_start or end_msk > day_end:
        return False

    # Между встречами / постоянной занятостью оставляем минимум 1 час.
    expanded_start = start_utc - timedelta(minutes=BUFFER_MINUTES)
    expanded_end = end_utc + timedelta(minutes=BUFFER_MINUTES)

    # 1) Обычные встречи.
    if core._list_meetings(
        config,
        owner_id,
        expanded_start,
        expanded_end,
    ):
        return False

    # 2) Постоянная занятость владельца (agency_calendar_blocks).
    # Именно этого раньше не было в проверке Неоны.
    try:
        response = requests.get(
            f"{config.supabase_url}/rest/v1/agency_calendar_blocks",
            headers=core._headers(config),
            params={
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "active": "eq.true",
                "weekday": f"eq.{int(start_msk.weekday())}",
                "select": "weekday,start_time,end_time,active,title",
                "order": "start_time.asc",
            },
            timeout=20,
        )
        response.raise_for_status()
        blocks = response.json()
    except Exception as exc:
        # Безопаснее считать время занятым, чем создать двойную встречу.
        print(
            "NEONA_CALENDAR_BLOCKS_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return False

    if not isinstance(blocks, list):
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

        block_start_msk = datetime.combine(
            start_msk.date(),
            block_start_clock,
            core.MSK,
        )
        block_end_msk = datetime.combine(
            start_msk.date(),
            block_end_clock,
            core.MSK,
        )

        block_start_utc = block_start_msk.astimezone(core.UTC)
        block_end_utc = block_end_msk.astimezone(core.UTC)

        if expanded_start < block_end_utc and expanded_end > block_start_utc:
            return False

    return True


_INACTIVE_MEETING_STATUSES = {
    "отменена",
    "перенесена",
    "завершена",
    "не состоялась",
    "cancelled",
    "canceled",
    "rescheduled",
    "completed",
    "no_show",
    "deleted",
}


def _scheduled_meeting_still_active(config, owner_id, context) -> bool:
    """
    Проверяет, существует ли ещё встреча, на которую ссылается stage='scheduled'.

    Если запись удалена, отменена, перенесена или уже закончилась, старое состояние
    диалога больше не должно блокировать новую проверку календаря.
    При временной ошибке Supabase сохраняем старое состояние, чтобы случайно не
    потерять реально существующую встречу.
    """
    if not isinstance(context, dict):
        return False

    meeting_id = str(context.get("meeting_id") or "").strip()
    if not meeting_id:
        return False

    try:
        response = requests.get(
            f"{config.supabase_url}/rest/v1/agency_meetings",
            headers=core._headers(config),
            params={
                "id": f"eq.{meeting_id}",
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "select": "id,status,start_at,end_at",
                "limit": 1,
            },
            timeout=20,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception as exc:
        print(
            "NEONA_MEETING_STATE_CHECK_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        # Ошибка связи не должна сама по себе отменять реальную встречу.
        return True

    if not isinstance(rows, list) or not rows:
        return False

    meeting = rows[0] if isinstance(rows[0], dict) else {}
    status = str(meeting.get("status") or "").strip().casefold()
    if status in _INACTIVE_MEETING_STATUSES:
        return False

    end_raw = str(meeting.get("end_at") or "").strip()
    if end_raw:
        try:
            end_utc = datetime.fromisoformat(
                end_raw.replace("Z", "+00:00")
            ).astimezone(core.UTC)
            if end_utc <= datetime.now(core.UTC):
                return False
        except (TypeError, ValueError):
            # Не удаляем состояние только из-за старого/нестандартного формата даты.
            pass

    return True


def _clear_stale_meeting_context(context):
    """Удаляет только технические поля старой встречи, сохраняя живую память диалога."""
    cleaned = dict(context or {})
    for key in (
        "meeting_id",
        "proposed_start_at",
        "requested_date",
        "requested_time",
        "offered_slots",
        "contact_timezone",
        "contact_city",
        "meeting_format",
    ):
        cleaned.pop(key, None)
    cleaned["stale_meeting_cleared_at"] = datetime.now(core.UTC).isoformat()
    return cleaned



def _call_openai(config, instructions: str, text: str) -> str:
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

    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))

    answer = "\n".join(parts).strip()
    if not answer:
        raise core.DialogError("OpenAI не сформировал ответ.")
    return answer



def _owner_forms(owner_name: str) -> dict[str, str]:
    """Безопасные формы имени владельца для естественной русской речи."""
    raw = re.sub(r"\s+", " ", str(owner_name or "").strip())
    lowered = raw.casefold()

    known = {
        "valentina": ("Валентина", "Валентины", "Валентиной"),
        "валентина": ("Валентина", "Валентины", "Валентиной"),
    }
    if lowered in known:
        nominative, genitive, instrumental = known[lowered]
        return {
            "nominative": nominative,
            "genitive": genitive,
            "instrumental": instrumental,
            "meeting_person": nominative,
        }

    # Для простых русских женских имён можно безопасно образовать частые формы.
    if raw and re.fullmatch(r"[А-Яа-яЁё-]+", raw):
        if raw.endswith("а"):
            stem = raw[:-1]
            ending = "и" if stem.lower().endswith(("г", "к", "х", "ж", "ч", "ш", "щ")) else "ы"
            return {
                "nominative": raw,
                "genitive": stem + ending,
                "instrumental": stem + "ой",
                "meeting_person": raw,
            }
        if raw.endswith("я"):
            stem = raw[:-1]
            return {
                "nominative": raw,
                "genitive": stem + "и",
                "instrumental": stem + "ей",
                "meeting_person": raw,
            }
        return {
            "nominative": raw,
            "genitive": raw,
            "instrumental": raw,
            "meeting_person": raw,
        }

    # Если имя пришло латиницей и мы не уверены в склонении, лучше не коверкать его.
    return {
        "nominative": raw or "владелец аккаунта",
        "genitive": "владельца аккаунта",
        "instrumental": "владельцем аккаунта",
        "meeting_person": raw or "владелец аккаунта",
    }



_TRANSCRIPTION_ARTIFACT_PATTERNS = (
    r"\bредактор\s+субтитров\b",
    r"\bавтор\s+субтитров\b",
    r"\bпродолжение\s+следует\b",
    r"\bспасибо\s+за\s+просмотр\b",
    r"\bподписывайтесь\s+на\s+канал\b",
)


def _looks_like_transcription_artifact(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
    return bool(normalized) and any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in _TRANSCRIPTION_ARTIFACT_PATTERNS
    )


def _sanitize_relationship_memory(context):
    """Remove known speech-to-text hallucination artefacts from stored dialog memory."""
    if not isinstance(context, dict):
        return {}
    result = dict(context)
    key = getattr(memory, "MEMORY_KEY", "relationship_memory")
    mem = result.get(key)
    if not isinstance(mem, dict):
        return result
    mem = dict(mem)

    for list_key in ("confirmed_facts", "goals_or_needs", "questions", "preferences"):
        values = mem.get(list_key)
        if isinstance(values, list):
            mem[list_key] = [item for item in values if not _looks_like_transcription_artifact(str(item))]

    turns = mem.get("turns")
    if isinstance(turns, list):
        clean_turns = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            incoming = str(turn.get("incoming") or "")
            summary = str(turn.get("summary") or "")
            if _looks_like_transcription_artifact(incoming) or _looks_like_transcription_artifact(summary):
                continue
            clean_turns.append(turn)
        mem["turns"] = clean_turns

    for scalar_key in ("last_incoming", "last_summary"):
        if _looks_like_transcription_artifact(str(mem.get(scalar_key) or "")):
            mem[scalar_key] = ""

    result[key] = mem
    return result


def _relationship_memory(context):
    if not isinstance(context, dict):
        return {}
    value = context.get(getattr(memory, "MEMORY_KEY", "relationship_memory"))
    return dict(value) if isinstance(value, dict) else {}


def _dialog_context_block(context, *, max_turns: int = 8) -> str:
    """Короткая живая история для ответа в контексте, без выдумывания фактов."""
    mem = _relationship_memory(context)
    turns = mem.get("turns") if isinstance(mem.get("turns"), list) else []
    lines = []
    for turn in turns[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        incoming = re.sub(r"\s+", " ", str(turn.get("incoming") or "")).strip()
        reply = re.sub(r"\s+", " ", str(turn.get("neona_reply") or "")).strip()
        if incoming:
            lines.append(f"Человек: {incoming[:500]}")
        if reply:
            lines.append(f"Неона: {reply[:500]}")

    needs = mem.get("goals_or_needs") if isinstance(mem.get("goals_or_needs"), list) else []
    facts = mem.get("confirmed_facts") if isinstance(mem.get("confirmed_facts"), list) else []
    preferences = mem.get("preferences") if isinstance(mem.get("preferences"), list) else []

    extra = []
    if needs:
        extra.append("Явно названные цели/задачи человека: " + "; ".join(str(x) for x in needs[-5:]))
    if facts:
        extra.append("Подтверждённые самим человеком факты: " + "; ".join(str(x) for x in facts[-5:]))
    if preferences:
        extra.append("Предпочтения человека: " + "; ".join(str(x) for x in preferences[-4:]))

    if not lines and not extra:
        return "Предыдущий контекст пока не накоплен."
    return "\n".join([*lines, *extra])


def _has_personal_reason(context) -> bool:
    """Есть ли уже личная причина, связывающая встречу с пользой для человека."""
    if not isinstance(context, dict):
        return False
    if str(context.get("personal_reason") or "").strip():
        return True
    mem = _relationship_memory(context)
    needs = mem.get("goals_or_needs") if isinstance(mem.get("goals_or_needs"), list) else []
    return any(str(item or "").strip() for item in needs)


def _current_text_has_personal_reason(text: str) -> bool:
    """Только явные бытовые сигналы; не пытаемся угадывать мотив человека."""
    lowered = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    patterns = (
        r"\bмне\s+(?:нужно|надо|важно|хочется|необходимо)\b",
        r"\bя\s+(?:хочу|ищу|пытаюсь|занимаюсь|веду|развиваю)\b",
        r"\bу\s+меня\s+(?:нет|много|мало|есть)\b",
        r"\bне\s+хватает\s+(?:времени|людей|клиентов|партн[её]ров)\b",
        r"\b(?:устал|устала|сложно|трудно)\b",
        r"\b(?:клиент|партн[её]р|команд|переписк|рутин|времен|бизнес)\w*\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _self_contact_intent(text: str) -> bool:
    """Человек сам берёт контакт с владельцем на себя — Неона не давит дальше."""
    lowered = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    patterns = (
        r"\bя\s+сам(?:а)?\s+(?:ей|ему)?\s*(?:позвоню|напишу|свяжусь)\b",
        r"\bсам(?:а)?\s+(?:ей|ему)?\s*(?:позвоню|напишу|свяжусь)\b",
        r"\bя\s+(?:ей|ему)\s+(?:позвоню|напишу)\b",
        r"\bя\s+свяжусь\s+(?:с\s+ней|с\s+ним|сам(?:а)?)\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _self_contact_reply(owner_name: str) -> str:
    forms = _owner_forms(owner_name)
    return (
        "Хорошо, договорились. Тогда оставлю это вам 🙂 "
        f"Если понадобится помочь согласовать время с {forms['instrumental']} — я рядом."
    )



_SCHEDULING_STAGES = {
    "invited_to_meeting",
    "collecting_meeting_details",
    "awaiting_confirmation",
    "awaiting_slot_choice",
    "scheduled",
}


def _meeting_cancel_intent(text: str) -> bool:
    """Явная отмена — не приглашение начать согласование заново."""
    value = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    patterns = (
        r"\bотмен(?:яем|яю|ить|ите|ена|а)\s+(?:эту\s+)?встреч\w*\b",
        r"\bвстреч\w*\s+отмен(?:яем|яю|ить|ите|ена|а)\b",
        r"\bне\s+(?:надо|нужно|хочу)\s+(?:эту\s+)?встреч\w*\b",
        r"\bвстреч\w*\s+не\s+(?:надо|нужна|нужно)\b",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def _meeting_reschedule_intent(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return any(
        token in value
        for token in (
            "перенести", "перенесем", "перенесём", "перенос",
            "другое время", "другой день", "другую дату",
            "на другой день", "на другое время",
        )
    )


def _meeting_cannot_attend_intent(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return bool(re.search(r"\b(?:не\s+смогу|не\s+могу\s+(?:прийти|быть|участвовать)|не\s+получится)\b", value))


def _short_negative(text: str) -> bool:
    value = re.sub(r"[^a-zа-яё0-9]+", " ", str(text or "").casefold()).strip()
    return value in {
        "нет", "не", "не надо", "не нужно", "не хочу", "не стоит",
        "нет спасибо", "не сейчас", "не надо спасибо",
    }


def _clear_meeting_context(context):
    result = dict(context or {})
    for key in (
        "meeting_id", "proposed_start_at", "requested_date", "requested_time",
        "offered_slots", "contact_timezone", "meeting_format", "meeting_reason",
    ):
        result.pop(key, None)
    return result


def _cancel_meeting_record(config, owner_id, context) -> None:
    """Best effort: если встреча уже записана, помечаем её отменённой."""
    meeting_id = str((context or {}).get("meeting_id") or "").strip()
    if not meeting_id:
        return
    try:
        response = requests.patch(
            f"{config.supabase_url}/rest/v1/agency_meetings",
            headers=core._headers(config, "return=minimal"),
            params={
                "id": f"eq.{meeting_id}",
                "owner_telegram_id": f"eq.{int(owner_id)}",
            },
            json={"status": "Отменена"},
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            "NEONA_MEETING_CANCEL_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def _warmup_business_signal(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return any(
        re.search(pattern, value)
        for pattern in (
            r"\bагентств\w*\b", r"\bии\b", r"\bискусственн\w+\s+интеллект\w*\b",
            r"\bассистент\w*\b", r"\bбизнес\w*\b", r"\bпартн[её]р\w*\b",
            r"\bклиент\w*\b", r"\bавтоматизац\w*\b",
        )
    )


def _meeting_allowed_now(context, text: str) -> bool:
    """После Story не прыгаем к встрече после первой вежливой реплики."""
    base = _has_personal_reason(context) or _current_text_has_personal_reason(text)
    if not base:
        return False
    if bool((context or {}).get("warmup_mode")):
        try:
            turns = int((context or {}).get("warmup_incoming_count") or 0)
        except (TypeError, ValueError):
            turns = 0
        if turns < 2 and not _warmup_business_signal(text):
            return False
    return True


def _ack_without_question(text: str) -> str:
    value = re.sub(r"[^a-zа-яё0-9]+", " ", str(text or "").casefold()).strip()
    if value in {"спасибо", "благодарю", "спасибо большое"}:
        return "Пожалуйста."
    if _short_negative(text):
        return "Поняла."
    if value in {"да", "хорошо", "ок", "okay", "договорились"}:
        return "Хорошо."
    return "Поняла вас."


def _question_signature(text: str) -> set[str]:
    stop = {
        "как", "какой", "какая", "какие", "какому", "каким", "какую",
        "что", "где", "когда", "зачем", "почему", "ли", "вам", "вас",
        "вы", "у", "в", "во", "на", "по", "для", "и", "или", "а", "это",
        "указано", "указать", "скажите", "подскажите", "пожалуйста",
    }
    result = set()
    for token in re.findall(r"[a-zа-яё0-9]+", str(text or "").casefold()):
        if token in stop or len(token) <= 2:
            continue
        # Грубый стем достаточен для защиты от «какой часовой пояс»/«по какому часовому поясу».
        result.add(token[:5] if len(token) >= 5 else token)
    return result


def _recent_neona_questions(context, *, max_turns: int = 8) -> list[str]:
    mem = _relationship_memory(context)
    turns = mem.get("turns") if isinstance(mem.get("turns"), list) else []
    questions: list[str] = []
    for turn in turns[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        reply = str(turn.get("neona_reply") or "")
        for part in re.findall(r"[^?]+\?", reply):
            value = re.sub(r"\s+", " ", part).strip()
            if value:
                questions.append(value)
    return questions


def _same_question(a: str, b: str) -> bool:
    na = _normalize_for_similarity(a)
    nb = _normalize_for_similarity(b)
    if not na or not nb:
        return False
    if na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.68:
        return True
    sa, sb = _question_signature(a), _question_signature(b)
    if not sa or not sb:
        return False
    overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
    return overlap >= 0.72


def _strip_repeated_questions(reply: str, context) -> str:
    previous_questions = _recent_neona_questions(context, max_turns=8)
    if not previous_questions or "?" not in str(reply or ""):
        return str(reply or "").strip()
    parts = re.findall(r"[^.!?]+[.!?]?", str(reply or ""))
    kept: list[str] = []
    for part in parts:
        clean = re.sub(r"\s+", " ", part).strip()
        if not clean:
            continue
        if clean.endswith("?") and any(_same_question(clean, old) for old in previous_questions):
            continue
        kept.append(clean)
    return " ".join(kept).strip()


_RESOURCE_PROMISE_RE = re.compile(
    r"\b(?:пришлю|отправлю|подготовлю|составлю|сделаю|могу\s+прислать|могу\s+отправить|могу\s+подготовить)\b"
    r"[^.!?]{0,120}\b(?:руководств\w*|гайд\w*|чек[ -]?лист\w*|pdf|ссылк\w*|инструкц\w*|файл\w*|шаблон\w*|таблиц\w*|материал\w*)\b",
    re.IGNORECASE,
)


def _strip_phantom_resource_promises(reply: str) -> str:
    value = str(reply or "").strip()
    if not _RESOURCE_PROMISE_RE.search(value):
        return value
    parts = re.findall(r"[^.!?]+[.!?]?", value)
    kept = [
        re.sub(r"\s+", " ", part).strip()
        for part in parts
        if part.strip() and not _RESOURCE_PROMISE_RE.search(part)
    ]
    return " ".join(part for part in kept if part).strip()


def _normalize_for_similarity(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", str(text or "").casefold()).strip()


def _reply_is_repetitive(reply: str, context) -> bool:
    mem = _relationship_memory(context)
    previous = str(mem.get("last_reply") or "").strip()
    if not previous or not reply:
        return False
    a = _normalize_for_similarity(previous)
    b = _normalize_for_similarity(reply)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.70


def _de_repeat_reply(config, reply: str, text: str, context) -> str:
    """Жёсткая страховка: Неона не повторяет вопросы и не обещает несуществующие материалы."""
    cleaned = _strip_phantom_resource_promises(reply)
    cleaned = _strip_repeated_questions(cleaned, context)
    if not cleaned:
        return _ack_without_question(text)

    if not _reply_is_repetitive(cleaned, context):
        return cleaned

    mem = _relationship_memory(context)
    previous = str(mem.get("last_reply") or "").strip()
    history = _dialog_context_block(context, max_turns=8)
    instructions = f"""
Ты Неона. Предыдущий ответ уже был: «{previous}».
Новый ответ получился слишком похожим. Перепиши его ОДИН раз.

ЖЁСТКО:
- НЕ повторяй прежний вопрос и НЕ перефразируй его;
- не задавай вопрос, который уже задавался в последних репликах;
- сначала ответь на последнюю реплику человека по смыслу;
- если человек уже отказался, отменил встречу или ответ уже известен — не выясняй это снова;
- не предлагай и не обещай PDF, ссылки, руководства, файлы, шаблоны или материалы, которых система реально не имеет;
- вопрос НЕ обязателен: если следующий вопрос не нужен, закончи точкой;
- 1–3 коротких предложения.

Контекст:
{history}
""".strip()
    rewritten = _call_openai(config, instructions, text)
    rewritten = _strip_phantom_resource_promises(rewritten)
    rewritten = _strip_repeated_questions(rewritten, context)
    if not rewritten or _reply_is_repetitive(rewritten, context):
        return _ack_without_question(text)
    return rewritten

def _general_reply(config, owner_name, first_name, text, greet, context=None):
    greeting_rule = (
        f"Начни с «{core._greeting(first_name)}»"
        if greet
        else "Не повторяй приветствие: диалог уже начат."
    )

    agency_core = str(getattr(core, "NEONA_DIALOG_CORE", "") or "").strip()
    forms = _owner_forms(owner_name)
    history = _dialog_context_block(context, max_turns=8)
    personal_reason_known = _has_personal_reason(context)
    personal_reason_now = _current_text_has_personal_reason(text)
    meeting_permission = _meeting_allowed_now(context, text)
    voice_unverified = bool((context or {}).get("incoming_voice_unverified"))
    voice_rule = (
        "Последняя реплика получена через автоматическую расшифровку голосового. "
        "Используй её для ответа, но НЕ объявляй имена, фамилии, цифры, названия или биографические сведения подтверждёнными фактами. "
        "Если такой фрагмент выглядит неожиданно или важен для дальнейшего разговора — мягко переспроси."
        if voice_unverified
        else "Последняя реплика не требует специальной оговорки о голосовой расшифровке."
    )

    meeting_rule = (
        "Личная причина уже проявилась. Ты МОЖЕШЬ очень мягко связать её с пользой Агентства W и, "
        "только если это действительно естественно именно сейчас, предложить знакомство/встречу с владельцем аккаунта."
        if meeting_permission
        else
        "Личная причина для встречи ЕЩЁ НЕ выявлена. СЕЙЧАС НЕ ПРЕДЛАГАЙ встречу и не спрашивай дату/время. "
        "Сначала поддержи тему и одним естественным вопросом узнай человека чуть лучше."
    )

    instructions = f"""
{agency_core}

Ты Неона — секретарь-референт {forms['genitive']} в Агентстве W.
Пиши по-русски естественно, тепло и по-человечески. Обычно 1–3 коротких предложения.

ЖИВОЙ КОНТЕКСТ ПОСЛЕДНИХ РЕПЛИК:
{history}

КРИТИЧЕСКОЕ ПРАВИЛО КОНТЕКСТА:
{voice_rule}
- прежде чем отвечать, восстанови, о чём идёт разговор;
- местоимения и короткие ответы («ответ», «развёрнутый», «да», «это») трактуй через предыдущие реплики;
- если ты сама только что задала загадку/вопрос, а человек просит ответ, ОТВЕТЬ, а не проси повторить загадку;
- если вопрос уже понятен, не задавай уточнение ради уточнения;
- никогда не проси человека повторить то, что уже есть в видимом контексте;
- не повторяй один и тот же вопрос двумя сообщениями подряд;
- вопрос НЕ обязан быть в каждом сообщении: если вопрос не нужен, закончи ответ точкой;
- нельзя возвращаться к уже закрытому вопросу (часовой пояс, время, формат, материал) только потому, что он есть в сценарии.

{objections.NEONA_OBJECTION_RULES_TEXT}

ТВОЯ ЛИНИЯ — НЕЗАМЕТНАЯ, НО ОСМЫСЛЕННАЯ:
Сначала поддержи реальную тему разговора — шутку, загадку, работу, путешествия, бизнес или любой другой предмет.
Затем постепенно узнавай человека: чем он занят, что ему важно, что отнимает время/силы, чего он хочет добиться.
Только ПОСЛЕ того, как обнаружена личная причина, показывай подходящую пользу Агентства W.
Встреча с владельцем аккаунта — дальняя цель, а не обязательный ответ на каждую реплику.
ИСКЛЮЧЕНИЕ: прямой вопрос о цене, стоимости, платности, оплате, тарифе или точных условиях — это уже конкретный интерес.
В таком случае НЕ выясняй новую личную причину, НЕ предлагай ничего от себя и сразу веди к осознанной встрече с владельцем аккаунта.
{meeting_rule}

ЖЁСТКАЯ ГРАНИЦА ПОЛНОМОЧИЙ:
- не предлагай от себя персональные сообщения, планы действий, сводки, тексты, шаблоны, таблицы, рассылки, инструкции или стратегии;
- не говори «могу подготовить», «могу составить», «могу сделать», «могу прислать» про такие материалы;
- конкретное решение предлагает только владелец аккаунта;
- единственное самостоятельное предложение Неоны — помочь договориться о встрече с владельцем аккаунта.

ЕСЛИ СПРАШИВАЮТ ОБ АГЕНТСТВЕ W:
- сначала обязательно ответь по существу;
- объясняй не техническим списком, а через пользу для человека;
- можно сказать, что Агентство W — это команда ИИ-помощников для бизнеса: помогает находить подходящих людей,
  поддерживать диалоги и договорённости, организовывать встречи, сопровождать новичков и снимать часть рутины;
- главная человеческая ценность: вернуть владельцу бизнеса время, при этом решения и контроль остаются у человека;
- после объяснения задай один вопрос, который поможет понять, какая из этих польз актуальна именно этому собеседнику;
- не уводи сразу к календарю.

ЕСЛИ ЧЕЛОВЕК ГОВОРИТ, ЧТО САМ СВЯЖЕТСЯ С {forms['instrumental']}:
уважь это. Не собирай дату, время, часовой пояс и формат встречи.

ЕСЛИ ЧЕЛОВЕК ЗАДАЁТ ВОПРОС:
- сначала ответь на сам вопрос;
- ВАЖНО: цена, стоимость, платность, оплата, тариф и точные условия — не обычный вопрос, а прямой мост к владельцу аккаунта;
- на такой вопрос не придумывай цифры и не предлагай альтернативные материалы: сразу предложи договориться о встрече с владельцем;
- остальные вопросы, требующие личного решения, точных условий или полномочий владельца, можно перенести к владельцу;
- не используй «лучше обсудить с владельцем» как способ уйти от обычного вопроса;
- не выдумывай цены, доходы, гарантии, условия проектов и факты, которых нет.

НЕ РАБОТАЙ КАК АНКЕТА:
- один вопрос за раз;
- не перечисляй функции без необходимости;
- не спрашивай одновременно дату, время, часовой пояс и формат;
- следующая реплика должна рождаться из ответа человека, а не из сценария.

ГОВОРИ ПО-ЧЕЛОВЕЧЕСКИ:
- обычные слова вместо маркетингового жаргона;
- допускается лёгкий естественный юмор;
- не называй человека «лидом», «кандидатом» или «целевой аудиторией»;
- не называй ИИ ботом или чат-ботом.

{greeting_rule}
Верни только готовую реплику человеку.
""".strip()

    reply = _call_openai(config, instructions, text)
    return _de_repeat_reply(config, reply, text, context)

_COMMERCIAL_INTENT_PATTERNS = (
    r"\bплатн\w*\b",
    r"\bбесплатн\w*\b",
    r"\bцен(?:а|ы|е|у|ой|ами|ах)?\b",
    r"\bстоимост\w*\b",
    r"\bсколько\s+(?:это\s+)?(?:стоит|будет\s+стоить)\b",
    r"\bпоч[её]м\b",
    r"\bтариф\w*\b",
    r"\bоплат\w*\b",
    r"\bплат[её]ж\w*\b",
    r"\bуслови(?:е|я|й|ям|ях)\b",
)


def _commercial_intent(text: str) -> bool:
    """Цена/оплата/условия — это прямой сигнал вести к Директору, а не фантазировать."""
    normalized = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return bool(normalized) and any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in _COMMERCIAL_INTENT_PATTERNS
    )


def _commercial_meeting_reply(owner_name: str, first_name: str, greet: bool, stage: str) -> str:
    forms = _owner_forms(owner_name)
    prefix = f"{core._greeting(first_name)} " if greet else ""

    if stage == "scheduled":
        return (
            f"{prefix}По стоимости и точным условиям я не буду придумывать — "
            f"это лучше обсудить с {forms['instrumental']} на уже назначенной встрече."
        ).strip()

    if stage in {"collecting_meeting_details", "awaiting_confirmation", "awaiting_slot_choice"}:
        return (
            f"{prefix}По стоимости и точным условиям я не буду придумывать — "
            f"это вопрос к {forms['genitive']}. Мы уже договариваемся о встрече, "
            "и там вы получите точный ответ."
        ).strip()

    return (
        f"{prefix}По стоимости и точным условиям я не буду придумывать — "
        f"это вопрос к {forms['genitive']}. Хотите, я помогу договориться "
        f"о короткой встрече с {forms['instrumental']}?"
    ).strip()


def _respectful_stop_reply() -> str:
    return "Поняла. Спасибо, что сказали. Больше писать вам не буду. Всего доброго."


def _repeat_objection_close() -> str:
    return (
        "Поняла вас. Не буду уговаривать или возвращаться к этому вопросу. "
        "Спасибо за откровенный ответ."
    )


def _objection_reply(config, owner_name, first_name, text, category, greet):
    greeting_rule = (
        f"Начни с «{core._greeting(first_name)}»"
        if greet
        else "Не повторяй приветствие: диалог уже начат."
    )
    agency_core = str(getattr(core, "NEONA_DIALOG_CORE", "") or "").strip()
    competitor_block = (
        objections.competitor_prompt_block(text)
        if category == "competitor"
        else ""
    )
    instructions = f"""
{agency_core}

Ты Неона — секретарь-референт {owner_name}.
Человек высказал СОМНЕНИЕ или ВОЗРАЖЕНИЕ. Это первая содержательная попытка его прояснить.

{objections.objection_prompt_block(category)}

{competitor_block}

КАК ОТВЕТИТЬ СЕЙЧАС:
- сначала коротко покажи, что услышала человека;
- не спорь и не говори «вы неправы»;
- используй максимум ОДИН проверенный факт или смысл Агентства W;
- не перечисляй функции;
- не приглашай на встречу механически;
- задай максимум ОДИН маленький вопрос, который помогает понять сомнение или увидеть релевантную пользу;
- если точного факта нет, скажи, что лучше уточнить у {owner_name}, и не выдумывай;
- 1–3 коротких предложения;
- {greeting_rule}

Верни только готовый ответ человеку.
""".strip()
    return _call_openai(config, instructions, text)


def _schedule_data_present(text, message_dt, context):
    if core._meeting_intent(text):
        return True
    if core._detect_time(text):
        return True
    if core._detect_format(text):
        return True
    if core._detect_timezone(text):
        return True
    if core._detect_date(text, message_dt, context.get("contact_timezone")):
        return True
    if core._is_yes(text) or core._is_no(text):
        return True
    if re.search(r"^\s*[12]\s*$", text):
        return True
    return False


def _simple_ack(text):
    normalized = re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()
    return normalized in {
        "понятно", "ясно", "хорошо", "ладно", "ок", "okay",
        "спасибо", "благодарю", "договорились",
    }


def _substantive_detour(text, message_dt, context):
    if _schedule_data_present(text, message_dt, context):
        return False
    if _simple_ack(text):
        return False

    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return False

    question_words = (
        "что", "как", "почему", "зачем", "сколько", "какой", "какая",
        "какие", "где", "когда", "можно ли", "а если", "расскажите",
        "объясните", "подробнее",
    )
    lowered = normalized.lower()

    return (
        "?" in normalized
        or any(lowered.startswith(word) for word in question_words)
        or len(normalized.split()) >= 5
    )


def _meeting_bridge(config, owner_name, first_name, text, stage, context, greet):
    """На этапе встречи сначала сохраняет нормальный разговор, а не анкету."""
    forms = _owner_forms(owner_name)
    history = _dialog_context_block(context, max_turns=8)
    prefix_rule = (
        f"Можно начать с «{core._greeting(first_name)}»."
        if greet
        else "Не повторяй приветствие."
    )

    if stage == "awaiting_confirmation" and context.get("proposed_start_at"):
        return_target = "Если уместно, после ответа напомни только о подтверждении уже предложенного времени."
    elif stage == "awaiting_slot_choice":
        return_target = "Если уместно, после ответа напомни только о выборе между уже предложенными вариантами."
    else:
        return_target = (
            "Не возвращай человека к встрече механически. Если его новая реплика ушла в другую содержательную тему, "
            "сначала полноценно поддержи эту тему. К встрече вернись только когда это снова естественно."
        )

    instructions = f"""
Ты Неона — секретарь-референт {forms['genitive']}.
Разговор ранее дошёл до темы встречи, но человек сейчас написал содержательную реплику.

Живой контекст:
{history}

Правила:
- сначала ответь именно на текущую реплику человека;
- не повторяй «назовите день и время», если человек уже отвечал или сменил тему;
- если вопрос понятен из контекста, не проси повторить его;
- если человек сказал, что сам свяжется с {forms['instrumental']}, уважай это и больше не собирай данные встречи;
- не задавай несколько организационных вопросов в одном сообщении;
- {return_target}
- ничего не выдумывай;
- 1–3 коротких предложения;
- {prefix_rule}

Верни только готовую реплику человеку.
""".strip()
    reply = _call_openai(config, instructions, text)
    return _de_repeat_reply(config, reply, text, context)

def _after_scheduled_reply(config, owner_name, first_name, text):
    forms = _owner_forms(owner_name)
    instructions = f"""
Ты Неона — секретарь-референт {forms['genitive']}.
Встреча с человеком УЖЕ назначена.

Ответь на его вопрос кратко и по существу.
Если вопрос действительно требует личного решения владельца, скажи, что его можно обсудить с {forms['instrumental']} на встрече.
Не приглашай на новую встречу и не начинай согласование времени заново.
Если формат встречи — WhatsApp, Telegram или Zoom, это только формат встречи.
Не обещай «отправить подтверждение» в WhatsApp/Telegram/Zoom, если система реально этого не делает.
Не выдумывай факты.
1–2 коротких предложения.
""".strip()
    return _call_openai(config, instructions, text)

def _process_message_without_memory(
    config,
    owner_id,
    owner_name,
    contact_id,
    first_name,
    username,
    text,
    message_dt,
    state,
):
    stage = str(state.get("stage") or "idle")
    greeted = bool(state.get("greeted", False))
    context = (
        state.get("context")
        if isinstance(state.get("context"), dict)
        else {}
    )
    greet = not greeted

    if bool(context.get("warmup_mode")):
        try:
            context["warmup_incoming_count"] = int(context.get("warmup_incoming_count") or 0) + 1
        except (TypeError, ValueError):
            context["warmup_incoming_count"] = 1
        if _warmup_business_signal(text):
            context["warmup_business_signal"] = True

    # stage="scheduled" нельзя считать истиной без проверки самой записи календаря.
    # Если встречу удалили/отменили/перенесли или она уже закончилась,
    # снимаем старый статус и обрабатываем текущее сообщение заново.
    if stage == "scheduled" and not _scheduled_meeting_still_active(
        config,
        owner_id,
        context,
    ):
        context = _clear_stale_meeting_context(context)
        stage = "idle"

    if stage in _SCHEDULING_STAGES and _meeting_cancel_intent(text):
        if stage == "scheduled":
            _cancel_meeting_record(config, owner_id, context)
        context = _clear_meeting_context(context)
        context["meeting_cancelled_by_contact"] = True
        context["meeting_cancelled_at"] = datetime.now(core.UTC).isoformat()
        return "Поняла, встречу отменяю. Спасибо, что предупредили.", "idle", True, context

    if stage in _SCHEDULING_STAGES and _short_negative(text):
        if stage == "scheduled":
            _cancel_meeting_record(config, owner_id, context)
        context = _clear_meeting_context(context)
        context["meeting_declined_by_contact"] = True
        context["meeting_declined_at"] = datetime.now(core.UTC).isoformat()
        return "Поняла. Тогда встречу не назначаем.", "idle", True, context

    if stage == "idle" and (context.get("meeting_cancelled_by_contact") or context.get("meeting_declined_by_contact")) and _short_negative(text):
        # После уже закрытого вопроса короткое «нет» не запускает сценарий заново.
        return "Поняла.", "idle", True, context

    classification = objections.classify_neona_reply(text)

    # Явная просьба прекратить контакт важнее любой стадии диалога.
    if classification.get("kind") == "hard_stop":
        context["contact_boundary"] = "do_not_contact"
        context["contact_boundary_at"] = datetime.now(core.UTC).isoformat()
        context["last_objection_category"] = "hard_stop"
        return _respectful_stop_reply(), "opted_out", True, context

    # Если после прежнего явного отказа человек сам снова написал, это новый входящий
    # контакт. Неона может ответить, но не делает никаких исходящих напоминаний сама.
    if stage == "opted_out":
        context.pop("contact_boundary", None)
        context["contact_reinitiated_at"] = datetime.now(core.UTC).isoformat()
        stage = "idle"

    # Человек сам берёт связь с владельцем на себя. Это не повод продолжать
    # собирать дату/время — наоборот, уважительно отпускаем инициативу человеку.
    if _self_contact_intent(text):
        for key in (
            "proposed_start_at", "requested_date", "requested_time",
            "offered_slots", "contact_timezone", "meeting_format",
        ):
            context.pop(key, None)
        context["meeting_deferred_by_contact"] = True
        context["meeting_deferred_at"] = datetime.now(core.UTC).isoformat()
        return _self_contact_reply(owner_name), "idle", True, context

    # Цена / платность / оплата / тариф / точные условия — жёсткий коммерческий триггер.
    # Здесь Неона не имеет права импровизировать или предлагать свои «услуги».
    # Единственный следующий шаг — осознанная встреча с Директором.
    if _commercial_intent(text):
        context["meeting_reason"] = "price_or_terms"
        context["commercial_interest_at"] = datetime.now(core.UTC).isoformat()
        reply = _commercial_meeting_reply(owner_name, first_name, greet, stage)

        if stage == "scheduled":
            return reply, "scheduled", True, context
        if stage in {"collecting_meeting_details", "awaiting_confirmation", "awaiting_slot_choice"}:
            return reply, stage, True, context
        return reply, "invited_to_meeting", True, context

    # Мягкое возражение не считаем окончательным отказом. Его можно содержательно
    # отработать один раз. Повтор того же сомнения — уважительное завершение.
    if stage == "idle" and classification.get("kind") == "objection":
        category = str(classification.get("category") or "other")
        counts = context.get("objection_counts")
        counts = dict(counts) if isinstance(counts, dict) else {}
        previous = int(counts.get(category) or 0)
        last_category = str(context.get("last_objection_category") or "")
        counts[category] = previous + 1
        context["objection_counts"] = counts

        if last_category == category and previous >= 1:
            context["last_objection_category"] = category
            context["soft_objection_closed"] = category
            return _repeat_objection_close(), stage, True, context

        context["last_objection_category"] = category
        context.pop("soft_objection_closed", None)

        return (
            _objection_reply(
                config,
                owner_name,
                first_name,
                text,
                category,
                greet,
            ),
            stage,
            True,
            context,
        )

    # Новый содержательный ответ после возражения означает, что разговор снова движется.
    if classification.get("kind") in {"interest", "question", "other"}:
        context.pop("last_objection_category", None)
        context.pop("soft_objection_closed", None)

    # Уже назначенная встреча.
    if stage == "scheduled":
        lowered = text.lower()

        if core._is_simple_acknowledgement(text):
            return _ack_without_question(text), "scheduled", True, context

        if _meeting_cannot_attend_intent(text):
            _cancel_meeting_record(config, owner_id, context)
            context = _clear_meeting_context(context)
            context["meeting_cancelled_by_contact"] = True
            context["meeting_cancelled_at"] = datetime.now(core.UTC).isoformat()
            return (
                "Поняла. Встречу на это время снимаю. Если захотите подобрать другое время — напишите.",
                "idle",
                True,
                context,
            )

        if _meeting_reschedule_intent(text):
            for key in ("proposed_start_at", "requested_date", "requested_time", "offered_slots"):
                context.pop(key, None)
            return (
                "Хорошо. Напишите новый удобный день и время. Часовой пояс и формат повторять не нужно, если они не меняются.",
                "collecting_meeting_details",
                True,
                context,
            )

        return (
            _after_scheduled_reply(
                config,
                owner_name,
                first_name,
                text,
            ),
            "scheduled",
            True,
            context,
        )

    scheduling_stage = stage in {
        "invited_to_meeting",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "awaiting_slot_choice",
    }

    # Интерес + содержательный вопрос: сначала отвечаем на вопрос и узнаём человека.
    # Встречу не подсовываем раньше личной причины.
    if (
        stage == "idle"
        and core._is_positive_interest(text)
        and _substantive_detour(text, message_dt, context)
    ):
        context = core._update_context_from_message(context, text, message_dt)
        reply = _general_reply(config, owner_name, first_name, text, greet, context)
        return reply, "idle", True, context

    # Короткое «да, интересно» ведёт к встрече только если уже понятна личная причина.
    # Иначе Неона продолжает живой разговор и выясняет, что человеку действительно нужно.
    if stage == "idle" and core._is_positive_interest(text):
        if _meeting_allowed_now(context, text):
            reply, new_stage, context = core._schedule_reply(
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
            return reply, new_stage, True, context
        reply = _general_reply(config, owner_name, first_name, text, greet, context)
        return reply, "idle", True, context

    # На этапе встречи содержательная новая тема важнее календарной анкеты.
    if scheduling_stage and _substantive_detour(text, message_dt, context):
        return (
            _meeting_bridge(config, owner_name, first_name, text, stage, context, greet),
            stage,
            True,
            context,
        )

    # Явные данные/намерение встречи продолжают календарный сценарий.
    if core._meeting_intent(text) or (
        scheduling_stage and _schedule_data_present(text, message_dt, context)
    ):
        reply, new_stage, context = core._schedule_reply(
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
        return reply, new_stage, True, context

    # Если мы технически остались в стадии встречи, но человек пишет обычную реплику,
    # не тащим его обратно к календарю. Поддерживаем разговор и ждём естественного момента.
    if scheduling_stage:
        reply = _general_reply(config, owner_name, first_name, text, greet, context)
        return reply, stage, True, context

    reply = _general_reply(config, owner_name, first_name, text, greet, context)
    meeting_allowed = _meeting_allowed_now(context, text)
    new_stage = "invited_to_meeting" if meeting_allowed and core._meeting_intent(reply) else stage
    return reply, new_stage, True, context


def _process_message(
    config,
    owner_id,
    owner_name,
    contact_id,
    first_name,
    username,
    text,
    message_dt,
    state,
):
    """Сохраняет рабочую политику Неоны и добавляет безопасную живую память."""
    state = dict(state or {})
    context_before = (
        dict(state.get("context"))
        if isinstance(state.get("context"), dict)
        else {}
    )
    context_before = _sanitize_relationship_memory(context_before)

    # ЖЁСТКИЙ FENCE: Telegram Story-handoff, созданный старой версией, мог
    # принести в новый диалог relationship_memory из переписки ДО первого
    # сообщения Неоны. Такой контекст запрещён. Очищаем его один раз и дальше
    # накапливаем память только из новых входящих после точки активации.
    if (
        str(context_before.get("activated_by") or "") == "telegram_story_reply"
        and not bool(context_before.get("post_activation_memory_started"))
    ):
        context_before.pop(getattr(memory, "MEMORY_KEY", "relationship_memory"), None)
        # Старая версия могла успеть увести контакт в календарную стадию из-за
        # чужого старого контекста. При первом сообщении после обновления также
        # сбрасываем этот ошибочный stage и его технические поля.
        context_before = _clear_meeting_context(context_before)
        context_before["activated_by"] = "telegram_story_reply"
        context_before["pre_activation_history_forbidden"] = True
        context_before["history_fence_enforced_at"] = datetime.now(core.UTC).isoformat()
        state["stage"] = "idle"
        state["greeted"] = True

    state["context"] = context_before

    previous_stage = str(state.get("stage") or "idle")
    voice_unverified = bool(context_before.get("incoming_voice_unverified"))
    memory_before = _relationship_memory(context_before)
    confirmed_before = list(memory_before.get("confirmed_facts") or []) if isinstance(memory_before.get("confirmed_facts"), list) else []

    reply, new_stage, greeted, context = _process_message_without_memory(
        config,
        owner_id,
        owner_name,
        contact_id,
        first_name,
        username,
        text,
        message_dt,
        state,
    )

    # Память не имеет права сорвать живой диалог. Для непроверенной расшифровки
    # разрешаем помнить ход разговора, но запрещаем превращать услышанное в подтверждённые факты.
    try:
        classification = objections.classify_neona_reply(text)
        context = memory.remember_dialog_turn(
            config,
            context=context,
            incoming_text=text,
            reply_text=reply,
            classification=classification,
            previous_stage=previous_stage,
            new_stage=new_stage,
            message_dt=message_dt,
        )
        context = _sanitize_relationship_memory(context)
        # После первой реально обработанной реплики память считается новой:
        # она содержит только диалог, начавшийся ПОСЛЕ сообщения Неоны.
        if str(context.get("activated_by") or "") == "telegram_story_reply":
            context["post_activation_memory_started"] = True
        if voice_unverified:
            key = getattr(memory, "MEMORY_KEY", "relationship_memory")
            mem = context.get(key)
            if isinstance(mem, dict):
                mem = dict(mem)
                mem["confirmed_facts"] = confirmed_before
                context[key] = mem
    except Exception:
        pass

    return reply, new_stage, greeted, context



# --- Telegram Stories -> live dialogue handoff ---------------------------------
# Пользователь отправляет подготовленный Radar-ответ вручную. Если Telegram
# помечает исходящее как reply_to Story, Неона автоматически принимает этот
# личный чат под наблюдение и обрабатывает следующий входящий ответ человека.
_CORE_ALLOWED_CONTACTS = core._allowed_contacts
_CORE_SYNC_OWNER_ONCE = core.sync_owner_once
_STORY_ALLOWED_BY_OWNER: dict[int, dict[int, dict]] = {}
_STORY_LAST_SCAN: dict[int, float] = {}
_STORY_SCAN_INTERVAL_SECONDS = 60
_STORY_LOOKBACK_HOURS = 96


def _story_reply_id(message) -> int:
    reply_to = getattr(message, "reply_to", None)
    value = getattr(reply_to, "story_id", None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _story_allowed_contacts(config, owner_id: int):
    allowed = dict(_CORE_ALLOWED_CONTACTS(config, int(owner_id)) or {})
    allowed.update(_STORY_ALLOWED_BY_OWNER.get(int(owner_id), {}))
    return allowed


async def _refresh_manual_story_warmups(owner_id: int) -> None:
    owner_id = int(owner_id)
    now_mono = time.monotonic()
    last_scan = float(_STORY_LAST_SCAN.get(owner_id) or 0.0)
    if now_mono - last_scan < _STORY_SCAN_INTERVAL_SECONDS:
        return
    _STORY_LAST_SCAN[owner_id] = now_mono

    config = core.load_config()
    base_allowed = _CORE_ALLOWED_CONTACTS(config, owner_id)
    cached = dict(_STORY_ALLOWED_BY_OWNER.get(owner_id, {}))
    session = core._get_telegram_session(config, owner_id)
    if not session:
        return

    cutoff = datetime.now(core.UTC) - timedelta(hours=_STORY_LOOKBACK_HOURS)
    client = core.TelegramClient(
        core.StringSession(session),
        config.telegram_api_id,
        config.telegram_api_hash,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return
        me = await client.get_me()
        if int(getattr(me, "id", 0) or 0) != owner_id:
            return

        async for dialog in client.iter_dialogs(limit=160):
            entity = dialog.entity
            contact_id = int(getattr(entity, "id", 0) or 0)
            if not contact_id or getattr(entity, "bot", False):
                continue
            if not getattr(entity, "first_name", None) and not getattr(entity, "last_name", None):
                continue
            if contact_id in base_allowed or contact_id in cached:
                continue

            latest = getattr(dialog, "message", None)
            latest_date = getattr(latest, "date", None)
            if latest_date is not None:
                try:
                    if latest_date.astimezone(core.UTC) < cutoff:
                        continue
                except Exception:
                    pass

            messages = []
            # Если последняя реплика — наша Story-reply, можно обойтись без второго запроса.
            if latest is not None and bool(getattr(latest, "out", False)) and _story_reply_id(latest):
                messages = [latest]
            elif latest is not None and not bool(getattr(latest, "out", False)):
                async for item in client.iter_messages(entity, limit=10):
                    item_date = getattr(item, "date", None)
                    if item_date is not None:
                        try:
                            if item_date.astimezone(core.UTC) < cutoff:
                                break
                        except Exception:
                            pass
                    messages.append(item)
            else:
                continue

            story_outgoing = [
                item for item in messages
                if bool(getattr(item, "out", False)) and _story_reply_id(item)
            ]
            if not story_outgoing:
                continue
            warmup = max(story_outgoing, key=lambda item: int(getattr(item, "id", 0) or 0))
            warmup_id = int(getattr(warmup, "id", 0) or 0)
            warmup_date = getattr(warmup, "date", None)
            sent_at = (
                warmup_date.astimezone(core.UTC).isoformat()
                if warmup_date is not None
                else datetime.now(core.UTC).isoformat()
            )
            story_id = _story_reply_id(warmup)

            name = core._first_name(entity, "")
            cached[contact_id] = {
                "sent_at": sent_at,
                "recipient_name": name,
                "message_id": warmup_id,
                "source": "telegram_story_reply",
                "story_id": story_id,
            }

            # Создаём точку отсчёта ДО ручного ответа на Story. Благодаря greeted=True
            # Неона не начинает внезапно с нового «Здравствуйте».
            # ЖЁСТКАЯ ТОЧКА ОТСЧЁТА = само первое сообщение Неоны / ручной
            # Story-reply. Никакой текст, факт, вопрос, встреча или память из
            # переписки с этим человеком ДО warmup_id не переносится в Неону.
            # Telegram message id достаточно как fence: все последующие ответы
            # человека имеют id больше этой точки.
            baseline = warmup_id

            fresh_context = {
                "activated_by": "telegram_story_reply",
                "warmup_mode": True,
                "warmup_incoming_count": 0,
                "story_reply_message_id": warmup_id,
                "story_id": story_id,
                "first_message_sent_at": sent_at,
                "history_fence_message_id": warmup_id,
                "pre_activation_history_forbidden": True,
                "post_activation_memory_started": False,
            }

            core._save_dialog_state(
                config,
                owner_id,
                contact_id,
                last_incoming_id=baseline,
                stage="idle",
                greeted=True,
                context=fresh_context,
            )
            print(
                f"[NeonaStoryHandoff] owner={owner_id} contact={contact_id} "
                f"story_id={story_id} outgoing_id={warmup_id}",
                flush=True,
            )

        _STORY_ALLOWED_BY_OWNER[owner_id] = cached
    finally:
        await client.disconnect()


async def _story_aware_sync_owner_once(owner_id: int, owner_name: str, *, initialize_new_dialogs: bool = True):
    try:
        await _refresh_manual_story_warmups(int(owner_id))
    except Exception as exc:
        # Ошибка радара не должна останавливать обычные диалоги Неоны.
        print(
            "NEONA_STORY_HANDOFF_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
    return await _CORE_SYNC_OWNER_ONCE(
        owner_id,
        owner_name,
        initialize_new_dialogs=initialize_new_dialogs,
    )


def apply_policy():
    """Подменяет только политику, не переписывая основной рабочий модуль."""

    core._slot_free = _slot_free_with_buffer
    core._openai_general_reply = _general_reply
    core._process_message = _process_message
    core._allowed_contacts = _story_allowed_contacts
    core.sync_owner_once = _story_aware_sync_owner_once


def initialize_dialog_after_first_message(
    owner_id,
    contact_id,
    *,
    baseline_incoming_id=0,
    sent_at="",
):
    """Жёстко начинает память Неоны только с её первого сообщения.

    Даже если для этого человека раньше уже существовал dialog_state, он
    полностью заменяется свежим состоянием без relationship_memory. Старую
    личную переписку владельца Неона не получает и не использует.
    """
    apply_policy()
    config = core.load_config()
    core._save_dialog_state(
        config,
        int(owner_id),
        int(contact_id),
        last_incoming_id=int(baseline_incoming_id or 0),
        stage="idle",
        greeted=False,
        context={
            "first_message_sent_at": str(sent_at or ""),
            "activated_by": "agency_w_first_message",
            "history_fence_incoming_id": int(baseline_incoming_id or 0),
            "pre_activation_history_forbidden": True,
        },
    )


def run_sync_owner_once(*args, **kwargs):
    apply_policy()
    return core.run_sync_owner_once(*args, **kwargs)


def worker_forever(*args, **kwargs):
    apply_policy()
    return core.worker_forever(*args, **kwargs)


DialogError = core.DialogError
