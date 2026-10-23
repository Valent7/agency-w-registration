"""Ora: isolated Telegram pilot. No automatic posting or group access until configured."""
import asyncio
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO)
# httpx logs the full Telegram Bot API URL, including the secret bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("ora_pilot")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TOKEN_VALID = bool(re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,}", TOKEN))
SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
ALLOWED_CHAT = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
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

async def send(chat_id, text):
    return await telegram("sendMessage", {"chat_id":chat_id, "text":text[:4096]})

async def is_chat_admin(chat_id, user_id):
    try:
        user = await telegram("getChatMember", {"chat_id":chat_id, "user_id":user_id})
        return user.get("status") in ("creator", "administrator")
    except Exception:
        log.warning("Cannot check administrator permissions")
        return False

async def reply_ai(question):
    if not OPENAI_KEY:
        return ("Мой ИИ-интеллект ещё не подключён. Пока я могу показать "
                "тестовые публикации и календарь встреч. 🌷")
    instruction = (
        "Ты Ора, цифровой ИИ-сотрудник группы ВРЕМЯ / WREMYA. "
        "Говори по-русски, тепло и содержательно, обычно 2–5 предложений. "
        "Темы: время в физике, ценность времени, жизнь и цели, технологии и освобождение от рутины. "
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
                 "secret_token":SECRET,"allowed_updates":["message"]})
            log.info("Telegram webhook registered")
        except Exception as exc:
            # Never log exception details: URLs can contain Telegram bearer token.
            log.warning("Telegram webhook not registered (%s); check secret values privately.", type(exc).__name__)
    else:
        log.warning("Telegram keys or URL missing; webhook inactive")
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
       "chat_bound":bool(ALLOWED_CHAT),
       "ai_configured":bool(OPENAI_KEY),
       "automatic_posts":False}

@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
    if not SECRET or not hmac.compare_digest(SECRET, x_telegram_bot_api_secret_token or ""):
        raise HTTPException(status_code=403)
    update = await request.json()
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not user_id or not text.startswith("/"):
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
                "Его следует указать только в настройках Render как TELEGRAM_ALLOWED_CHAT_ID.")
        return {"ok":True}
    # Refuse all other traffic from unrelated groups.
    if not ALLOWED_CHAT or ALLOWED_CHAT != str(chat_id):
        return {"ok":True}
    if command == "/ask":
        if not argument:
            await send(chat_id, "Напишите вопрос после команды /ask. Например: "
                 "/ask Почему нам кажется, что время летит?")
        else:
            try:
                await send(chat_id, await reply_ai(argument))
            except Exception:
                log.exception("LLM unavailable")
                await send(chat_id, "Пока не могу получить проверенный ответ. "
                              "Не стану придумывать — попробуйте позднее.")
        return {"ok":True}
    if not await is_chat_admin(chat_id, user_id):
        return {"ok":True}
    if command == "/intro":
        await send(chat_id, INTRO)
    elif command == "/preview":
        await send(chat_id, POSTS.get(argument.lower(), 
             "Выберите /preview science, /preview thought или /preview humor"))
    elif command == "/poll":
        await telegram("sendPoll", {"chat_id":chat_id,
          "question":"⏳ Если бы у вас появился свободный час, на что бы вы его потратили?",
          "options":[{"text":v} for v in ["На близких ❤️","На отдых 🌿","На увлечение 🎨","На мечту ✨"]],
          "is_anonymous":True})
    elif command == "/meeting":
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
        await send(chat_id, "Ора — тестовый режим. Команды: /intro, "
          "/preview science|thought|humor, /meeting wednesday, /poll, /ask вопрос. "
          "Автоматические публикации отключены. "
          + ("ИИ подключён." if OPENAI_KEY else "ИИ пока не подключён."))
    return {"ok":True}
