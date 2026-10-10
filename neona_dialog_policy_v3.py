from __future__ import annotations

"""
Неона 3.0 — «легко, интересно, приятно».

Архитектура:
- GPT-6.1 Sol является основным смысловым мозгом обычного диалога;
- neona_dialog_policy_v2.py остаётся только исполнительным слоем для календаря,
  жёсткого запрета контакта и других реальных действий;
- слова-триггеры не имеют права заменять понимание смысла;
- safety-правила работают как ограждения ПОСЛЕ понимания контекста, а не как
  набор шаблонных ответов;
- календарь и реальные действия остаются детерминированными и не отдаются
  модели на фантазию.
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
                # Логируем фактически использованную модель без содержания переписки:
                # иначе по одним настройкам невозможно понять, сработал ли fallback.
                print(f"[NeonaModel] selected={model}", flush=True)
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


def _must_use_deterministic_policy(text: str) -> bool:
    """Только настоящая жёсткая граница обходит смысловой слой.

    Отказ от конкретного предложения, вопрос об источнике, здоровье, цене,
    ИИ или встрече — это всё ещё разговор и должно пониматься по смыслу.
    Детерминированно обрабатываем только прямой запрет дальнейшего контакта.
    """
    return bool(v2._do_not_contact(text))


def _repair_false_legacy_stage(stage: str, context: dict) -> tuple[str, dict]:
    """Снимает старую ложную стадию встречи, созданную keyword-скриптом."""
    context = dict(context or {})
    if stage not in {"invited_to_meeting", "awaiting_meeting_consent"}:
        return stage, context

    mem = legacy._relationship_memory(context)
    previous_incoming = str(mem.get("last_incoming") or "").strip()
    previous_reply = str(mem.get("last_reply") or "").strip()

    if (
        previous_incoming
        and not legacy._commercial_intent(previous_incoming)
        and "по стоимости" in previous_reply.casefold()
    ):
        context = legacy._clear_meeting_context(context)
        context.pop("commercial_interest_at", None)
        context.pop("meeting_reason", None)
        context["semantic_stage_recovered"] = True
        return "idle", context

    return stage, context


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

ГЛАВНЫЙ ПРИНЦИП — СНАЧАЛА СМЫСЛ, ПОТОМ ЦЕЛЬ:
Не отвечай на ключевые слова. Отвечай на СМЫСЛ последней реплики в контексте разговора.
Перед ответом молча пойми:
- на что именно человек отвечает;
- что он сейчас делает: рассказывает о себе, отвечает на вопрос, предлагает свою услугу,
  делится ссылкой, шутит, сомневается, интересуется, отказывает или задаёт вопрос;
- что человек ХОЧЕТ от этого сообщения, если вообще чего-то хочет;
- что уже известно и что нельзя спрашивать повторно;
- какой ОДИН следующий шаг естественен — или что следующий шаг сейчас вообще не нужен.

КРИТИЧЕСКИ:
- слова «бесплатно», «без оплаты», «ИИ», «здоровье», «клиенты», «встреча» и другие
  НЕ являются намерением сами по себе;
- если человек рассказывает О СВОЁМ продукте, тесте, работе или услуге — не превращай
  это в вопрос о цене/услугах Агентства W;
- если человек прислал ссылку, поблагодари за источник, но НЕ обещай «зайти»,
  «посмотреть», «изучить» или «прочитать», если система реально этого не сделала;
- если сообщение содержательное, ответ должен опираться хотя бы на ОДНУ конкретную
  деталь из него. Нельзя отвечать универсальной заготовкой, подходящей к любому человеку;
- не вытаскивай старую тему из памяти, если последняя реплика явно о другом;
- не веди к Агентству W в каждой реплике: сначала будь нормальным собеседником.

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
- Если человек просто работает с продуктами/темами здоровья, это НЕ медицинский вопрос.
  Ограничение про врача нужно только когда человек реально просит медицинский совет,
  диагноз, лечение или оценку здоровья.
- Не консультируй по праву, финансам, криптовалютам, медицине, рекламе и чужой стратегии.
- Не обещай доход, количество клиентов/партнёров и гарантированный результат.
- Не придумывай цены, кейсы, факты, файлы, услуги и действия.
- Не говори, что уже что-то записала/передала/отправила, если действие не выполнено системой.
- Если прямо спрашивают, человек ли ты / ИИ ли ты — ответь честно и спокойно:
  ты Неона, цифровой секретарь-референт {forms['genitive']}, работающий с ИИ.
  НЕ говори «вы меня раскусили», не шути про разоблачение и не делай из этого событие.
- Если НЕ спрашивают о твоей природе — вообще не поднимай тему ИИ.
- Не называй внутренние имена ИИ-агентов без необходимости.
- Не дави на семью, возраст, страх потери времени или деньги.
- Не задавай несколько вопросов подряд.
- Не заканчивай каждую реплику вопросом автоматически.
- {greeting_rule}

СТАНДАРТ НЕОНЫ 4.0 — ИНТЕЛЛЕКТ БЕЗ ШАБЛОНОВ:
- Цель Агентства W помни всегда: полезное знакомство, доверие,
  осмысленный интерес к возможностям и добровольная встреча с владельцем.
- Стратегия не означает давление: при отказе прекращай предложение,
  при отсутствии интереса не маскируй продажи под дружескую беседу.
- Учитывай индивидуальные детали и тон конкретного собеседника,
  не повторяй заученные одинаковые вопросы и рекламные обороты.
- На сложный вопрос ответь простым языком, сохрани точность,
  при неопределённости честно обозначь её.
- Комментарий к публикации опирай на содержание самой публикации,
  не выдумывай, что видела фото, сторис или прочитала ссылку.
- Владельцы кабинетов различны: говори только от имени владельца
  ТЕКУЩЕГО разговора и не переноси чужие имена и истории.
- Остроумие — инструмент, не обязанность; при тревоге, боли,
  серьёзных возражениях или отказе шутка неуместна.

ЮМОР / ЭРУДИЦИЯ:
Можно добавить ОДНУ маленькую человеческую искру: лёгкую шутку, метафору
или очень редкую латинскую фразу с переводом — только если это действительно к месту.
Не превращай ответ в выступление и не демонстрируй эрудицию ради эрудиции.

ФОРМАТ ОТВЕТА:
Верни ТОЛЬКО JSON, без markdown:
{{
  "reply": "готовая реплика человеку, обычно 1–3 коротких предложения",
  "intent": "share | answer | question | offer | interest | objection | identity | meeting | close | other",
  "action": "continue | ask_meeting_consent | schedule_now | close_gently",
  "why": "очень коротко, 3–8 слов, только для внутреннего журнала"
}}

action:
- continue — обычный живой разговор;
- ask_meeting_consent — встреча ещё НЕ согласована, но естественно спросить согласие;
- schedule_now — человек САМ прямо попросил встречу/созвон или уже явно согласился;
- close_gently — разговор естественно завершён, но человек НЕ просил запретить контакт.

Никогда не показывай человеку поля why/intent и внутренний анализ.
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
    intent = str(data.get("intent") or "other").strip().lower()
    action = str(data.get("action") or "continue").strip()
    why = str(data.get("why") or "").strip()[:120]

    if intent not in {
        "share", "answer", "question", "offer", "interest",
        "objection", "identity", "meeting", "close", "other",
    }:
        intent = "other"

    if action not in {
        "continue", "ask_meeting_consent", "schedule_now", "close_gently"
    }:
        action = "continue"

    # Если модель не соблюла JSON, безопасно используем текст как обычную реплику.
    if not reply:
        reply = raw.strip()
        action = "continue"

    # Старые реплики в памяти могли содержать «Валентина». В кабинете другого
    # владельца это не должно менять того, от чьего имени работает Неона.
    reply = v2._rewrite_wrong_owner_reference(reply, owner_name)

    # Семантические предохранители: не позволяем старым темам/ключевым словам
    # просочиться в ответ, если человек их сейчас не спрашивал.
    if not v2._health_topic(text):
        if re.search(r"\b(?:врач|доктор|медицин|лечени|лекарств|здоровь)\w*\b", reply, flags=re.I):
            reply = _repair_reply(
                config, owner_name, text, reply,
                "В текущем сообщении нет медицинского вопроса. Убери медицинскую тему и ответь на реальный смысл сообщения.",
            )

    if (
        not legacy._commercial_intent(text)
        and re.search(r"\b(?:стоимост|цен[аеуы]|оплат|тариф)\w*\b", reply, flags=re.I)
    ):
        reply = _repair_reply(
            config, owner_name, text, reply,
            "Человек не спрашивал цену или оплату Агентства. Не придумывай коммерческий вопрос; ответь на то, что человек реально сообщил.",
        )
        if action in {"ask_meeting_consent", "schedule_now"}:
            action = "continue"

    if (
        not _identity_question_v3(text)
        and re.search(r"\b(?:искусственн\w+\s+интеллект|я\s+ии|я\s+бот|я\s+робот)\b", reply, flags=re.I)
    ):
        reply = _repair_reply(
            config, owner_name, text, reply,
            "Человек не спрашивал, ИИ ли ты. Не поднимай тему своей природы; ответь на смысл его сообщения.",
        )

    if re.search(
        r"\b(?:загляну|посмотрю|изучу|прочитаю|перейду\s+по\s+ссылке)\b",
        reply,
        flags=re.I,
    ) and re.search(r"https?://|t\.me/", str(text or ""), flags=re.I):
        reply = _repair_reply(
            config, owner_name, text, reply,
            "Не обещай открыть или изучить присланную ссылку. Поблагодари за источник и продолжи разговор только по доступному тексту.",
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

    return {"reply": reply, "intent": intent, "action": action, "why": why}


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
    state = dict(state or {})
    stage = str(state.get("stage") or "idle")
    greeted = bool(state.get("greeted", False))
    greet = not greeted
    context = v2._ctx(state)

    # Лечим старые ложные стадии, созданные keyword-скриптами.
    stage, context = _repair_false_legacy_stage(stage, context)
    state["stage"] = stage
    state["context"] = context

    # После реального согласия календарь остаётся детерминированным.
    if stage in {
        "awaiting_meeting_consent",
        "invited_to_meeting",
        "awaiting_slot_choice",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "scheduled",
    }:
        return _ORIGINAL_V2_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    # Только прямой запрет дальнейшего контакта имеет право обойти смысловой мозг.
    if _must_use_deterministic_policy(text):
        return _ORIGINAL_V2_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    # ВСЕ остальные обычные реплики — включая цену, здоровье как сферу бизнеса,
    # вопросы об ИИ, рассказы о себе, ссылки, возражения и интерес — сначала
    # понимает сильная модель в полном контексте.
    result = _reasoned_turn(
        config, owner_name, first_name, text, greet, context
    )
    reply = legacy._de_repeat_reply(
        config, result["reply"], text, context
    )

    context["neona_v3_last_intent"] = result.get("intent") or "other"
    context["neona_v3_last_action"] = result["action"]
    if result["why"]:
        context["neona_v3_last_reason"] = result["why"]

    print(
        "[NeonaBrain] "
        f"owner={int(owner_id)} contact={int(contact_id)} "
        f"intent={context['neona_v3_last_intent']} "
        f"action={result['action']}",
        flush=True,
    )

    if result["action"] == "schedule_now":
        # Человек сам прямо попросил встречу/созвон или уже согласился.
        return _ORIGINAL_V2_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    if result["action"] == "ask_meeting_consent":
        context["meeting_reason"] = "semantic_interest"
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
