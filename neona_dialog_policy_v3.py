from __future__ import annotations

"""
Неона 3.0 — «легко, интересно, приятно».

Архитектура:
- neona_dialog_policy_v2.py остаётся базой жёстких правил, календаря и safety;
- этот слой отвечает за понимание смысла, характер и естественное ведение разговора;
- свободный диалог обрабатывает более сильная модель;
- календарь, согласие на встречу, стоп-сигналы и реальные действия остаются
  детерминированными и не отдаются модели на фантазию.
"""

import json
import os
import re
from pathlib import Path

import requests
import neona_dialog_policy_v2 as v2

legacy = v2.legacy
core = v2.core

_ORIGINAL_V2_PROCESS = v2._process_v2

_BASE_DIR = Path(__file__).resolve().parent
_PERSONALITY_PATH = _BASE_DIR / "NEONA_PERSONALITY.md"

_DEFAULT_PERSONALITY = """
Неона — умная, тёплая, образованная секретарь-референт Валентины.
С ней должно быть легко, интересно и приятно.
Допускаются лёгкий юмор, метафоры и очень редкая уместная латынь.
Главная деловая задача — понять человека, показать ценность времени и,
при реальном интересе, получить согласие на встречу с Валентиной.
Интерес не равен согласию на встречу.
""".strip()


def _load_personality() -> str:
    try:
        value = _PERSONALITY_PATH.read_text(encoding="utf-8").strip()
        return value or _DEFAULT_PERSONALITY
    except Exception:
        return _DEFAULT_PERSONALITY


def _owner_forms(owner_name: str) -> dict[str, str]:
    return legacy._owner_forms(owner_name)


def _personalize_owner_text(value: str, owner_name: str) -> str:
    forms = _owner_forms(owner_name)
    text = str(value or "")
    # Важно заменять падежные формы раньше именительного.
    for old, new in (
        ("Валентиной", forms["instrumental"]),
        ("Валентины", forms["genitive"]),
        ("Валентина", forms["nominative"]),
    ):
        text = text.replace(old, new)
    return text


def _model_candidates() -> list[str]:
    """Качество по умолчанию, но с мягким fallback без поломки диалогов."""
    requested = str(os.getenv("NEONA_DIALOG_MODEL") or "gpt-6.1-sol").strip()
    fallback = str(os.getenv("NEONA_DIALOG_FALLBACK_MODEL") or "gpt-6-luna").strip()
    result = []
    for item in (requested, fallback, "gpt-5-mini"):
        if item and item not in result:
            result.append(item)
    return result


def _response_text(data: dict) -> str:
    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def _call_reasoning_model(config, instructions: str, input_text: str) -> str:
    last_error = None
    for model in _model_candidates():
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {config.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "instructions": instructions,
                    "input": input_text,
                    "store": False,
                },
                timeout=90,
            )
            if not response.ok:
                last_error = RuntimeError(
                    f"{model}: HTTP {response.status_code}: {response.text[:400]}"
                )
                # Если конкретная модель недоступна аккаунту, пробуем следующую.
                if response.status_code in {400, 403, 404}:
                    continue
                response.raise_for_status()

            answer = _response_text(response.json())
            if answer:
                return answer
            last_error = RuntimeError(f"{model}: пустой ответ")
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise RuntimeError("Неона не получила ответ модели.")


def _extract_json(raw: str) -> dict:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", value, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _identity_question_v3(text: str) -> bool:
    """Ловит и прямой вопрос, и шутливое «я с роботом общаюсь 😂»."""
    value = v2._norm(text)
    ai_words = (
        "робот", "бот", "ии", "искусственный интеллект", "нейросет",
        "нейрон", "машина",
    )
    context_words = (
        "общаюсь", "разговариваю", "переписываюсь", "мне пишет",
        "со мной говорит", "вы кто", "ты кто",
    )
    if not any(word in value for word in ai_words):
        return False
    return (
        "?" in str(text or "")
        or any(word in value for word in context_words)
        or bool(re.search(r"\b(?:вы|ты|это)\b", value))
    )


def _identity_reply(owner_name: str, first_name: str, greet: bool) -> str:
    prefix = f"{core._greeting(first_name)} " if greet else ""
    forms = _owner_forms(owner_name)
    return (
        prefix
        + f"Да, вы меня раскусили 😄 Я искусственный интеллект, "
          f"секретарь-референт {forms['genitive']}. "
          "Стараюсь быть полезной без лишнего шума — и кофе, к счастью, не требую."
    )


def _hard_case(text: str, context: dict, owner_name: str = "") -> bool:
    """То, что должно пройти через проверенные жёсткие правила V2."""
    return any((
        v2._do_not_contact(text),
        v2._decline(text),
        v2._source_question(text),
        v2._guarantee_question(text),
        v2._health_topic(text),
        bool(v2._meeting_reason(text, context, owner_name)),
    ))


def _dialog_instructions(owner_name: str, first_name: str, context: dict, greet: bool) -> str:
    history = legacy._dialog_context_block(context, max_turns=12)
    forms = _owner_forms(owner_name)
    personality = _personalize_owner_text(_load_personality(), owner_name)
    greeting_rule = (
        f"Это первое сообщение в текущем диалоге. Можно начать с «{core._greeting(first_name)}»."
        if greet
        else "Диалог уже идёт: не повторяй приветствие."
    )

    return f"""
Ты Неона — секретарь-референт {forms['genitive']} в Агентстве W.

ТВОЙ ХАРАКТЕР И МАНЕРА:
{personality}

ПОСЛЕДНИЙ КОНТЕКСТ ДИАЛОГА:
{history}

ГЛАВНЫЙ ПРИНЦИП:
Не отвечай на ключевые слова. Отвечай на СМЫСЛ последней реплики в контексте разговора.
Перед ответом молча пойми:
- на что человек отвечает;
- что он сейчас сообщает: факт, шутку, сомнение, интерес, отказ или вопрос;
- что уже известно и что нельзя спрашивать повторно;
- какой ОДИН следующий шаг естественен.

ДЕЛОВОЙ КОМПАС:
Агентству W нужны партнёры и клиенты, но человека нельзя тянуть к встрече механически.
Твоя задача — сделать разговор с {forms['instrumental']} естественным продолжением реального интереса.
Важная линия: бизнес → люди → время → ценность освобождённого времени → автоматизация →
согласие поговорить с {forms['instrumental']}.

ЕСЛИ ЧЕЛОВЕК ЗАНИМАЕТСЯ БИЗНЕСОМ:
Не устраивай анкету. Разумно подведи к теме времени:
поиск людей, переписка, первые разговоры, возражения, встречи, сопровождение.
Хороший вопрос, когда он уместен:
«А если бы у вас освободилось несколько часов в неделю, на что вам хотелось бы их потратить?»
Позже:
«А хотели бы вы тратить на поиск и переписку меньше времени?»

ЕСЛИ У ЧЕЛОВЕКА УЖЕ ЕСТЬ ИИ ИЛИ КОМАНДА:
Не спорь и не доказывай, что наше решение лучше.
Можно мягко посмотреть, решена ли задача поиска людей у всей команды, а не только у самого человека.

ЕСЛИ БИЗНЕСА НЕТ:
Не продавай. Можно тепло завершить разговор и оставить дверь Агентства W открытой.

ВСТРЕЧА:
Ты НЕ имеешь права показывать время календаря в свободном диалоге.
Интерес к Агентству или автоматизации ещё НЕ означает согласия на встречу.
Если по смыслу уже естественно предложить встречу, action должен быть "ask_meeting_consent",
а reply должен спросить, хочет ли человек поговорить с {forms['instrumental']}.
Только программная календарная ветка после явного согласия покажет свободное время.

ЖЁСТКИЕ ГРАНИЦЫ:
- Если текущее сообщение НЕ о здоровье, вообще не упоминай врача, лечение, медицину или здоровье.
- Не консультируй по праву, финансам, криптовалютам, медицине, рекламе и чужой стратегии.
- Не обещай доход, количество клиентов/партнёров и гарантированный результат.
- Не придумывай цены, кейсы, факты, файлы, услуги и действия.
- Не говори, что уже что-то записала/передала/отправила, если действие не выполнено системой.
- Не называй внутренние имена ИИ-агентов без необходимости.
- Не дави на семью, возраст, страх потери времени или деньги.
- Не задавай несколько вопросов подряд.
- Не заканчивай каждую реплику вопросом автоматически.
- {greeting_rule}

ЮМОР / ЭРУДИЦИЯ:
Можно добавить ОДНУ маленькую человеческую искру: лёгкую шутку, метафору
или очень редкую латинскую фразу с переводом — только если это действительно к месту.
Не превращай ответ в выступление и не демонстрируй эрудицию ради эрудиции.

ФОРМАТ ОТВЕТА:
Верни ТОЛЬКО JSON, без markdown:
{{
  "reply": "готовая реплика человеку, обычно 1–3 коротких предложения",
  "action": "continue | ask_meeting_consent | close_gently",
  "why": "очень коротко, 3–8 слов, только для внутреннего журнала"
}}

action:
- continue — обычный живой разговор;
- ask_meeting_consent — человек уже достаточно заинтересован, естественно спросить про разговор с {forms['instrumental']};
- close_gently — разговор естественно завершён, но человек НЕ просил запретить контакт.

Никогда не показывай человеку поле why и внутренний анализ.
""".strip()


def _repair_reply(config, owner_name: str, text: str, reply: str, problem: str) -> str:
    forms = _owner_forms(owner_name)
    instructions = f"""
Ты редактор одной реплики Неоны — секретаря-референта {forms['genitive']}.
Исправь только указанную ошибку, сохрани смысл и естественный тон.
Не добавляй новых фактов, встречи, времени или советов.
1–3 коротких предложения.

Ошибка: {problem}
Исходная реплика человека: {text}
Черновик Неоны: {reply}

Верни только исправленную реплику, без пояснений.
""".strip()
    return _call_reasoning_model(config, instructions, text)


def _reasoned_turn(config, owner_name, first_name, text, greet, context):
    instructions = _dialog_instructions(owner_name, first_name, context, greet)
    raw = _call_reasoning_model(
        config,
        instructions,
        "НОВОЕ СООБЩЕНИЕ ЧЕЛОВЕКА:\n" + str(text or ""),
    )
    data = _extract_json(raw)
    reply = str(data.get("reply") or "").strip()
    action = str(data.get("action") or "continue").strip()
    why = str(data.get("why") or "").strip()[:120]

    if action not in {"continue", "ask_meeting_consent", "close_gently"}:
        action = "continue"

    # Если модель не соблюла JSON, безопасно используем текст как обычную реплику.
    if not reply:
        reply = raw.strip()
        action = "continue"

    # Старые реплики в памяти могли содержать «Валентина». В кабинете другого
    # владельца это не должно менять того, от чьего имени работает Неона.
    reply = v2._rewrite_wrong_owner_reference(reply, owner_name)

    # Защита от сегодняшнего казуса: медицина не может «просочиться» по ассоциации.
    if not v2._health_topic(text):
        if re.search(r"\b(?:врач|доктор|медицин|лечени|лекарств|здоровь)\w*\b", reply, flags=re.I):
            reply = _repair_reply(
                config, owner_name, text, reply,
                "В текущем сообщении нет темы здоровья. Полностью убери медицинскую тему.",
            )

    # До согласия нельзя выдавать слоты календаря.
    if action != "ask_meeting_consent" and re.search(r"\b\d{1,2}:\d{2}\b", reply):
        reply = _repair_reply(
            config, owner_name, text, reply,
            "Нельзя предлагать время встречи до отдельного согласия человека.",
        )

    # Если модель решила предложить встречу, формулировка должна быть именно просьбой о согласии.
    if action == "ask_meeting_consent":
        forms = _owner_forms(owner_name)
        owner_variants = {
            forms["nominative"].casefold(),
            forms["genitive"].casefold(),
            forms["instrumental"].casefold(),
        }
        owner_mentioned = any(value and value in reply.casefold() for value in owner_variants)
        meeting_mentioned = any(
            word in reply.casefold() for word in ("встреч", "поговор", "созвон")
        )
        if not (owner_mentioned and meeting_mentioned and "?" in reply):
            reply = reply.rstrip(" .")
            if reply:
                reply += ". "
            reply += f"Хотите, я организую вам встречу с {forms['instrumental']}?"

    return {"reply": reply, "action": action, "why": why}


def _general_reply_v3(config, owner_name, first_name, text, greet, context=None):
    """Используется и V2-ветками, когда им нужна свободная человеческая реплика."""
    context = dict(context or {})
    result = _reasoned_turn(
        config, owner_name, first_name, text, greet, context
    )
    return legacy._de_repeat_reply(
        config, result["reply"], text, context
    )


def _process_v3(
    config, owner_id, owner_name, contact_id, first_name, username,
    text, message_dt, state,
):
    stage = str((state or {}).get("stage") or "idle")
    greeted = bool((state or {}).get("greeted", False))
    greet = not greeted
    context = v2._ctx(state)

    # Все календарные стадии, подтверждение встречи, стоп-сигналы и прочие
    # детерминированные действия оставляем V2.
    if stage != "idle":
        return _ORIGINAL_V2_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    # Шутливое «я с роботом общаюсь 😂» тоже требует честного ответа.
    if _identity_question_v3(text):
        return _identity_reply(owner_name, first_name, greet), "idle", True, context

    # Всё, что имеет жёсткое правило или прямой мост к Валентине, не отдаём импровизации.
    if _hard_case(text, context, owner_name):
        return _ORIGINAL_V2_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    # Обычный живой разговор: одна сильная модель видит контекст и выбирает следующий шаг.
    result = _reasoned_turn(
        config, owner_name, first_name, text, greet, context
    )
    reply = legacy._de_repeat_reply(
        config, result["reply"], text, context
    )

    context["neona_v3_last_action"] = result["action"]
    if result["why"]:
        context["neona_v3_last_reason"] = result["why"]

    if result["action"] == "ask_meeting_consent":
        context["meeting_reason"] = "interest"
        return reply, "awaiting_meeting_consent", True, context

    if result["action"] == "close_gently":
        context["neona_v3_gentle_close_at"] = ""
        return reply, "idle", True, context

    return reply, "idle", True, context


def apply_policy():
    """Сначала поднимаем проверенную V2-политику, затем накладываем V3-характер."""
    try:
        v2.apply_policy()
    finally:
        v2._general_reply_v2 = _general_reply_v3
        v2._process_v2 = _process_v3

        legacy._general_reply = _general_reply_v3
        legacy._process_message_without_memory = _process_v3

        # V2-календарь и consent остаются источником истины.
        core._schedule_reply = v2._schedule_reply_v2
        core._meeting_intent = v2._meeting_intent_v2


# Накладываем V3 сразу при импорте.
apply_policy()


def initialize_dialog_after_first_message(*args, **kwargs):
    apply_policy()
    return v2.initialize_dialog_after_first_message(*args, **kwargs)


def run_sync_owner_once(*args, **kwargs):
    apply_policy()
    return v2.run_sync_owner_once(*args, **kwargs)


def worker_forever(*args, **kwargs):
    apply_policy()
    return v2.worker_forever(*args, **kwargs)


DialogError = v2.DialogError


def __getattr__(name):
    return getattr(v2, name)
