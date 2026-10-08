from __future__ import annotations

"""Неона 2.0 — деловая политика Агентства W поверх существующей инфраструктуры."""

import re
from datetime import datetime, timedelta
import neona_dialog_policy as legacy

core = legacy.core
_ORIG_PROCESS = legacy._process_message_without_memory
_ORIG_MEETING_INTENT = core._meeting_intent
_ORIG_COMMERCIAL = legacy._commercial_intent


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").casefold()).strip()


def _owner_forms(owner_name: str) -> dict[str, str]:
    return legacy._owner_forms(owner_name)


def _mentions_owner(text: str, owner_name: str) -> bool:
    forms = _owner_forms(owner_name)
    value = _norm(text)
    return any(
        form and form.casefold() in value
        for form in (
            forms["nominative"],
            forms["genitive"],
            forms["instrumental"],
        )
    )


def _ctx(state):
    raw = (state or {}).get("context")
    return dict(raw) if isinstance(raw, dict) else {}


def _last_reply(context):
    try:
        mem = legacy._relationship_memory(context)
    except Exception:
        mem = {}
    return str(
        (mem or {}).get("last_reply")
        or (mem or {}).get("last_reply_text")
        or context.get("last_reply_text")
        or ""
    ).strip()


def _yes(text):
    value = re.sub(r"[^a-zа-яё0-9]+", " ", _norm(text)).strip()
    return value in {
        "да", "да конечно", "конечно", "интересно", "да интересно",
        "хочу", "да хочу", "давайте", "можно", "согласен", "согласна",
        "готов", "готова", "ок", "okay",
    }


def _do_not_contact(text):
    v = _norm(text)
    return any(re.search(p, v) for p in (
        r"\bне\s+пишите\b", r"\bне\s+пиши\b",
        r"\bбольше\s+не\s+(?:пишите|пиши|беспокойте|беспокой)\b",
        r"\bоставьте\s+меня\s+в\s+покое\b", r"\bотстаньте\b",
    ))


def _decline(text):
    v = re.sub(r"[^a-zа-яё0-9 ]+", " ", _norm(text)).strip()
    exact = {
        "нет", "нет спасибо", "не интересно", "неинтересно",
        "мне не интересно", "мне это не интересно", "мне это не нужно",
        "не нужно", "не надо", "не хочу", "я уже решила", "я уже решил",
        "автоматизация не нужна", "мне автоматизация не нужна",
        "не хочу автоматизацию",
    }
    return v in exact or any(x in v for x in (
        "мне не нужна автоматизация", "автоматизация мне не нужна",
        "это меня не интересует",
    ))


def _wants_human(text, owner_name=""):
    v = _norm(text)
    generic = any(re.search(p, v) for p in (
        r"\bне\s+хочу\s+(?:разговаривать|общаться)\s+(?:с\s+)?(?:ии|искусственн\w+\s+интеллект\w*|машин\w*)\b",
        r"\bхочу\s+(?:поговорить|общаться)\s+(?:с\s+)?человек\w*\b",
        r"\bсоедините\s+(?:меня\s+)?(?:с\s+)?человек\w*\b",
        r"\bпозвоните\s+мне\b", r"\bперезвоните\s+мне\b",
    ))
    if generic:
        return True
    if owner_name and _mentions_owner(text, owner_name):
        return any(word in v for word in (
            "поговор", "созвон", "встрет", "соедин", "позвон", "напиш",
        ))
    return False


def _source_question(text):
    v = _norm(text)
    return any(x in v for x in (
        "где вы меня нашли", "откуда вы меня нашли",
        "откуда вы знаете чем я занимаюсь",
        "откуда вы знаете, чем я занимаюсь",
        "где нашли мой профиль", "откуда у вас информация обо мне",
    ))


def _guarantee_question(text):
    v = _norm(text)
    return any(x in v for x in (
        "что вы гарантируете", "какие гарантии", "гарантируете результат",
        "сколько людей вы мне найдете", "сколько людей вы мне найдёте",
        "сколько партнеров", "сколько партнёров", "сколько клиентов",
        "сколько я заработаю", "какой доход",
    ))


def _technical_question(text):
    v = _norm(text)
    return any(x in v for x in (
        "как система ищет", "как именно система ищет", "какие технологии",
        "как подключаются соцсет", "как подключить соцсет", "какие api",
        "как устроена система", "как устроено агентство технически",
        "что нужно для запуска", "как это подключается", "техническ",
    ))


def _cases_question(text):
    v = _norm(text)
    return any(x in v for x in (
        "покажите кейс", "покажите кейсы", "ваши кейсы",
        "покажите результат", "покажите результаты", "примеры работы",
        "какие результаты", "есть результаты", "есть кейсы",
        "можно кейсы", "можно примеры",
    ))


def _price_question(text):
    try:
        return bool(_ORIG_COMMERCIAL(text))
    except Exception:
        v = _norm(text)
        return any(x in v for x in ("сколько стоит", "стоимость", "цена", "тариф", "оплата"))


def _agency_explanation_question(text):
    v = _norm(text)
    return any(x in v for x in (
        "как работает агентство", "как работает ваше агентство",
        "что делает агентство", "что вы делаете", "чем вы можете помочь",
    ))


def _automation_interest(text, context):
    v = _norm(text)
    if _agency_explanation_question(text) and not any(
        x in v for x in ("можно посмотреть", "хочу посмотреть", "покажите как")
    ):
        return False
    if any(x in v for x in (
        "звучит интересно", "мне интересно", "интересна автоматизац",
        "интересует автоматизац", "хочу автоматиз",
        "хочу посмотреть как это работает", "можно посмотреть как это работает",
        "можно посмотреть, как это работает", "покажите как это работает",
        "покажите, как это работает", "хочу узнать про автоматизац",
        "готов посмотреть", "готова посмотреть", "давайте встреч",
    )):
        return True
    if _yes(text):
        prev = _norm(_last_reply(context))
        if any(x in prev for x in (
            "хотели бы вы тратить на поиск и переписку меньше времени",
            "актуально сократить время на поиск людей и переписку",
            "интересна автоматизация", "встретиться с валентиной",
            "организовать встречу с валентиной",
        )):
            return True
    return False


def _health_topic(text):
    v = _norm(text)
    health = (
        "болею", "болезн", "диагноз", "лекарств", "лечение", "лечить",
        "врач", "доктор", "боль", "давление", "сустав", "сердц",
        "анализы", "бад", "бады", "витамин", "таблет", "реабилитац",
    )
    advice = (
        "что делать", "что принимать", "посовет", "рекоменд", "как леч",
        "можно ли принимать", "как принимать", "чем леч",
    )
    return any(x in v for x in health) and (
        any(x in v for x in advice) or "?" in str(text or "") or len(v.split()) >= 5
    )


def _callback(text, owner_name=""):
    v = _norm(text)
    if any(x in v for x in (
        "позвоните мне", "перезвоните мне", "хочу звонок",
    )):
        return True
    return bool(
        owner_name
        and _mentions_owner(text, owner_name)
        and any(word in v for word in ("позвон", "перезвон"))
    )


def _explicit_owner_meeting_request(text, owner_name=""):
    """Прямой запрос на разговор/встречу/звонок с текущим владельцем кабинета."""
    v = _norm(text)
    if any(re.search(pattern, v) for pattern in (
        r"\bпозвоните\s+мне\b",
        r"\bперезвоните\s+мне\b",
        r"\bназнач(?:ьте|ить)\s+встреч\w*\b",
        r"\bдавайте\s+(?:встретимся|созвонимся)\b",
    )):
        return True
    if not owner_name or not _mentions_owner(text, owner_name):
        return False
    return any(word in v for word in (
        "поговор", "встрет", "созвон", "соедин", "позвон", "напиш",
    ))


def _meeting_consent_bridge(reason, owner_name):
    """Интерес к теме ещё не равен согласию на встречу."""
    forms = _owner_forms(owner_name)
    lead = {
        "price": (
            "По стоимости я не уполномочена давать точную информацию. "
            f"Этот вопрос лучше обсудить с {forms['instrumental']}."
        ),
        "technical": (
            "По технической части я не уполномочена консультировать. "
            f"Эти вопросы лучше задать {forms['dative'] if 'dative' in forms else forms['nominative']}."
        ),
        "cases": f"Кейсы, примеры и результаты лучше покажет {forms['nominative']}.",
        "interest": "Рада, что тема вам интересна.",
    }.get(reason, f"Этот вопрос лучше обсудить с {forms['instrumental']}.")
    return lead + f" Хотите, я организую вам встречу с {forms['instrumental']}?"


def _meeting_reason(text, context, owner_name=""):
    if _wants_human(text, owner_name):
        return "callback" if _callback(text, owner_name) else "human"
    if _price_question(text):
        return "price"
    if _technical_question(text):
        return "technical"
    if _cases_question(text):
        return "cases"
    if _automation_interest(text, context):
        return "interest"
    return ""


def _prelude(reason, owner_name):
    forms = _owner_forms(owner_name)
    owner = forms["nominative"]
    owner_with = forms["instrumental"]
    return {
        "price": f"По стоимости я не уполномочена давать точную информацию. Этот вопрос лучше обсудить с {owner_with}.",
        "technical": f"По технической части я не уполномочена консультировать. Эти вопросы лучше задать владельцу кабинета — {owner}.",
        "cases": f"Кейсы, примеры и результаты лучше покажет {owner} — и ответит на ваши вопросы.",
        "human": f"Конечно. {owner} сможет поговорить с вами лично и ответить на ваши вопросы.",
        "callback": f"Конечно. Я подберу время, когда {owner} сможет вам позвонить.",
        "interest": f"Отлично. Тогда подберу время для встречи с {owner_with}.",
    }.get(reason, f"Хорошо. Тогда подберу время для встречи с {owner_with}.")


def _fmt_msk(start_utc):
    msk = start_utc.astimezone(core.MSK)
    return f"{msk:%d.%m.%Y} в {msk:%H:%M} МСК"


def _find_slots(config, owner_id, after_utc=None, limit=3):
    now = datetime.now(core.UTC)
    earliest = max(after_utc or now, now + timedelta(minutes=60))
    start_day = earliest.astimezone(core.MSK).date()
    duration = int(getattr(core, "DURATION_MINUTES", 30))
    found = []
    for day_offset in range(31):
        day = start_day + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        cursor = datetime.combine(day, datetime.min.time(), core.MSK).replace(hour=10)
        end = datetime.combine(day, datetime.min.time(), core.MSK).replace(hour=20)
        while cursor + timedelta(minutes=duration) <= end:
            start = cursor.astimezone(core.UTC)
            finish = start + timedelta(minutes=duration)
            if start >= earliest:
                try:
                    free = legacy._slot_free_with_buffer(config, owner_id, start, finish)
                except Exception:
                    free = False
                if free:
                    found.append(start)
                    if len(found) >= limit:
                        return found
            cursor += timedelta(minutes=30)
    return found


def _offer_slots(config, owner_id, owner_name, context, reason="", after_utc=None, prefix=""):
    context = dict(context or {})
    context["contact_timezone"] = "Europe/Moscow"
    if reason:
        context["meeting_reason"] = reason
    slots = _find_slots(config, owner_id, after_utc=after_utc, limit=3)
    if not slots:
        return (
            prefix + (_prelude(reason, owner_name) + " " if reason else "")
            + "В ближайшем рабочем окне свободного времени не нашлось. "
              "Встречу можно будет согласовать позже.",
            "idle", context,
        )
    context["offered_slots"] = [x.isoformat() for x in slots]
    context.pop("proposed_start_at", None)
    context.pop("meeting_format", None)
    options = "\n".join(f"{i}. {_fmt_msk(x)}" for i, x in enumerate(slots, 1))
    lead = (_prelude(reason, owner_name) + "\n") if reason else ""
    return (
        prefix + lead + f"У {_owner_forms(owner_name)['genitive']} сейчас свободны такие варианты:\n"
        + options + "\nКакой вариант вам подходит?",
        "awaiting_slot_choice", context,
    )


def _slot_choice(text, slots):
    v = _norm(text)
    rules = {
        0: (r"^\s*1\s*$", r"\bперв(?:ый|ое|ого)\b"),
        1: (r"^\s*2\s*$", r"\bвтор(?:ой|ое|ого)\b"),
        2: (r"^\s*3\s*$", r"\bтрет(?:ий|ье|ьего)\b"),
    }
    for idx, pats in rules.items():
        if idx < len(slots) and any(re.search(p, v) for p in pats):
            return idx
    for idx, raw in enumerate(slots):
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(core.MSK)
        except Exception:
            continue
        if f"{dt:%H:%M}" in v:
            return idx
    return None


def _create_meeting(config, owner_id, owner_name, contact_id, first_name, username, context, fmt):
    raw = str(context.get("proposed_start_at") or "")
    start = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(core.UTC)
    duration = int(getattr(core, "DURATION_MINUTES", 30))
    finish = start + timedelta(minutes=duration)
    if not legacy._slot_free_with_buffer(config, owner_id, start, finish):
        return None

    zoom_link, zoom_note = "", ""
    if fmt == "Zoom":
        try:
            zoom_link, zoom_note = core._load_stagirite_zoom_link(config, owner_id)
        except Exception:
            pass

    channel = str(context.get("channel") or "telegram").casefold()
    source = {
        "telegram": "Неона 2.0 — Telegram диалог",
        "instagram": "Неона 2.0 — Instagram диалог",
        "vk": "Неона 2.0 — VK диалог",
    }.get(channel, "Неона 2.0 — диалог")
    reason = str(context.get("meeting_reason") or "")
    owner_forms = _owner_forms(owner_name)
    notes = {
        "price": f"Вопрос о стоимости/условиях — мостик к {owner_forms['nominative']}.",
        "technical": f"Технический вопрос — мостик к {owner_forms['nominative']}.",
        "cases": f"Запрос кейсов/примеров — мостик к {owner_forms['nominative']}.",
        "human": "Человек предпочёл разговор с человеком.",
        "callback": f"Человек попросил звонок {owner_forms['genitive']}.",
        "interest": "Человек проявил интерес к автоматизации Агентства W.",
    }.get(reason, "Назначено Неоной 2.0 после выбора времени и формата.")

    payload = {
        "owner_telegram_id": int(owner_id),
        "owner_name": owner_name,
        "contact_name": first_name or "Без имени",
        "contact_username": username or None,
        "contact_city": "МСК",
        "contact_timezone": "Europe/Moscow",
        "start_at": start.isoformat(),
        "end_at": finish.isoformat(),
        "meeting_format": fmt,
        "meeting_link": zoom_link or None,
        "status": "Подтверждена",
        "notes": notes,
        "source": source,
    }
    try:
        payload["contact_telegram_id"] = int(contact_id)
    except Exception:
        pass

    created = core._create_meeting(config, payload)
    context["meeting_id"] = created.get("id")
    context["meeting_format"] = fmt
    context["meeting_confirmed_at"] = datetime.now(core.UTC).isoformat()
    return created, start, zoom_link, zoom_note


def _schedule_reply_v2(
    config, owner_id, owner_name, contact_id, first_name, username,
    text, message_dt, stage, context, greet,
):
    """Интерес → 3 слота МСК → выбор → формат → сразу запись."""
    context = dict(context or {})
    context["contact_timezone"] = "Europe/Moscow"
    prefix = f"{core._greeting(first_name)} " if greet else ""
    reason = str(context.get("meeting_reason") or "")

    if stage not in {"awaiting_slot_choice", "collecting_meeting_details", "awaiting_confirmation"}:
        return _offer_slots(config, owner_id, owner_name, context, reason=reason, prefix=prefix)

    if stage == "awaiting_slot_choice":
        slots = context.get("offered_slots") or []
        slots = slots if isinstance(slots, list) else []
        v = _norm(text)

        if any(x in v for x in (
            "не подходит", "не подходят", "ни один", "другие варианты",
            "другое время", "следующие варианты", "неудобно",
        )):
            after = None
            if slots:
                try:
                    after = datetime.fromisoformat(
                        str(slots[-1]).replace("Z", "+00:00")
                    ).astimezone(core.UTC) + timedelta(minutes=30)
                except Exception:
                    pass
            return _offer_slots(config, owner_id, owner_name, context, after_utc=after, prefix=prefix)

        choice = _slot_choice(text, slots)
        if choice is None:
            return (
                prefix + "Выберите, пожалуйста, один из предложенных вариантов — 1, 2 или 3.",
                "awaiting_slot_choice", context,
            )

        selected = str(slots[choice])
        context["proposed_start_at"] = selected
        context.pop("offered_slots", None)
        fmt = core._detect_format(text)
        if fmt:
            context["meeting_format"] = fmt
        else:
            start = datetime.fromisoformat(selected.replace("Z", "+00:00")).astimezone(core.UTC)
            return (
                prefix + f"Отлично, выбрали {_fmt_msk(start)}. "
                "Как вам удобнее встретиться — Zoom, Telegram или WhatsApp?",
                "collecting_meeting_details", context,
            )

    if context.get("proposed_start_at"):
        fmt = core._detect_format(text) or str(context.get("meeting_format") or "")
        if fmt not in {"Zoom", "Telegram", "WhatsApp"}:
            return (
                prefix + "Как вам удобнее встретиться — Zoom, Telegram или WhatsApp?",
                "collecting_meeting_details", context,
            )

        context["meeting_format"] = fmt
        made = _create_meeting(
            config, owner_id, owner_name, contact_id, first_name, username, context, fmt
        )
        if made is None:
            after = datetime.fromisoformat(
                str(context["proposed_start_at"]).replace("Z", "+00:00")
            ).astimezone(core.UTC) + timedelta(minutes=30)
            context.pop("proposed_start_at", None)
            context.pop("meeting_format", None)
            reply, new_stage, context = _offer_slots(
                config, owner_id, owner_name, context, after_utc=after, prefix=prefix
            )
            return "Пока мы выбирали формат, это время стало занято.\n" + reply, new_stage, context

        created, start, zoom_link, zoom_note = made
        owner_forms = _owner_forms(owner_name)
        if str(context.get("meeting_reason") or "") == "callback":
            answer = f"Договорились. {owner_forms['nominative']} позвонит вам {_fmt_msk(start)}. Формат — {fmt}."
        else:
            answer = f"Договорились. Встреча с {owner_forms['instrumental']} записана на {_fmt_msk(start)}. Формат — {fmt}."

        if fmt == "Zoom" and zoom_link:
            answer += f" Ссылка Zoom: {zoom_link}."
            if zoom_note:
                answer += f" {zoom_note}"

        farewell = "Хорошего вам дня!" if datetime.now(core.MSK).hour < 18 else "Хорошего вам вечера!"
        return answer + " " + farewell, "scheduled", context

    return _offer_slots(config, owner_id, owner_name, context, reason=reason, prefix=prefix)


def _open_door_close():
    return (
        "Поняла вас. Двери Агентства W всегда открыты. "
        "Если когда-нибудь захотите сократить время на поиск людей и переписку "
        "и передать часть рутины системе, Агентство W будет к вашим услугам. "
        "Всего доброго!"
    )


def _general_reply_v2(config, owner_name, first_name, text, greet, context=None):
    context = dict(context or {})
    forms = _owner_forms(owner_name)
    history = legacy._dialog_context_block(context, max_turns=10)
    greeting = (
        f"Если это первое сообщение, можно начать с «{core._greeting(first_name)}»."
        if greet else "Не повторяй приветствие."
    )
    instructions = f"""
Ты Неона — секретарь-референт {forms['genitive']} в Агентстве W.
Ты ведёшь короткий, живой и деловой разговор. Ты НЕ универсальный консультант.

ЖИВОЙ КОНТЕКСТ:
{history}

ГЛАВНАЯ ЛИНИЯ:
бизнес → люди → сколько времени уходит → что человек сделал бы с освобождённым временем →
«А хотели бы вы тратить на поиск и переписку меньше времени?» → при интересе встреча с {forms['instrumental']}.

ПРАВИЛА:
- После ответа на комментарий к Story/Reels/посту достаточно 1–2 вежливых реплик,
  затем естественно спроси: «А чем вы занимаетесь в интернете?»
- Если уже известно, что у человека есть бизнес, не спрашивай это повторно.
  Подведи смысл: бизнесу нужны люди, и переходи к ВРЕМЕНИ.
- Не устраивай анкету «где ищете / как ищете».
- Выясняй, сколько времени уходит на поиск, переписку, возражения, приглашения на встречи,
  первые переговоры и сопровождение.
- Ключевой вопрос: «А если бы у вас освободилось несколько часов в неделю,
  на что вам хотелось бы их потратить?»
- Не спрашивай сама про детей, внуков, семью или хобби. Если человек сам назвал — поддержи.
- Потом естественно: «А хотели бы вы тратить на поиск и переписку меньше времени?»
- Если бизнеса нет — не продавай. Доброжелательно оставь дверь открытой.

ЕСЛИ СПРОСИЛИ, КАК РАБОТАЕТ АГЕНТСТВО:
Скажи коротко: сначала определяется целевая аудитория именно под проект клиента —
партнёры, клиенты или другие нужные люди. Затем система ищет таких людей по открытым
источникам в интернете, ведёт первые диалоги и согласовывает встречу заинтересованного
человека с владельцем бизнеса.
Не перечисляй Неонию, Неолу, Стагирита. Для клиента это «система Агентства W».
Можно сказать, что часть сопровождения нового партнёра тоже можно передать системе.

ЕСЛИ УЖЕ ЕСТЬ КОМАНДА:
это не конец разговора. Можно поинтересоваться, сколько времени у людей команды уходит
на поиск и переписку. Если интересно команде — {forms['nominative']} может встретиться с командой.
Если человек хочет сначала посмотреть сам — сначала личная встреча.

ГРАНИЦЫ:
- Неона не даёт юридических, финансовых, криптовалютных, рекламных и иных
  посторонних консультаций.
- МЕДИЦИНСКУЮ ТЕМУ ЗДЕСЬ ВООБЩЕ НЕ УПОМИНАЙ. Отдельная программная проверка
  сама включит медицинскую границу только если текущее сообщение действительно о здоровье.
- Цена, техника, кейсы/результаты — вопросы к {forms['nominative']}.
- Не обещай доход, количество партнёров/клиентов или гарантированный результат.
- Не придумывай чек-листы, планы, файлы и помощь «от себя».
- Если спрашивают, где нашли: «По открытой информации в интернете.»
- Если прямо спрашивают, ИИ ли ты: честно скажи, что да, ты искусственный интеллект,
  секретарь-референт {forms['genitive']}.
- Если человеку приятнее говорить с человеком — сразу мост к {forms['nominative']}.
- Явный отказ — закончить без спора и без нового вопроса.

СТИЛЬ:
- уже начавшийся диалог — обычно 1–2 коротких предложения;
- максимум один смысловой вопрос;
- никаких «Факт:», лекций, допросов и нескольких вопросов подряд;
- следующая реплика рождается из последнего ответа человека;
- имена внутренних агентов не использовать;
- не называй себя ботом;
- {greeting}

Верни только готовую реплику человеку.
""".strip()
    reply = legacy._call_openai(config, instructions, text)
    return legacy._de_repeat_reply(config, reply, text, context)


def _meeting_intent_v2(text):
    if _wants_human(text) or _callback(text):
        return True
    try:
        return bool(_ORIG_MEETING_INTENT(text))
    except Exception:
        return False


def _process_v2(
    config, owner_id, owner_name, contact_id, first_name, username,
    text, message_dt, state,
):
    stage = str((state or {}).get("stage") or "idle")
    greeted = bool((state or {}).get("greeted", False))
    greet = not greeted
    context = _ctx(state)
    prefix = f"{core._greeting(first_name)} " if greet else ""

    if stage == "scheduled":
        return _ORIG_PROCESS(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, state
        )

    if _do_not_contact(text):
        context["contact_boundary"] = "do_not_contact"
        context["contact_boundary_at"] = datetime.now(core.UTC).isoformat()
        return prefix + "Поняла. Больше писать вам не буду. Всего доброго.", "opted_out", True, context

    # Неона уже предложила встречу: сначала ждём явного согласия.
    if stage == "awaiting_meeting_consent":
        if _yes(text):
            reply, new_stage, context = _schedule_reply_v2(
                config, owner_id, owner_name, contact_id, first_name, username,
                text, message_dt, "invited_to_meeting", context, greet
            )
            return reply, new_stage, True, context

        if _decline(text) or any(x in _norm(text) for x in ("не сейчас", "позже", "пока нет")):
            return prefix + _open_door_close(), "opted_out", True, context

        # Нет ясного «да» — календарь НЕ показываем.
        reply = _general_reply_v2(config, owner_name, first_name, text, greet, context)
        return reply, "awaiting_meeting_consent", True, context

    reason = _meeting_reason(text, context, owner_name)
    if reason:
        context["meeting_reason"] = reason

        # Только прямой запрос на Валентину уже сам является согласием.
        if _explicit_owner_meeting_request(text, owner_name):
            reply, new_stage, context = _schedule_reply_v2(
                config, owner_id, owner_name, contact_id, first_name, username,
                text, message_dt, "invited_to_meeting", context, greet
            )
            return reply, new_stage, True, context

        # Интерес / цена / техника / кейсы — сначала спросить согласие на встречу.
        if reason in {"interest", "price", "technical", "cases"}:
            return (
                prefix + _meeting_consent_bridge(reason, owner_name),
                "awaiting_meeting_consent",
                True,
                context,
            )

        # Просьба именно поговорить с человеком/получить звонок — явный запрос контакта.
        reply, new_stage, context = _schedule_reply_v2(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, "invited_to_meeting", context, greet
        )
        return reply, new_stage, True, context

    # Если календарь уже начат, Неона не возвращается к старой анкете
    # (дата/время/часовой пояс/подтверждение), а ведёт новый сценарий:
    # слот МСК -> формат -> запись.
    if stage in {"invited_to_meeting", "awaiting_slot_choice", "collecting_meeting_details", "awaiting_confirmation"}:
        v = _norm(text)
        if any(x in v for x in ("встреча не нужна", "не хочу встречу", "не хочу встречаться")):
            return prefix + _open_door_close(), "opted_out", True, context
        reply, new_stage, context = _schedule_reply_v2(
            config, owner_id, owner_name, contact_id, first_name, username,
            text, message_dt, stage, context, greet
        )
        return reply, new_stage, True, context

    if stage == "idle" and _decline(text):
        context["contact_boundary"] = "business_declined"
        context["contact_boundary_at"] = datetime.now(core.UTC).isoformat()
        return prefix + _open_door_close(), "opted_out", True, context

    if stage == "idle" and _identity_question(text):
        forms = _owner_forms(owner_name)
        return (
            prefix + f"Да, я искусственный интеллект, секретарь-референт {forms['genitive']}. "
            f"Если вам удобнее поговорить с человеком, я могу согласовать встречу с {forms['instrumental']}.",
            "idle", True, context,
        )

    if stage == "idle" and _source_question(text):
        return prefix + "По открытой информации в интернете.", "idle", True, context

    if stage == "idle" and _guarantee_question(text):
        return (
            prefix + "Гарантировать количество партнёров, клиентов или доход мы не можем — "
            "слишком многое зависит от самого проекта и людей. Но мы можем взять на себя "
            "значительную часть рутины: поиск вашей целевой аудитории, первые диалоги "
            "и согласование встреч, освобождая ваше время.",
            "idle", True, context,
        )

    if stage == "idle" and _health_topic(text):
        return (
            prefix + "Понимаю. По вопросам здоровья я не даю медицинских рекомендаций — "
            "лучше обратиться к врачу. А чем вы занимаетесь в интернете?",
            "idle", True, context,
        )

    return _ORIG_PROCESS(
        config, owner_id, owner_name, contact_id, first_name, username,
        text, message_dt, state
    )


# Подмена только политики; инфраструктура остаётся старой и проверенной.
legacy._general_reply = _general_reply_v2
legacy._respectful_stop_reply = _open_door_close
legacy._repeat_objection_close = _open_door_close
legacy._process_message_without_memory = _process_v2
core._schedule_reply = _schedule_reply_v2
core._meeting_intent = _meeting_intent_v2

apply_policy = legacy.apply_policy
initialize_dialog_after_first_message = legacy.initialize_dialog_after_first_message
run_sync_owner_once = legacy.run_sync_owner_once
worker_forever = legacy.worker_forever
DialogError = getattr(legacy, "DialogError", getattr(core, "DialogError", RuntimeError))


def __getattr__(name):
    return getattr(legacy, name)
