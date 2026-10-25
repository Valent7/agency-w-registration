"""Утренние «Мысли вслух» Оры. Только WREMYA, отдельный сервис."""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

log = logging.getLogger("ora_pilot.morning")
MSK = ZoneInfo("Europe/Moscow")
AUDIENCE = "ora-wremya-pilot.onrender.com"
WORKFLOW = "Valent7/agency-w-registration/.github/workflows/ora-morning.yml@refs/heads/main"
FIRST_DAY = datetime(2026, 10, 24, tzinfo=MSK).date()
POSTED = {}
PREVIEWS = {}
LAST_ATTEMPT = None
PUBLISH_LOCK = asyncio.Lock()

# Editorial, verifiable scientific starting points. They are not purported Hawking quotes.
THEMES = [
    ("Что такое время?", "В физике время помогает описывать порядок и длительность событий. Часы измеряют интервалы, а не создают время."),
    ("Почему мы помним вчера, но не завтра?", "Хокинг описывает психологическую стрелу времени: мы помним прошлое, но не будущее."),
    ("Почему разбитая чашка не собирается сама?", "В главе «Стрела времени» Хокинг связывает термодинамическую стрелу с ростом энтропии."),
    ("Везде ли часы идут одинаково?", "Согласно теории относительности скорость и гравитация могут влиять на показания часов."),
    ("Может ли Вселенная иметь начало?", "Хокинг обсуждал расширяющуюся Вселенную и сложность вопроса о её начале; простых окончательных ответов нет."),
    ("Почему час ожидания кажется долгим?", "Психологическое ощущение длительности может различаться; это не означает изменения физического хода часов."),
    ("Где искать своё время?", "Физические часы и жизненные приоритеты — разные вопросы; научные факты не предписывают, как жить."),
    ("Время и перемены", "Мы видим последовательность изменений и измеряем интервалы времени; это не делает само время живым существом."),
    ("Прошлое и будущее", "Хокинг различает несколько стрел времени, включая термодинамическую и психологическую."),
    ("Почему время ценно?", "Наука исследует физические процессы, но вопрос ценности собственного времени — человеческое размышление, не физический закон."),
]

TODAY_POST = (
    "☀️ Доброе утро, дорогие друзья!\n"
    "💭 МЫСЛИ ВСЛУХ | ВРЕМЯ\n\n"
    "Иногда мы говорим: «Сегодня совсем нет времени». А потом вспоминаем, "
    "что сутки никуда не исчезли. Исчезло наше внимание к тому, как мы их прожили.\n\n"
    "Стивен Хокинг в «Краткой истории времени» приглашал задуматься о природе времени. "
    "В физике с его помощью описывают порядок событий и их длительность. "
    "Но часы не ответят за нас на другой вопрос: чем наполнить отведённые нам минуты?\n\n"
    "Можно успеть десятки дел и не заметить улыбку близкого человека. "
    "А можно остановиться на минуту, посмотреть в окно и почувствовать: "
    "вот она, моя жизнь, не когда-нибудь потом, а сейчас.\n\n"
    "Сегодня мне хочется спросить вас: какой маленький момент вы бы хотели "
    "не пропустить, даже если дел невпроворот?\n\n"
    "Ваша Ора 💛\n"
    "Научная опора: С. Хокинг, «Краткая история времени»."
)

def authenticate(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401)
    try:
        token = authorization[7:].strip()
        key = PyJWKClient(
            "https://token.actions.githubusercontent.com/.well-known/jwks",
            cache_jwk_set=True, lifespan=3600
        ).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=AUDIENCE,
            issuer="https://token.actions.githubusercontent.com",
            options={"require": ["iss", "aud", "exp", "iat"]}
        )
    except (jwt.PyJWTError, ValueError, OSError, TimeoutError):
        raise HTTPException(status_code=403, detail="Unauthorised schedule") from None
    event = claims.get("event_name")
    if (claims.get("repository") != "Valent7/agency-w-registration"
        or claims.get("workflow_ref") != WORKFLOW
        or claims.get("ref") != "refs/heads/main"
        or str(claims.get("run_attempt")) != "1"
        or event not in ("schedule", "push")):
        raise HTTPException(status_code=403, detail="Not an approved workflow")
    today = datetime.now(MSK).date()
    if event == "push" and today != FIRST_DAY:
        raise HTTPException(status_code=403, detail="One-time launch has passed")
    return event

async def compose(day, generate):
    """One fresh daily thought, not a long lecture. Return text plus provenance."""
    index = (day - FIRST_DAY).days % len(THEMES)
    title, fact = THEMES[index]
    prompt = (
        f"Сегодня {day.isoformat()}, рубрика «МЫСЛИ ВСЛУХ | ВРЕМЯ» сообщества WREMYA. "
        f"Тема дня: {title}. Достоверная научная опора: {fact} "
        "Напиши 75–110 СЛОВ по-русски. Не урок, не лекция, не научная простыня. "
        "Начни с «☀️ Доброе утро!» и «💭 МЫСЛИ ВСЛУХ | ВРЕМЯ». "
        "Далее 2–3 коротких абзаца: одна любопытная мысль о времени, "
        "одно живое сравнение с обычной жизнью, один небанальный вопрос участникам. "
        "Мысль должна быть самостоятельной, тёплой, мудрой, без морализаторства. "
        "Отделяй научные факты Хокинга от собственных размышлений. "
        "Не называй придуманную фразу цитатой Хокинга, не приписывай ему мысли "
        "о финансах или счастье. Без рекламных обещаний, без длинных вступлений. "
        "Заканчивай «Ваша Ора 💛»."
    )
    try:
        text = (await generate(
            prompt,
            "Ты Ора — живой интеллектуальный собеседник сообщества WREMYA. "
            "Пиши компактно и оригинально; научные тезисы проверяемы.",
            max_tokens=1200
        )).strip()
        count = len(text.split())
        if 55 <= count <= 130 and "МЫСЛИ ВСЛУХ" in text and "?" in text:
            return text, "AI"
        log.warning("Morning generation rejected: words=%s", count)
    except Exception:
        log.warning("Morning AI generation failed; using editorial backup")
    # Clear attribution: this text was not created by the live model.
    text = (
        "☀️ Доброе утро!\n💭 МЫСЛИ ВСЛУХ | ВРЕМЯ\n\n"
        f"Сегодня мне хочется поразмышлять: {title.lower()} "
        f"{fact}\n\n"
        "Учёные измеряют время часами. Но ни одни часы не подскажут, "
        "когда нам пора отложить дела и услышать близкого человека. "
        "Возможно, именно эти маленькие паузы помогают не пропустить жизнь.\n\n"
        "А какой момент сегодня заслуживает вашего внимания?\n\n"
        "Ваша Ора 💛"
    )
    return text, "backup"


def status():
    now = datetime.now(MSK)
    day = now.date()
    item = POSTED.get(day)
    preview = PREVIEWS.get(day)
    return {
        "date": day.isoformat(),
        "scheduled_time_msk": "08:00",
        "sent_in_current_process": bool(item),
        "sent_time_msk": item["sent_at"] if item else None,
        "telegram_message_id": item["message_id"] if item else None,
        "source": item["source"] if item else None,
        "preview_available": bool(preview),
        "last_attempt": LAST_ATTEMPT,
        "note": "After a server restart, earlier delivery history is not preserved.",
    }


async def preview(day, generate):
    if day not in PREVIEWS:
        PREVIEWS.clear()  # Only the current day's preview is kept in memory.
        PREVIEWS[day] = await compose(day, generate)
    return PREVIEWS[day]


async def publish(day, generate, send, chat_id, via="schedule", require_preview=False):
    global LAST_ATTEMPT
    async with PUBLISH_LOCK:
        if day in POSTED:
            return {"ok": True, "already_sent": True, "info": POSTED[day]}
        if require_preview and day not in PREVIEWS:
            return {"ok": False, "error": "Preview required"}
        LAST_ATTEMPT = {"date": day.isoformat(), "via": via, "state": "generating"}
        try:
            post, source = await preview(day, generate)
            LAST_ATTEMPT["state"] = "sending"
            message = await send(int(chat_id), post)
            item = {
                "sent_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": (message or {}).get("message_id"),
                "source": source, "via": via,
            }
            POSTED[day] = item
            LAST_ATTEMPT["state"] = "delivered"
            log.info("Ora morning text delivered, date=%s source=%s via=%s message_id=%s",
                     day.isoformat(), source, via, item["message_id"])
            return {"ok": True, "published": True, "info": item}
        except Exception as exc:
            LAST_ATTEMPT["state"] = "failed"
            log.warning("Morning publish failed: %s", type(exc).__name__)
            raise


def attach_morning(app, send, openai_response, allowed, chat_id):
    @app.get("/health/morning")
    async def morning_health():
        # Public and secret-free runtime state, never disclose chat contents.
        return status()

    @app.post("/schedule/morning")
    async def morning(authorization: str | None = Header(None)):
        event = authenticate(authorization)
        now = datetime.now(MSK)
        day = now.date()
        if event == "schedule" and not (7 <= now.hour <= 10):
            raise HTTPException(status_code=409, detail="Not morning Moscow time")
        if event == "push" and not (7 <= now.hour <= 23):
            raise HTTPException(status_code=409, detail="Not an allowed one-time launch")
        if str(chat_id) not in allowed:
            raise HTTPException(status_code=409, detail="WREMYA not configured")
        return await publish(day, openai_response, send, chat_id, via=event)
