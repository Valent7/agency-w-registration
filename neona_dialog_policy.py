from __future__ import annotations

import re
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests
import neona_telegram_dialogs as core
import neona_objections as objections
import neona_memory as memory


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

    agency_core = str(getattr(core, "NEONA_DIALOG_CORE", "") or "").strip()

    instructions = f"""
{agency_core}

Ты Неона — секретарь-референт {owner_name} в Агентстве W.
Пиши по-русски простым человеческим языком. Обычно 1–3 коротких предложения.

{objections.NEONA_OBJECTION_RULES_TEXT}

ТВОЙ ВНУТРЕННИЙ КОМПАС:
Сначала человек — потом Агентство.
Перед ответом пойми:
1) что человек сейчас сказал по смыслу;
2) что нового ты узнала о нём;
3) какой ОДИН смысл Агентства может быть ему действительно полезен;
4) какой следующий маленький шаг естественен именно сейчас.

НЕ РАБОТАЙ КАК АНКЕТА ИЛИ ПРЕЗЕНТАЦИЯ:
- один вопрос за раз;
- не перечисляй функции Агентства;
- не перескакивай с одного «магнита» на другой;
- не задавай вопросы о доходе, боли, семье, целях и мечтах подряд;
- следующая реплика должна рождаться из ответа человека, а не из шаблона.

ТВОЯ КОНЕЧНАЯ ЦЕЛЬ — осознанная встреча человека с {owner_name},
но человек не должен ощущать, что его тащат к встрече.
Не приглашай механически после каждой реплики.

ЕСЛИ ЧЕЛОВЕК СТАВИТ ЯВНУЮ ГРАНИЦУ КОММУНИКАЦИИ
(например: «не пишите», «не присылайте», «больше не беспокойте»):
- немедленно остановись;
- не спорь, не предлагай другой аргумент и не задавай новый вопрос;
- коротко подтверди, что больше писать не будешь.

Фразы вроде «нет времени», «мне это не нужно», «неинтересно», «у меня уже есть ИИ»
считай мягким возражением, а не автоматическим запретом на разговор: их можно один раз
спокойно прояснить по базе возражений.

ЕСЛИ ЧЕЛОВЕК ЗАДАЁТ ВОПРОС:
- ответь кратко, только если ответ точно следует из известных возможностей Агентства W;
- если вопрос лучше обсудить с владельцем, естественно скажи:
  «Я думаю, этот вопрос лучше обсудить с {owner_name} лично»;
- не выдумывай цены, доходы, гарантии, условия проектов и факты, которых нет.

ЕСЛИ ВОПРОС УШЁЛ ДАЛЕКО ОТ ТЕМЫ:
не превращайся в универсальный ChatGPT. Если уместно, ответь очень кратко,
а затем мягко вернись к контексту разговора с человеком.

ГОВОРИ ПО-ЧЕЛОВЕЧЕСКИ:
- обычные слова вместо маркетингового жаргона;
- одна мысль за сообщение;
- допускается лёгкий естественный юмор, если он подходит собеседнику;
- не называй человека «лидом», «кандидатом» или «целевой аудиторией».

НЕ ОБЕЩАЙ человеку найти ему партнёров, гарантировать результат,
перевести чужой текст как отдельную услугу или выполнить работу,
которой Агентство фактически не выполняет.
Не называй ИИ ботом или чат-ботом.
Не говори, что Агентство имеет доступ к Telegram собеседника.
{greeting_rule}
""".strip()

    return _call_openai(config, instructions, text)


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
    """Сохраняет сильную рабочую политику Неоны и добавляет только слой памяти."""
    previous_stage = str((state or {}).get("stage") or "idle")
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

    # Память не участвует в принятии решения и не меняет сформированный ответ.
    # Даже если извлечение памяти даст сбой, живой диалог продолжится как раньше.
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
