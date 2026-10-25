"""Private administrator control panel for morning WREMYA reflections."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import morning
import editorial_calendar

MSK = ZoneInfo("Europe/Moscow")
WREMYA_CHAT_ID = -1004303939698
CONTROL_ACTIONS = {"ora:menu", "ora:morning:status", "ora:calendar", "ora:tomorrow:preview",
                   "ora:morning:edit", "ora:morning:preview",
                   "ora:morning:publish", "ora:morning:confirm"}

def keys(rows):
    return {"inline_keyboard": rows}

def key(label, action):
    return {"text": label, "callback_data": action}

MENU = keys([
    [key("📊 Проверить утро", "ora:morning:status")],
    [key("👀 Посмотреть текст", "ora:morning:preview")],
    [key("📅 Темы на неделю", "ora:calendar"), key("🔭 Завтра", "ora:tomorrow:preview")],
    [key("✏️ Исправить сегодняшний текст", "ora:morning:edit")],
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
                f"📖 Тема: {result.get('editorial_title') or 'вне книжной серии'}\n"
                f"📚 Глава: {result.get('editorial_chapter') or '—'}\n"
                f"📤 {line}\n"
                f"🧠 Источник опубликованного текста: {model}\n"
                f"👀 Предпросмотр: {'готов' if result['preview_available'] else 'ещё не создан'}\n"
                f"{note}\n\n"
                "Предпросмотр доступен администраторам в личном чате. "
                "Ручная публикация — после подтверждения; "
                "автоматическая утренняя отправка работает отдельно.",
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
    if action == "ora:calendar":
        day = datetime.now(MSK).date()
        plan = editorial_calendar.upcoming(day, 8)
        if not plan:
            await send(chat_id, "Книжная серия завершена 30 ноября 2026 года.")
        else:
            lines = [f"{item['date'][8:10]}.{item['date'][5:7]} — {item['title']} (глава {item['chapter']})"
                     for item in plan]
            await send(chat_id, "📚 КНИЖНЫЙ ПЛАН ОРЫ\n"
                       "Утренние темы на ближайшие дни:\n\n" + "\n".join(lines)
                       + "\n\nКаждый текст написан заранее. "
                       "Посмотреть текст на завтра можно отдельной кнопкой.")
        return
    if action == "ora:tomorrow:preview":
        tomorrow = datetime.now(MSK).date() + timedelta(days=1)
        item = editorial_calendar.get_entry(tomorrow)
        if not item:
            await send(chat_id, "На завтра нет подготовленного текста книжной серии.")
        else:
            await send(chat_id,
                f"🔭 ЗАВТРА: {tomorrow.strftime('%d.%m.%Y')}\n"
                f"Глава {item['chapter']}: {item['title']}\n"
                "Только просмотр. В группу НЕ отправлено.\n\n"
                + editorial_calendar.compose_entry(tomorrow))
        return
    if action == "ora:morning:edit":
        await send(chat_id,
            "✏️ Для изменения СЕГОДНЯШНЕГО ещё не опубликованного текста "
            "пришли мне личным сообщением команду:\n\n"
            "/replace Здесь твой полный новый текст с вопросом в конце.\n\n"
            "Ора покажет его в предпросмотре. "
            "Автоматическая публикация не приостанавливается; "
            "после отправки в группу изменить сегодняшнюю публикацию этим способом нельзя. "
            "Черновик хранится только до перезапуска сервера.")
        return
    if action.startswith("/replace "):
        day = datetime.now(MSK).date()
        if morning.status()["sent_in_current_process"]:
            await send(chat_id, "Сегодняшнее сообщение уже отправлено; менять его через /replace поздно.")
            return
        copy = action[len("/replace "):].strip()
        words = len(copy.split())
        if words < 35 or words > 125 or "?" not in copy:
            await send(chat_id, "Нужны 35–125 слов и хотя бы один вопрос. "
                       "Пришли /replace и весь текст ещё раз.")
            return
        if len(copy) > 2500:
            await send(chat_id, "Текст слишком длинный для коротких «Мыслей вслух».")
            return
        cleaned = copy.replace("☀️ Доброе утро, друзья!", "").replace(
            "💭 МЫСЛИ ВСЛУХ | ВРЕМЯ", "").replace("Ваша Ора 💛", "").strip()
        post = ("☀️ Доброе утро, друзья!\n💭 МЫСЛИ ВСЛУХ | ВРЕМЯ\n\n"
                + cleaned + "\n\nВаша Ора 💛")
        morning.PREVIEWS[day] = (post, "owner-edited")
        await send(chat_id, "✅ Новый текст принят в предпросмотр на сегодня. "
                   "Посмотри его кнопкой «Посмотреть текст». "
                   "Этот черновик сохранён только в памяти работающего сервера.")
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
