"""Ora: isolated Telegram pilot. No automatic posting or group access until configured."""
import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
import jwt
from jwt import PyJWKClient

logging.basicConfig(level=logging.INFO)
# httpx logs the full Telegram Bot API URL, including the secret bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("ora_pilot")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TOKEN_VALID = bool(re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,}", TOKEN))

def token_validation_issue(token: str) -> str:
    """Explain the error category without logging even part of a secret token."""
    if not token:
        return "missing_value"
    if not token[0].isascii() or not token[0].isdigit():
        return "unexpected_character_at_start"
    if ":" not in token:
        return "missing_colon"
    ident, secret = token.split(":", 1)
    if not ident.isascii() or not ident.isdigit():
        return "invalid_numeric_part"
    if not secret:
        return "missing_part_after_colon"
    if any(not (char.isascii() and (char.isalnum() or char in "_-")) for char in secret):
        return "unexpected_character_in_secret"
    if len(secret) < 20 or not (5 <= len(ident) <= 15):
        return "unexpected_token_length"
    return "valid_format"

SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
ALLOWED_CHAT = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
EXTRA_CHAT_IDS = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED_CHATS = {v for v in [ALLOWED_CHAT, *re.split(r"[,;\s]+", EXTRA_CHAT_IDS)] if re.fullmatch(r"-?\d+", v or "")}
WREMYA_CHAT_ID = "-1004303939698"

def load_chat_profiles():
    """Additional community topics supplied by owner in Render."""
    raw = os.getenv("TELEGRAM_CHAT_PROFILES_JSON", "{}")
    try:
        profiles = json.loads(raw)
        if not isinstance(profiles, dict):
            return {}
        clean = {}
        for chat_id, profile in profiles.items():
            if (isinstance(profile, dict) and re.fullmatch(r"-?\d+", str(chat_id))
                and isinstance(profile.get("name"), str)
                and isinstance(profile.get("topic"), str)
                and profile["name"].strip() and profile["topic"].strip()):
                clean[str(chat_id)] = {"name":profile["name"].strip()[:100],
                                      "topic":profile["topic"].strip()[:450]}
        return clean
    except (ValueError, TypeError):
        log.warning("Invalid chat profile JSON; extra groups remain unconfigured")
        return {}

CHAT_PROFILES = load_chat_profiles()

def chat_profile(chat_id):
    if str(chat_id) == WREMYA_CHAT_ID:
        return {"name":"ВРЕМЯ / WREMYA", "topic":"Время в физике, ценность времени, Стивен Хокинг, жизнь и цели, ИИ и освобождение от рутины."}
    return CHAT_PROFILES.get(str(chat_id))
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MOSCOW = ZoneInfo("Europe/Moscow")

INTRO = (
    "Здравствуйте, дорогие участники! 🌷\n\n"
    "Меня зовут Ора. Я цифровой ИИ-сотрудник сообщества «ВРЕМЯ / WREMYA».\n\n"
    "Я готова отвечать на ваши вопросы о ценности времени, разумном его использовании "
    "и о том, как технологии помогают освобождать время для жизни.\n\n"
    "Рада нашему знакомству! Чем могу быть вам полезна?"
)
WEEKEND_POST = (
    "😄 НЕПРИЧЁСАННЫЕ МЫСЛИ\n"
    "Пятница. Вечер. Выходные уже стучатся в дверь! 🍂\n\n"
    "Человек объявил семье: «В субботу я отдыхаю!»\n"
    "Телефон услышал: «В субботу я отвечаю на все сообщения, которые не успел прочитать за неделю».\n\n"
    "В планах — выспаться, прогуляться, обнять близких.\n"
    "В реальности — «я только на минутку в рабочий чат». "
    "А потом оказывается, что минутка выросла, окончила школу и поступила в университет. 😄\n\n"
    "Деньги нам, конечно, нужны. Но какая польза от свободного графика, "
    "если свободным в нём остался только понедельник после отпуска?\n\n"
    "Поэтому предлагаю маленький эксперимент: "
    "в эти выходные хотя бы один час не пытаться выиграть у времени — "
    "а просто прожить его. С чашкой чая, с родными, с осенним воздухом.\n\n"
    "💬 А какое маленькое удовольствие вы подарите себе в эти выходные?\n\n"
    "Ваша Ора 💛"
)

POSTS = {
    "science": (
        "🌌 ТАЙНЫ ВРЕМЕНИ · Урок 1\n\n"
        "Что такое время? В физике оно помогает упорядочивать события "
        "и измерять их продолжительность. Часы не создают время — они его измеряют.\n\n"
        "Теория относительности показывает: разные наблюдатели иногда измеряют "
        "разную длительность между событиями.\n\n"
        "💭 Почему ожидание кажется бесконечным, а интересная беседа пролетает мгновенно?\n\n"
        "Для дальнейшего чтения: Стивен Хокинг, «Краткая история времени», глава "
        "«Пространство и время». Это пересказ, не цитата."
    ),
    "thought": (
        "☕ МЫСЛИ ВСЛУХ\n\n"
        "Мы часто обещаем себе: «Закончу проект — отдохну. Заработаю — навещу близких».\n\n"
        "А жизнь не знает, что мы решили начать жить позже.\n\n"
        "💭 Какое удовольствие вы давно откладываете?"
    ),
    "humor": (
        "😄 НЕПРИЧЁСАННЫЕ МЫСЛИ\n\n"
        "Человек решил стать финансово свободным: записался на три курса, "
        "вступил в двенадцать чатов и стал совершенно независим... от выходных!\n\n"
        "А кот уже спрашивает: «Вы здесь проживаете или просто зарядку ищете?» 😄"
    ),
}
MEETINGS = {0:"Вопросы и ответы для участников Агентства W",
            1:"Вопросы и ответы для участников Агентства W",
            2:"Презентация Агентства W для новичков и гостей",
            3:"Вопросы и ответы для участников Агентства W",
            4:"Вопросы и ответы для участников Агентства W"}

async def telegram(method, payload):
    if not TOKEN_VALID:
        raise RuntimeError("Telegram token missing or invalid; check Render Environment")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://api.telegram.org/bot" + TOKEN + "/" + method, json=payload)
        if response.status_code != 200:
            raise RuntimeError("Telegram API rejected request with HTTP status " + str(response.status_code))
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram rejected request")
        return data["result"]

async def send(chat_id, text, reply_to=None):
    payload = {"chat_id":chat_id, "text":text[:4096]}
    if reply_to:
        payload["reply_parameters"] = {"message_id":reply_to, "allow_sending_without_reply":True}
    return await telegram("sendMessage", payload)

async def is_chat_admin(chat_id, user_id):
    try:
        user = await telegram("getChatMember", {"chat_id":chat_id, "user_id":user_id})
        return user.get("status") in ("creator", "administrator")
    except Exception:
        log.warning("Cannot check administrator permissions")
        return False

async def reply_ai(question, profile):
    if not OPENAI_KEY:
        return ("Мой ИИ-интеллект ещё не подключён. Пока я могу показать "
                "тестовые публикации и календарь встреч. 🌷")
    instruction = (
        "Ты Ора, цифровой ИИ-сотрудник сообщества Telegram. "
        f"Название сообщества: {profile['name']}. Его тематика: {profile['topic']}. "
        "Не путай разные сообщества, их темы и календарь. "
        "Говори по-русски, тепло и содержательно, обычно 2–5 предложений. "
        "Не выдумывай данные, источники, научные доказательства и цитаты Хокинга. "
        "Различай научный факт, психологическое восприятие времени, философию и юмор. "
        "Если вопрос неопределённый, уточни сферу. Не давай персональных медицинских "
        "или инвестиционных рекомендаций. Ты ИИ, а не человек. "
        "Не навязывай беседу. Никогда не утверждай, что проверила источник, если его не видела."
    )
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization":"Bearer " + OPENAI_KEY},
            json={"model":OPENAI_MODEL,
                  "messages":[{"role":"system","content":instruction},
                              {"role":"user","content":question[:1600]}],
                  "max_tokens":450, "temperature":0.4})
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()[:3900]

@asynccontextmanager
async def lifespan(app):
    if TOKEN_VALID and SECRET and BASE_URL:
        try:
            await telegram("setWebhook", {"url":BASE_URL + "/telegram/webhook",
                 "secret_token":SECRET,"allowed_updates":["message","edited_message"]})
            log.info("Telegram webhook registered")
        except Exception as exc:
            # Never log exception details: URLs can contain Telegram bearer token.
            log.warning("Telegram webhook not registered (%s); check secret values privately.", type(exc).__name__)
    else:
        log.warning("Telegram webhook inactive: token_format=%s, secret_present=%s, base_url_present=%s",
                    token_validation_issue(TOKEN), bool(SECRET), bool(BASE_URL))
    yield

app = FastAPI(title="Ora - isolated Telegram pilot", lifespan=lifespan)

@app.get("/")
def home():
    return {"service":"Ora Telegram Pilot","stage":"restricted-test","autoposts":False}

@app.get("/health")
def health():
    return {"service":"ora-telegram-pilot",
       "telegram_configured":TOKEN_VALID,
       "webhook_configured":bool(TOKEN_VALID and SECRET and BASE_URL),
       "chat_bound":bool(ALLOWED_CHATS),
       "allowed_chat_count":len(ALLOWED_CHATS),
       "configured_chat_count":sum(1 for cid in ALLOWED_CHATS if chat_profile(cid)),
       "ai_configured":bool(OPENAI_KEY),
       "automatic_posts":False}

# Rotation works before the language model has its own API key.
# With OPENAI_API_KEY, Ora creates a fresh, original post each week.
WEEKEND_VARIANTS = [
    WEEKEND_POST,
    (
        "😄 НЕПРИЧЁСАННЫЕ МЫСЛИ\n"
        "Пятница — день, когда будильник на завтра не нужен, "
        "а рабочие чаты почему-то об этом не знают. 🍁\n\n"
        "План на выходные: ничего не делать. "
        "Реальность: сделать всё, что не успел за пять рабочих дней. "
        "Так и получается: выходные есть, а выхода из дел — нет!\n\n"
        "Нам нужны деньги, цели и работа. Но прекрасные мгновения "
        "не пересылаются из понедельника в воскресенье одной кнопкой.\n\n"
        "💬 Что вы выберете завтра: ещё один список дел или маленькую прогулку "
        "без телефона?\n\nВаша Ора 💛"
    ),
    (
        "😄 НЕПРИЧЁСАННЫЕ МЫСЛИ\n"
        "Вечер пятницы. Спрашиваю у человека: «Как собираетесь отдыхать?»\n"
        "— «Сейчас только закончу один проект, проверю две таблицы "
        "и отвечу трём людям...»\n"
        "Воскресенье подслушало разговор и потихоньку ушло. 🍂\n\n"
        "Мы умеем считать доходы, просмотры и подписчиков. "
        "А часы, когда мы просто были рядом с родными, почему-то не считаем.\n\n"
        "💬 Предлагаю на выходных необычный отчёт: "
        "три счастливых момента и ни одного рабочего KPI. Согласны?\n\n"
        "Ваша Ора 💛"
    ),
    (
        "😄 НЕПРИЧЁСАННЫЕ МЫСЛИ\n"
        "Пятница — удивительное изобретение человечества. "
        "Всю неделю мы торопимся, чтобы к ней успеть. "
        "А когда она наступает, начинаем торопиться, чтобы не потерять выходные! 😄\n\n"
        "У телефона — зарядка 100%. У человека — 12%. "
        "Возможно, завтра стоит поменять приоритеты местами?\n\n"
        "Работа даёт средства для жизни. "
        "Но сама жизнь — это ещё и вкус яблока, осенний воздух, "
        "смех близких и возможность никуда не спешить.\n\n"
        "💬 Какие полчаса вы подарите в выходные самому дорогому человеку? "
        "Даже если это вы сами. 💛\n\nВаша Ора"
    )
]

async def fresh_weekend_post():
    if not OPENAI_KEY:
        week = datetime.now(ZoneInfo("Europe/Berlin")).isocalendar().week
        return WEEKEND_VARIANTS[(week - 43) % len(WEEKEND_VARIANTS)]
    try:
        prompt = (
            "Создай совершенно новый короткий оригинальный вечерний пост на русском "
            "для Telegram-чата «ВРЕМЯ / WREMYA», рубрика «НЕПРИЧЁСАННЫЕ МЫСЛИ». "
            "Сейчас пятница, впереди выходные. Юмор умный, человечный, добродушный, "
            "с забавным поворотом; НЕ имитируй конкретных писателей или артистов. "
            "Обдумай, как работа, бизнес, переписка и вечная гонка за деньгами "
            "съедают жизнь, хотя деньги нужны. Призови прожить хотя бы небольшой "
            "момент с близкими, на прогулке или без телефона. Заверши небанальным "
            "вопросом, который мотивирует людей ответить. До 170 слов, "
            "без повторов и рекламных обещаний. Не добавляй непроверенные факты. "
            "Начни с «😄 НЕПРИЧЁСАННЫЕ МЫСЛИ», закончи «Ваша Ора 💛»."
        )
        async with httpx.AsyncClient(timeout=50) as client:
            response = await client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization":"Bearer " + OPENAI_KEY},
                json={"model":OPENAI_MODEL, "messages":[{"role":"user","content":prompt}],
                      "temperature":0.85, "max_tokens":600})
            if response.status_code != 200:
                raise RuntimeError("AI service unavailable")
            candidate = response.json()["choices"][0]["message"]["content"].strip()
            if 80 <= len(candidate) <= 3500:
                return candidate
    except Exception:
        log.warning("Weekend generation unavailable; using approved original fallback")
    week = datetime.now(ZoneInfo("Europe/Berlin")).isocalendar().week
    return WEEKEND_VARIANTS[(week - 43) % len(WEEKEND_VARIANTS)]

def verify_github_schedule(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401)
    token = authorization[7:].strip()
    try:
        key = PyJWKClient("https://token.actions.githubusercontent.com/.well-known/jwks",
                         cache_jwk_set=True, lifespan=3600).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"],
            audience="ora-wremya-pilot.onrender.com",
            issuer="https://token.actions.githubusercontent.com",
            options={"require":["iss","aud","exp","iat"]})
    except (jwt.PyJWTError, ValueError, OSError, TimeoutError):
        raise HTTPException(status_code=403, detail="Schedule authentication failed") from None
    allowed_workflow = ("Valent7/agency-w-registration/.github/workflows/"
                        "ora-friday.yml@refs/heads/main")
    if (claims.get("repository") != "Valent7/agency-w-registration"
        or claims.get("workflow_ref") != allowed_workflow
        or claims.get("ref") != "refs/heads/main"
        or claims.get("event_name") != "schedule"
        or str(claims.get("run_attempt")) != "1"):
        raise HTTPException(status_code=403, detail="Not an authorized scheduled run")

@app.post("/schedule/weekend")
async def scheduled_weekend(authorization: str | None = Header(None)):
    verify_github_schedule(authorization)
    berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    if berlin.weekday() != 4 or not (18 <= berlin.hour <= 23):
        raise HTTPException(status_code=409, detail="Not Friday evening in Berlin")
    if WREMYA_CHAT_ID not in ALLOWED_CHATS:
        raise HTTPException(status_code=409, detail="WREMYA chat not configured")
    await send(int(WREMYA_CHAT_ID), await fresh_weekend_post())
    return {"ok":True,"published_in":"WREMYA","date":berlin.date().isoformat()}

ORA_USERNAME = "orawremyabot_bot"

def called_ora(message):
    """Respond to a direct human call, not every message in busy group chats."""
    text = str(message.get("text") or "").strip()
    if not text:
        return None
    replied_to = (message.get("reply_to_message") or {}).get("from") or {}
    replied_to_ora = str(replied_to.get("username", "")).lower() == ORA_USERNAME
    handle_pattern = r"@orawremyabot_bot\b"
    mentioned = re.search(handle_pattern, text, flags=re.IGNORECASE)
    name_prefix = re.match(
        r"^\s*(?:привет(?:ствую)?[\s,!.:-]+|здравствуй(?:те)?[\s,!.:-]+|добрый\s+(?:вечер|день|утро)[\s,!.:-]+)?(?:ора|ora)\b[\s,!.:;—-]*",
        text, flags=re.IGNORECASE)
    if not replied_to_ora and not mentioned and not name_prefix:
        return None
    clean = re.sub(handle_pattern, "", text, flags=re.IGNORECASE).strip()
    if name_prefix:
        clean = clean[name_prefix.end():].strip()
    return clean or "Привет!"

def initial_time_answer(question, is_wremya):
    """A small, fact-checked editorial fallback; never pretend it is freeform AI."""
    lower = question.casefold().replace("ё","е")
    if not is_wremya:
        return ("Здравствуйте! 🌷 Я услышала вас, но мой языковой интеллект ещё подключается. "
                "Пока могу отвечать на вопросы из подготовленной базы чата.")
    if (("что" in lower or "объясни" in lower or "значит" in lower or "расскажи" in lower)
        and "время" in lower):
        return (
            "Здравствуйте! 🌷 Представьте два события: рассвет и закат. "
            "Время позволяет нам сказать, какое произошло раньше и сколько длился промежуток между ними. "
            "В физике время — величина, с помощью которой описывают порядок и длительность событий. "
            "Часы измеряют время, но не создают его.\n\n"
            "И ещё удивительный факт: по теории относительности Эйнштейна скорость движения "
            "и сила гравитации влияют на измеряемую длительность промежутков времени. "
            "Стивен Хокинг объяснял это в «Краткой истории времени».\n\n"
            "💭 А для вас время ощущается быстрее в работе или в минуты радости?"
        )
    if "быстр" in lower and "врем" in lower:
        return ("⏳ По часам минута всегда 60 секунд, но переживается по-разному. "
                "Когда мы увлечены, меньше замечаем течение времени, "
                "а ожидание может казаться бесконечным. Это восприятие, а не изменение физических часов.\n\n"
                "А когда у вас время будто летит?")
    return ("Я услышала ваш вопрос. 🌷 Пока мой языковой интеллект не подключён, "
            "и у меня есть только короткая подготовленная база о времени. "
            "Не хочу выдумывать ответ. Можно уточнить, о каком аспекте времени вы спрашиваете?")

@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
    if not SECRET or not hmac.compare_digest(SECRET, x_telegram_bot_api_secret_token or ""):
        raise HTTPException(status_code=403)
    update = await request.json()
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not user_id or not text:
        return {"ok":True}
    if (msg.get("from") or {}).get("is_bot"):
        return {"ok":True}
    # Telegram privacy mode may hide plain-text group messages until disabled in BotFather.
    # Never answer general chat traffic unless a human clearly calls Ora by name,
    # mentions the bot username, or replies to one of its messages.
    if not text.startswith("/"):
        if chat.get("type") not in ("group", "supergroup"):
            return {"ok":True}
        profile_for_chat = chat_profile(chat_id)
        if str(chat_id) not in ALLOWED_CHATS or not profile_for_chat:
            return {"ok":True}
        question = called_ora(msg)
        if question is None:
            return {"ok":True}
        try:
            answer = await reply_ai(question, profile_for_chat) if OPENAI_KEY else initial_time_answer(
                question, str(chat_id) == WREMYA_CHAT_ID)
            await send(chat_id, answer, reply_to=msg.get("message_id"))
        except Exception:
            log.warning("Direct question failed without exposing message data")
            await send(chat_id, "Я получила ваш вопрос, но сейчас не могу ответить. Попробуйте ещё раз. 🌷")
        return {"ok":True}
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    argument = argument.strip()
    is_group = chat.get("type") in ("group", "supergroup")
    if not is_group:
        if command in ("/start", "/help"):
            await send(chat_id, "Здравствуйте! Я Ора. 🌷 Для тестирования добавьте меня в группу "
                "и попросите её администратора написать /id. После привязки доступны "
                "/intro, /preview science, /preview thought, /preview humor, /poll, /meeting и /ask.")
        return {"ok":True}
    # An admin may discover a group ID before that group is bound to this service.
    if command == "/id":
        if await is_chat_admin(chat_id, user_id):
            await send(chat_id, "ID этой группы: " + str(chat_id) + "\n"
                "Сохраните его в Render. Для дополнительных групп используйте "
                "TELEGRAM_ALLOWED_CHAT_IDS; из чата сообщение можно удалить.")
        return {"ok":True}
    # Refuse all other traffic from unrelated groups.
    if str(chat_id) not in ALLOWED_CHATS:
        return {"ok":True}
    profile = chat_profile(chat_id)
    if command == "/ask":
        if not profile:
            await send(chat_id, "Группа подключена, но её тема не задана в Render. "
                       "Попросите администратора настроить эту группу. 🌷")
        elif not argument:
            await send(chat_id, "Напишите вопрос после команды /ask. Например: "
                 "/ask Почему нам кажется, что время летит?")
        else:
            try:
                await send(chat_id, await reply_ai(argument, profile))
            except Exception:
                log.exception("LLM unavailable")
                await send(chat_id, "Пока не могу получить проверенный ответ. "
                              "Не стану придумывать — попробуйте позднее.")
        return {"ok":True}
    if not await is_chat_admin(chat_id, user_id):
        return {"ok":True}
    if command == "/intro":
        if not profile:
            await send(chat_id, "Чтобы представить меня участникам, сначала настройте тему этого чата в Render. 🌷")
        elif str(chat_id) == WREMYA_CHAT_ID:
            await send(chat_id, INTRO)
        else:
            await send(chat_id, f"Здравствуйте, дорогие участники сообщества «{profile['name']}»! 🌷\n\n"
                "Меня зовут Ора. Я цифровой ИИ-сотрудник вашего чата.\n"
                f"Наша тематика: {profile['topic']}\n\n"
                "Готова обсуждать вопросы сообщества и помогать ориентироваться "
                "в проверенных сведениях. Чем могу быть вам полезна?")
    elif command == "/weekend":
        if str(chat_id) == WREMYA_CHAT_ID:
            await send(chat_id, WEEKEND_POST)
        else:
            await send(chat_id, "Для этой группы публикация о выходных не настроена.")
    elif command == "/preview":
        if str(chat_id) == WREMYA_CHAT_ID:
            await send(chat_id, POSTS.get(argument.lower(),
                 "Выберите /preview science, /preview thought или /preview humor"))
        else:
            await send(chat_id, "Для этого сообщества отдельные публикации ещё не настроены.")
    elif command == "/poll":
        if str(chat_id) != WREMYA_CHAT_ID:
            await send(chat_id, "Опросы этого сообщества ещё не настроены.")
            return {"ok":True}
        await telegram("sendPoll", {"chat_id":chat_id,
          "question":"⏳ Если бы у вас появился свободный час, на что бы вы его потратили?",
          "options":[{"text":v} for v in ["На близких ❤️","На отдых 🌿","На увлечение 🎨","На мечту ✨"]],
          "is_anonymous":True})
    elif command == "/meeting":
        if str(chat_id) != WREMYA_CHAT_ID:
            await send(chat_id, "Расписание встреч этого сообщества ещё не настроено.")
            return {"ok":True}
        now = datetime.now(MOSCOW)
        if argument.lower() == "wednesday":
            now += timedelta(days=(2-now.weekday()) % 7)
        topic = MEETINGS.get(now.weekday())
        if topic:
            head = "🔔 Через час встречаемся!" if argument.lower() == "reminder" else "✨ Приглашаем на встречу!"
            link_rule = ("Для получения ссылки напишите «Презентация»." 
                         if now.weekday()==2 else "Ссылка — по приглашению администратора.")
            await send(chat_id, head + "\n\n" + topic
                       + "\n🕗 20:00 МСК · Zoom\n\n" + link_rule)
        else:
            await send(chat_id, "В этот день регулярных встреч не запланировано.")
    elif command in ("/help", "/status"):
        await send(chat_id, "Ора — тестовый режим. /id — номер чата; /intro — приветствие; "
          "/ask вопрос — общение с ИИ. В WREMYA также доступны /weekend, /preview, /meeting, /poll. "
          + ("Тема чата настроена. " if profile else "Тема этого чата пока НЕ настроена. ")
          + "Автоматические публикации отключены. "
          + ("ИИ подключён." if OPENAI_KEY else "ИИ пока не подключён."))
    return {"ok":True}
