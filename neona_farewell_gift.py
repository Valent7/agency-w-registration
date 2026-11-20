"""A single optional time-calculator farewell for Neona (Telegram, VK, Instagram).

Only a gentle refusal of the OFFER, never a refusal of contact or a message
sent later. The human may refuse without a follow-up. The referral is looked
up for the current verified partner; never guess another person's code.
"""
from __future__ import annotations

import re
from typing import Any

import requests

CALCULATOR_BASE_URL = "https://agency-w-time.onrender.com/"
GIFT_FLAG = "neona_farewell_gift_attempted"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").casefold().replace("ё", "е")).strip()


def is_hard_stop(text: str) -> bool:
    """Never pitch a gift to someone who forbids further messages."""
    t = _norm(text)
    return any(re.search(pattern, t) for pattern in (
        r"\bне\s+пиши(?:те)?\b",
        r"\bбольше\s+не\s+(?:пиши(?:те)?|звони(?:те)?|беспокой(?:те)?)\b",
        r"\b(?:перестань(?:те)?|прекрати(?:те)?)\s+(?:мне\s+)?писать\b",
        r"\b(?:не\s+надо|не\s+нужно)\s+(?:мне\s+)?писать\b",
        r"\bоставь(?:те)?\s+меня\s+в\s+покое\b",
        r"\bотстань(?:те)?\b",
        r"\bне\s+хочу\s+(?:с\s+(?:вами|тобой)\s+)?(?:общаться|разговаривать|переписываться)\b",
        r"\bмне\s+не\s*интересно\s+(?:с\s+(?:вами|тобой)\s+)?общаться\b",
        r"\bне\s*интересно\s+общаться\s+(?:с\s+(?:вами|тобой))\b",
        r"\bудалите\s+(?:мой\s+)?(?:номер|контакт)\b",
        r"\b(?:заблокирую|спам|жалоба)\b",
    ))


def polite_offer_refusal(text: str, stage: str = "idle") -> bool:
    """High-confidence declines only. A question about rejection is not one."""
    t = _norm(text)
    if not t or is_hard_stop(t) or "?" in t:
        return False
    if re.search(r"\b(?:не\s+отказываюсь|не\s+против|хочу\s+узнать|интересно\s+узнать)\b", t):
        return False
    compact = re.sub(r"[^a-zа-я0-9 ]+", " ", t).strip()
    simple = {
        "нет спасибо", "нет благодарю", "пока нет", "пока нет спасибо",
        "мне это не нужно", "мне не интересно", "мне неинтересно",
        "мне это не интересно", "мне это неинтересно",
        "не интересно", "неинтересно", "не надо", "не нужно",
        "сейчас не нужно", "сейчас не интересует", "мне не подходит",
        "это меня не интересует", "не хочу", "я пока не готова",
        "я пока не готов", "спасибо не интересно", "спасибо неинтересно",
    }
    if compact in simple:
        return True
    if compact in {"нет", "нет не хочу", "не хочу спасибо", "спасибо нет"}:
        return stage in {"awaiting_meeting_consent", "invited_to_meeting"}
    patterns = (
        r"\b(?:пока|сейчас)\s+не\s+готов(?:а)?\b",
        r"\b(?:не\s+хочу|не\s+планирую)\s+(?:встреч|сотруднич|регистр|участв|покуп|подключ|начинать)\w*\b",
        r"\b(?:не\s+нужн|не\s+интересн|неинтересн)\w*\s+(?:мне\s+)?(?:сотрудничество|встреча|проект|агентство|автоматизация)\b",
        r"\b(?:мне\s+)?(?:это|ваше\s+предложение)\s+не\s+подходит\b",
        r"\b(?:пока|сейчас)\s+не\s+интересует\b",
    )
    return any(re.search(pattern, compact) for pattern in patterns)


def _verified_partner_url(config: Any, owner_id: int) -> str | None:
    """Do not leak a referral or misattribute a visitor to a different partner."""
    try:
        base = str(config.supabase_url).rstrip("/")
        key = str(config.supabase_secret_key)
        if not (base.startswith("https://") and key and int(owner_id) > 0):
            return None
        response = requests.get(
            base + "/rest/v1/agency_members",
            headers={"apikey": key, "Authorization": "Bearer " + key},
            params={"telegram_id": "eq." + str(int(owner_id)),
                    "select": "telegram_id,member_code", "limit": 1},
            timeout=6,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        row = rows[0]
        code = str(row.get("member_code") or "").strip()
        if int(row.get("telegram_id") or 0) != int(owner_id):
            return None
        if re.fullmatch(r"W[1-9][0-9]{4,14}", code):
            return CALCULATOR_BASE_URL + "?ref=" + code
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    return None


def closing_gift(
    text: str, stage: str, context: dict[str, Any], config: Any, owner_id: int
) -> tuple[str, dict[str, Any]] | None:
    """One reply to a polite NO. No gift at explicit STOP. No retries or follow-ups."""
    if not polite_offer_refusal(text, stage):
        return None
    ctx = dict(context or {})
    if ctx.get(GIFT_FLAG) or ctx.get("neona_time_calculator_offered"):
        return "Понимаю, благодарю за ответ. Всего вам доброго!", ctx

    ctx[GIFT_FLAG] = True
    url = _verified_partner_url(config, owner_id)
    if not url:
        # Sending an untagged link would silently assign a partner's contact
        # to the director. Prefer a polite farewell to wrong attribution.
        return "Понимаю вас, спасибо, что ответили. Всего вам доброго!", ctx

    ctx["neona_time_calculator_offered"] = True
    return (
        "Понимаю, благодарю за ответ! На прощание — маленький подарок "
        "без регистрации и обязательств: " + url + "\n"
        "Калькулятор считает часы, а не уговаривает 🙂. Хорошего дня!",
        ctx,
    )
