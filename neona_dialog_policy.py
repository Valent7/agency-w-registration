from __future__ import annotations

import re
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
    return SequenceMatcher(None, a, b).ratio() >= 0.78


def _de_repeat_reply(config, reply: str, text: str, context) -> str:
    """Страховка от повторения одного и того же вопроса двумя сообщениями подряд."""
    if not _reply_is_repetitive(reply, context):
        return reply
    mem = _relationship_memory(context)
    previous = str(mem.get("last_reply") or "").strip()
    history = _dialog_context_block(context, max_turns=6)
    instructions = f"""
Ты Неона. Предыдущий ответ уже был: «{previous}».
Новый ответ получился слишком похожим. Исправь это.

Правила:
- НЕ повторяй тот же вопрос и не перефразируй его;
- сначала ответь на последнюю реплику человека по смыслу;
- используй контекст разговора ниже;
- если вопрос уже понятен из контекста, ответь на него без уточнений;
- сделай один естественный следующий шаг;
- 1–3 коротких предложения.

Контекст:
{history}
""".strip()
    return _call_openai(config, instructions, text)


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
    meeting_permission = personal_reason_known or personal_reason_now
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
- не повторяй один и тот же вопрос двумя сообщениями подряд.

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
            return "", "scheduled", True, context

        if any(token in lowered for token in (
            "перенести", "другое время", "другой день", "не смогу",
            "не могу", "отменить", "отмена",
        )):
            context.pop("proposed_start_at", None)
            context.pop("requested_date", None)
            context.pop("requested_time", None)
            context.pop("offered_slots", None)
            return (
                "Хорошо. Напишите, пожалуйста, какой новый день и время вам удобны. "
                "Если часовой пояс и формат встречи остаются прежними, "
                "повторять их не нужно.",
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
        if _has_personal_reason(context):
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
    meeting_allowed = _has_personal_reason(context) or _current_text_has_personal_reason(text)
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


def apply_policy():
    """Подменяет только политику, не переписывая основной рабочий модуль."""

    core._slot_free = _slot_free_with_buffer
    core._openai_general_reply = _general_reply
    core._process_message = _process_message


def initialize_dialog_after_first_message(*args, **kwargs):
    apply_policy()
    return core.initialize_dialog_after_first_message(*args, **kwargs)


def run_sync_owner_once(*args, **kwargs):
    apply_policy()
    return core.run_sync_owner_once(*args, **kwargs)


def worker_forever(*args, **kwargs):
    apply_policy()
    return core.worker_forever(*args, **kwargs)


DialogError = core.DialogError
