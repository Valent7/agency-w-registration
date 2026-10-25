"""Private administrator control panel for morning WREMYA reflections."""
from datetime import datetime
from zoneinfo import ZoneInfo

import morning

MSK = ZoneInfo("Europe/Moscow")
WREMYA_CHAT_ID = -1004303939698
CONTROL_ACTIONS = {"ora:menu", "ora:morning:status", "ora:morning:preview",
                   "ora:morning:publish", "ora:morning:confirm"}

def keys(rows):
    return {"inline_keyboard": rows}

def key(label, action):
    return {"text": label, "callback_data": action}

MENU = keys([
    [key("📊 Проверить утро", "ora:morning:status")],
    [key("👀 Посмотреть текст", "ora:morning:preview")],
    [key("📣 Отправить сегодня", "ora:morning:publish")],
])

async def panel(chat_id, telegram):
    result = morning.status()
    line = (f"✅ Отправлено в {result['sent_time_msk']} МСК, Telegram ID {result['telegram_message_id']}"
            if result["sent_in_current_process"]
            else "⚠️ Нет подтверждённой отправки в памяти текущего запуска")
    model = result["source"] or "неизвестно"
    note = "После перезапуска Render история в этом пилоте очищается."
    await telegram("sendMessage", {
        "chat_id": chat_id,
        "text": "🔐 Пульт Оры — WREMYA\n"
                "☀️ Рубрика: «Мысли вслух»\n"
                f"📅 Сегодня {result['date']}, 08:00 МСК\n"
                f"📤 {line}\n"
                f"🧠 Источник опубликованного текста: {model}\n"
                f"👀 Предпросмотр: {'готов' if result['preview_available'] else 'ещё не создан'}\n"
                f"{note}\n\n"
                "Предпросмотр доступен только тебе в личном чате; "
                "публикация в WREMYA — после подтверждения.",
        "reply_markup": MENU,
    })

async def process_action(action, user_id, chat_id, telegram, is_chat_admin,
                         generate, send):
    if not await is_chat_admin(WREMYA_CHAT_ID, user_id):
        await send(chat_id, "Этот пульт доступен только администраторам WREMYA.")
        return
    if action in ("ora:menu", "/start", "/ora", "/help", "ora:morning:status"):
        await panel(chat_id, telegram)
        return
    if action == "ora:morning:preview":
        day = datetime.now(MSK).date()
        await send(chat_id, "Готовлю короткие «Мысли вслух» для просмотра. 🌷")
        text, source = await morning.preview(day, generate)
        await telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"👀 ПРЕДПРОСМОТР — НЕ ОТПРАВЛЕНО В ГРУППУ\n"
                    f"Источник текста: {'ИИ' if source == 'AI' else 'резервный текст'}\n\n"
                    f"{text}",
            "reply_markup": keys([
                [key("📣 Опубликовать", "ora:morning:publish")],
                [key("↩️ Пульт", "ora:menu")],
            ])
        })
        return
    if action == "ora:morning:publish":
        day = datetime.now(MSK).date()
        if morning.status()["sent_in_current_process"]:
            await send(chat_id, "Утренний текст уже отмечен как отправленный сегодня.")
            return
        if day not in morning.PREVIEWS:
            await send(chat_id, "Сначала нажми «Посмотреть текст», затем подтверди отправку.")
            await panel(chat_id, telegram)
            return
        await telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "Отправить показанный текст от имени Оры в общий чат WREMYA? "
                    "После подтверждения сообщение увидят все участники.",
            "reply_markup": keys([
                [key("✅ Да, отправить", "ora:morning:confirm")],
                [key("❌ Отмена", "ora:menu")],
            ])
        })
        return
    if action == "ora:morning:confirm":
        day = datetime.now(MSK).date()
        if day not in morning.PREVIEWS:
            await send(chat_id, "Предпросмотр утра устарел; открой его снова.")
            return
        if morning.status()["sent_in_current_process"]:
            await send(chat_id, "Текст уже отмечен как отправленный; повтор не нужен.")
            return
        try:
            result = await morning.publish(day, generate, send, WREMYA_CHAT_ID,
                                           via="admin", require_preview=True)
            if result.get("published"):
                meta = result.get("info") or {}
                await send(chat_id, "✅ Ора опубликовала текст в WREMYA. "
                           f"Telegram ID: {meta.get('message_id')}.")
            else:
                await send(chat_id, "Повторная отправка не выполнена: запись за сегодня уже есть.")
        except Exception:
            await send(chat_id, "❌ Telegram не подтвердил отправку. "
                       "Посмотри статус в пульте и журнал Render.")
        return
