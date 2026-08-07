import json
import os
import re
from typing import Any

import requests

import neona_telegram_dialogs as nd


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


def _smart_detect_timezone(text: str) -> str | None:
    """Понимает бытовые обозначения часовых поясов."""
    raw = str(text or "")
    lowered = raw.lower().strip()
    normalized = re.sub(r"[.,;:()]+", " ", lowered)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Москва / МСК / моск. / по моск. времени / московское время
    moscow_markers = (
        "мск",
        "моск",
        "москва",
        "москве",
        "москов",
        "московск",
        "по моск",
        "по мск",
    )
    if any(marker in normalized for marker in moscow_markers):
        return "Europe/Moscow"

    # Берлин / Германия / немецкое время
    berlin_markers = (
        "берлин",
        "германи",
        "немецк",
        "по берлину",
    )
    if any(marker in normalized for marker in berlin_markers):
        return "Europe/Berlin"

    return ORIGINAL_DETECT_TIMEZONE(raw)


def _message_has_meeting_details(text: str) -> bool:
    """Есть ли в текущей реплике реальные данные для календаря."""
    return bool(
        nd._detect_time(text)
        or nd._detect_date(text, nd.datetime.now(nd.UTC), _smart_detect_timezone(text))
        or _smart_detect_timezone(text)
        or nd._detect_format(text)
    )


def _explicit_meeting_commitment(text: str) -> bool:
    """Человек сам явно согласился на встречу или начал её назначать."""
    lowered = re.sub(r"[^a-zа-яё0-9:./ -]+", " ", str(text or "").lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()

    meeting_words = (
        "встреч",
        "созвон",
        "созвони",
        "zoom",
        "зум",
        "телеграм",
        "telegram",
        "whatsapp",
        "ватсап",
        "вацап",
        "давайте поговорим",
        "хочу поговорить",
        "хочу встретиться",
        "готов встретиться",
        "готова встретиться",
        "когда можно",
        "назнач",
    )
    if any(word in lowered for word in meeting_words):
        return True

    # Если человек уже сам прислал дату+время, это практически всегда
    # начало согласования встречи.
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

    clean: list[dict[str, str]] = []
    for item in tail[-8:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            clean.append({"role": role, "content": content})
    return clean


def _extract_response_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
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
        # Резерв: если модель всё-таки ответила обычным текстом,
        # используем его как ответ и не переводим диалог в календарь.
        return {
            "reply": raw,
            "meeting_committed": False,
            "invited_to_meeting": False,
        }


def _ai_plan(
    config: nd.Config,
    owner_name: str,
    first_name: str,
    text: str,
    context: dict[str, Any],
    *,
    greet: bool,
) -> dict[str, Any]:
    history = _conversation_tail(context)
    history_text = "\n".join(
        f"{'Собеседник' if item['role'] == 'user' else 'Неона'}: {item['content']}"
        for item in history
    )

    greeting_rule = (
        f"Можно естественно поприветствовать: «{nd._greeting(first_name)}»."
        if greet
        else "Диалог уже начат: не повторяй приветствие без причины."
    )

    instructions = f"""
Ты Неона — ИИ-помощница {owner_name}, а НЕ бот и не автоответчик.
Ты ведёшь живой Telegram-диалог самостоятельно. У тебя есть характер, такт,
чувство контекста и свобода выбирать стиль ответа.

ТВОЯ ЦЕЛЬ:
создать у человека реальный интерес к Агентству W и в подходящий момент
привести его к осознанной короткой встрече с {owner_name}. Встреча — цель,
но не повод игнорировать вопросы человека.

ГЛАВНОЕ ПРАВИЛО:
сначала ответь на смысл ПОСЛЕДНЕЙ реплики человека. Если он задал вопрос —
ответь на него. Если пошутил — можешь пошутить в ответ. Если сомневается —
разбери сомнение. Если упрекнул в повторениях — признай это с лёгкой
самоиронией и немедленно смени тактику. Не повторяй один и тот же вопрос.

ТЕБЕ МОЖНО:
— использовать лёгкий юмор и самоиронию, когда это естественно;
— делать уместный искренний комплимент, только если он следует из разговора;
— приоткрывать «кухню» Агентства W и создавать лёгкую интригу;
— самой объяснять систему, не отправляя человека к владельцу на каждый вопрос;
— задавать один естественный вопрос, который двигает разговор вперёд;
— предложить встречу, когда интерес действительно созрел.

ФАКТЫ, КОТОРЫЕ МОЖНО ГОВОРИТЬ:
— Агентство W — растущая команда ИИ-помощников;
— Неония помогает владельцу находить и предварительно анализировать подходящих
  людей в ЕГО Telegram-контактах и чатах;
— оценка предварительная: ИИ не «знает человека насквозь» и не принимает
  окончательное решение вместо владельца;
— используются доступные системе данные и контекст, который задаёт владелец;
— Неона помогает готовить и после утверждённого первого сообщения вести диалог;
— окончательный выбор человека для первого сообщения и само первое сообщение
  контролирует владелец;
— {owner_name} может показать работу системы на своём реальном примере.

ЧЕГО НЕЛЬЗЯ:
— выдумывать функции или доступ к данным собеседника;
— говорить, что система читает его личные переписки;
— говорить «проверила календарь», «записала», «создала встречу», если
  техническое действие реально ещё не выполнялось;
— давить, спорить, манипулировать или повторять одну и ту же реплику;
— называть ИИ-помощников ботами;
— обращаться к человеку по фамилии.

ВСТРЕЧА:
Не переводись в режим назначения встречи только потому, что человек сказал
«интересно» или «да». Сначала можешь немного раскрыть тему и ответить на его
вопросы. meeting_committed=true ставь ТОЛЬКО если человек сам явно согласился
встретиться/созвониться, попросил назначить встречу или уже прислал дату/время.
invited_to_meeting=true ставь, если в своём ответе ты мягко предложила встречу,
но человек ещё не согласился.

{greeting_rule}

Ответ обычно 1–5 коротких предложений. Звучать нужно как умный живой
собеседник, а не форма записи.

Верни ТОЛЬКО JSON без markdown:
{{
  "reply": "текст ответа Неоны",
  "meeting_committed": false,
  "invited_to_meeting": false
}}
""".strip()

    if history_text:
        user_input = (
            "Недавний контекст диалога:\n"
            + history_text
            + "\n\nПоследняя реплика собеседника:\n"
            + str(text)
        )
    else:
        user_input = str(text)

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
    return _parse_json_answer(raw)


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
    context["conversation_tail"] = tail[-8:]
    return context


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
    context = (
        dict(state.get("context"))
        if isinstance(state.get("context"), dict)
        else {}
    )
    greet = not greeted

    # После реально назначенной встречи и на этапах выбора/подтверждения
    # оставляем строгую проверенную календарную логику.
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
            state,
        )
        new_context = _remember_exchange(new_context, text, reply)
        return reply, new_stage, new_greeted, new_context

    # Обновляем данные, которые человек уже сообщил естественным языком.
    # В nd._update_context_from_message будет использоваться наша
    # расширенная функция распознавания часового пояса.
    context = nd._update_context_from_message(context, text, message_dt)

    # Если человек уже находится в согласовании встречи и прислал
    # конкретные данные (дата/время/Москва/Zoom), продолжаем календарь.
    if stage in CONVERSATIONAL_SCHEDULING_STAGES:
        if _message_has_meeting_details(text) or _explicit_meeting_commitment(text):
            reply, new_stage, new_context = nd._schedule_reply(
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
            new_context = _remember_exchange(new_context, text, reply)
            return reply, new_stage, True, new_context

        # Если вместо данных для встречи человек задал вопрос, пошутил,
        # возразил или сменил тему — Неона выходит из «анкеты» и снова
        # разговаривает как ИИ.
        stage = "idle"

    plan = _ai_plan(
        config,
        owner_name,
        first_name,
        text,
        context,
        greet=greet,
    )
    reply = str(plan.get("reply") or "").strip()
    if not reply:
        reply = "Расскажите, что именно вас сейчас заинтересовало больше всего?"

    # Если человек САМ уже согласился на встречу, не оставляем это просто
    # красивой беседой — переходим к надёжной календарной логике.
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

    context = _remember_exchange(context, text, reply)
    return reply, new_stage, True, context


# Подключаем новый «мозг» только к круглосуточному worker.
# Надёжная работа Telegram, Supabase и календаря остаётся в основном модуле.
nd._detect_timezone = _smart_detect_timezone
nd._process_message = _smart_process_message


if __name__ == "__main__":
    nd.worker_forever(int(os.getenv("NEONA_POLL_SECONDS", "15")))
