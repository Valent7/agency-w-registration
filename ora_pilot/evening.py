"""Evening reflections: original, warm, humorous text from Ora to WREMYA."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

log = logging.getLogger("ora_pilot.evening")
MSK = ZoneInfo("Europe/Moscow")
AUDIENCE = "ora-wremya-pilot.onrender.com"
WORKFLOW = "Valent7/agency-w-registration/.github/workflows/ora-evening.yml@refs/heads/main"
FIRST_DAY = datetime(2026, 10, 24, tzinfo=MSK).date()
LAST_DAY = datetime(2026, 11, 30, tzinfo=MSK).date()
POSTED = set()

# Historical thinkers are inspiration for ideas and attitude, not authors of Ora's text.
VOICES = [
    ("Элеонора Рузвельт", "достоинство, личная смелость, независимость от чужих оценок, забота о людях"),
    ("Сенека", "ценность собственных часов, краткость жизни, самоирония"),
    ("Сократ", "вопросы вместо готовых ответов, интеллектуальная ирония"),
    ("Марк Твен", "добрый сарказм о привычках, здравый смысл"),
    ("Оскар Уайльд", "парадоксы и игра ожиданий, тонкая ирония"),
    ("Монтень", "повседневные наблюдения, самоирония"),
    ("Бернард Шоу", "парадоксальный взгляд на работу и успех"),
    ("Диоген", "свобода от лишних вещей и показных достижений"),
    ("Антон Чехов", "наблюдательность к человеческим слабостям без унижения"),
    ("Фаина Раневская", "остроумие и прямота без копирования чужих афоризмов"),
]
BACKUP = (
    "🌙 НЕПРИЧЁСАННЫЕ МЫСЛИ | ВРЕМЯ\n\n"
    "Сегодня я думала о странной привычке человека: мы так заботимся о том, "
    "чтобы не опоздать на встречу, что иногда опаздываем к собственной жизни.\n\n"
    "Календарь у нас расписан строго. А вот радость — почему-то в режиме "
    "«когда появится свободное окошко». И обычно это окошко переносится "
    "на следующую неделю. 😄\n\n"
    "Смелость — не только сказать важное слово перед большой аудиторией. "
    "Иногда она состоит в том, чтобы закрыть рабочий чат и уделить "
    "пятнадцать минут человеку, который рядом.\n\n"
    "Возможно, пора перестать спрашивать у жизни, где взять ещё час, "
    "и спросить себя: на что я сегодня решусь ради того, что действительно дорого?\n\n"
    "Ваша Ора 💛"
)

def authenticate(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401)
    try:
        key = PyJWKClient(
            "https://token.actions.githubusercontent.com/.well-known/jwks",
            cache_jwk_set=True, lifespan=3600
        ).get_signing_key_from_jwt(authorization[7:].strip()).key
        claims = jwt.decode(
            authorization[7:].strip(), key, algorithms=["RS256"], audience=AUDIENCE,
            issuer="https://token.actions.githubusercontent.com",
            options={"require": ["iss", "aud", "exp", "iat"]}
        )
    except (jwt.PyJWTError, ValueError, OSError, TimeoutError):
        raise HTTPException(status_code=403, detail="Unauthorized schedule") from None
    if (claims.get("repository") != "Valent7/agency-w-registration"
        or claims.get("workflow_ref") != WORKFLOW
        or claims.get("ref") != "refs/heads/main"
        or str(claims.get("run_attempt")) != "1"
        or claims.get("event_name") not in ("schedule", "push")):
        raise HTTPException(status_code=403, detail="Unapproved scheduled request")
    if claims.get("event_name") == "push" and datetime.now(MSK).date() != FIRST_DAY:
        raise HTTPException(status_code=403, detail="One-time launch expired")
    return claims.get("event_name")

async def compose(day, generate):
    i = (day - FIRST_DAY).days % len(VOICES)
    person, inspiration = VOICES[i]
    prompt = (
        f"Дата: {day.isoformat()}. Ты — Ора, самостоятельный ИИ-редактор сообщества "
        "«ВРЕМЯ / WREMYA». Напиши новый вечерний пост на русском "
        "«🌙 НЕПРИЧЁСАННЫЕ МЫСЛИ | ВРЕМЯ». "
        "Это НЕ мини-урок: это остроумная, изящная человеческая мысль о том, "
        "как суета крадёт счастливые мгновения, а смелость помогает жить осмысленно. "
        f"Сегодня вдохновение — ценности и мировоззрение мыслителя {person}: {inspiration}. "
        "Опирайся на идеи, но НЕ приписывай этому человеку ни одной придуманной цитаты; "
        "не выдавай пост за написанный им. У тебя собственный тёплый, "
        "немного озорной голос Оры. 120–180 слов, оригинальный смешной поворот, "
        "яркий образ из повседневной жизни, уважение к собеседникам, "
        "один интересный вопрос в конце, подпись «Ваша Ора 💛». "
        "Не делай пост рекламным, пафосным или унизительным."
    )
    try:
        result = (await generate(
            prompt, "Ты Ора. Ты вдохновляешься идеями исторических личностей, "
            "но никогда не подделываешь их цитаты или подписи. "
            "Содержание должно быть оригинальным, точным, лёгким и человечным.",
            max_tokens=2600
        )).strip()
        if 200 <= len(result) <= 3500 and "НЕПРИЧЁСАННЫЕ МЫСЛИ" in result:
            return result, True
        log.warning("Evening generated result was incomplete; using vetted backup")
    except Exception:
        log.warning("Evening model unavailable; using vetted backup")
    return BACKUP, False

def attach_evening(app, send, openai_response, allowed, chat_id):
    @app.post("/schedule/evening")
    async def evening(authorization: str | None = Header(None)):
        event = authenticate(authorization)
        now = datetime.now(MSK)
        day = now.date()
        if not FIRST_DAY <= day <= LAST_DAY:
            raise HTTPException(status_code=409, detail="Outside approved pilot dates")
        if now.hour < 21 or now.hour > 23:
            raise HTTPException(status_code=409, detail="Not evening Moscow time")
        if event == "schedule" and day == FIRST_DAY:
            # First day has its own one-off push launch; avoid duplicate sends.
            return {"ok": True, "skipped_first_day_schedule": True}
        if str(chat_id) not in allowed:
            raise HTTPException(status_code=409, detail="WREMYA not configured")
        if day in POSTED:
            return {"ok": True, "already_sent": True}
        content, generated = await compose(day, openai_response)
        await send(int(chat_id), content)
        POSTED.add(day)
        log.info("Ora evening reflection sent, date=%s model_generated=%s", day.isoformat(), generated)
        return {"ok": True, "published": True, "model_generated": generated}
