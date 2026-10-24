"""Утренние «Мысли вслух» Оры. Только WREMYA, отдельный сервис."""
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
POSTED = set()

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
    index = (day - FIRST_DAY).days % len(THEMES)
    title, fact = THEMES[index]
    # The first publication is carefully edited rather than an unverified first model output.
    if day == FIRST_DAY:
        return TODAY_POST
    prompt = (
        f"Сегодня {day.isoformat()}. Рубрика Telegram WREMYA: «МЫСЛИ ВСЛУХ». "
        "Напиши самостоятельное, красивое и ТЁПЛОЕ утреннее размышление по-русски, "
        "НЕ УРОК и НЕ ЛЕКЦИЮ. Начни с «☀️ Доброе утро!» и «💭 МЫСЛИ ВСЛУХ | ВРЕМЯ». "
        f"Сюжет сегодня: «{title}». Научная опора: {fact} "
        "Отделяй научное объяснение по мотивам книги Стивена Хокинга "
        "«Краткая история времени» от философских размышлений о жизни. "
        "Никаких придуманных цитат, неточных дат, обещаний или назидания. "
        "Около 130-190 слов, один понятный пример из обычной жизни, "
        "добрый тон, свежий образ, вопрос для обсуждения в конце. "
        "Не повторяй вчерашнюю мысль. В самом конце «Ваша Ора 💛». "
        "Никаких призывов покупать, вступать или зарабатывать."
    )
    try:
        text = (await generate(
            prompt,
            "Ты Ора, самостоятельный редактор WREMYA. Пиши как размышляющий собеседник. "
            "Проверяемость фактов обязательна; если сомневаешься, не добавляй факт.",
            max_tokens=1800
        )).strip()
        if 250 <= len(text) <= 3300 and "МЫСЛИ ВСЛУХ" in text:
            return text
    except Exception:
        log.warning("Morning model response unavailable; using vetted reflection")
    return (
        f"☀️ Доброе утро!\n💭 МЫСЛИ ВСЛУХ | ВРЕМЯ\n\n"
        f"Сегодня думаю о простом вопросе: {title.lower()} "
        f"{fact}\n\n"
        "Но какое отношение это имеет к нашей обычной жизни? "
        "Наверное, самое важное — не путать календарь с прожитыми мгновениями. "
        "Мы можем торопиться к цели и всё-таки находить время заметить родных, "
        "природу и самих себя.\n\n"
        "А что сегодня вы хотите заметить, вместо того чтобы проскочить мимо?\n\n"
        "Ваша Ора 💛"
    )

def attach_morning(app, send, openai_response, allowed, chat_id):
    @app.post("/schedule/morning")
    async def morning(authorization: str | None = Header(None)):
        event = authenticate(authorization)
        now = datetime.now(MSK)
        day = now.date()
        # Do not send the initial Saturday post before the user-requested one-off launch.
        if event == "schedule" and not (7 <= now.hour <= 10):
            raise HTTPException(status_code=409, detail="Not near morning Moscow time")
        if event == "push" and not (7 <= now.hour <= 23):
            raise HTTPException(status_code=409, detail="Not yet time for the one-off post")
        if str(chat_id) not in allowed:
            raise HTTPException(status_code=409, detail="WREMYA is not in allowed chats")
        if day in POSTED:
            return {"ok": True, "already_sent": True, "day": day.isoformat()}
        post = await compose(day, openai_response)
        await send(int(chat_id), post)
        POSTED.add(day)
        log.info("Ora morning reflections published for Moscow day %s", day.isoformat())
        return {"ok": True, "published": True, "day": day.isoformat()}
