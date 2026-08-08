import json
import os
import re
from pathlib import Path
from typing import Any

import requests

import neona_telegram_dialogs as nd


BRAIN_VERSION = "4.0"
APP_DIR = Path(__file__).resolve().parent
CORE_PATH = APP_DIR / "NEONA_CORE.md"

ORIGINAL_PROCESS_MESSAGE = nd._process_message
ORIGINAL_DETECT_TIMEZONE = nd._detect_timezone

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


def _message_has_meeting_details(text: str) -> bool:
    return bool(
        nd._detect_time(text)
        or nd._detect_date(text, nd.datetime.now(nd.UTC), _smart_detect_timezone(text))
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
    jargon_hits = sum(1 for token in TECH_JARGON if token in lowered)
    return jargon_hits >= 2


def _reply_is_too_long(reply: str) -> bool:
    text = str(reply or "").strip()
    return len(text) > 900 or text.count("\n") > 5


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
- ответ был простым, живым, человеческим, обычно 2–4 предложения;
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
    context, stage = _migrate_context(raw_context, stage)
    greet = not greeted

    # Надёжную календарную часть не переписываем.
    if stage == "scheduled" or stage in STRICT_SCHEDULING_STAGES:
        reply, new_stage, new_greeted, new_context = ORIGINAL_PROCESS_MESSAGE(
            config,
            owner_id,
            owner_name,
            contact_id,
            first_name,
            username,
            text,
            message_dt,
            {**state, "stage": stage, "context": context},
        )
        new_context["brain_version"] = BRAIN_VERSION
        new_context = _remember_exchange(new_context, text, reply)
        return reply, new_stage, new_greeted, new_context

    context = nd._update_context_from_message(context, text, message_dt)
    context["brain_version"] = BRAIN_VERSION

    # Уже начатое согласование: если человек сообщает реальные данные встречи,
    # отдаём управление проверенной календарной логике.
    if stage in CONVERSATIONAL_SCHEDULING_STAGES:
        if _message_has_meeting_details(text) or _explicit_meeting_commitment(text):
            reply, new_stage, context = nd._schedule_reply(
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

        # Человек вместо данных встречи продолжил разговор —
        # возвращаемся в живой диалог, не повторяем анкету.
        stage = "idle"

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
        reply = "Расскажите, что для вас сейчас в этой теме самое важное?"

    context = _merge_profile(context, plan.get("person_updates"))
    context["conversation_stage"] = str(plan.get("conversation_stage") or "discover")
    context["meeting_readiness"] = int(plan.get("meeting_readiness") or 0)
    context["next_action"] = str(plan.get("next_action") or "listen")

    # Человек САМ согласился на встречу — включаем календарный контур.
    committed = bool(plan.get("meeting_committed")) or _explicit_meeting_commitment(text)
    if committed:
        reply, new_stage, context = nd._schedule_reply(
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
nd._process_message = _smart_process_message


if __name__ == "__main__":
    nd.worker_forever(int(os.getenv("NEONA_POLL_SECONDS", "15")))
