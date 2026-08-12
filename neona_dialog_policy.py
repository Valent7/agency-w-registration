from __future__ import annotations

import re
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests
import neona_telegram_dialogs as core


BUFFER_MINUTES = 60
MIN_LEAD_MINUTES = 60


def _slot_free_with_buffer(config, owner_id, start_utc, end_utc):
    """Свободный слот с рабочим окном 10:00–20:00 МСК и часовым буфером."""

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

    # Между встречами должен оставаться минимум 1 час.
    expanded_start = start_utc - timedelta(minutes=BUFFER_MINUTES)
    expanded_end = end_utc + timedelta(minutes=BUFFER_MINUTES)

    return not core._list_meetings(
        config,
        owner_id,
        expanded_start,
        expanded_end,
    )


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


def _general_reply(config, owner_name, first_name, text, greet):
    greeting_rule = (
        f"Начни с «{core._greeting(first_name)}»"
        if greet
        else "Не повторяй приветствие: диалог уже начат."
    )

    instructions = f"""
Ты Неона — секретарь-референт {owner_name} в Агентстве W.
Пиши по-русски простым человеческим языком. 1–3 коротких предложения.

Твоя конечная цель — осознанная встреча человека с {owner_name},
но нельзя вести себя как попугай и механически повторять приглашение.

Если человек задаёт вопрос:
- кратко ответь, только если ответ точно следует из известных возможностей Агентства W;
- если вопрос лучше обсудить с владельцем, скажи естественно:
  «Я думаю, этот вопрос лучше обсудить с {owner_name} лично»
  или близко по смыслу;
- не выдумывай цены, доходы, гарантии, условия проектов и факты, которых нет.

Не обещай человеку найти ему партнёров, написать за него первые сообщения,
перевести чужой текст или выполнить другую работу, которой он не просил.
Не называй ИИ ботом или чат-ботом.
Не говори, что Агентство уже имеет доступ к Telegram собеседника.
{greeting_rule}
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
    prefix_rule = (
        f"Можно начать с «{core._greeting(first_name)}»."
        if greet
        else "Не повторяй приветствие."
    )

    if stage == "awaiting_confirmation" and context.get("proposed_start_at"):
        return_target = (
            "После ответа мягко вернись к уже предложенному времени: "
            "попроси подтвердить его, не задавая всё заново."
        )
    elif stage == "awaiting_slot_choice":
        return_target = (
            "После ответа мягко вернись к двум предложенным вариантам "
            "и попроси выбрать 1 или 2."
        )
    elif not context.get("requested_date") or not context.get("requested_time"):
        return_target = (
            f"После ответа мягко верни разговор к встрече с {owner_name}. "
            "Естественный финал: «Итак, когда вам будет удобно встретиться?» "
            "или близко по смыслу."
        )
    elif not context.get("contact_timezone"):
        return_target = (
            "После ответа уточни часовой пояс, чтобы правильно согласовать время."
        )
    elif not context.get("meeting_format"):
        return_target = (
            "После ответа уточни, как удобнее встретиться: Zoom, Telegram или WhatsApp."
        )
    else:
        return_target = (
            "После ответа вернись к подтверждению встречи, не повторяя всю анкету."
        )

    instructions = f"""
Ты Неона — секретарь-референт {owner_name}.
Человек уже проявил интерес и вы находитесь на этапе организации встречи.

Он задал отвлечённый или уточняющий вопрос.
НЕЛЬЗЯ игнорировать его и механически повторять:
«назовите время и формат встречи».

Сначала отреагируй на вопрос по-человечески:
- если знаешь точный, безопасный ответ — ответь одной короткой фразой;
- если вопрос требует объяснений владельца или точных условий, скажи:
  «Я думаю, этот вопрос лучше обсудить с {owner_name} лично»
  или естественный вариант этой мысли;
- ничего не выдумывай.

Потом мягко верни разговор к встрече.
{return_target}

Не обещай искать человеку партнёров, писать за него сообщения,
делать переводы или оказывать услуги, которых он не просил.
1–3 коротких предложения.
{prefix_rule}
""".strip()

    return _call_openai(config, instructions, text)


def _after_scheduled_reply(config, owner_name, first_name, text):
    instructions = f"""
Ты Неона — секретарь-референт {owner_name}.
Встреча с человеком УЖЕ назначена.

Ответь на его вопрос кратко.
Если вопрос лучше обсудить с {owner_name}, скажи об этом прямо и тепло:
«Я думаю, этот вопрос лучше обсудить с {owner_name} на встрече».
Не приглашай на новую встречу и не начинай согласование времени заново.
Не выдумывай факты.
1–2 коротких предложения.
""".strip()
    return _call_openai(config, instructions, text)


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
    stage = str(state.get("stage") or "idle")
    greeted = bool(state.get("greeted", False))
    context = (
        state.get("context")
        if isinstance(state.get("context"), dict)
        else {}
    )
    greet = not greeted

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

    # Человек сказал «да/интересно» и одновременно задал вопрос.
    if (
        stage == "idle"
        and core._is_positive_interest(text)
        and _substantive_detour(text, message_dt, context)
    ):
        context = core._update_context_from_message(
            context,
            text,
            message_dt,
        )
        return (
            _meeting_bridge(
                config,
                owner_name,
                first_name,
                text,
                "invited_to_meeting",
                context,
                greet,
            ),
            "invited_to_meeting",
            True,
            context,
        )

    # Явный интерес без отвлечённого вопроса — сразу к встрече.
    if stage == "idle" and core._is_positive_interest(text):
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

    # На этапе встречи сначала уважаем содержательный вопрос.
    if (
        scheduling_stage
        and _substantive_detour(text, message_dt, context)
    ):
        return (
            _meeting_bridge(
                config,
                owner_name,
                first_name,
                text,
                stage,
                context,
                greet,
            ),
            stage,
            True,
            context,
        )

    if core._meeting_intent(text) or scheduling_stage:
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

    reply = _general_reply(
        config,
        owner_name,
        first_name,
        text,
        greet,
    )
    new_stage = (
        "invited_to_meeting"
        if core._meeting_intent(reply)
        else stage
    )
    return reply, new_stage, True, context


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
