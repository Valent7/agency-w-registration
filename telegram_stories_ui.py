from __future__ import annotations

"""Agency W — test radar for Telegram Stories.

This module only READS the current owner's active Telegram Stories feed and
prepares warm reply suggestions. It does not send reactions or messages.
"""

import asyncio
import base64
import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests
import streamlit as st
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import GetAllStoriesRequest

from neona_telegram_dialogs import initialize_dialog_after_story_reply
from workspace_persistence import persist_workspace_if_changed

UTC = timezone.utc
MAX_STORIES = 10
MAX_RECOMMENDATIONS = 10


class TelegramStoriesError(RuntimeError):
    pass


def _secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    except Exception:
        pass
    return str(os.getenv(name, default) or default).strip()


def _supabase_config() -> tuple[str, str]:
    url = _secret("SUPABASE_URL").rstrip("/")
    key = _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise TelegramStoriesError("Не найдены настройки Supabase")
    return url, key


def _load_session(owner_id: int) -> str:
    url, key = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/telegram_sessions",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={
            "telegram_id": f"eq.{int(owner_id)}",
            "select": "telegram_id,encrypted_session",
            "limit": 1,
        },
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json() if response.text.strip() else []
    if not rows:
        raise TelegramStoriesError("Telegram ещё не подключён к этому кабинету")

    encrypted = str(rows[0].get("encrypted_session") or "")
    if not encrypted:
        raise TelegramStoriesError("Telegram-сессия пуста")

    key_text = _secret("FERNET_KEY")
    if not key_text:
        raise TelegramStoriesError("Не найдена настройка FERNET_KEY")
    try:
        return Fernet(key_text.encode("utf-8")).decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise TelegramStoriesError("Не удалось расшифровать Telegram-сессию") from exc


def _api_credentials() -> tuple[int, str]:
    api_id = _secret("TELEGRAM_API_ID")
    api_hash = _secret("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise TelegramStoriesError("Не найдены TELEGRAM_API_ID / TELEGRAM_API_HASH")
    return int(api_id), api_hash


def _peer_user_id(peer: Any) -> int | None:
    # Current Telegram layers use PeerUser(user_id=...). We deliberately keep
    # this duck-typed so the code is tolerant to minor Telethon schema changes.
    value = getattr(peer, "user_id", None)
    try:
        uid = int(value)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _story_date(story: Any) -> datetime | None:
    value = getattr(story, "date", None)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _story_expire_date(story: Any) -> datetime | None:
    value = getattr(story, "expire_date", None)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _user_name(user: Any) -> str:
    parts = [
        str(getattr(user, "first_name", "") or "").strip(),
        str(getattr(user, "last_name", "") or "").strip(),
    ]
    return " ".join(x for x in parts if x) or "Telegram-контакт"


def _compress_preview(raw: bytes) -> tuple[str, str] | tuple[None, None]:
    """Returns (base64, mime) for a small image preview."""
    if not raw:
        return None, None
    try:
        image = Image.open(io.BytesIO(raw))
        image = image.convert("RGB")
        image.thumbnail((768, 768))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=72, optimize=True)
        return base64.b64encode(out.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        return None, None


async def _download_story_preview(client: TelegramClient, story: Any) -> tuple[str | None, str | None, str]:
    """Best-effort visual preview: photo itself or a video/document thumbnail."""
    media = getattr(story, "media", None)
    if media is None:
        return None, None, "без медиа"

    media_name = media.__class__.__name__.lower()
    is_photo = "photo" in media_name or getattr(media, "photo", None) is not None
    is_document = "document" in media_name or getattr(media, "document", None) is not None

    raw: bytes | None = None
    label = "медиа"
    try:
        if is_photo:
            label = "фото"
            raw = await client.download_media(media, file=bytes)
        elif is_document:
            label = "видео/медиа"
            # For videos, use a thumbnail instead of downloading the whole file.
            try:
                raw = await client.download_media(media, file=bytes, thumb=-1)
            except Exception:
                raw = None
    except Exception:
        raw = None

    if not raw:
        return None, None, label
    preview_b64, mime = _compress_preview(raw)
    return preview_b64, mime, label


async def fetch_telegram_story_feed(owner_id: int, *, limit: int = MAX_STORIES) -> dict[str, Any]:
    """Fetch up to N active user stories from the owner's Telegram story bar.

    Telegram's stories.getAllStories is a user-only MTProto method and returns
    active stories shown in the home action bar. We intentionally ignore channel
    stories in this first warm-up test: the goal is person-to-person acquaintance.
    """
    session_string = _load_session(int(owner_id))
    api_id, api_hash = _api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramStoriesError("Telegram-сессия больше не авторизована")

        # Telethon 1.44 exposes the current raw Telegram stories API.
        result = await client(GetAllStoriesRequest(next=False, hidden=False, state=None))

        users = {
            int(getattr(user, "id")): user
            for user in (getattr(result, "users", None) or [])
            if getattr(user, "id", None) is not None
        }

        feed: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        # Keep Telegram's peer order (the same order used for the story action bar),
        # but take at most one newest active story per person in one scan.
        peer_stories = list(getattr(result, "peer_stories", None) or [])
        for group in peer_stories:
            uid = _peer_user_id(getattr(group, "peer", None))
            if not uid:
                # Channel/supergroup story: not part of this person-to-person test.
                continue
            user = users.get(uid)
            if user is None or bool(getattr(user, "bot", False)) or bool(getattr(user, "deleted", False)):
                continue

            stories = []
            for story in (getattr(group, "stories", None) or []):
                sid = getattr(story, "id", None)
                if sid is None:
                    continue
                expire_at = _story_expire_date(story)
                if expire_at is not None and expire_at.astimezone(UTC) <= now:
                    continue
                stories.append(story)
            if not stories:
                continue

            stories.sort(key=lambda item: _story_date(item) or datetime.min.replace(tzinfo=UTC), reverse=True)
            story = stories[0]
            sid = int(getattr(story, "id"))
            caption = str(getattr(story, "caption", "") or "").strip()
            preview_b64, preview_mime, media_kind = await _download_story_preview(client, story)
            username = str(getattr(user, "username", "") or "").strip()
            story_url = f"https://t.me/{username}/s/{sid}" if username else ""

            feed.append(
                {
                    "telegram_id": uid,
                    "name": _user_name(user),
                    "first_name": str(getattr(user, "first_name", "") or "").strip(),
                    "username": username,
                    "story_id": sid,
                    "story_url": story_url,
                    "caption": caption[:1500],
                    "media_kind": media_kind,
                    "preview_b64": preview_b64,
                    "preview_mime": preview_mime,
                    "date": (_story_date(story) or now).astimezone(UTC).isoformat(),
                    "expire_date": (_story_expire_date(story).astimezone(UTC).isoformat() if _story_expire_date(story) else ""),
                    "contact": bool(getattr(user, "contact", False)),
                    "mutual_contact": bool(getattr(user, "mutual_contact", False)),
                }
            )
            if len(feed) >= max(1, min(30, int(limit))):
                break

        return {
            "stories": feed,
            "checked": len(feed),
            "available_people": len(peer_stories),
        }
    finally:
        await client.disconnect()


def _extract_response_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except Exception:
        return []
    return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []


def _analyze_story_feed(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not stories:
        return []

    api_key = _secret("OPENAI_API_KEY")
    if not api_key:
        raise TelegramStoriesError("Ключ OpenAI не найден")

    manifest = []
    content: list[dict[str, Any]] = []
    for idx, item in enumerate(stories, start=1):
        manifest.append(
            {
                "story_index": idx,
                "name": item.get("name"),
                "first_name": item.get("first_name"),
                "caption": item.get("caption"),
                "media_kind": item.get("media_kind"),
                "has_visual_preview": bool(item.get("preview_b64")),
                "contact": bool(item.get("contact")),
                "mutual_contact": bool(item.get("mutual_contact")),
            }
        )
        if item.get("preview_b64"):
            content.append(
                {
                    "type": "input_text",
                    "text": f"Изображение ниже относится к story_index={idx}, автор: {item.get('name') or 'контакт'}.",
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{item.get('preview_mime') or 'image/jpeg'};base64,{item['preview_b64']}",
                    "detail": "low",
                }
            )

    system_prompt = """
Ты — Неона, помощница по мягкому человеческому знакомству в Агентстве W.
Перед тобой до 10 свежих Telegram Stories реальных людей из ленты владельца кабинета.
Твоя задача — НЕ написать ответ любой ценой, а выбрать только те Stories, где есть
естественный смысловой повод для живого разговора. Качество важнее количества.

Главный принцип:
СМЫСЛ STORY → ЖИВОЙ ОТКЛИК → КОРОТКАЯ СОБСТВЕННАЯ МЫСЛЬ → ЕСТЕСТВЕННЫЙ ВОПРОС,
если вопрос действительно помогает человеку продолжить тему.

Правила:
- сначала пойми, о чём Story НА САМОМ ДЕЛЕ: мысль, позиция, опыт, чувство, решение, ценность, событие;
- отделяй центральный смысл от случайных деталей кадра. Машина, дорога, кафе, одежда, фон и место сами по себе не являются темой разговора;
- если человек выражает позицию или убеждение, откликайся именно на эту позицию, а не на декорации вокруг неё;
- хороший ответ должен давать человеку естественный повод рассказать о себе, своём опыте или мнении;
- открытый вопрос уместен, когда он продолжает смысл. Например: «Вы всегда придерживались такой позиции или пришли к ней с опытом?»;
- НЕ задавай формальные вопросы только ради вопроса: «Куда вы ехали?», «Где это?», «Что за машина?», если сама Story не про поездку, место или машину;
- не пиши дежурные фразы вроде «Спасибо, что поделились», «Классная сторис», «Приятно было увидеть» как самостоятельный ответ;
- опирайся только на реально видимый кадр/превью и подпись; ничего не выдумывай;
- если контекста недостаточно для содержательного и естественного отклика — ПРОПУСТИ Story;
- лучше вернуть 2 сильных ответа из 10, чем 10 пустых;
- ответ 1–2 коротких предложения, живой, конкретный, без лести и без канцелярита;
- вопрос не обязателен. Если без вопроса отклик звучит естественнее — не добавляй его;
- никакого Агентства W, ИИ, партнёрства, заработка, предложения, ссылки или рекламы;
- не делай вывод, что человеку нужны деньги, партнёры или бизнес, только потому что он публикует Story;
- не оценивай внешность, возраст, здоровье, достаток или другие чувствительные признаки.

Самопроверка перед выдачей каждого ответа:
1. Я отвечаю на смысл Story, а не цепляюсь за случайную деталь?
2. Этот текст мог бы сказать живой внимательный человек?
3. У автора есть естественный повод ответить?
Если хотя бы на один вопрос ответ «нет» — пропусти эту Story.

Верни ТОЛЬКО JSON-массив. Можно вернуть меньше объектов, чем передано Stories.
Включай только Stories, прошедшие самопроверку:
[{"story_index":1,"reason":"какой смысл Story уловлен и почему ответ способен открыть разговор","reply":"готовый ответ"}]
""".strip()

    content.insert(
        0,
        {
            "type": "input_text",
            "text": "ДАННЫЕ STORIES:\n" + json.dumps(manifest, ensure_ascii=False, indent=2),
        },
    )

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _secret("TELEGRAM_STORIES_OPENAI_MODEL", "gpt-5-mini") or "gpt-5-mini",
            "instructions": system_prompt,
            "input": [{"role": "user", "content": content}],
            "store": False,
        },
        timeout=180,
    )
    response.raise_for_status()
    parsed = _parse_json_array(_extract_response_text(response.json()))

    recommendations: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in parsed:
        try:
            index = int(item.get("story_index"))
        except (TypeError, ValueError):
            continue
        if index < 1 or index > len(stories) or index in seen:
            continue
        reply = re.sub(r"\s+", " ", str(item.get("reply") or "")).strip()
        if not reply:
            continue
        seen.add(index)
        source = stories[index - 1]
        recommendations.append(
            {
                **source,
                "story_index": index,
                "reason": str(item.get("reason") or "").strip(),
                "reply": reply[:700],
            }
        )
        if len(recommendations) >= MAX_RECOMMENDATIONS:
            break

    # Качество важнее количества: пропущенные моделью Stories НЕ добиваем
    # шаблонными комментариями. Если смыслового повода нет, Неона молчит.

    recommendations.sort(key=lambda item: int(item.get("story_index") or 0))
    return recommendations


def _run(coro):
    # Streamlit executes the page synchronously in Agency W, like the existing
    # Telegram helpers. Keep a tiny fallback for environments with a loop.
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


async def _latest_incoming_message_id(owner_id: int, contact_id: int) -> int:
    """Возвращает последний входящий message_id до ручного ответа на Story.

    Эта точка отсчёта нужна, чтобы Неона не отвечала на старую переписку и
    подхватила только НОВЫЙ ответ человека после тёплого касания.
    """
    session_string = _load_session(int(owner_id))
    api_id, api_hash = _api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramStoriesError("Telegram-сессия больше не авторизована")
        entity = await client.get_entity(int(contact_id))
        async for message in client.iter_messages(entity, limit=50):
            if bool(getattr(message, "out", False)):
                continue
            return int(getattr(message, "id", 0) or 0)
        return 0
    finally:
        await client.disconnect()


def _register_story_reply_for_neona(
    owner_id: int,
    item: dict[str, Any],
) -> None:
    """Регистрирует вручную отправленный ответ на Story как вход в диалог Неоны."""
    contact_id = int(item.get("telegram_id") or 0)
    if contact_id <= 0:
        raise TelegramStoriesError("Не найден Telegram ID автора Story")

    story_id = int(item.get("story_id") or 0)
    baseline_incoming_id = int(_run(_latest_incoming_message_id(owner_id, contact_id)) or 0)
    sent_at = datetime.now(UTC).isoformat()

    initialize_dialog_after_story_reply(
        int(owner_id),
        contact_id,
        baseline_incoming_id=baseline_incoming_id,
        sent_at=sent_at,
        story_id=story_id,
        recipient_name=str(item.get("name") or "Telegram-контакт"),
    )

    # Неона-worker берёт разрешённые диалоги из рабочего sent_log.
    # Для Story используем отдельный kind и story_sent_at. Поле sent_at оставляем
    # пустым намеренно: тёплые ответы на Stories НЕ должны уменьшать дневной лимит
    # первых холодных сообщений, который в текущем Streamlit считает sent_at.
    sent_log_key = f"neona_first_message_sent_log_{int(owner_id)}"
    sent_log = st.session_state.get(sent_log_key, [])
    if not isinstance(sent_log, list):
        sent_log = []

    already_registered = any(
        isinstance(event, dict)
        and str(event.get("kind") or "") == "story_reply"
        and int(event.get("telegram_id") or 0) == contact_id
        and int(event.get("story_id") or 0) == story_id
        for event in sent_log
    )
    if not already_registered:
        sent_log.append(
            {
                "telegram_id": contact_id,
                "recipient_name": str(item.get("name") or "Telegram-контакт"),
                "sent_at": "",
                "story_sent_at": sent_at,
                "message_id": 0,
                "kind": "story_reply",
                "story_id": story_id,
                "source": "telegram_story",
            }
        )
        st.session_state[sent_log_key] = sent_log
        persist_workspace_if_changed(int(owner_id), force=True)


def prepare_telegram_stories_radar(owner_id: int, *, limit: int = MAX_STORIES) -> dict[str, Any]:
    feed = _run(fetch_telegram_story_feed(int(owner_id), limit=limit))
    stories = list(feed.get("stories") or [])
    return {
        **feed,
        "recommendations": _analyze_story_feed(stories),
    }


def render_telegram_stories_radar(owner_id: int) -> None:
    owner_id = int(owner_id)
    state_key = f"telegram_stories_radar_{owner_id}"
    result = st.session_state.get(state_key)

    st.markdown("### 🌿 Радар Stories Telegram")
    st.caption(
        "Неона смотрит до 10 верхних активных Stories людей из вашей Telegram-ленты и выбирает только те, где есть естественный повод для живого разговора. "
        "Качество важнее количества: если смыслового повода нет, Неона Story пропускает."
    )
    st.info(
        "В Telegram ответ на Story — это личное сообщение автору, а не публичный комментарий. "
        "На этапе теста Неона только предлагает текст, ничего сама не отправляет."
    )

    cols = st.columns([2.2, 1])
    with cols[0]:
        if st.button(
            "🔎 Проверить 10 Stories Telegram",
            key=f"telegram_stories_run_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            try:
                with st.spinner("Неона смотрит свежие Stories и выбирает только сильные поводы для живого разговора..."):
                    result = prepare_telegram_stories_radar(owner_id, limit=10)
                st.session_state[state_key] = result
                for item in result.get("recommendations") or []:
                    idx = int(item.get("story_index") or 0)
                    st.session_state[f"telegram_story_reply_{owner_id}_{idx}"] = str(item.get("reply") or "")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось прочитать Stories Telegram: {exc}")

    with cols[1]:
        if isinstance(result, dict) and st.button(
            "🧹 Очистить",
            key=f"telegram_stories_clear_{owner_id}",
            use_container_width=True,
        ):
            st.session_state.pop(state_key, None)
            for key in list(st.session_state.keys()):
                if str(key).startswith(f"telegram_story_reply_{owner_id}_"):
                    st.session_state.pop(key, None)
            st.rerun()

    if not isinstance(result, dict):
        return

    stories = list(result.get("stories") or [])
    recommendations = list(result.get("recommendations") or [])
    st.caption(
        f"Просмотрено Stories людей: {len(stories)} · "
        f"Неона выбрала для тёплого ответа: {len(recommendations)}"
    )

    if not stories:
        st.warning(
            "Сейчас в доступной Telegram-ленте нет активных Stories людей. "
            "Попробуйте ещё раз позже."
        )
        return

    if not recommendations:
        st.info(
            "Неона посмотрела текущие Stories, но не стала придумывать ответ там, "
            "где не увидела достаточно понятного контекста."
        )
        return

    for item in recommendations:
        idx = int(item.get("story_index") or 0)
        name = str(item.get("name") or "Telegram-контакт")
        username = str(item.get("username") or "").strip()
        reply_key = f"telegram_story_reply_{owner_id}_{idx}"

        with st.container(border=True):
            st.markdown(f"#### {name}")
            if username:
                st.caption(f"@{username}")
            if item.get("preview_b64"):
                try:
                    st.image(base64.b64decode(item["preview_b64"]), width=360)
                except Exception:
                    pass
            caption = str(item.get("caption") or "").strip()
            if caption:
                st.write(f"**Подпись Story:** {caption}")
            reason = str(item.get("reason") or "").strip()
            if reason:
                st.caption(f"Почему Неона выбрала эту Story: {reason}")

            story_url = str(item.get("story_url") or "").strip()
            if story_url:
                st.link_button("Открыть Story в Telegram", story_url, use_container_width=False)
            else:
                st.caption("У автора нет публичного username — откройте его Story в ленте Telegram.")

            st.markdown("**Ответ Неоны:**")
            st.text_area(
                "Ответ на Story",
                value=str(st.session_state.get(reply_key, item.get("reply") or "")),
                key=reply_key,
                height=110,
                label_visibility="collapsed",
            )

            contact_id = int(item.get("telegram_id") or 0)
            story_id = int(item.get("story_id") or 0)
            registered_key = (
                f"telegram_story_registered_{owner_id}_{contact_id}_{story_id}"
            )

            # Если событие уже сохранено в рабочем sent_log, состояние переживает rerun.
            sent_log_key = f"neona_first_message_sent_log_{owner_id}"
            current_sent_log = st.session_state.get(sent_log_key, [])
            if isinstance(current_sent_log, list):
                registered_from_log = any(
                    isinstance(event, dict)
                    and str(event.get("kind") or "") == "story_reply"
                    and int(event.get("telegram_id") or 0) == contact_id
                    and int(event.get("story_id") or 0) == story_id
                    for event in current_sent_log
                )
                if registered_from_log:
                    st.session_state[registered_key] = True

            if st.session_state.get(registered_key):
                st.success(
                    "✅ Ответ на Story отмечен как отправленный. Неона ждёт новый "
                    "входящий ответ этого человека и продолжит диалог сама."
                )
                st.caption(
                    "Если человек попросит материал или согласится посмотреть пример, "
                    "Неона сможет дать ссылку на Telegram-канал Агентства W. "
                    "Сама напоминать «посмотрели?» она не будет."
                )
            else:
                st.caption(
                    "Сначала отправьте этот ответ человеку вручную в Telegram, затем "
                    "нажмите кнопку ниже. Это подключит Неону только к НОВЫМ ответам "
                    "человека и не затронет старую переписку."
                )
                if st.button(
                    "✅ Ответ на Story отправлен",
                    key=f"telegram_story_sent_{owner_id}_{contact_id}_{story_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        _register_story_reply_for_neona(owner_id, item)
                        st.session_state[registered_key] = True
                        st.success(
                            "Готово. Неона подключена к продолжению этого диалога."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(
                            "Не удалось зарегистрировать ответ на Story для Неоны: "
                            + str(exc)
                        )

    st.caption(
        "Ответ на Story по-прежнему отправляется вручную. После кнопки «Ответ на Story отправлен» "
        "Неона подключается только к продолжению диалога: ждёт новый входящий ответ, ведёт "
        "разговор и даёт материалы Агентства W только после интереса или согласия человека."
    )
