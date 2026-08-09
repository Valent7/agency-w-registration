import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from neonia_contacts import render_neonia_contacts
from neonia_chats import render_neonia_chats
from neona_reglament import (
    NEONA_FORBIDDEN_AI_LABELS,
    NEONA_FORBIDDEN_CLAIMS,
    build_neona_first_message_system_prompt,
    build_neona_first_messages_system_prompt,
    neona_identity,
    neona_reglament_markdown,
)
from agency_calendar import (
    render_agency_calendar,
    render_today_meetings_compact,
)
from team_center import render_team_center

from neona_telegram_dialogs import (
    DialogError as NeonaDialogError,
    initialize_dialog_after_first_message,
    run_sync_owner_once,
)
from workspace_persistence import (
    hydrate_workspace_state_once,
    persist_workspace_if_changed,
)
import asyncio
import json
import re
import base64
from pathlib import Path

from PIL import Image
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerUser
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    FloodWaitError,
)
from cryptography.fernet import Fernet, InvalidToken

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
ICON_PATH = ASSETS_DIR / "agency_w_icon.png"
WAVE_PATH = ASSETS_DIR / "agency_w_wave.png"

page_icon = (
    Image.open(ICON_PATH)
    if ICON_PATH.exists()
    else "W"
)

st.set_page_config(
    page_title="Агентство W",
    page_icon=page_icon,
    layout="centered",
)
def _image_data_uri(path):
    """Возвращает PNG как data URI для точного HTML-размещения."""

    if not path.exists():
        return ""

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_agency_w_logo(compact=False):
    """Рисует фирменную шапку: графика отдельно, текст — текстом сайта."""

    emblem_uri = _image_data_uri(ICON_PATH)
    wave_uri = _image_data_uri(WAVE_PATH)
    mode_class = "agency-brand--compact" if compact else "agency-brand--full"

    emblem_html = (
        f'<img class="agency-brand__emblem" src="{emblem_uri}" '
        'alt="Эмблема Агентства W">'
        if emblem_uri
        else '<div class="agency-brand__emblem-fallback">W</div>'
    )
    wave_html = (
        f'<img class="agency-brand__wave" src="{wave_uri}" alt="">'
        if wave_uri
        else ""
    )

    st.markdown(
        f"""
        <section class="agency-brand {mode_class}" aria-label="Агентство W">
            <div class="agency-brand__top">
                <div class="agency-brand__emblem-wrap">
                    {emblem_html}
                </div>
                <div class="agency-brand__wordmark">
                    <div class="agency-brand__name">Агентство <span>W</span></div>
                    <div class="agency-brand__rule"></div>
                    <div class="agency-brand__latin">Acta, non verba.</div>
                    <div class="agency-brand__translation">Дела, а не слова.</div>
                </div>
            </div>
            <div class="agency-brand__present">Мы создаём своё настоящее</div>
            <div class="agency-brand__ornament" aria-hidden="true">
                <span></span><b>◇</b><span></span>
            </div>
            {wave_html}
        </section>
        """,
        unsafe_allow_html=True,
    )


def get_session_cipher():
    key = st.secrets.get("FERNET_KEY")

    if not key:
        raise RuntimeError("FERNET_KEY не найден в Streamlit Secrets.")

    return Fernet(str(key).encode())


def encrypt_telegram_session(session_string):
    if not session_string:
        return ""

    cipher = get_session_cipher()
    return cipher.encrypt(session_string.encode()).decode()


def decrypt_telegram_session(encrypted_session):
    if not encrypted_session:
        return ""

    try:
        cipher = get_session_cipher()
        return cipher.decrypt(encrypted_session.encode()).decode()
    except InvalidToken:
        return ""
# Получаем код пригласившего из ссылки вида:
# https://agency-w.streamlit.app/?ref=W12345
referral_code = st.query_params.get("ref", "").strip()
def ask_openai(
    system_prompt,
    user_message,
    uploaded_files=None,
    use_web_search=False,
):
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        return "Ключ OpenAI не найден в настройках приложения."

    uploaded_files = uploaded_files or []

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
    }

    try:
        input_content = [
            {
                "type": "input_text",
                "text": user_message,
            }
        ]

        for uploaded_file in uploaded_files:
            upload_response = requests.post(
                "https://api.openai.com/v1/files",
                headers=auth_headers,
                data={
                    "purpose": "user_data",
                    "expires_after[anchor]": "created_at",
                    "expires_after[seconds]": "86400",
                },
                files={
                    "file": (
                        uploaded_file.name,
                        uploaded_file.getvalue(),
                        uploaded_file.type
                        or "application/octet-stream",
                    )
                },
                timeout=120,
            )

            upload_response.raise_for_status()
            file_id = upload_response.json()["id"]

            file_item = {
                "type": "input_file",
                "file_id": file_id,
            }

            if uploaded_file.name.lower().endswith(".pdf"):
                file_item["detail"] = "high"

            input_content.insert(0, file_item)

        request_body = {
            "model": "gpt-5-mini",
            "instructions": system_prompt,
            "input": [
                {
                    "role": "user",
                    "content": input_content,
                }
            ],
            "store": False,
        }

        if use_web_search:
            request_body["tools"] = [
                {
                    "type": "web_search",
                    "search_context_size": "medium",
                }
            ]

        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                **auth_headers,
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=180,
        )

        response.raise_for_status()
        data = response.json()

        text_parts = []

        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_parts.append(content.get("text", ""))

        answer = "\n".join(text_parts).strip()

        if not answer:
            return "ИИ не сформировал ответ. Попробуйте ещё раз."

        return answer

    except requests.exceptions.HTTPError as error:
        if error.response is not None:
            details = error.response.text[:500]
            return f"Ошибка OpenAI: {details}"

        return "OpenAI вернул ошибку без пояснения."

    except requests.exceptions.RequestException:
        return (
            "Не удалось связаться с ИИ. "
            "Проверьте подключение и повторите попытку."
        )

    except (KeyError, ValueError):
        return "Не удалось обработать ответ OpenAI."
        
def save_member_to_supabase(telegram_data, referral_code):
    telegram_id = int(telegram_data["id"])
    member_code = f"W{telegram_id}"

    payload = {
        "telegram_id": telegram_id,
        "first_name": telegram_data.get("first_name", "Пользователь"),
        "username": telegram_data.get("username") or None,
        "member_code": member_code,
        "referrer_code": referral_code or None,
    }

    response = requests.post(
        f"{st.secrets['SUPABASE_URL']}/rest/v1/agency_members",
        headers={
            "apikey": st.secrets["SUPABASE_SECRET_KEY"],
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=payload,
        timeout=10,
    )

    if response.status_code == 409:
        return member_code, False

    response.raise_for_status()
    return member_code, True


def save_telegram_session_to_supabase(telegram_id, session_string):
    encrypted_session = encrypt_telegram_session(session_string)
    secret_key = st.secrets["SUPABASE_SECRET_KEY"]

    payload = {
        "telegram_id": int(telegram_id),
        "encrypted_session": encrypted_session,
        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }

    response = requests.post(
        (
            f"{st.secrets['SUPABASE_URL']}"
            "/rest/v1/telegram_sessions?on_conflict=telegram_id"
        ),
        headers={
            "apikey": secret_key,
            
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
        json=payload,
        timeout=10,
    )

    response.raise_for_status()
    return True


def load_telegram_session_from_supabase(telegram_id):
    secret_key = st.secrets["SUPABASE_SECRET_KEY"]

    response = requests.get(
        f"{st.secrets['SUPABASE_URL']}/rest/v1/telegram_sessions",
        headers={
            "apikey": secret_key,
            
        },
        params={
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "encrypted_session",
            "limit": 1,
        },
        timeout=10,
    )

    response.raise_for_status()
    rows = response.json()

    if not rows:
        return ""

    encrypted_session = rows[0].get("encrypted_session", "")
    return decrypt_telegram_session(encrypted_session)


def run_telegram_async(coroutine):
    return asyncio.run(coroutine)


def get_telegram_api_credentials():
    api_id = int(st.secrets["TELEGRAM_API_ID"])
    api_hash = str(st.secrets["TELEGRAM_API_HASH"])
    return api_id, api_hash


async def request_telegram_login_code(phone):
    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(StringSession(), api_id, api_hash)

    await client.connect()

    try:
        sent_code = await client.send_code_request(phone)

        return {
            "pending_session": encrypt_telegram_session(
                client.session.save()
            ),
            "phone_code_hash": sent_code.phone_code_hash,
        }
    finally:
        await client.disconnect()


async def verify_telegram_login_code(
    phone,
    code,
    pending_session,
    phone_code_hash,
):
    session_string = decrypt_telegram_session(pending_session)
    api_id, api_hash = get_telegram_api_credentials()

    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    await client.connect()

    try:
        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except SessionPasswordNeededError:
            return {
                "needs_password": True,
                "pending_session": encrypt_telegram_session(
                    client.session.save()
                ),
            }

        telegram_user = await client.get_me()

        return {
            "needs_password": False,
            "telegram_id": int(telegram_user.id),
            "session_string": client.session.save(),
        }
    finally:
        await client.disconnect()


async def verify_telegram_2fa_password(
    pending_session,
    password,
):
    session_string = decrypt_telegram_session(pending_session)
    api_id, api_hash = get_telegram_api_credentials()

    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    await client.connect()

    try:
        await client.sign_in(password=password)
        telegram_user = await client.get_me()

        return {
            "telegram_id": int(telegram_user.id),
            "session_string": client.session.save(),
        }
    finally:
        await client.disconnect()


async def fetch_telegram_contacts(telegram_id):
    session_string = load_telegram_session_from_supabase(
        telegram_id
    )

    if not session_string:
        return []

    api_id, api_hash = get_telegram_api_credentials()

    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    await client.connect()

    try:
        if not await client.is_user_authorized():
            return []

        result = await client(
            GetContactsRequest(hash=0)
        )

        contacts = []

        for user in result.users:
            if getattr(user, "deleted", False):
                continue

            if getattr(user, "bot", False):
                continue

            first_name = (user.first_name or "").strip()
            last_name = (user.last_name or "").strip()

            full_name = " ".join(
                part
                for part in [first_name, last_name]
                if part
            ).strip()

            contacts.append(
                {
                    "telegram_id": int(user.id),
                    "name": full_name or "Без имени",
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": user.username or "",
                    "phone": user.phone or "",
                }
            )

        contacts.sort(
            key=lambda contact: contact["name"].lower()
        )

        return contacts

    finally:
        await client.disconnect()



async def fetch_telegram_chats(telegram_id):
    """Получает доступные группы, супергруппы и каналы владельца."""

    session_string = load_telegram_session_from_supabase(
        telegram_id
    )

    if not session_string:
        return []

    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    await client.connect()

    try:
        if not await client.is_user_authorized():
            return []

        chats = []

        async for dialog in client.iter_dialogs(limit=500):
            is_group = bool(
                getattr(dialog, "is_group", False)
            )
            is_channel = bool(
                getattr(dialog, "is_channel", False)
            )

            # Личные диалоги с людьми здесь не показываем.
            if not (is_group or is_channel):
                continue

            entity = dialog.entity
            if getattr(entity, "left", False):
                continue

            try:
                chat_id = int(getattr(entity, "id"))
            except (TypeError, ValueError):
                continue

            if getattr(entity, "broadcast", False):
                chat_type = "Канал"
            elif getattr(entity, "megagroup", False):
                chat_type = "Супергруппа"
            elif is_group:
                chat_type = "Группа"
            else:
                chat_type = "Чат"

            username = str(
                getattr(entity, "username", "") or ""
            ).strip()

            try:
                participants_count = int(
                    getattr(entity, "participants_count", 0)
                    or 0
                )
            except (TypeError, ValueError):
                participants_count = 0

            try:
                unread_count = int(
                    getattr(dialog, "unread_count", 0)
                    or 0
                )
            except (TypeError, ValueError):
                unread_count = 0

            folder_id = getattr(dialog, "folder_id", None)

            chats.append(
                {
                    "chat_id": chat_id,
                    "title": (
                        str(dialog.name or "").strip()
                        or "Без названия"
                    ),
                    "type": chat_type,
                    "username": username,
                    "is_public": bool(username),
                    "participants_count": participants_count,
                    "unread_count": unread_count,
                    "is_archived": folder_id == 1,
                    "is_pinned": bool(
                        getattr(dialog, "pinned", False)
                    ),
                }
            )

        chats.sort(
            key=lambda item: (
                item.get("is_archived", False),
                item.get("type", ""),
                item.get("title", "").lower(),
            )
        )

        return chats

    finally:
        await client.disconnect()


async def resolve_telegram_chat_entity(client, chat):
    """Находит Telegram-сущность чата по username или сохранённому ID."""

    username = str(chat.get("username") or "").strip().lstrip("@")
    if username:
        try:
            return await client.get_entity(username)
        except Exception:
            pass

    try:
        expected_chat_id = int(chat.get("chat_id"))
    except (TypeError, ValueError):
        expected_chat_id = 0

    if expected_chat_id:
        async for dialog in client.iter_dialogs(limit=500):
            entity = dialog.entity
            try:
                entity_id = int(getattr(entity, "id"))
            except (TypeError, ValueError):
                continue

            if entity_id == expected_chat_id:
                return entity

    raise RuntimeError(
        "Чат не найден в подключённом Telegram. "
        "Обновите список чатов и повторите попытку."
    )


async def fetch_telegram_chat_members(
    telegram_id,
    chat,
    limit=200,
):
    """Получает доступных участников выбранной группы без ботов."""

    session_string = load_telegram_session_from_supabase(
        telegram_id
    )
    if not session_string:
        return []

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(10, min(500, limit))

    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )
    await client.connect()

    try:
        if not await client.is_user_authorized():
            return []

        chat_entity = await resolve_telegram_chat_entity(
            client,
            chat,
        )
        current_user = await client.get_me()
        owner_id = int(current_user.id)
        members = []

        async for user in client.iter_participants(
            chat_entity,
            limit=limit,
        ):
            if getattr(user, "deleted", False):
                continue
            if getattr(user, "bot", False):
                continue
            if int(user.id) == owner_id:
                continue

            first_name = str(
                getattr(user, "first_name", "") or ""
            ).strip()
            last_name = str(
                getattr(user, "last_name", "") or ""
            ).strip()
            full_name = " ".join(
                part
                for part in (first_name, last_name)
                if part
            ).strip()

            access_hash = getattr(user, "access_hash", None)
            try:
                access_hash = (
                    int(access_hash)
                    if access_hash is not None
                    else None
                )
            except (TypeError, ValueError):
                access_hash = None

            members.append(
                {
                    "telegram_id": int(user.id),
                    "access_hash": access_hash,
                    "name": full_name or "Без имени",
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": str(
                        getattr(user, "username", "") or ""
                    ).strip(),
                    "mutual_contact": bool(
                        getattr(user, "mutual_contact", False)
                    ),
                    "verified": bool(
                        getattr(user, "verified", False)
                    ),
                    "telegram_warning": bool(
                        getattr(user, "scam", False)
                        or getattr(user, "fake", False)
                    ),
                    "source_chat_id": int(chat["chat_id"]),
                    "source_chat_title": str(
                        chat.get("title") or "Без названия"
                    ),
                }
            )

        members.sort(
            key=lambda item: item["name"].lower()
        )
        return members

    except ChatAdminRequiredError as exc:
        raise RuntimeError(
            "Telegram не разрешил получить список участников этого чата. "
            "Возможно, список скрыт или доступен только администраторам."
        ) from exc
    except ChannelPrivateError as exc:
        raise RuntimeError(
            "Этот чат закрыт или больше недоступен подключённому аккаунту."
        ) from exc
    except FloodWaitError as exc:
        raise RuntimeError(
            "Telegram временно ограничил запросы. Повторите попытку через "
            f"{getattr(exc, 'seconds', 60)} секунд."
        ) from exc
    finally:
        await client.disconnect()


def build_chat_member_peer(member):
    """Создаёт InputPeerUser из сохранённого ID и access_hash."""

    try:
        user_id = int(member.get("telegram_id"))
        access_hash = member.get("access_hash")
        if access_hash is None:
            return None
        return InputPeerUser(
            user_id=user_id,
            access_hash=int(access_hash),
        )
    except (TypeError, ValueError):
        return None


async def fetch_chat_member_contexts(
    telegram_id,
    chat,
    members_batch,
):
    """Получает bio и публичные сообщения участников в выбранном чате."""

    session_string = load_telegram_session_from_supabase(
        telegram_id
    )
    if not session_string:
        return []

    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )
    await client.connect()

    contexts = []

    try:
        if not await client.is_user_authorized():
            return []

        chat_entity = await resolve_telegram_chat_entity(
            client,
            chat,
        )

        for member in members_batch:
            context = {
                "telegram_id": int(member["telegram_id"]),
                "name": member.get("name") or "Без имени",
                "first_name": member.get("first_name") or "",
                "username": member.get("username") or "",
                "about": "",
                "mutual_contact": bool(
                    member.get("mutual_contact", False)
                ),
                "verified": bool(member.get("verified", False)),
                "telegram_warning": bool(
                    member.get("telegram_warning", False)
                ),
                "source_chat_id": int(chat["chat_id"]),
                "source_chat_title": str(
                    chat.get("title") or "Без названия"
                ),
                "recent_public_messages": [],
            }

            member_peer = build_chat_member_peer(member)
            member_entity = None

            if member_peer is not None:
                try:
                    member_entity = await client.get_entity(member_peer)
                except Exception:
                    member_entity = None

            if member_entity is None and member.get("username"):
                try:
                    member_entity = await client.get_entity(
                        str(member["username"]).lstrip("@")
                    )
                except Exception:
                    member_entity = None

            if member_entity is not None:
                try:
                    full_user = await client(
                        GetFullUserRequest(member_entity)
                    )
                    context["about"] = (
                        getattr(
                            full_user.full_user,
                            "about",
                            "",
                        )
                        or ""
                    )[:700]
                except Exception:
                    pass

                try:
                    async for message in client.iter_messages(
                        chat_entity,
                        from_user=member_entity,
                        limit=5,
                    ):
                        message_text = str(
                            getattr(message, "message", "") or ""
                        ).strip()
                        if not message_text:
                            continue

                        context["recent_public_messages"].append(
                            {
                                "text": message_text[:600],
                                "date": (
                                    message.date.isoformat()
                                    if getattr(message, "date", None)
                                    else ""
                                ),
                            }
                        )
                except Exception:
                    pass

            contexts.append(context)

        return contexts

    except FloodWaitError as exc:
        raise RuntimeError(
            "Telegram временно ограничил запросы. Повторите попытку через "
            f"{getattr(exc, 'seconds', 60)} секунд."
        ) from exc
    finally:
        await client.disconnect()


def analyze_chat_members_for_target_audience(
    passport_analysis,
    member_contexts,
):
    """Сравнивает участников выбранного чата с паспортом ЦА."""

    system_prompt = """
Ты — Неония, аналитик и селектор Агентства W.

Сравни участников Telegram-чата с паспортом целевой аудитории проекта.
Используй только переданные сведения: имя, username, bio и публичные
сообщения человека в выбранном чате. Не используй личную переписку.
Не делай выводов по имени, полу, возрасту, национальности, фотографии,
языку или иным чувствительным признакам. Не придумывай факты.

Если данных мало, прямо укажи: «Недостаточно данных».

Для каждого человека верни:
- telegram_id;
- segment;
- score — целое число от 0 до 100;
- confidence: «высокая», «средняя» или «низкая»;
- reasons — список из 1–3 коротких оснований;
- recommendation — строго одно из:
  «Передать Неоне»,
  «Нужно больше данных»,
  «Пока не подходит»;
- message_angle — безопасная тема возможного знакомства,
  без обещаний дохода, давления и массовой рассылки.

Отсутствие username или bio не является отрицательным признаком.
Telegram-предупреждение scam/fake является основанием не рекомендовать
человека для обращения.

Верни ТОЛЬКО JSON-массив без пояснений и без Markdown.
"""

    request = (
        "ПАСПОРТ ЦЕЛЕВОЙ АУДИТОРИИ:\n"
        f"{passport_analysis}\n\n"
        "УЧАСТНИКИ ВЫБРАННОГО ЧАТА:\n"
        f"{json.dumps(member_contexts, ensure_ascii=False)}"
    )

    answer = ask_openai(system_prompt, request)
    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    raw_results = extract_json_array(answer)
    source_by_id = {
        int(item["telegram_id"]): item
        for item in member_contexts
    }
    normalized = []

    for item in raw_results:
        try:
            member_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue

        source = source_by_id.get(member_id)
        if not source:
            continue

        try:
            score = int(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))

        reasons = item.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            reasons = []

        recommendation = item.get(
            "recommendation",
            "Нужно больше данных",
        )
        allowed_recommendations = {
            "Передать Неоне",
            "Нужно больше данных",
            "Пока не подходит",
        }
        if recommendation not in allowed_recommendations:
            recommendation = "Нужно больше данных"

        if source.get("telegram_warning"):
            recommendation = "Пока не подходит"
            score = min(score, 20)
            reasons = [
                "В Telegram есть предупреждение о профиле."
            ]

        normalized.append(
            {
                "telegram_id": member_id,
                "name": source.get("name") or "Без имени",
                "first_name": source.get("first_name") or "",
                "username": source.get("username") or "",
                "segment": str(
                    item.get("segment") or "Не определён"
                ),
                "score": score,
                "confidence": str(
                    item.get("confidence") or "низкая"
                ),
                "reasons": [
                    str(reason)[:300]
                    for reason in reasons[:3]
                ] or ["Недостаточно данных"],
                "recommendation": recommendation,
                "message_angle": str(
                    item.get("message_angle")
                    or "Нейтральное знакомство"
                )[:500],
                "status": "Новый кандидат",
                "source": "Участник Telegram-чата",
                "source_chat_id": int(
                    source.get("source_chat_id") or 0
                ),
                "source_chat_title": str(
                    source.get("source_chat_title")
                    or "Без названия"
                ),
                "profile_about": str(
                    source.get("about") or ""
                )[:700],
                "public_messages": [
                    {
                        "text": str(message.get("text") or "")[:600],
                        "date": str(message.get("date") or ""),
                    }
                    for message in (
                        source.get("recent_public_messages") or []
                    )[:5]
                    if str(message.get("text") or "").strip()
                ],
                "mutual_contact": bool(
                    source.get("mutual_contact", False)
                ),
                "verified": bool(source.get("verified", False)),
                "analyzed_at": datetime.now(
                    ZoneInfo("Europe/Berlin")
                ).isoformat(),
            }
        )

    normalized.sort(
        key=lambda item: item.get("score", 0),
        reverse=True,
    )
    return normalized


async def send_telegram_first_message(
    owner_telegram_id,
    recipient_telegram_id,
    recipient_username,
    message,
    recipient_source_chat_id=None,
):
    """Отправляет одно утверждённое первое сообщение от аккаунта владельца."""

    message = str(message or "").strip()
    if not message:
        raise RuntimeError("Текст сообщения пуст.")

    owner_telegram_id = int(owner_telegram_id)
    recipient_telegram_id = int(recipient_telegram_id)

    if owner_telegram_id == recipient_telegram_id:
        raise RuntimeError(
            "Нельзя отправить первое сообщение самому себе."
        )

    session_string = load_telegram_session_from_supabase(
        owner_telegram_id
    )
    if not session_string:
        raise RuntimeError(
            "Сессия Telegram не найдена. Подключите Telegram заново."
        )

    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )
    await client.connect()

    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram-сессия больше не авторизована."
            )

        current_user = await client.get_me()
        if int(current_user.id) != owner_telegram_id:
            raise RuntimeError(
                "Подключён другой Telegram-аккаунт."
            )

        entity = None
        contacts_result = await client(
            GetContactsRequest(hash=0)
        )
        for user in contacts_result.users:
            if int(user.id) == recipient_telegram_id:
                entity = user
                break

        if entity is None and recipient_username:
            entity = await client.get_entity(
                str(recipient_username).lstrip("@")
            )

        # Для кандидата, найденного в группе без username, пробуем найти
        # Telegram-сущность внутри исходного чата. Это не читает личную
        # переписку и используется только для адресной отправки уже
        # утверждённого владельцем первого сообщения.
        if entity is None and recipient_source_chat_id:
            try:
                source_chat = {
                    "chat_id": int(recipient_source_chat_id),
                    "username": "",
                }
                chat_entity = await resolve_telegram_chat_entity(
                    client,
                    source_chat,
                )
                async for participant in client.iter_participants(
                    chat_entity,
                    limit=5000,
                ):
                    if int(participant.id) == recipient_telegram_id:
                        entity = participant
                        break
            except Exception:
                entity = None

        if entity is None:
            raise RuntimeError(
                "Telegram не смог определить получателя. У человека нет "
                "доступного username, он не найден среди контактов или "
                "исходный чат больше недоступен."
            )

        # Последнее входящее сообщение, существовавшее ДО первого
        # сообщения Агентства W. Всё, что придёт позже, должна обработать Неона.
        baseline_incoming_message_id = 0
        try:
            async for previous_message in client.iter_messages(entity, limit=50):
                if previous_message.out:
                    continue
                if not getattr(previous_message, "message", None):
                    continue
                baseline_incoming_message_id = int(previous_message.id)
                break
        except Exception:
            baseline_incoming_message_id = 0

        sent_message = await client.send_message(
            entity,
            message,
            parse_mode=None,
            link_preview=False,
        )

        sent_at = datetime.now(
            ZoneInfo("Europe/Berlin")
        ).isoformat()
        return {
            "message_id": int(sent_message.id),
            "sent_at": sent_at,
            "baseline_incoming_message_id": int(
                baseline_incoming_message_id
            ),
        }
    finally:
        await client.disconnect()


def friendly_telegram_send_error(error):
    """Преобразует техническую ошибку Telegram в понятный текст."""

    error_name = error.__class__.__name__

    if error_name == "FloodWaitError":
        seconds = getattr(error, "seconds", None)
        if seconds:
            return (
                "Telegram временно ограничил отправку. "
                f"Повторите через {seconds} секунд."
            )
        return "Telegram временно ограничил отправку. Повторите позже."

    error_messages = {
        "PeerFloodError": (
            "Telegram ограничил новые обращения. "
            "Сегодня больше не отправляйте первые сообщения."
        ),
        "UserPrivacyRestrictedError": (
            "Настройки приватности этого человека не позволяют "
            "отправить ему сообщение."
        ),
        "UserIsBlockedError": (
            "Отправка невозможна: контакт заблокирован."
        ),
        "InputUserDeactivatedError": (
            "Аккаунт этого человека удалён или деактивирован."
        ),
        "PeerIdInvalidError": (
            "Telegram не смог определить получателя. "
            "Обновите список контактов."
        ),
        "MessageTooLongError": (
            "Сообщение слишком длинное. Сократите текст и повторите."
        ),
    }

    return error_messages.get(
        error_name,
        f"Telegram не отправил сообщение: {error}",
    )


def count_first_messages_sent_today(sent_log):
    """Считает первые сообщения, отправленные сегодня по Берлину."""

    today = datetime.now(
        ZoneInfo("Europe/Berlin")
    ).date().isoformat()

    return sum(
        1
        for event in sent_log
        if str(event.get("sent_at", ""))[:10] == today
    )

def extract_json_array(answer):
    """Извлекает JSON-массив из ответа ИИ."""

    if not answer:
        raise ValueError("ИИ вернул пустой ответ.")

    cleaned = answer.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "", 1)
        cleaned = cleaned.replace("```", "")

    start = cleaned.find("[")
    end = cleaned.rfind("]")

    if start == -1 or end == -1 or end < start:
        raise ValueError(
            "ИИ не вернул структурированный список результатов."
        )

    return json.loads(cleaned[start:end + 1])


async def fetch_telegram_contact_contexts(
    telegram_id,
    contacts_batch,
):
    """Получает минимум доступного контекста для партии контактов."""

    session_string = load_telegram_session_from_supabase(
        telegram_id
    )

    if not session_string:
        return []

    api_id, api_hash = get_telegram_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    await client.connect()
    contexts = []

    try:
        if not await client.is_user_authorized():
            return []

        for contact in contacts_batch:
            context = {
                "telegram_id": int(contact["telegram_id"]),
                "name": contact.get("name") or "Без имени",
                "first_name": contact.get("first_name") or "",
                "username": contact.get("username") or "",
                "about": "",
                "mutual_contact": False,
                "verified": False,
                "telegram_warning": False,
                "recent_messages": [],
            }

            try:
                entity = await client.get_entity(
                    int(contact["telegram_id"])
                )

                context["mutual_contact"] = bool(
                    getattr(entity, "mutual_contact", False)
                )
                context["verified"] = bool(
                    getattr(entity, "verified", False)
                )
                context["telegram_warning"] = bool(
                    getattr(entity, "scam", False)
                    or getattr(entity, "fake", False)
                )

                try:
                    full_user = await client(
                        GetFullUserRequest(entity)
                    )
                    context["about"] = (
                        getattr(
                            full_user.full_user,
                            "about",
                            "",
                        )
                        or ""
                    )[:700]
                except Exception:
                    pass

                try:
                    async for message in client.iter_messages(
                        entity,
                        limit=6,
                    ):
                        message_text = (
                            getattr(message, "message", "")
                            or ""
                        ).strip()

                        if not message_text:
                            continue

                        context["recent_messages"].append(
                            {
                                "direction": (
                                    "от владельца"
                                    if getattr(message, "out", False)
                                    else "от контакта"
                                ),
                                "text": message_text[:500],
                                "date": (
                                    message.date.isoformat()
                                    if getattr(message, "date", None)
                                    else ""
                                ),
                            }
                        )
                except Exception:
                    pass

            except Exception:
                pass

            contexts.append(context)

        return contexts

    finally:
        await client.disconnect()


def analyze_contacts_for_target_audience(
    passport_analysis,
    contact_contexts,
):
    """Сравнивает партию контактов с паспортом ЦА."""

    system_prompt = """
Ты — Неония, аналитик и селектор Агентства W.

Сравни контакты с паспортом целевой аудитории проекта.
Используй только переданные данные. Не делай выводов по одному имени,
национальности, полу, возрасту или фотографии. Не придумывай факты.

Если сведений мало, прямо укажи:
«Недостаточно данных».

Для каждого контакта верни:
- telegram_id;
- segment;
- score — целое число от 0 до 100;
- confidence: «высокая», «средняя» или «низкая»;
- reasons — список из 1–3 коротких оснований;
- recommendation — строго одно из:
  «Передать Неоне»,
  «Нужно больше данных»,
  «Пока не подходит»;
- message_angle — безопасная тема первого обращения,
  без обещаний дохода и давления.

Оценка должна опираться прежде всего на bio, прежнюю переписку
и явные интересы человека. Отсутствие username, телефона или bio
не является отрицательным признаком.

Верни ТОЛЬКО JSON-массив без пояснений и без Markdown.
"""

    request = (
        "ПАСПОРТ ЦЕЛЕВОЙ АУДИТОРИИ:\n"
        f"{passport_analysis}\n\n"
        "КОНТАКТЫ ДЛЯ АНАЛИЗА:\n"
        f"{json.dumps(contact_contexts, ensure_ascii=False)}"
    )

    answer = ask_openai(
        system_prompt,
        request,
    )

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    raw_results = extract_json_array(answer)
    source_by_id = {
        int(item["telegram_id"]): item
        for item in contact_contexts
    }
    normalized = []

    for item in raw_results:
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue

        source = source_by_id.get(contact_id)
        if not source:
            continue

        try:
            score = int(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0

        score = max(0, min(100, score))

        reasons = item.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            reasons = []

        recommendation = item.get(
            "recommendation",
            "Нужно больше данных",
        )
        allowed_recommendations = {
            "Передать Неоне",
            "Нужно больше данных",
            "Пока не подходит",
        }
        if recommendation not in allowed_recommendations:
            recommendation = "Нужно больше данных"

        normalized.append(
            {
                "telegram_id": contact_id,
                "name": source.get("name") or "Без имени",
                "first_name": source.get("first_name") or "",
                "username": source.get("username") or "",
                "segment": (
                    str(item.get("segment") or "Не определён")
                ),
                "score": score,
                "confidence": (
                    str(item.get("confidence") or "низкая")
                ),
                "reasons": [
                    str(reason)[:300]
                    for reason in reasons[:3]
                ] or ["Недостаточно данных"],
                "recommendation": recommendation,
                "message_angle": (
                    str(
                        item.get("message_angle")
                        or "Нейтральное знакомство"
                    )[:500]
                ),
                "status": "Новый кандидат",
                "analyzed_at": datetime.now(
                    ZoneInfo("Europe/Berlin")
                ).isoformat(),
            }
        )

    return normalized


def merge_candidate_results(existing, new_results):
    """Добавляет новые результаты без дублей."""

    merged = {
        int(item["telegram_id"]): item
        for item in existing
    }

    for item in new_results:
        contact_id = int(item["telegram_id"])
        previous = merged.get(contact_id, {})
        item["status"] = previous.get(
            "status",
            item.get("status", "Новый кандидат"),
        )
        merged[contact_id] = item

    return sorted(
        merged.values(),
        key=lambda item: (
            item.get("recommendation") != "Передать Неоне",
            -int(item.get("score", 0)),
            item.get("name", "").lower(),
        ),
    )


NEONA_FIRST_MESSAGE_FORBIDDEN = NEONA_FORBIDDEN_CLAIMS


def candidate_first_name(contact):
    """Возвращает безопасное короткое имя для обращения."""

    first_name = str(contact.get("first_name") or "").strip()
    if first_name and len(first_name) <= 40:
        return first_name

    full_name = str(contact.get("name") or "").strip()
    if not full_name:
        return ""

    first_word = full_name.split()[0].strip(" ,.!?;:()[]{}\"'")
    if not first_word or len(first_word) > 40:
        return ""

    if any(character.isdigit() for character in first_word):
        return ""

    return first_word


def build_neona_safe_first_message(owner_name, contact):
    """Правдивый запасной текст о реально работающих возможностях."""

    first_name = candidate_first_name(contact)
    greeting = (
        f"{first_name}, здравствуйте!"
        if first_name
        else "Здравствуйте!"
    )

    return (
        f"{greeting} {neona_identity(owner_name)} "
        f"{owner_name} создаёт команду ИИ-помощников, где каждый отвечает "
        "за свою часть работы. Один находит подходящих людей, другой готовит "
        "для каждого персональное первое сообщение, а окончательное решение "
        "всегда остаётся за человеком. Уже сейчас они работают как единая "
        "команда, и со временем она будет расти. Хотите увидеть, как это "
        "выглядит на реальном примере?"
    )


def validate_neona_first_message(message, owner_name):
    """Проверяет только существенные рамки первого сообщения Неоны.

    Валидатор не считает знаки и предложения и не требует точного совпадения
    с шаблонными фразами. Он блокирует только действительно опасные случаи:
    отсутствие представления, несколько вопросов, запрещённые обещания,
    слово «бот» и ссылки в первом сообщении.
    """

    message = str(message or "").strip()
    lowered = message.lower()
    errors = []

    if not message:
        return ["сообщение пустое"]

    has_neona_name = "меня зовут неона" in lowered
    has_helper_role = "помощниц" in lowered
    if not (has_neona_name and has_helper_role):
        errors.append("нет понятного представления Неоны как помощницы владельца")

    if message.count("?") != 1:
        errors.append("в первом сообщении должен быть один простой вопрос")

    if "ии-помощник" not in lowered and "ии‑помощник" not in lowered:
        errors.append("не сказано о команде ИИ-помощников")

    if "http://" in lowered or "https://" in lowered or "www." in lowered:
        errors.append("в первом сообщении нельзя отправлять ссылку")

    message_words = set(
        re.findall(r"[a-zа-яё]+(?:[-‑][a-zа-яё]+)?", lowered)
    )
    forbidden_ai_labels = set(NEONA_FORBIDDEN_AI_LABELS)
    if message_words.intersection(forbidden_ai_labels):
        errors.append(
            "ИИ-помощников нельзя называть ботами или чат-ботами"
        )

    for phrase in NEONA_FIRST_MESSAGE_FORBIDDEN:
        if phrase in lowered:
            errors.append(f"запрещённая формулировка: {phrase}")

    return errors

def finalize_neona_first_message(message, owner_name, contact):
    """Нормализует текст и заменяет небезопасный вариант шаблоном."""

    message = ensure_neona_identity(message, owner_name)
    message = re.sub(r"\s+", " ", message).strip()
    errors = validate_neona_first_message(message, owner_name)

    if errors:
        return build_neona_safe_first_message(owner_name, contact)

    return message


def generate_neona_first_messages(
    owner_name,
    passport_analysis,
    selected_candidates,
):
    """Создаёт первые сообщения для выбранных кандидатов."""

    system_prompt = build_neona_first_messages_system_prompt(
        owner_name
    )

    safe_candidates = [
        {
            "telegram_id": item["telegram_id"],
            "name": item.get("name", ""),
            "first_name": item.get("first_name", ""),
            "username": item.get("username", ""),
            "segment": item.get("segment", ""),
            "score": item.get("score", 0),
            "reasons": item.get("reasons", []),
            "message_angle": item.get("message_angle", ""),
            "source": item.get("source", "Рекомендация Неонии"),
            "source_chat_title": item.get("source_chat_title", ""),
            "profile_about": item.get("profile_about", ""),
            "public_messages": item.get("public_messages", [])[:3],
        }
        for item in selected_candidates
    ]

    request = (
        "ПАСПОРТ ЦА:\n"
        f"{passport_analysis}\n\n"
        "ВЫБРАННЫЕ КАНДИДАТЫ:\n"
        f"{json.dumps(safe_candidates, ensure_ascii=False)}"
    )

    answer = ask_openai(system_prompt, request)

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    raw_messages = extract_json_array(answer)
    allowed_ids = {
        int(item["telegram_id"])
        for item in selected_candidates
    }
    candidate_lookup = {
        int(item["telegram_id"]): item
        for item in selected_candidates
    }
    result = {}

    for item in raw_messages:
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue

        if contact_id not in allowed_ids:
            continue

        message = str(item.get("message") or "").strip()
        if not message:
            continue

        contact = candidate_lookup.get(contact_id, {})
        message = finalize_neona_first_message(
            message,
            owner_name,
            contact,
        )

        result[contact_id] = {
            "message": message,
            "approved": False,
            "status": "Сообщение подготовлено",
        }

    # Даже если модель пропустила кандидата, создаём безопасный черновик.
    for contact_id in allowed_ids:
        if contact_id in result:
            continue
        contact = candidate_lookup.get(contact_id, {})
        result[contact_id] = {
            "message": build_neona_safe_first_message(
                owner_name,
                contact,
            ),
            "approved": False,
            "status": "Сообщение подготовлено",
        }

    return result

def search_known_contacts(contacts, query, limit=20):
    """Ищет знакомого среди уже загруженных Telegram-контактов."""

    query = (query or "").strip()
    if len(query) < 2:
        return []

    lowered_query = query.lower().lstrip("@")
    query_digits = "".join(
        character
        for character in query
        if character.isdigit()
    )

    matches = []

    for contact in contacts:
        name = str(contact.get("name") or "").strip()
        username = str(
            contact.get("username") or ""
        ).strip().lstrip("@")
        phone = str(contact.get("phone") or "").strip()
        phone_digits = "".join(
            character
            for character in phone
            if character.isdigit()
        )

        name_match = lowered_query in name.lower()
        username_match = (
            bool(username)
            and lowered_query in username.lower()
        )
        phone_match = (
            len(query_digits) >= 3
            and query_digits in phone_digits
        )

        if name_match or username_match or phone_match:
            matches.append(contact)

    matches.sort(
        key=lambda contact: (
            lowered_query
            not in str(
                contact.get("name") or ""
            ).lower(),
            str(contact.get("name") or "").lower(),
        )
    )

    return matches[:limit]



def ensure_neona_identity(message, owner_name):
    """Гарантирует одно простое представление Неоны без повторов."""

    message = str(message or "").strip()
    identity = neona_identity(owner_name)

    # Старое представление из предыдущих версий заменяем новым.
    old_identity_patterns = (
        r"(?i)меня\s+зовут\s+неона,?\s+я\s+виртуальн(?:ый|ая)\s+"
        r"секретарь[\s‑-]*референт\s+[^.!?]+[.]?",
        r"(?i)я\s*[—–-]?\s*виртуальн(?:ый|ая)\s+"
        r"секретарь[\s‑-]*референт\s+[^.!?]+[.]?",
    )
    for pattern in old_identity_patterns:
        message = re.sub(pattern, identity, message, count=1)

    # Убираем возможные повторы имени Неоны.
    message = re.sub(
        r"(?i)(?:\bменя\s+зовут\s+неона[\s,.;:…—–-]*){2,}",
        "Меня зовут Неона, ",
        message,
    )

    has_identity = identity.lower() in message[:350].lower()
    if not has_identity:
        # Если есть другое короткое представление как помощницы, нормализуем его.
        helper_pattern = (
            rf"(?i)меня\s+зовут\s+неона,?\s+я\s+помощница\s+"
            rf"{re.escape(str(owner_name).strip())}[.]?"
        )
        message, replacements = re.subn(
            helper_pattern,
            identity,
            message,
            count=1,
        )

        if replacements == 0:
            greeting_match = re.match(
                r"^(.{1,140}?[!?.])\s*(.*)$",
                message,
                flags=re.DOTALL,
            )
            if greeting_match:
                greeting = greeting_match.group(1).strip()
                remainder = greeting_match.group(2).strip()
                message = f"{greeting} {identity}"
                if remainder:
                    message += f" {remainder}"
            else:
                message = f"{identity} {message}".strip()

    message = re.sub(r"\s{2,}", " ", message).strip()
    return message[:1000]


def generate_neona_first_message(
    owner_name,
    passport_analysis,
    contact,
):
    """Создаёт одно первое сообщение выбранному контакту."""

    is_known_contact = (
        contact.get("source")
        == "Знакомый — выбран директором"
    )

    system_prompt = build_neona_first_message_system_prompt(
        owner_name
    )

    safe_contact = {
        "telegram_id": contact.get("telegram_id"),
        "name": contact.get("name", ""),
        "first_name": contact.get("first_name", ""),
        "username": contact.get("username", ""),
        "source": contact.get("source", "Рекомендация Неонии"),
        "segment": contact.get("segment", ""),
        "score": contact.get("score", 0),
        "reasons": contact.get("reasons", []),
        "message_angle": contact.get("message_angle", ""),
        "source_chat_title": contact.get("source_chat_title", ""),
        "profile_about": contact.get("profile_about", ""),
        "public_messages": contact.get("public_messages", [])[:3],
        "familiarity_note": contact.get("familiarity_note", ""),
        "owner_draft": contact.get("owner_draft", ""),
        "must_mention": contact.get("must_mention", ""),
        "avoid": contact.get("avoid", ""),
        "known_contact": is_known_contact,
    }

    request = (
        "ПАСПОРТ ЦА И ПРОЕКТА:\n"
        f"{passport_analysis}\n\n"
        "ДАННЫЕ О КОНТАКТЕ:\n"
        f"{json.dumps(safe_contact, ensure_ascii=False)}"
    )

    answer = ask_openai(system_prompt, request).strip()

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    if not answer:
        return build_neona_safe_first_message(
            owner_name,
            contact,
        )

    if answer.startswith('"') and answer.endswith('"'):
        answer = answer[1:-1].strip()

    return finalize_neona_first_message(
        answer,
        owner_name,
        contact,
    )

def render_telegram_connection(expected_telegram_id):
    expected_telegram_id = int(expected_telegram_id)

    connected_key = f"telegram_connected_{expected_telegram_id}"
    phone_key = f"telegram_phone_{expected_telegram_id}"
    pending_key = f"telegram_pending_session_{expected_telegram_id}"
    hash_key = f"telegram_phone_code_hash_{expected_telegram_id}"
    password_step_key = f"telegram_needs_password_{expected_telegram_id}"

    if connected_key not in st.session_state:
        try:
            existing_session = load_telegram_session_from_supabase(
                expected_telegram_id
            )
        except Exception:
            existing_session = ""

        st.session_state[connected_key] = bool(existing_session)

    if st.session_state[connected_key]:
        st.success("🟢 Telegram подключён")
        return True

    st.subheader("Подключение Telegram")

    st.write(
        "Подключите Telegram один раз. "
        "После этого Неония сможет работать с доступными контактами и чатами."
    )

    phone = st.text_input(
        "Номер телефона Telegram",
        placeholder="+49...",
        key=phone_key,
    )

    if pending_key not in st.session_state:
        if st.button(
            "Получить код Telegram",
            type="primary",
            key=f"telegram_request_code_{expected_telegram_id}",
        ):
            if not phone.strip():
                st.warning("Введите номер телефона вместе с кодом страны.")
                return False

            try:
                result = run_telegram_async(
                    request_telegram_login_code(phone.strip())
                )

                st.session_state[pending_key] = result["pending_session"]
                st.session_state[hash_key] = result["phone_code_hash"]
                st.rerun()

            except PhoneNumberInvalidError:
                st.error("Telegram не распознал номер телефона.")

            except Exception as exc:
                st.error(f"Не удалось отправить код: {exc}")

        return False

    if st.session_state.get(password_step_key, False):
        password = st.text_input(
            "Пароль двухэтапной защиты Telegram",
            type="password",
            key=f"telegram_2fa_password_{expected_telegram_id}",
        )

        if st.button(
            "Подтвердить пароль",
            type="primary",
            key=f"telegram_confirm_password_{expected_telegram_id}",
        ):
            try:
                result = run_telegram_async(
                    verify_telegram_2fa_password(
                        st.session_state[pending_key],
                        password,
                    )
                )

                if int(result["telegram_id"]) != expected_telegram_id:
                    st.error(
                        "Подключён другой Telegram-аккаунт. "
                        "Используйте аккаунт, через который вы зарегистрировались."
                    )
                    return False

                save_telegram_session_to_supabase(
                    expected_telegram_id,
                    result["session_string"],
                )

                st.session_state[connected_key] = True
                st.session_state.pop(pending_key, None)
                st.session_state.pop(hash_key, None)
                st.session_state.pop(password_step_key, None)
                st.rerun()

            except PasswordHashInvalidError:
                st.error("Неверный пароль двухэтапной защиты.")

            except Exception as exc:
                st.error(f"Не удалось подключить Telegram: {exc}")

        return False

    code = st.text_input(
        "Код из Telegram",
        placeholder="12345",
        key=f"telegram_login_code_{expected_telegram_id}",
    )

    if st.button(
        "Подтвердить код",
        type="primary",
        key=f"telegram_confirm_code_{expected_telegram_id}",
    ):
        try:
            result = run_telegram_async(
                verify_telegram_login_code(
                    phone,
                    code.strip(),
                    st.session_state[pending_key],
                    st.session_state[hash_key],
                )
            )

            if result["needs_password"]:
                st.session_state[pending_key] = result["pending_session"]
                st.session_state[password_step_key] = True
                st.rerun()

            if int(result["telegram_id"]) != expected_telegram_id:
                st.error(
                    "Подключён другой Telegram-аккаунт. "
                    "Используйте аккаунт, через который вы зарегистрировались."
                )
                return False

            save_telegram_session_to_supabase(
                expected_telegram_id,
                result["session_string"],
            )

            st.session_state[connected_key] = True
            st.session_state.pop(pending_key, None)
            st.session_state.pop(hash_key, None)
            st.rerun()

        except PhoneCodeInvalidError:
            st.error("Неверный код Telegram.")

        except PhoneCodeExpiredError:
            st.session_state.pop(pending_key, None)
            st.session_state.pop(hash_key, None)
            st.error("Срок действия кода закончился. Получите новый код.")

        except Exception as exc:
            st.error(f"Не удалось подтвердить код: {exc}")

    return False


st.markdown(
    """
    <style>
        .stApp {
            background:
                radial-gradient(circle at top, #28231d 0%, #101116 45%, #08090d 100%);
        }

        .main-title {
            text-align: center;
            font-size: 3rem;
            font-weight: 800;
            color: #ffffff;
            margin-top: 2rem;
        }


        .agency-brand {
            width: 100%;
            margin: 0 auto 1.5rem;
            text-align: center;
            color: #d9b45b;
        }

        .agency-brand--full {
            max-width: 1080px;
        }

        .agency-brand--compact {
            max-width: 800px;
            margin-bottom: 1.1rem;
        }

        .agency-brand__top {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: clamp(1rem, 3vw, 2.5rem);
        }

        .agency-brand__emblem-wrap {
            flex: 0 0 auto;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .agency-brand__emblem {
            display: block;
            width: clamp(150px, 19vw, 220px);
            height: auto;
            object-fit: contain;
            filter: drop-shadow(0 0 12px rgba(213, 171, 74, 0.20));
        }

        .agency-brand--compact .agency-brand__emblem {
            width: clamp(105px, 14vw, 150px);
        }

        .agency-brand__emblem-fallback {
            width: 150px;
            height: 150px;
            display: grid;
            place-items: center;
            font-family: Georgia, "Times New Roman", serif;
            font-size: 5rem;
            color: #d9b45b;
        }

        .agency-brand__wordmark {
            min-width: 0;
            text-align: center;
        }

        .agency-brand__name {
            font-family: Georgia, "Times New Roman", serif;
            font-size: clamp(2.4rem, 5vw, 4.6rem);
            line-height: 1;
            font-weight: 500;
            letter-spacing: 0.01em;
            color: #d9b45b;
            text-shadow: 0 0 16px rgba(217, 180, 91, 0.10);
            white-space: nowrap;
        }

        .agency-brand--compact .agency-brand__name {
            font-size: clamp(2rem, 3.7vw, 3.1rem);
        }

        .agency-brand__name span {
            font-size: 1.08em;
        }

        .agency-brand__rule {
            width: 92%;
            height: 1px;
            margin: 0.65rem auto 0.65rem;
            background: linear-gradient(
                90deg,
                transparent,
                rgba(217, 180, 91, 0.72),
                transparent
            );
        }

        .agency-brand__latin {
            font-family: Georgia, "Times New Roman", serif;
            font-size: clamp(1.15rem, 2vw, 1.55rem);
            line-height: 1.25;
            font-style: italic;
            color: #d9b45b;
        }

        .agency-brand--compact .agency-brand__latin {
            font-size: clamp(1rem, 1.6vw, 1.25rem);
        }

        .agency-brand__translation {
            margin-top: 0.22rem;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            font-size: clamp(1rem, 1.65vw, 1.3rem);
            line-height: 1.3;
            font-weight: 500;
            color: #ffffff;
        }

        .agency-brand--compact .agency-brand__translation {
            font-size: clamp(0.95rem, 1.4vw, 1.12rem);
        }

        .agency-brand__present {
            margin-top: clamp(0.8rem, 2vw, 1.35rem);
            font-family: Georgia, "Times New Roman", serif;
            font-size: clamp(1.75rem, 3.4vw, 3rem);
            line-height: 1.15;
            font-weight: 500;
            color: #d9b45b;
            letter-spacing: 0.005em;
        }

        .agency-brand--compact .agency-brand__present {
            margin-top: 0.75rem;
            font-size: clamp(1.35rem, 2.6vw, 2rem);
        }

        .agency-brand__ornament {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.35rem;
            width: min(280px, 44%);
            margin: 0.65rem auto 0.15rem;
            color: #d9b45b;
        }

        .agency-brand__ornament span {
            height: 1px;
            flex: 1;
            background: linear-gradient(90deg, transparent, #d9b45b);
        }

        .agency-brand__ornament span:last-child {
            background: linear-gradient(90deg, #d9b45b, transparent);
        }

        .agency-brand__ornament b {
            font-size: 0.95rem;
            font-weight: 400;
        }

        .agency-brand__wave {
            display: block;
            width: 100%;
            height: auto;
            max-height: 138px;
            object-fit: fill;
            margin: -0.15rem auto 0;
            opacity: 0.96;
            filter: drop-shadow(0 0 7px rgba(217, 180, 91, 0.10));
        }

        .agency-brand--compact .agency-brand__wave {
            max-height: 86px;
            opacity: 0.88;
        }

        .registration-card {
            padding: 2rem;
            border: 1px solid rgba(224, 205, 171, 0.25);
            border-radius: 22px;
            background: rgba(255, 255, 255, 0.05);
            box-shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
        }

        .registration-card h2 {
            color: #ffffff;
            text-align: center;
        }

        .registration-card p {
            color: #e7e0d4;
            text-align: center;
            line-height: 1.6;
        }

                /* Мобильная версия */
        @media (max-width: 768px) {


            .agency-brand {
                margin-bottom: 1rem;
            }

            .agency-brand__top {
                flex-direction: column;
                gap: 0.2rem;
            }

            .agency-brand__emblem,
            .agency-brand--compact .agency-brand__emblem {
                width: clamp(105px, 31vw, 145px);
            }

            .agency-brand__name,
            .agency-brand--compact .agency-brand__name {
                font-size: clamp(2rem, 10vw, 2.75rem);
            }

            .agency-brand__rule {
                margin-top: 0.45rem;
                margin-bottom: 0.45rem;
            }

            .agency-brand__latin,
            .agency-brand--compact .agency-brand__latin {
                font-size: clamp(1rem, 4.7vw, 1.22rem);
            }

            .agency-brand__translation,
            .agency-brand--compact .agency-brand__translation {
                font-size: clamp(0.95rem, 4.2vw, 1.1rem);
            }

            .agency-brand__present,
            .agency-brand--compact .agency-brand__present {
                margin-top: 0.8rem;
                font-size: clamp(1.45rem, 7vw, 2rem);
            }

            .agency-brand__ornament {
                width: 58%;
                margin-top: 0.5rem;
            }

            .agency-brand__wave,
            .agency-brand--compact .agency-brand__wave {
                max-height: 70px;
            }

            .block-container {
                padding-top: 1rem;
                padding-left: 1rem;
                padding-right: 1rem;
                max-width: 100%;
            }

            .main-title {
                font-size: clamp(1.7rem, 8vw, 2.3rem);
                line-height: 1.15;
                white-space: nowrap;
                margin-bottom: 0.5rem;
            }

            .registration-card {
                padding: 1.5rem 1rem;
                border-radius: 1.25rem;
                min-height: auto !important;
                height: auto !important;
                justify-content: flex-start !important;
            }

            .registration-card h2 {
                font-size: 2rem;
                line-height: 1.15;
                margin-bottom: 1rem;
            }

            .registration-card p {
                font-size: 1rem;
                line-height: 1.5;
            }
div[data-testid="stAlert"] {
    padding: 0.9rem 1rem !important;
}

div[data-testid="stAlert"] p {
    color: #f5f5f5 !important;
    font-size: 0.95rem !important;
    line-height: 1.4 !important;
}
            div[data-testid="stCode"] pre {
                white-space: pre-wrap !important;
                overflow-wrap: anywhere;
                word-break: break-all;
                font-size: 0.85rem;
            }
        }

        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        #MainMenu,
        footer {
            display: none !important;
        }
                /* Убираем нижний значок и плашку Streamlit */
        div[class*="viewerBadge"],
        div[class*="ViewerBadge"],
        [data-testid="stViewerBadge"] {
            display: none !important;
        }

        /* Делаем обычный текст читаемым на тёмном фоне */
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] strong {
            color: #e7e0d4 !important;
        }

        /* Текст цветных уведомлений оставляем светлым */
        div[data-testid="stAlert"] p {
            color: #f5f5f5 !important;
        }

        /* Все основные (красные) кнопки делаем зелёными */
        button[kind="primary"],
        div.stButton > button[kind="primary"],
        div[data-testid="stFormSubmitButton"] > button[kind="primary"] {
            background: linear-gradient(180deg, #275d3b 0%, #1f4d31 100%) !important;
            color: #ffffff !important;
            border: 1px solid rgba(151, 190, 164, 0.28) !important;
            box-shadow: 0 7px 16px rgba(7, 34, 19, 0.26) !important;
        }

        button[kind="primary"]:hover,
        div.stButton > button[kind="primary"]:hover,
        div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
            background: linear-gradient(180deg, #316c47 0%, #275d3b 100%) !important;
            border-color: rgba(177, 211, 187, 0.42) !important;
            color: #ffffff !important;
        }

        button[kind="primary"]:active,
        div.stButton > button[kind="primary"]:active,
        div[data-testid="stFormSubmitButton"] > button[kind="primary"]:active {
            background: linear-gradient(180deg, #1f4d31 0%, #183d27 100%) !important;
            color: #ffffff !important;
        }

        button[kind="primary"]:focus,
        div.stButton > button[kind="primary"]:focus,
        div[data-testid="stFormSubmitButton"] > button[kind="primary"]:focus {
            outline: none !important;
            border-color: rgba(177, 211, 187, 0.52) !important;
            box-shadow: 0 0 0 0.18rem rgba(66, 116, 82, 0.22) !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

if not st.query_params.get("hash"):
    render_agency_w_logo()

    st.markdown(
        """
        <div class="registration-card">
            <h2>Добро пожаловать!</h2>
            <p>
                Сегодня искусственный интеллект способен взять на себя
                тысячи часов рутинной работы.
            </p>
            <p>
                Агентство W создаёт для каждого человека команду цифровых
                помощников, которые помогают искать людей, вести первые
                диалоги, анализировать информацию и освобождать время для
                самого главного — жизни, семьи и развития.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if referral_code:
        st.success(f"Приглашение партнёра принято: {referral_code}")
    else:
        st.info("Вы открыли сайт без персональной партнёрской ссылки.")
# Защищённый вход через Telegram
import hashlib
import hmac
import html
import time
from urllib.parse import urlencode

telegram_keys = (
    "id",
    "first_name",
    "last_name",
    "username",
    "photo_url",
    "auth_date",
)

telegram_data = {
    key: str(st.query_params.get(key))
    for key in telegram_keys
    if st.query_params.get(key) is not None
}

received_hash = str(st.query_params.get("hash", ""))


def telegram_auth_is_valid(data, received_hash):
    if not data or not received_hash:
        return False

    try:
        auth_date = int(data["auth_date"])
    except (KeyError, TypeError, ValueError):
        return False

    # Не принимаем устаревшие данные авторизации
    if abs(time.time() - auth_date) > 86400:
        return False

    data_check_string = "\n".join(
        f"{key}={data[key]}" for key in sorted(data)
    )

    secret_key = hashlib.sha256(
        st.secrets["TELEGRAM_BOT_TOKEN"].encode("utf-8")
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated_hash, received_hash)


if received_hash:
    if telegram_auth_is_valid(telegram_data, received_hash):
        first_name = telegram_data.get("first_name", "Пользователь")
        telegram_id = telegram_data.get("id", "")
        member_code, created = save_member_to_supabase(telegram_data, referral_code)


        partner_link = f"https://agency-w.streamlit.app/?ref={member_code}"

        berlin_hour = datetime.now(
            ZoneInfo("Europe/Berlin")
        ).hour
        if berlin_hour < 12:
            greeting = "Доброе утро"
            greeting_icon = "☀️"
        elif berlin_hour < 18:
            greeting = "Добрый день"
            greeting_icon = "🌤️"
        else:
            greeting = "Добрый вечер"
            greeting_icon = "🌙"

        render_agency_w_logo(compact=True)

        st.markdown(
            f"## {greeting}, {first_name}! {greeting_icon}"
        )
        st.markdown(
            f"""
            <div class="registration-card">
                <h2>Директор Агентства W</h2>
                <p>
                    <strong>{first_name}</strong>, сегодня под вашим
                    руководством работает команда ИИ-агентов.
                </p>
                <p>
                    🧭 Стагирит — координатор &nbsp;·&nbsp;
                    🔎 Неония — аналитик &nbsp;·&nbsp;
                    💬 Неона — секретарь-референт &nbsp;·&nbsp;
                    🌱 Неола — наставник
                </p>
                <p><strong>🟢 Команда готова к работе.</strong></p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        telegram_connected = render_telegram_connection(telegram_id)
        st.session_state["neonia_telegram_connected"] = telegram_connected

        if not telegram_connected:
            st.stop()

        hydrate_workspace_state_once(telegram_id)

        persistence_ready = st.session_state.get(
            f"agency_workspace_persistence_ready_{telegram_id}",
            False,
        )

        if persistence_ready:
            st.caption(
                "💾 Рабочее состояние сохраняется автоматически"
            )
        else:
            load_error = st.session_state.get(
                f"agency_workspace_load_error_{telegram_id}",
                "",
            )
            st.warning(
                "Постоянное сохранение ещё не подключено. "
                "Создайте таблицу agency_workspace_states "
                "в Supabase."
                + (
                    f" Техническая причина: {load_error}"
                    if load_error
                    else ""
                )
            )

        main_section = st.segmented_control(
            "Главное меню",
            ["☀️ День", "📅 Календарь", "🤖 Агенты", "👥 Команда", "👤 Профиль"],
            default="☀️ День",
            required=True,
            label_visibility="collapsed",
            width="stretch",
            key="main_section",
        )

        if main_section == "☀️ День":
            st.markdown("### ☀️ Мой день")

            daily_moods = [
                "Сегодня важно сделать один конкретный шаг, который приблизит вас к цели.",
                "Не стремитесь сделать всё сразу. Главное — двигаться вперёд каждый день.",
                "Своё настоящее мы создаём решениями, которые принимаем сегодня.",
                "Спокойствие, ясность и последовательность сильнее суеты.",
                "Сегодня хороший день, чтобы завершить то, что давно откладывалось.",
                "Большой результат начинается с одного простого действия.",
                "Сосредоточьтесь не на трудностях, а на следующем возможном шаге.",
                "Каждый новый человек — это новая история, а не просто контакт.",
                "Сегодня выбирайте действия, которые создают результат, а не занятость.",
                "Уверенность появляется не до действия, а после первых сделанных шагов.",
                "Не сравнивайте своё начало с чужим результатом. Продолжайте свой путь.",
                "Сегодня достаточно стать немного сильнее, чем вы были вчера.",
                "Возможности замечает тот, кто готов действовать.",
                "Ваш опыт — это ваша сила. Технологии помогают её масштабировать.",
            ]
    
            berlin_today = datetime.now(
                    ZoneInfo("Europe/Berlin")
            ).date()
    
            mood_index = berlin_today.toordinal() % len(daily_moods)
            mood_of_the_day = daily_moods[mood_index]
    
            st.info(f"Настрой дня: {mood_of_the_day}")

            candidates_key = (
                f"neonia_candidates_{telegram_id}"
            )
            selected_candidates_key = (
                f"neonia_selected_candidates_{telegram_id}"
            )
            neona_drafts_key = (
                f"neona_first_message_drafts_{telegram_id}"
            )
            sent_log_key = (
                f"neona_first_message_sent_log_{telegram_id}"
            )
            passport_key = (
                f"neonia_target_audience_passport_{telegram_id}"
            )
            contacts_state_key = (
                f"neonia_telegram_contacts_{telegram_id}"
            )
            chat_members_map_key = (
                f"neonia_chat_members_{telegram_id}"
            )
            chat_offsets_map_key = (
                f"neonia_chat_offsets_{telegram_id}"
            )
            offset_key = (
                f"neonia_selection_offset_{telegram_id}"
            )

            candidate_results = st.session_state.get(
                candidates_key,
                [],
            )
            all_contacts = st.session_state.get(
                contacts_state_key,
                [],
            )
            chat_members_map = st.session_state.get(
                chat_members_map_key,
                {},
            )
            if not isinstance(chat_members_map, dict):
                chat_members_map = {}
            chat_offsets_map = st.session_state.get(
                chat_offsets_map_key,
                {},
            )
            if not isinstance(chat_offsets_map, dict):
                chat_offsets_map = {}

            personal_analyzed_count = min(
                st.session_state.get(offset_key, 0),
                len(all_contacts),
            )
            chat_members_count = sum(
                len(members)
                for members in chat_members_map.values()
                if isinstance(members, list)
            )
            chat_analyzed_count = sum(
                max(0, int(offset or 0))
                for offset in chat_offsets_map.values()
            )
            total_people_count = (
                len(all_contacts) + chat_members_count
            )
            analyzed_count = (
                personal_analyzed_count + chat_analyzed_count
            )
            recommended_candidates = [
                candidate
                for candidate in candidate_results
                if candidate.get("recommendation")
                == "Передать Неоне"
            ]
            recommended_candidates.sort(
                key=lambda item: -int(item.get("score", 0))
            )
            top_candidates = recommended_candidates[:10]
            insufficient_count = sum(
                1
                for candidate in candidate_results
                if candidate.get("recommendation")
                == "Нужно больше данных"
            )
            not_fit_count = sum(
                1
                for candidate in candidate_results
                if candidate.get("recommendation")
                == "Пока не подходит"
            )

            owner_contacts_key = (
                f"neonia_owner_known_contacts_{telegram_id}"
            )
            known_search_results_key = (
                f"neonia_known_search_results_{telegram_id}"
            )
            owner_contacts = st.session_state.get(
                owner_contacts_key,
                {},
            )

            def prepare_one_first_message(contact):
                passport = st.session_state.get(
                    passport_key
                )

                if not passport:
                    st.warning(
                        "Сначала создайте паспорт ЦА."
                    )
                    return

                with st.spinner(
                    "Неона готовит первое сообщение..."
                ):
                    try:
                        message = generate_neona_first_message(
                            first_name,
                            passport["analysis"],
                            contact,
                        )

                        drafts = st.session_state.get(
                            neona_drafts_key,
                            {},
                        )
                        contact_id = int(
                            contact["telegram_id"]
                        )
                        drafts[contact_id] = {
                            "message": message,
                            "approved": False,
                            "status": "Сообщение подготовлено",
                        }
                        st.session_state[
                            neona_drafts_key
                        ] = drafts

                        if (
                            contact.get("source")
                            == "Знакомый — выбран директором"
                        ):
                            owner_contacts[
                                contact_id
                            ]["status"] = (
                                "Сообщение подготовлено"
                            )
                            st.session_state[
                                owner_contacts_key
                            ] = owner_contacts
                        else:
                            for candidate_item in candidate_results:
                                if int(
                                    candidate_item[
                                        "telegram_id"
                                    ]
                                ) == contact_id:
                                    candidate_item["status"] = (
                                        "Сообщение подготовлено"
                                    )
                            st.session_state[
                                candidates_key
                            ] = candidate_results

                        persist_workspace_if_changed(
                            telegram_id,
                            force=True,
                        )
                        st.rerun()

                    except Exception as exc:
                        st.error(
                            "Не удалось подготовить "
                            f"сообщение: {exc}"
                        )

            with st.container(border=True):
                st.markdown(
                    "**🎯 10 кандидатов Неонии на сегодня**"
                )

                if total_people_count:
                    st.write(
                        f"Личные контакты: **{len(all_contacts)}** · "
                        f"Участники чатов: **{chat_members_count}** · "
                        f"Проанализировано: **{analyzed_count}** · "
                        f"Соответствует ЦА: "
                        f"**{len(recommended_candidates)}**"
                    )
                    if analyzed_count < total_people_count:
                        st.caption(
                            "Результат предварительный: Неония ещё не "
                            "проанализировала всех загруженных людей."
                        )
                else:
                    st.caption(
                        "Сначала загрузите личные контакты или участников "
                        "выбранного чата в разделе Неонии."
                    )

                if not top_candidates:
                    st.caption(
                        "Подходящих кандидатов пока нет. "
                        "Запустите селекцию в разделе Неонии."
                    )
                else:
                    st.info(
                        "Неония предлагает до 10 лучших кандидатов "
                        f"из уже проанализированных. {first_name} может "
                        "выбрать из них людей для первого сообщения."
                    )
                    if len(top_candidates) < 10:
                        st.warning(
                            f"Пока найдено {len(top_candidates)} из 10 "
                            "кандидатов. Продолжите поиск и анализ "
                            "в разделе Неонии."
                        )

                    candidate_by_id = {
                        int(candidate["telegram_id"]): candidate
                        for candidate in top_candidates
                    }
                    familiar_count = len(owner_contacts)
                    recommended_limit = max(
                        0,
                        5 - familiar_count,
                    )

                    st.markdown(
                        f"#### ✅ Выбор {first_name} из списка Неонии"
                    )
                    st.info(
                        "Неония только сформировала список до 10 "
                        "подходящих кандидатов. Кого взять в работу, "
                        f"решает {first_name}: поставьте галочку прямо "
                        "в карточке нужного человека."
                    )

                    previous_selection = [
                        int(contact_id)
                        for contact_id in st.session_state.get(
                            selected_candidates_key,
                            [],
                        )
                        if (
                            int(contact_id) in candidate_by_id
                            and int(contact_id) not in owner_contacts
                        )
                    ][:recommended_limit]

                    # Сохраняем состояние галочек между обновлениями страницы.
                    for contact_id in candidate_by_id:
                        checkbox_key = (
                            "owner_select_candidate_"
                            f"{telegram_id}_{contact_id}"
                        )
                        if checkbox_key not in st.session_state:
                            st.session_state[checkbox_key] = (
                                contact_id in previous_selection
                            )

                    current_checked_ids = [
                        contact_id
                        for contact_id in candidate_by_id
                        if st.session_state.get(
                            (
                                "owner_select_candidate_"
                                f"{telegram_id}_{contact_id}"
                            ),
                            False,
                        )
                    ]

                    st.markdown(
                        "#### 📋 Кандидаты, найденные Неонией"
                    )

                    for number, candidate in enumerate(
                        top_candidates,
                        start=1,
                    ):
                        contact_id = int(
                            candidate["telegram_id"]
                        )
                        checkbox_key = (
                            "owner_select_candidate_"
                            f"{telegram_id}_{contact_id}"
                        )
                        is_checked = st.session_state.get(
                            checkbox_key,
                            False,
                        )
                        limit_reached = (
                            len(current_checked_ids)
                            >= recommended_limit
                            and not is_checked
                        )
                        username = (
                            f"@{candidate['username']}"
                            if candidate.get("username")
                            else "без username"
                        )

                        with st.container(border=True):
                            st.checkbox(
                                (
                                    f"Выбрать: {candidate['name']} · "
                                    f"{candidate['score']}% · "
                                    f"{username}"
                                ),
                                key=checkbox_key,
                                disabled=(
                                    recommended_limit == 0
                                    or limit_reached
                                ),
                            )

                            with st.expander(
                                "Посмотреть карточку кандидата"
                            ):
                                st.write(
                                    f"**Сегмент:** "
                                    f"{candidate['segment']}"
                                )
                                st.write(
                                    f"**Уверенность:** "
                                    f"{candidate['confidence']}"
                                )
                                st.write(
                                    "**Почему предложен:** "
                                    + "; ".join(
                                        candidate.get(
                                            "reasons",
                                            [],
                                        )
                                    )
                                )
                                st.write(
                                    f"**Подход к знакомству:** "
                                    f"{candidate['message_angle']}"
                                )

                    # После отрисовки считываем окончательный выбор владельца.
                    selected_ids = [
                        contact_id
                        for contact_id in candidate_by_id
                        if st.session_state.get(
                            (
                                "owner_select_candidate_"
                                f"{telegram_id}_{contact_id}"
                            ),
                            False,
                        )
                    ][:recommended_limit]

                    st.session_state[
                        selected_candidates_key
                    ] = selected_ids

                    persist_workspace_if_changed(
                        telegram_id
                    )

                    total_selected = (
                        len(selected_ids)
                        + len(owner_contacts)
                    )
                    st.caption(
                        f"Выбрано — {first_name}: "
                        f"{total_selected} из 5"
                    )

                    if recommended_limit == 0:
                        st.warning(
                            "Лимит 5 человек уже заполнен "
                            "знакомыми контактами."
                        )
                    elif not selected_ids:
                        st.warning(
                            "Пока никто не выбран. Поставьте галочку "
                            "в карточке нужного кандидата."
                        )

                    if selected_ids:
                        st.markdown(
                            f"#### ✍️ Люди, выбранные — {first_name}"
                        )

                        for contact_id in selected_ids:
                            candidate = candidate_by_id[
                                contact_id
                            ]
                            with st.container(border=True):
                                st.markdown(
                                    f"**{candidate['name']}**"
                                )
                                st.caption(
                                    f"{candidate['score']}% · "
                                    f"{candidate['segment']}"
                                )

                                if st.button(
                                    "✍️ Подготовить первое сообщение",
                                    key=(
                                        "neona_prepare_recommended_"
                                        f"{telegram_id}_{contact_id}"
                                    ),
                                ):
                                    prepare_one_first_message(
                                        candidate
                                    )

                st.divider()
                st.markdown("### 🤝 Найти знакомого")
                st.write(
                    "Найдите любого человека среди уже загруженных "
                    "Telegram-контактов — даже если Неония не "
                    "рекомендовала его по критериям ЦА."
                )

                if not all_contacts:
                    st.warning(
                        "Сначала загрузите контакты Telegram "
                        "в разделе Неонии."
                    )
                else:
                    known_query = st.text_input(
                        "Имя, @username или номер телефона",
                        placeholder=(
                            "Например: Наталья, @username или +49..."
                        ),
                        key=f"known_contact_query_{telegram_id}",
                    )

                    if st.button(
                        "🔎 Найти знакомого",
                        key=f"known_contact_search_{telegram_id}",
                    ):
                        search_results = search_known_contacts(
                            all_contacts,
                            known_query,
                        )
                        st.session_state[
                            known_search_results_key
                        ] = search_results

                        if not search_results:
                            st.warning(
                                "В загруженных Telegram-контактах "
                                "совпадений не найдено."
                            )

                    known_results = st.session_state.get(
                        known_search_results_key,
                        [],
                    )

                    if known_results:
                        known_by_id = {
                            int(item["telegram_id"]): item
                            for item in known_results
                        }
                        known_options = [None] + list(
                            known_by_id.keys()
                        )

                        chosen_known_id = st.selectbox(
                            "Выберите найденного человека",
                            options=known_options,
                            format_func=lambda contact_id: (
                                "Выберите контакт"
                                if contact_id is None
                                else (
                                    f"{known_by_id[contact_id]['name']} "
                                    + (
                                        f"@{known_by_id[contact_id]['username']}"
                                        if known_by_id[
                                            contact_id
                                        ].get("username")
                                        else ""
                                    )
                                )
                            ),
                            key=(
                                "known_contact_select_"
                                f"{telegram_id}"
                            ),
                        )

                        if chosen_known_id is not None:
                            chosen_contact = known_by_id[
                                chosen_known_id
                            ]
                            already_added = (
                                chosen_known_id
                                in owner_contacts
                            )
                            already_recommended = (
                                chosen_known_id
                                in selected_ids
                            )
                            limit_reached = (
                                len(selected_ids)
                                + len(owner_contacts)
                                >= 5
                            )

                            familiarity_note = st.text_area(
                                "Откуда и как вы знакомы?",
                                placeholder=(
                                    "Например: вместе работали "
                                    "в проекте два года назад."
                                ),
                                key=(
                                    "known_familiarity_"
                                    f"{telegram_id}_"
                                    f"{chosen_known_id}"
                                ),
                            )
                            owner_draft = st.text_area(
                                "Ваш набросок первого сообщения",
                                placeholder=(
                                    "Напишите простыми словами, "
                                    "что Вы хотите сказать."
                                ),
                                key=(
                                    "known_owner_draft_"
                                    f"{telegram_id}_"
                                    f"{chosen_known_id}"
                                ),
                            )
                            must_mention = st.text_input(
                                "Что обязательно упомянуть?",
                                key=(
                                    "known_must_mention_"
                                    f"{telegram_id}_"
                                    f"{chosen_known_id}"
                                ),
                            )
                            avoid = st.text_input(
                                "Чего лучше не говорить?",
                                key=(
                                    "known_avoid_"
                                    f"{telegram_id}_"
                                    f"{chosen_known_id}"
                                ),
                            )

                            if already_added:
                                st.info(
                                    "Этот знакомый уже добавлен "
                                    "к сегодняшней работе."
                                )
                            elif already_recommended:
                                st.info(
                                    "Этот человек уже выбран среди "
                                    "рекомендаций Неонии."
                                )
                            elif limit_reached:
                                st.warning(
                                    "Лимит 5 человек уже заполнен."
                                )

                            if st.button(
                                "➕ Добавить знакомого к работе",
                                disabled=(
                                    already_added
                                    or already_recommended
                                    or limit_reached
                                ),
                                key=(
                                    "add_known_contact_"
                                    f"{telegram_id}_"
                                    f"{chosen_known_id}"
                                ),
                            ):
                                if not owner_draft.strip():
                                    st.warning(
                                        "Добавьте хотя бы короткий "
                                        "набросок первого сообщения."
                                    )
                                else:
                                    owner_contacts[
                                        chosen_known_id
                                    ] = {
                                        "telegram_id": int(
                                            chosen_known_id
                                        ),
                                        "name": (
                                            chosen_contact.get(
                                                "name"
                                            )
                                            or "Без имени"
                                        ),
                                        "first_name": (
                                            chosen_contact.get(
                                                "first_name"
                                            )
                                            or ""
                                        ),
                                        "username": (
                                            chosen_contact.get(
                                                "username"
                                            )
                                            or ""
                                        ),
                                        "source": (
                                            "Знакомый — выбран директором"
                                        ),
                                        "segment": (
                                            "Выбран директором"
                                        ),
                                        "score": 0,
                                        "confidence": (
                                            "решение владельца"
                                        ),
                                        "reasons": [
                                            familiarity_note.strip()
                                            or (
                                                "Добавлен владельцем "
                                                "как знакомый контакт"
                                            )
                                        ],
                                        "recommendation": (
                                            "Добавлен директором"
                                        ),
                                        "message_angle": (
                                            owner_draft.strip()
                                        ),
                                        "familiarity_note": (
                                            familiarity_note.strip()
                                        ),
                                        "owner_draft": (
                                            owner_draft.strip()
                                        ),
                                        "must_mention": (
                                            must_mention.strip()
                                        ),
                                        "avoid": avoid.strip(),
                                        "status": (
                                            "Знакомый добавлен директором"
                                        ),
                                    }
                                    st.session_state[
                                        owner_contacts_key
                                    ] = owner_contacts

                                    persist_workspace_if_changed(
                                        telegram_id,
                                        force=True,
                                    )
                                    st.rerun()

                if owner_contacts:
                    st.markdown(
                        "#### 🤝 Знакомые, добавленные владельцем"
                    )

                    for contact_id, contact in list(
                        owner_contacts.items()
                    ):
                        with st.container(border=True):
                            username = (
                                f"@{contact['username']}"
                                if contact.get("username")
                                else "без username"
                            )
                            st.markdown(
                                f"**{contact['name']}** · {username}"
                            )
                            st.caption(
                                f"Добавлен — {{first_name}} — "
                                "не является рекомендацией Неонии"
                            )

                            if contact.get("familiarity_note"):
                                st.write(
                                    "**Знакомство:** "
                                    f"{contact['familiarity_note']}"
                                )
                            st.write(
                                "**Ваш смысл сообщения:** "
                                f"{contact['owner_draft']}"
                            )

                            left_column, right_column = st.columns(
                                2
                            )

                            with left_column:
                                if st.button(
                                    "✍️ Подготовить первое сообщение",
                                    key=(
                                        "neona_prepare_known_"
                                        f"{telegram_id}_{contact_id}"
                                    ),
                                ):
                                    prepare_one_first_message(
                                        contact
                                    )

                            with right_column:
                                if st.button(
                                    "Убрать из работы",
                                    key=(
                                        "remove_known_contact_"
                                        f"{telegram_id}_{contact_id}"
                                    ),
                                ):
                                    owner_contacts.pop(
                                        contact_id,
                                        None,
                                    )
                                    drafts = st.session_state.get(
                                        neona_drafts_key,
                                        {},
                                    )
                                    drafts.pop(
                                        int(contact_id),
                                        None,
                                    )
                                    st.session_state[
                                        owner_contacts_key
                                    ] = owner_contacts
                                    st.session_state[
                                        neona_drafts_key
                                    ] = drafts

                                    persist_workspace_if_changed(
                                        telegram_id,
                                        force=True,
                                    )
                                    st.rerun()

            neona_drafts = st.session_state.get(
                neona_drafts_key,
                {},
            )
            sent_log = st.session_state.get(
                sent_log_key,
                [],
            )

            if neona_drafts:
                with st.container(border=True):
                    st.markdown(
                        "**✍️ Первые сообщения Неоны "
                        "на утверждение**"
                    )
                    st.caption(
                        f"{first_name} утверждает только первое "
                        "сообщение. После ответа человека Неона "
                        "ведёт диалог самостоятельно по правилам."
                    )

                    candidate_lookup = {
                        int(item["telegram_id"]): item
                        for item in candidate_results
                    }
                    candidate_lookup.update(
                        {
                            int(item["telegram_id"]): item
                            for item in owner_contacts.values()
                        }
                    )

                    sent_today = count_first_messages_sent_today(
                        sent_log
                    )
                    st.caption(
                        f"Сегодня отправлено первых сообщений: "
                        f"{sent_today} из 5"
                    )

                    for contact_id, draft in list(
                        neona_drafts.items()
                    ):
                        contact_id = int(contact_id)
                        candidate = candidate_lookup.get(
                            contact_id,
                            {},
                        )
                        candidate_name = candidate.get(
                            "name",
                            "Кандидат",
                        )
                        candidate_username = candidate.get(
                            "username",
                            "",
                        )

                        normalized_message = ensure_neona_identity(
                            draft.get("message", ""),
                            first_name,
                        )
                        if normalized_message != draft.get(
                            "message",
                            "",
                        ):
                            draft["message"] = normalized_message
                            neona_drafts[contact_id] = draft
                            st.session_state[
                                neona_drafts_key
                            ] = neona_drafts
                            persist_workspace_if_changed(
                                telegram_id,
                                force=True,
                            )

                        st.markdown(f"#### {candidate_name}")
                        edited_message = st.text_area(
                            "Текст первого сообщения",
                            value=draft["message"],
                            height=150,
                            disabled=bool(draft.get("sent")),
                            key=(
                                "neona_draft_text_"
                                f"{telegram_id}_{contact_id}"
                            ),
                        )

                        if (
                            not draft.get("sent")
                            and edited_message.strip()
                            and edited_message.strip()
                            != str(draft.get("message", "")).strip()
                        ):
                            draft["message"] = edited_message.strip()
                            draft["approved"] = False
                            draft["status"] = "Сообщение отредактировано"
                            neona_drafts[contact_id] = draft
                            st.session_state[
                                neona_drafts_key
                            ] = neona_drafts
                            persist_workspace_if_changed(
                                telegram_id
                            )

                        if not draft.get("sent") and st.button(
                            "✅ Утвердить первое сообщение",
                            key=(
                                "neona_approve_first_message_"
                                f"{telegram_id}_{contact_id}"
                            ),
                        ):
                            final_message = edited_message.strip()
                            if not final_message:
                                st.warning(
                                    "Сначала заполните текст сообщения."
                                )
                            else:
                                draft["message"] = final_message
                                draft["approved"] = True
                                draft["status"] = (
                                    "Первое сообщение утверждено"
                                )
                                neona_drafts[contact_id] = draft
                                st.session_state[
                                    neona_drafts_key
                                ] = neona_drafts

                                for candidate_item in candidate_results:
                                    if int(
                                        candidate_item["telegram_id"]
                                    ) == contact_id:
                                        candidate_item["status"] = (
                                            "Первое сообщение утверждено"
                                        )

                                st.session_state[
                                    candidates_key
                                ] = candidate_results

                                if contact_id in owner_contacts:
                                    owner_contacts[
                                        contact_id
                                    ]["status"] = (
                                        "Первое сообщение утверждено"
                                    )
                                    st.session_state[
                                        owner_contacts_key
                                    ] = owner_contacts

                                persist_workspace_if_changed(
                                    telegram_id,
                                    force=True,
                                )
                                st.rerun()

                        if draft.get("sent"):
                            sent_at = str(
                                draft.get("sent_at", "")
                            )
                            st.success(
                                "📨 Первое сообщение отправлено"
                                + (
                                    f" · {sent_at[11:16]}"
                                    if len(sent_at) >= 16
                                    else ""
                                )
                            )
                        elif draft.get("approved"):
                            st.success(
                                "✅ Первое сообщение утверждено"
                            )

                            sent_today = (
                                count_first_messages_sent_today(
                                    sent_log
                                )
                            )
                            already_sent_to_contact = any(
                                int(event.get("telegram_id", 0))
                                == contact_id
                                for event in sent_log
                            )

                            if already_sent_to_contact:
                                st.info(
                                    "Первое сообщение этому контакту "
                                    "уже было отправлено."
                                )
                            elif sent_today >= 5:
                                st.warning(
                                    "Дневной лимит достигнут: "
                                    "сегодня уже отправлено 5 первых "
                                    "сообщений."
                                )
                            else:
                                st.caption(
                                    "Сообщение уйдёт с подключённого "
                                    "Telegram-аккаунта владельца."
                                )
                                if st.button(
                                    "📨 Отправить первое сообщение",
                                    type="primary",
                                    key=(
                                        "neona_send_first_message_"
                                        f"{telegram_id}_{contact_id}"
                                    ),
                                ):
                                    try:
                                        with st.spinner(
                                            "Отправляем сообщение "
                                            "в Telegram..."
                                        ):
                                            send_result = (
                                                run_telegram_async(
                                                    send_telegram_first_message(
                                                        telegram_id,
                                                        contact_id,
                                                        candidate_username,
                                                        draft["message"],
                                                    )
                                                )
                                            )

                                        draft["sent"] = True
                                        draft["approved"] = True
                                        draft["sent_at"] = (
                                            send_result["sent_at"]
                                        )
                                        draft["telegram_message_id"] = (
                                            send_result["message_id"]
                                        )
                                        draft["status"] = (
                                            "Первое сообщение отправлено"
                                        )
                                        neona_drafts[
                                            contact_id
                                        ] = draft
                                        st.session_state[
                                            neona_drafts_key
                                        ] = neona_drafts

                                        sent_log.append(
                                            {
                                                "telegram_id": contact_id,
                                                "recipient_name": candidate_name,
                                                "sent_at": send_result["sent_at"],
                                                "message_id": send_result["message_id"],
                                                "kind": "first_message",
                                            }
                                        )
                                        st.session_state[
                                            sent_log_key
                                        ] = sent_log

                                        try:
                                            initialize_dialog_after_first_message(
                                                telegram_id,
                                                contact_id,
                                                baseline_incoming_id=int(
                                                    send_result.get(
                                                        "baseline_incoming_message_id",
                                                        0,
                                                    )
                                                ),
                                                sent_at=send_result["sent_at"],
                                            )
                                        except Exception as dialog_exc:
                                            draft["dialog_activation_error"] = str(
                                                dialog_exc
                                            )
                                            neona_drafts[contact_id] = draft
                                            st.session_state[
                                                neona_drafts_key
                                            ] = neona_drafts

                                        for candidate_item in candidate_results:
                                            if int(
                                                candidate_item["telegram_id"]
                                            ) == contact_id:
                                                candidate_item["status"] = (
                                                    "Первое сообщение отправлено"
                                                )
                                        st.session_state[
                                            candidates_key
                                        ] = candidate_results

                                        if contact_id in owner_contacts:
                                            owner_contacts[
                                                contact_id
                                            ]["status"] = (
                                                "Первое сообщение отправлено"
                                            )
                                            st.session_state[
                                                owner_contacts_key
                                            ] = owner_contacts

                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                                        st.rerun()

                                    except Exception as exc:
                                        st.error(
                                            friendly_telegram_send_error(
                                                exc
                                            )
                                        )

            with st.container(border=True):
                st.markdown("**📅 Встречи**")
                render_today_meetings_compact(int(telegram_id))

            with st.container(border=True):
                st.markdown("**✅ Задачи на сегодня**")
                st.caption("Главные задачи дня появятся здесь.")

            with st.container(border=True):
                st.markdown("**📊 Итоги**")
                st.caption("Здесь будут итоги недели и месяца.")

        elif main_section == "📅 Календарь":
            render_agency_calendar(
                owner_telegram_id=int(telegram_id),
                owner_name=first_name,
            )

        elif main_section == "🤖 Агенты":
            st.markdown("### 🤖 Агенты")

            selected_agent = st.selectbox(
                "Выберите агента",
                ["Стагирит", "Неония", "Неона", "Неола"],
                key="selected_agent",
            )

            agent_descriptions = {
                "Стагирит": (
                    "Главный координатор и заместитель директора. "
                    "Распределяет задачи между агентами и контролирует результат."
                ),
                "Неония": (
                    "Анализирует проекты, сегментирует людей "
                    "и находит подходящих кандидатов."
                ),
                "Неона": (
                    "Ведёт диалог, отвечает по контексту "
                    "и подводит человека к осознанной встрече."
                ),
                "Неола": (
                    "Проводит онбординг, помогает новичку начать работу "
                    "и сопровождает его после регистрации."
                ),
            }

            with st.container(border=True):
                st.markdown(f"#### {selected_agent}")
                st.write(agent_descriptions[selected_agent])
                if selected_agent == "Неония":
                    st.caption(
                "Неония работает по этапам: от анализа проекта "
                "до передачи выбранных контактов Неоне."
            )

                    neonia_mode = st.radio(
                "Выберите задачу Неонии:",
                [
                    "🎯 Анализ проекта и ЦА",
                    "🔎 Поиск чатов",
                    "🎯 Поиск контактов в чатах по ЦА",
                    "👥 Поиск контактов",
                    "🧠 Анализ контактов по ЦА",
                ],
                horizontal=True,
                key="neonia_mode",
            )
                    if neonia_mode != "🎯 Анализ проекта и ЦА":
                        mode_messages = {
                        "🔎 Поиск чатов": (
                            "Здесь Неония загружает доступные Telegram-группы "
                            "и каналы. Затем выберите «Поиск контактов в чатах "
                            "по ЦА»."
                        ),
                        "🎯 Поиск контактов в чатах по ЦА": (
                            "Здесь Неония выбирает найденную группу, получает "
                            "доступных участников и сравнивает их с паспортом ЦА."
                        ),
                        "👥 Поиск контактов": (
                            "Здесь Неония работает с личной адресной книгой Telegram."
                        ),
                        "🧠 Анализ контактов по ЦА": (
                            "Здесь Неония будет анализировать найденных людей, "
                            "создавать карточки и готовить выбранные контакты для Неоны."
                        ),
                        }
                        st.info(mode_messages[neonia_mode])
                        if neonia_mode == "🔎 Поиск чатов":
                            chats_result = render_neonia_chats()

                            chats_state_key = (
                                f"neonia_telegram_chats_{telegram_id}"
                            )
                            chats_search_done_key = (
                                f"neonia_chats_search_done_{telegram_id}"
                            )

                            if chats_result["find_chats"]:
                                with st.spinner(
                                    "Неония получает список групп и каналов..."
                                ):
                                    try:
                                        chats = run_telegram_async(
                                            fetch_telegram_chats(telegram_id)
                                        )
                                        st.session_state[
                                            chats_state_key
                                        ] = chats
                                        st.session_state[
                                            chats_search_done_key
                                        ] = True

                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )

                                    except Exception as exc:
                                        st.error(
                                            "Не удалось получить чаты: "
                                            f"{exc}"
                                        )

                            chats = st.session_state.get(
                                chats_state_key,
                                [],
                            )

                            if chats:
                                group_count = sum(
                                    1
                                    for chat in chats
                                    if chat.get("type")
                                    in {"Группа", "Супергруппа"}
                                )
                                channel_count = sum(
                                    1
                                    for chat in chats
                                    if chat.get("type") == "Канал"
                                )
                                public_count = sum(
                                    1
                                    for chat in chats
                                    if chat.get("is_public", False)
                                )

                                metric_columns = st.columns(4)
                                metric_columns[0].metric(
                                    "Всего",
                                    len(chats),
                                )
                                metric_columns[1].metric(
                                    "Группы",
                                    group_count,
                                )
                                metric_columns[2].metric(
                                    "Каналы",
                                    channel_count,
                                )
                                metric_columns[3].metric(
                                    "Публичные",
                                    public_count,
                                )

                                filter_columns = st.columns([2, 1])
                                chat_query = filter_columns[0].text_input(
                                    "Найти чат по названию или username",
                                    key=(
                                        "neonia_chat_query_"
                                        f"{telegram_id}"
                                    ),
                                    placeholder="Например: бизнес или @username",
                                )

                                available_types = sorted(
                                    {
                                        chat.get("type", "Чат")
                                        for chat in chats
                                    }
                                )
                                selected_types = filter_columns[1].multiselect(
                                    "Тип",
                                    available_types,
                                    default=available_types,
                                    key=(
                                        "neonia_chat_types_"
                                        f"{telegram_id}"
                                    ),
                                )

                                show_archived = st.checkbox(
                                    "Показывать архивные чаты",
                                    value=False,
                                    key=(
                                        "neonia_show_archived_chats_"
                                        f"{telegram_id}"
                                    ),
                                )

                                normalized_query = (
                                    chat_query.strip().lower().lstrip("@")
                                )
                                filtered_chats = []

                                for chat in chats:
                                    if (
                                        selected_types
                                        and chat.get("type")
                                        not in selected_types
                                    ):
                                        continue

                                    if (
                                        not show_archived
                                        and chat.get("is_archived", False)
                                    ):
                                        continue

                                    if normalized_query:
                                        title = str(
                                            chat.get("title", "")
                                        ).lower()
                                        username = str(
                                            chat.get("username", "")
                                        ).lower().lstrip("@")
                                        if (
                                            normalized_query not in title
                                            and normalized_query not in username
                                        ):
                                            continue

                                    filtered_chats.append(chat)

                                if filtered_chats:
                                    chats_for_table = [
                                        {
                                            "Название": chat["title"],
                                            "Тип": chat["type"],
                                            "Username": (
                                                f"@{chat['username']}"
                                                if chat["username"]
                                                else "—"
                                            ),
                                            "Участников": (
                                                chat["participants_count"]
                                                or "—"
                                            ),
                                            "Непрочитано": chat[
                                                "unread_count"
                                            ],
                                            "Архив": (
                                                "Да"
                                                if chat["is_archived"]
                                                else "Нет"
                                            ),
                                        }
                                        for chat in filtered_chats
                                    ]

                                    st.dataframe(
                                        chats_for_table,
                                        use_container_width=True,
                                        hide_index=True,
                                    )
                                    st.caption(
                                        "Показано: "
                                        f"{len(filtered_chats)} из "
                                        f"{len(chats)}. Личные диалоги "
                                        "с людьми в этот список не входят."
                                    )
                                else:
                                    st.info(
                                        "По выбранным фильтрам чаты не найдены."
                                    )

                            elif st.session_state.get(
                                chats_search_done_key,
                                False,
                            ):
                                st.info(
                                    "В Telegram не найдено доступных "
                                    "групп или каналов."
                                )

                        elif neonia_mode == "🎯 Поиск контактов в чатах по ЦА":
                            passport_key = (
                                f"neonia_target_audience_passport_{telegram_id}"
                            )
                            chats_state_key = (
                                f"neonia_telegram_chats_{telegram_id}"
                            )
                            selected_chat_key = (
                                f"neonia_selected_source_chat_{telegram_id}"
                            )
                            members_map_key = (
                                f"neonia_chat_members_{telegram_id}"
                            )
                            chat_candidates_map_key = (
                                f"neonia_chat_candidates_{telegram_id}"
                            )
                            chat_offsets_map_key = (
                                f"neonia_chat_offsets_{telegram_id}"
                            )
                            global_candidates_key = (
                                f"neonia_candidates_{telegram_id}"
                            )
                            selected_candidates_key = (
                                f"neonia_selected_candidates_{telegram_id}"
                            )
                            owner_contacts_key = (
                                f"neonia_owner_known_contacts_{telegram_id}"
                            )

                            passport = st.session_state.get(passport_key)
                            chats = st.session_state.get(
                                chats_state_key,
                                [],
                            )
                            eligible_chats = [
                                chat
                                for chat in chats
                                if chat.get("type")
                                in {"Группа", "Супергруппа"}
                            ]

                            if not passport:
                                st.warning(
                                    "Сначала откройте «Анализ проекта и ЦА» "
                                    "и создайте паспорт целевой аудитории."
                                )
                            elif not eligible_chats:
                                st.warning(
                                    "Сначала откройте «Поиск чатов» и загрузите "
                                    "Telegram-группы. Каналы на этом этапе не "
                                    "используются, потому что нужен список людей."
                                )
                            else:
                                chat_by_id = {
                                    int(chat["chat_id"]): chat
                                    for chat in eligible_chats
                                }
                                chat_ids = list(chat_by_id)
                                saved_chat_id = st.session_state.get(
                                    selected_chat_key
                                )
                                try:
                                    saved_chat_id = int(saved_chat_id)
                                except (TypeError, ValueError):
                                    saved_chat_id = None

                                default_index = 0
                                if saved_chat_id in chat_ids:
                                    default_index = chat_ids.index(
                                        saved_chat_id
                                    )

                                selected_chat_id = st.selectbox(
                                    "Выберите чат для поиска контактов",
                                    chat_ids,
                                    index=default_index,
                                    format_func=lambda value: (
                                        f"{chat_by_id[value]['title']} · "
                                        f"{chat_by_id[value]['type']} · "
                                        f"{chat_by_id[value].get('participants_count') or '—'} участников"
                                    ),
                                    key=(
                                        "neonia_target_chat_select_"
                                        f"{telegram_id}"
                                    ),
                                )
                                selected_chat = chat_by_id[
                                    int(selected_chat_id)
                                ]
                                st.session_state[selected_chat_key] = int(
                                    selected_chat_id
                                )

                                st.caption(
                                    "Неония использует только доступные Telegram-профили "
                                    "и публичные сообщения в выбранной группе. Личная "
                                    "переписка и номера телефонов не анализируются."
                                )

                                pass_limit = st.selectbox(
                                    "Сколько доступных участников проверить на первом проходе",
                                    [100, 200, 500],
                                    index=1,
                                    key=(
                                        "neonia_chat_members_limit_"
                                        f"{telegram_id}_{selected_chat_id}"
                                    ),
                                )

                                members_map = st.session_state.get(
                                    members_map_key,
                                    {},
                                )
                                if not isinstance(members_map, dict):
                                    members_map = {}

                                chat_candidates_map = st.session_state.get(
                                    chat_candidates_map_key,
                                    {},
                                )
                                if not isinstance(chat_candidates_map, dict):
                                    chat_candidates_map = {}

                                chat_offsets_map = st.session_state.get(
                                    chat_offsets_map_key,
                                    {},
                                )
                                if not isinstance(chat_offsets_map, dict):
                                    chat_offsets_map = {}

                                chat_map_id = str(int(selected_chat_id))
                                members = members_map.get(
                                    chat_map_id,
                                    [],
                                )
                                candidate_results = chat_candidates_map.get(
                                    chat_map_id,
                                    [],
                                )
                                try:
                                    current_offset = int(
                                        chat_offsets_map.get(
                                            chat_map_id,
                                            0,
                                        )
                                        or 0
                                    )
                                except (TypeError, ValueError):
                                    current_offset = 0

                                load_members = st.button(
                                    "👥 Загрузить участников выбранного чата",
                                    type="primary",
                                    key=(
                                        "neonia_load_chat_members_"
                                        f"{telegram_id}_{selected_chat_id}"
                                    ),
                                )

                                if load_members:
                                    with st.spinner(
                                        "Неония получает доступных участников чата..."
                                    ):
                                        try:
                                            members = run_telegram_async(
                                                fetch_telegram_chat_members(
                                                    telegram_id,
                                                    selected_chat,
                                                    limit=pass_limit,
                                                )
                                            )
                                            members_map[chat_map_id] = members
                                            chat_candidates_map[chat_map_id] = []
                                            chat_offsets_map[chat_map_id] = 0
                                            candidate_results = []
                                            current_offset = 0

                                            st.session_state[
                                                members_map_key
                                            ] = members_map
                                            st.session_state[
                                                chat_candidates_map_key
                                            ] = chat_candidates_map
                                            st.session_state[
                                                chat_offsets_map_key
                                            ] = chat_offsets_map

                                            persist_workspace_if_changed(
                                                telegram_id,
                                                force=True,
                                            )

                                            if members:
                                                st.success(
                                                    f"Получено участников для проверки: "
                                                    f"{len(members)}."
                                                )
                                            else:
                                                st.info(
                                                    "Telegram не вернул доступных "
                                                    "участников этого чата."
                                                )

                                        except Exception as exc:
                                            st.error(
                                                "Не удалось получить участников: "
                                                f"{exc}"
                                            )

                                if members:
                                    analyzed_count = min(
                                        current_offset,
                                        len(members),
                                    )
                                    recommended_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Передать Неоне"
                                    )
                                    more_data_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Нужно больше данных"
                                    )
                                    not_fit_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Пока не подходит"
                                    )

                                    metric_columns = st.columns(4)
                                    metric_columns[0].metric(
                                        "Загружено",
                                        len(members),
                                    )
                                    metric_columns[1].metric(
                                        "Проанализировано",
                                        analyzed_count,
                                    )
                                    metric_columns[2].metric(
                                        "Соответствует ЦА",
                                        recommended_count,
                                    )
                                    metric_columns[3].metric(
                                        "Недостаточно данных",
                                        more_data_count,
                                    )

                                    if analyzed_count < len(members):
                                        button_label = (
                                            "🧠 Начать поиск по критериям ЦА"
                                            if analyzed_count == 0
                                            else "🧠 Проверить следующие 10 участников"
                                        )
                                        analyze_members = st.button(
                                            button_label,
                                            type="primary",
                                            key=(
                                                "neonia_analyze_chat_members_"
                                                f"{telegram_id}_{selected_chat_id}_"
                                                f"{current_offset}"
                                            ),
                                        )
                                    else:
                                        analyze_members = False
                                        st.success(
                                            "Все загруженные участники проверены."
                                        )

                                    if analyze_members:
                                        batch = members[
                                            current_offset:
                                            current_offset + 10
                                        ]
                                        with st.spinner(
                                            "Неония изучает профили и публичные "
                                            "сообщения, затем сравнивает людей "
                                            "с паспортом ЦА..."
                                        ):
                                            try:
                                                member_contexts = (
                                                    run_telegram_async(
                                                        fetch_chat_member_contexts(
                                                            telegram_id,
                                                            selected_chat,
                                                            batch,
                                                        )
                                                    )
                                                )
                                                batch_results = (
                                                    analyze_chat_members_for_target_audience(
                                                        passport["analysis"],
                                                        member_contexts,
                                                    )
                                                )
                                                candidate_results = (
                                                    merge_candidate_results(
                                                        candidate_results,
                                                        batch_results,
                                                    )
                                                )
                                                current_offset += len(batch)

                                                chat_candidates_map[
                                                    chat_map_id
                                                ] = candidate_results
                                                chat_offsets_map[
                                                    chat_map_id
                                                ] = current_offset
                                                st.session_state[
                                                    chat_candidates_map_key
                                                ] = chat_candidates_map
                                                st.session_state[
                                                    chat_offsets_map_key
                                                ] = chat_offsets_map

                                                global_candidates = (
                                                    st.session_state.get(
                                                        global_candidates_key,
                                                        [],
                                                    )
                                                )
                                                st.session_state[
                                                    global_candidates_key
                                                ] = merge_candidate_results(
                                                    global_candidates,
                                                    batch_results,
                                                )

                                                persist_workspace_if_changed(
                                                    telegram_id,
                                                    force=True,
                                                )
                                                st.success(
                                                    "Партия обработана. "
                                                    f"Проверено: {current_offset} "
                                                    f"из {len(members)}."
                                                )

                                            except Exception as exc:
                                                st.error(
                                                    "Поиск по критериям ЦА "
                                                    f"не выполнен: {exc}"
                                                )

                                    analyzed_count = min(
                                        current_offset,
                                        len(members),
                                    )
                                    recommended_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Передать Неоне"
                                    )
                                    more_data_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Нужно больше данных"
                                    )
                                    not_fit_count = sum(
                                        1
                                        for item in candidate_results
                                        if item.get("recommendation")
                                        == "Пока не подходит"
                                    )

                                    st.write(
                                        f"Всего загружено: **{len(members)}** · "
                                        f"Проанализировано: **{analyzed_count}** · "
                                        f"Соответствует ЦА: **{recommended_count}** · "
                                        f"Недостаточно данных: **{more_data_count}** · "
                                        f"Не подходит: **{not_fit_count}**"
                                    )
                                    st.progress(
                                        analyzed_count / len(members)
                                    )

                                    suitable_candidates = sorted(
                                        [
                                            item
                                            for item in candidate_results
                                            if item.get("recommendation")
                                            == "Передать Неоне"
                                        ],
                                        key=lambda item: item.get(
                                            "score",
                                            0,
                                        ),
                                        reverse=True,
                                    )

                                    if suitable_candidates:
                                        top_chat_candidates = suitable_candidates[:10]
                                        st.markdown(
                                            "#### ⭐ 10 лучших кандидатов из чата"
                                        )
                                        st.caption(
                                            "Неония рекомендует людей, но окончательный "
                                            "выбор делает владелец кабинета. Откройте "
                                            "карточку, изучите основания и отметьте нужных "
                                            "людей. Общий лимит — не более 5 человек."
                                        )

                                        global_candidates = st.session_state.get(
                                            global_candidates_key,
                                            [],
                                        )
                                        global_candidate_by_id = {
                                            int(item["telegram_id"]): item
                                            for item in global_candidates
                                            if item.get("telegram_id") is not None
                                        }
                                        owner_contacts = st.session_state.get(
                                            owner_contacts_key,
                                            {},
                                        )
                                        if not isinstance(owner_contacts, dict):
                                            owner_contacts = {}
                                        known_contact_ids = {
                                            int(contact_id)
                                            for contact_id in owner_contacts
                                        }
                                        recommended_limit = max(
                                            0,
                                            5 - sum(1 for contact in owner_contacts.values() if isinstance(contact, dict) and str(contact.get("work_date") or "") == datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()),
                                        )

                                        existing_selected_ids = []
                                        for contact_id in st.session_state.get(
                                            selected_candidates_key,
                                            [],
                                        ):
                                            try:
                                                contact_id = int(contact_id)
                                            except (TypeError, ValueError):
                                                continue
                                            if (
                                                contact_id in global_candidate_by_id
                                                and contact_id not in known_contact_ids
                                                and contact_id not in existing_selected_ids
                                            ):
                                                existing_selected_ids.append(contact_id)
                                        existing_selected_ids = existing_selected_ids[
                                            :recommended_limit
                                        ]

                                        top_candidate_by_id = {
                                            int(item["telegram_id"]): item
                                            for item in top_chat_candidates
                                        }
                                        top_candidate_ids = list(top_candidate_by_id)
                                        selected_elsewhere_ids = [
                                            contact_id
                                            for contact_id in existing_selected_ids
                                            if contact_id not in top_candidate_by_id
                                        ]
                                        current_chat_limit = max(
                                            0,
                                            recommended_limit
                                            - len(selected_elsewhere_ids),
                                        )

                                        for contact_id in top_candidate_ids:
                                            checkbox_key = (
                                                "chat_candidate_select_"
                                                f"{telegram_id}_{selected_chat_id}_"
                                                f"{contact_id}"
                                            )
                                            if checkbox_key not in st.session_state:
                                                st.session_state[checkbox_key] = (
                                                    contact_id in existing_selected_ids
                                                )

                                        currently_checked = [
                                            contact_id
                                            for contact_id in top_candidate_ids
                                            if st.session_state.get(
                                                "chat_candidate_select_"
                                                f"{telegram_id}_{selected_chat_id}_"
                                                f"{contact_id}",
                                                False,
                                            )
                                        ]

                                        if selected_elsewhere_ids:
                                            st.info(
                                                "В других источниках уже выбрано: "
                                                f"{len(selected_elsewhere_ids)}. "
                                                "Для этого чата осталось мест: "
                                                f"{current_chat_limit}."
                                            )

                                        for number, candidate in enumerate(
                                            top_chat_candidates,
                                            start=1,
                                        ):
                                            contact_id = int(
                                                candidate["telegram_id"]
                                            )
                                            checkbox_key = (
                                                "chat_candidate_select_"
                                                f"{telegram_id}_{selected_chat_id}_"
                                                f"{contact_id}"
                                            )
                                            is_checked = bool(
                                                st.session_state.get(
                                                    checkbox_key,
                                                    False,
                                                )
                                            )
                                            limit_reached = (
                                                len(currently_checked)
                                                >= current_chat_limit
                                                and not is_checked
                                            )
                                            username = (
                                                f"@{candidate['username']}"
                                                if candidate.get("username")
                                                else "без username"
                                            )

                                            with st.container(border=True):
                                                st.checkbox(
                                                    (
                                                        f"Выбрать кандидата №{number}: "
                                                        f"{candidate['name']}"
                                                    ),
                                                    key=checkbox_key,
                                                    disabled=(
                                                        current_chat_limit == 0
                                                        or limit_reached
                                                    ),
                                                )
                                                st.markdown(
                                                    f"**{candidate['score']}% соответствия** "
                                                    f"· {candidate['segment']}"
                                                )
                                                st.caption(
                                                    f"{username} · источник: "
                                                    f"{candidate.get('source_chat_title') or selected_chat['title']}"
                                                )

                                                with st.expander(
                                                    "📋 Открыть карточку кандидата"
                                                ):
                                                    st.write(
                                                        f"**Уверенность Неонии:** "
                                                        f"{candidate.get('confidence', '—')}"
                                                    )
                                                    st.write(
                                                        "**Почему соответствует ЦА:**"
                                                    )
                                                    for reason in candidate.get(
                                                        "reasons",
                                                        [],
                                                    ):
                                                        st.write(f"• {reason}")

                                                    profile_about = str(
                                                        candidate.get(
                                                            "profile_about",
                                                            "",
                                                        )
                                                        or ""
                                                    ).strip()
                                                    if profile_about:
                                                        st.write("**Bio профиля:**")
                                                        st.write(profile_about)

                                                    public_messages = candidate.get(
                                                        "public_messages",
                                                        [],
                                                    )
                                                    if public_messages:
                                                        st.write(
                                                            "**Публичные сообщения, "
                                                            "учтённые при анализе:**"
                                                        )
                                                        for message in public_messages[:3]:
                                                            message_text = str(
                                                                message.get("text") or ""
                                                            ).strip()
                                                            if message_text:
                                                                st.markdown(
                                                                    f"> {message_text}"
                                                                )

                                                    facts = []
                                                    if candidate.get("mutual_contact"):
                                                        facts.append(
                                                            "есть взаимный контакт"
                                                        )
                                                    if candidate.get("verified"):
                                                        facts.append(
                                                            "профиль подтверждён Telegram"
                                                        )
                                                    if facts:
                                                        st.caption(
                                                            "Дополнительно: "
                                                            + "; ".join(facts)
                                                        )

                                                    st.write(
                                                        "**Безопасная тема первого "
                                                        "обращения:**"
                                                    )
                                                    st.write(
                                                        candidate.get(
                                                            "message_angle",
                                                            "Нейтральное знакомство",
                                                        )
                                                    )

                                        checked_current_ids = [
                                            contact_id
                                            for contact_id in top_candidate_ids
                                            if st.session_state.get(
                                                "chat_candidate_select_"
                                                f"{telegram_id}_{selected_chat_id}_"
                                                f"{contact_id}",
                                                False,
                                            )
                                        ]
                                        if len(checked_current_ids) > current_chat_limit:
                                            overflow_ids = checked_current_ids[
                                                current_chat_limit:
                                            ]
                                            for contact_id in overflow_ids:
                                                st.session_state[
                                                    "chat_candidate_select_"
                                                    f"{telegram_id}_{selected_chat_id}_"
                                                    f"{contact_id}"
                                                ] = False
                                            checked_current_ids = checked_current_ids[
                                                :current_chat_limit
                                            ]

                                        final_selected_ids = (
                                            selected_elsewhere_ids
                                            + checked_current_ids
                                        )[:recommended_limit]
                                        st.session_state[
                                            selected_candidates_key
                                        ] = final_selected_ids
                                        persist_workspace_if_changed(telegram_id)

                                        selected_total = (
                                            len(final_selected_ids)
                                            + len(owner_contacts)
                                        )
                                        st.markdown(
                                            f"**Выбрано владельцем: "
                                            f"{selected_total} из 5**"
                                        )

                                        selected_from_this_chat = [
                                            top_candidate_by_id[contact_id]
                                            for contact_id in checked_current_ids
                                            if contact_id in top_candidate_by_id
                                        ]
                                        if selected_from_this_chat:
                                            st.markdown(
                                                "#### ✅ Выбраны из этого чата"
                                            )
                                            for candidate in selected_from_this_chat:
                                                st.write(
                                                    f"• **{candidate['name']}** · "
                                                    f"{candidate['score']}% · "
                                                    f"{candidate['segment']}"
                                                )

                                        confirm_selection = st.button(
                                            "✅ Сохранить выбор и передать Неоне",
                                            type="primary",
                                            disabled=not final_selected_ids,
                                            key=(
                                                "confirm_chat_candidates_"
                                                f"{telegram_id}_{selected_chat_id}"
                                            ),
                                        )
                                        if confirm_selection:
                                            selected_id_set = set(
                                                final_selected_ids
                                            )
                                            for item in global_candidates:
                                                try:
                                                    item_id = int(
                                                        item.get("telegram_id")
                                                    )
                                                except (TypeError, ValueError):
                                                    continue
                                                if item_id in selected_id_set:
                                                    item["status"] = (
                                                        "Выбран владельцем"
                                                    )
                                            st.session_state[
                                                global_candidates_key
                                            ] = global_candidates
                                            for item in candidate_results:
                                                try:
                                                    item_id = int(
                                                        item.get("telegram_id")
                                                    )
                                                except (TypeError, ValueError):
                                                    continue
                                                if item_id in selected_id_set:
                                                    item["status"] = (
                                                        "Выбран владельцем"
                                                    )
                                            chat_candidates_map[
                                                chat_map_id
                                            ] = candidate_results
                                            st.session_state[
                                                chat_candidates_map_key
                                            ] = chat_candidates_map
                                            persist_workspace_if_changed(
                                                telegram_id,
                                                force=True,
                                            )
                                            st.success(
                                                "Выбор сохранён. Неона получила "
                                                "выбранных кандидатов для следующего "
                                                "этапа — подготовки персональных "
                                                "первых сообщений."
                                            )

                                    if candidate_results:
                                        with st.expander(
                                            "Все результаты по выбранному чату"
                                        ):
                                            all_results_table = [
                                                {
                                                    "Имя": item["name"],
                                                    "Username": (
                                                        f"@{item['username']}"
                                                        if item.get("username")
                                                        else "—"
                                                    ),
                                                    "Сегмент": item["segment"],
                                                    "Соответствие": (
                                                        f"{item['score']}%"
                                                    ),
                                                    "Уверенность": item[
                                                        "confidence"
                                                    ],
                                                    "Рекомендация": item[
                                                        "recommendation"
                                                    ],
                                                }
                                                for item in sorted(
                                                    candidate_results,
                                                    key=lambda value: value.get(
                                                        "score",
                                                        0,
                                                    ),
                                                    reverse=True,
                                                )
                                            ]
                                            st.dataframe(
                                                all_results_table,
                                                use_container_width=True,
                                                hide_index=True,
                                            )

                                else:
                                    st.info(
                                        "Выберите чат и нажмите «Загрузить "
                                        "участников выбранного чата»."
                                    )

                        elif neonia_mode == "👥 Поиск контактов":
                            contacts_result = render_neonia_contacts()
                        
                            contacts_state_key = (
                                f"neonia_telegram_contacts_{telegram_id}"
                            )
                            search_done_key = (
                                f"neonia_contacts_search_done_{telegram_id}"
                            )
                        
                            if contacts_result["find_contacts"]:
                                with st.spinner(
                                    "Неония получает контакты из Telegram..."
                                ):
                                    try:
                                        contacts = run_telegram_async(
                                            fetch_telegram_contacts(telegram_id)
                                        )
                        
                                        st.session_state[
                                            contacts_state_key
                                        ] = contacts
                        
                                        st.session_state[
                                            search_done_key
                                        ] = True

                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                        
                                    except Exception as exc:
                                        st.error(
                                            f"Не удалось получить контакты: {exc}"
                                        )
                        
                            contacts = st.session_state.get(
                                contacts_state_key,
                                [],
                            )
                        
                            if contacts:
                                st.success(
                                    f"Найдено контактов: {len(contacts)}"
                                )
                        
                                contacts_for_table = [
                                    {
                                        "Имя": contact["name"],
                                        "Username": (
                                            f"@{contact['username']}"
                                            if contact["username"]
                                            else "—"
                                        ),
                                        "Телефон": contact["phone"] or "—",
                                    }
                                    for contact in contacts
                                ]
                        
                                st.dataframe(
                                    contacts_for_table,
                                    use_container_width=True,
                                    hide_index=True,
                                )
                        
                            elif st.session_state.get(
                                search_done_key,
                                False,
                            ):
                                st.info(
                                    "В Telegram не найдено доступных контактов."
                                )

                        elif neonia_mode == "🧠 Анализ контактов по ЦА":
                            passport_key = (
                                f"neonia_target_audience_passport_{telegram_id}"
                            )
                            contacts_state_key = (
                                f"neonia_telegram_contacts_{telegram_id}"
                            )

                            passport = st.session_state.get(
                                passport_key
                            )
                            contacts = st.session_state.get(
                                contacts_state_key,
                                [],
                            )

                            if not passport:
                                st.warning(
                                    "Сначала проведите анализ проекта "
                                    "и создайте паспорт целевой аудитории."
                                )

                            elif not contacts:
                                st.warning(
                                    "Сначала откройте «Поиск контактов» "
                                    "и загрузите контакты из Telegram."
                                )

                            else:
                                st.success(
                                    "✅ Паспорт ЦА и контакты готовы"
                                )
                                st.write(
                                    "Контактов для дальнейшей селекции: "
                                    f"{len(contacts)}"
                                )

                                with st.expander(
                                    "Посмотреть паспорт целевой аудитории"
                                ):
                                    st.write(
                                        passport["analysis"]
                                    )

                                candidates_key = (
                                    f"neonia_candidates_{telegram_id}"
                                )
                                offset_key = (
                                    f"neonia_selection_offset_{telegram_id}"
                                )

                                current_offset = st.session_state.get(
                                    offset_key,
                                    0,
                                )
                                candidate_results = (
                                    st.session_state.get(
                                        candidates_key,
                                        [],
                                    )
                                )

                                st.caption(
                                    "Для оценки ИИ получает имя, "
                                    "username, bio и несколько последних "
                                    "текстовых сообщений. Номер телефона "
                                    "в анализ не передаётся."
                                )

                                button_label = (
                                    "🧠 Начать селекцию контактов"
                                    if current_offset == 0
                                    else "🧠 Проанализировать "
                                    "следующие 10 контактов"
                                )

                                analyze_batch = st.button(
                                    button_label,
                                    type="primary",
                                    disabled=(
                                        current_offset >= len(contacts)
                                    ),
                                    key=(
                                        "neonia_start_contact_selection_"
                                        f"{telegram_id}_{current_offset}"
                                    ),
                                )

                                if analyze_batch:
                                    batch = contacts[
                                        current_offset:
                                        current_offset + 10
                                    ]

                                    with st.spinner(
                                        "Неония изучает доступный "
                                        "контекст и сравнивает контакты "
                                        "с паспортом ЦА..."
                                    ):
                                        try:
                                            contact_contexts = (
                                                run_telegram_async(
                                                    fetch_telegram_contact_contexts(
                                                        telegram_id,
                                                        batch,
                                                    )
                                                )
                                            )
                                            batch_results = (
                                                analyze_contacts_for_target_audience(
                                                    passport["analysis"],
                                                    contact_contexts,
                                                )
                                            )
                                            candidate_results = (
                                                merge_candidate_results(
                                                    candidate_results,
                                                    batch_results,
                                                )
                                            )
                                            st.session_state[
                                                candidates_key
                                            ] = candidate_results
                                            st.session_state[
                                                offset_key
                                            ] = (
                                                current_offset
                                                + len(batch)
                                            )
                                            current_offset = (
                                                st.session_state[
                                                    offset_key
                                                ]
                                            )

                                            persist_workspace_if_changed(
                                                telegram_id,
                                                force=True,
                                            )
                                            st.success(
                                                "Партия обработана. "
                                                f"Проверено: "
                                                f"{current_offset} из "
                                                f"{len(contacts)}."
                                            )

                                        except Exception as exc:
                                            st.error(
                                                "Селекция не выполнена: "
                                                f"{exc}"
                                            )

                                analyzed_count = min(
                                    current_offset,
                                    len(contacts),
                                )
                                recommended_count = sum(
                                    1
                                    for item in candidate_results
                                    if item.get(
                                        "recommendation"
                                    ) == "Передать Неоне"
                                )
                                more_data_count = sum(
                                    1
                                    for item in candidate_results
                                    if item.get(
                                        "recommendation"
                                    ) == "Нужно больше данных"
                                )
                                not_fit_count = sum(
                                    1
                                    for item in candidate_results
                                    if item.get(
                                        "recommendation"
                                    ) == "Пока не подходит"
                                )

                                st.markdown(
                                    "#### 📊 Ход анализа контактов"
                                )
                                st.write(
                                    f"Всего контактов: **{len(contacts)}** · "
                                    f"Проанализировано: "
                                    f"**{analyzed_count}** · "
                                    f"Соответствует ЦА: "
                                    f"**{recommended_count}** · "
                                    f"Недостаточно данных: "
                                    f"**{more_data_count}** · "
                                    f"Не подходит: **{not_fit_count}**"
                                )

                                if contacts:
                                    st.progress(
                                        analyzed_count / len(contacts)
                                    )

                                if analyzed_count < len(contacts):
                                    st.info(
                                        "Это предварительный результат. "
                                        "Чтобы узнать точное количество "
                                        "подходящих людей из всех контактов, "
                                        "продолжайте анализ партиями по 10."
                                    )

                                if (
                                    recommended_count < 10
                                    and analyzed_count < len(contacts)
                                ):
                                    st.warning(
                                        f"Для рабочего стола нужно до 10 "
                                        f"кандидатов. Сейчас найдено "
                                        f"{recommended_count}. Нажмите "
                                        "«Проанализировать следующие 10 "
                                        "контактов»."
                                    )

                                if candidate_results:
                                    st.markdown(
                                        "#### 📋 Результаты уже "
                                        "проанализированных контактов"
                                    )
                                    results_for_table = [
                                        {
                                            "Имя": item["name"],
                                            "Username": (
                                                f"@{item['username']}"
                                                if item.get("username")
                                                else "—"
                                            ),
                                            "Сегмент": item["segment"],
                                            "Соответствие": (
                                                f"{item['score']}%"
                                            ),
                                            "Уверенность": (
                                                item["confidence"]
                                            ),
                                            "Рекомендация": (
                                                item["recommendation"]
                                            ),
                                        }
                                        for item in candidate_results
                                    ]

                                    st.dataframe(
                                        results_for_table,
                                        use_container_width=True,
                                        hide_index=True,
                                    )

                                    st.info(
                                        "На рабочем столе показываются "
                                        "10 лучших кандидатов с рекомендацией "
                                        f"«Передать Неоне». Из них {first_name} "
                                        "самостоятельно выбирает не более 5."
                                    )

                                    if st.button(
                                        "Сбросить результаты и "
                                        "начать заново",
                                        key=(
                                            "neonia_reset_selection_"
                                            f"{telegram_id}"
                                        ),
                                    ):
                                        st.session_state[
                                            candidates_key
                                        ] = []
                                        st.session_state[
                                            offset_key
                                        ] = 0
                                        st.session_state.pop(
                                            f"neonia_selected_candidates_"
                                            f"{telegram_id}",
                                            None,
                                        )
                                        st.session_state.pop(
                                            f"neona_first_message_drafts_"
                                            f"{telegram_id}",
                                            None,
                                        )

                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                                        st.rerun()

                        st.stop()
                    with st.form("neonia_source_form"):
                        project_links = st.text_area(
                            "🔗 Ссылки на проект",
                            placeholder=(
                                "Официальный сайт, страница продукта, "
                                "презентация или ролик — каждая ссылка "
                                "с новой строки."
                            ),
                            height=120,
                        )

                        project_files = st.file_uploader(
                            "📄 Загрузите материалы проекта",
                            type=[
                                "pdf",
                                "docx",
                                "pptx",
                                "txt",
                                "csv",
                                "xlsx",
                            ],
                            accept_multiple_files=True,
                            help=(
                                "Можно загрузить сразу несколько документов."
                            ),
                        )

                        owner_note = st.text_area(
                            "📝 Комментарий владельца — необязательно",
                            placeholder=(
                                "Например: отдельно проверь продукт, "
                                "маркетинг, доходность и возможные риски."
                            ),
                            height=100,
                        )

                        neonia_submitted = st.form_submit_button(
                            "🎯 Изучить проект и определить ЦА"
                        )

                        if neonia_submitted:
                            if not project_links.strip() and not project_files:
                                st.warning(
                                    "Добавьте хотя бы одну ссылку или загрузите материал проекта."
                                )
                            else:
                                neonia_prompt = """
    Ты — Неония, ИИ-аналитик Агентства W.

Сейчас ты работаешь только в разделе
«Анализ проекта и определение целевой аудитории».

Твоя задача:
— изучить предоставленные ссылки и документы;
— понять суть проекта, продукта и предложения;
— определить, какие реальные проблемы людей решает проект;
— проверить основные заявления проекта;
— выявить сильные стороны, ограничения и возможные риски;
— определить отдельные портреты потенциального клиента и партнёра;
— сформировать критерии, по которым позже будут анализироваться
контакты и участники чатов.

На этом этапе не анализируй конкретного человека.
Не создавай раздел «Анализ человека» и не сообщай,
что данные о человеке не предоставлены.

Отвечай простым русским языком.

Обязательно разделяй:
— подтверждённые факты;
— заявления самого проекта;
— независимые подтверждения;
— аналитические выводы;
— предположения;
— сведения, которых недостаточно для вывода.

Структура ответа:

1. Суть проекта.
2. Продукты или услуги и их реальная ценность.
3. Какие проблемы людей может решать проект.
4. Как устроена модель проекта и из чего формируется доход.
5. Условия входа: деньги, время, навыки и возможные сложности.
6. Сильные стороны проекта.
7. Ограничения и возможные риски.
8. Основные сегменты целевой аудитории.
9. Портрет потенциального клиента.
10. Портрет потенциального партнёра.
11. Признаки, по которым искать подходящих людей в контактах.
12. Признаки, по которым искать подходящих людей в чатах.
13. Кому проект, вероятно, не подходит.
14. Какие данные ещё необходимо проверить.
15. Критерии для следующего этапа:
«Анализ контактов по параметрам ЦА».

Не придумывай факты и не выдавай рекламные заявления
за независимо подтверждённую информацию.
    """
    
                            file_names = ", ".join(file.name for file in project_files) if project_files else "Файлы не загружены."
                            neonia_request = f"Ссылки на проект:\n{project_links.strip() or 'Ссылки не указаны.'}\n\nЗагруженные материалы:\n{file_names}\n\nКомментарий:\n{owner_note.strip() or 'Комментарий не указан.'}"
    
                            with st.spinner("Неония проводит анализ..."):
                                neonia_answer = ask_openai(
                                    neonia_prompt,
                                    neonia_request,
                                    uploaded_files=project_files,
                                    use_web_search=bool(project_links.strip()),
                                )

                            passport_key = (
                                f"neonia_target_audience_passport_{telegram_id}"
                            )

                            st.session_state[passport_key] = {
                                "analysis": neonia_answer,
                                "project_links": project_links.strip(),
                                "file_names": file_names,
                                "owner_note": owner_note.strip(),
                                "saved_at": datetime.now(
                                    ZoneInfo("Europe/Berlin")
                                ).isoformat(),
                            }

                            persist_workspace_if_changed(
                                telegram_id,
                                force=True,
                            )

                            st.success(
                                "✅ Паспорт целевой аудитории сохранён"
                            )
                            st.markdown(
                                "#### 📋 Результат Неонии"
                            )
                            st.write(neonia_answer)
    
                elif selected_agent == "Неона":
                    st.caption(
                        "Неона получает только тех людей, которых выбрал владелец "
                        "кабинета. Её задача — правдиво заинтересовать человека "
                        "и постепенно привести к осознанной встрече с владельцем. "
                        "Первое сообщение она готовит, но не отправляет без "
                        "утверждения владельца."
                    )

                    with st.expander("📜 Задача и регламент Неоны"):
                        st.markdown(
                            neona_reglament_markdown(first_name)
                        )
                        st.caption(
                            "Согласованные встречи сохраняются во внутреннем "
                            "календаре Агентства W. Основное время — МСК; "
                            "местное время человека рассчитывается на дату встречи."
                        )

                    with st.expander("💬 Входящие сообщения Telegram — тест"):
                        st.caption(
                            "На этом этапе Неона отвечает только людям, которым "
                            "из Агентства W уже было отправлено утверждённое первое "
                            "сообщение. Старые сообщения не обрабатываются. Первый "
                            "запуск создаёт безопасную точку отсчёта."
                        )
                        st.info(
                            "Тестовый режим: проверка выполняется по кнопке. "
                            "После проверки логики подключим отдельный круглосуточный "
                            "worker, чтобы ответы не зависели от открытого сайта."
                        )
                        if st.button(
                            "🔄 Проверить входящие сейчас",
                            type="primary",
                            key=f"neona_sync_incoming_{telegram_id}",
                        ):
                            try:
                                with st.spinner("Неона проверяет новые ответы..."):
                                    dialog_stats = run_sync_owner_once(
                                        int(telegram_id),
                                        first_name,
                                        initialize_new_dialogs=True,
                                    )
                                if dialog_stats.get("initialized", 0) and not dialog_stats.get("processed", 0):
                                    st.success(
                                        "Точка отсчёта создана. Старую переписку "
                                        "Неона не тронула. Теперь можно отправить "
                                        "новое тестовое сообщение и нажать кнопку ещё раз."
                                    )
                                elif dialog_stats.get("replied", 0):
                                    st.success(
                                        "Новые ответы обработаны: "
                                        f"{dialog_stats.get('replied', 0)}."
                                    )
                                elif dialog_stats.get("errors", 0):
                                    st.warning(
                                        "Обработка завершилась с ошибкой. "
                                        "Ни одно неподтверждённое действие не было "
                                        "объявлено выполненным."
                                    )
                                else:
                                    st.info("Новых ответов пока нет.")
                                st.caption(
                                    "Контактов после первого сообщения: "
                                    f"{dialog_stats.get('allowed', 0)} · "
                                    "новых входящих: "
                                    f"{dialog_stats.get('processed', 0)}"
                                )
                            except NeonaDialogError as exc:
                                st.error(str(exc))
                            except Exception as exc:
                                st.error(
                                    "Не удалось проверить входящие сообщения: "
                                    + str(exc)
                                )

                    candidates_key = (
                        f"neonia_candidates_{telegram_id}"
                    )
                    selected_candidates_key = (
                        f"neonia_selected_candidates_{telegram_id}"
                    )
                    owner_contacts_key = (
                        f"neonia_owner_known_contacts_{telegram_id}"
                    )
                    contacts_state_key = (
                        f"neonia_telegram_contacts_{telegram_id}"
                    )
                    known_search_results_key = (
                        f"neonia_known_search_results_{telegram_id}"
                    )
                    passport_key = (
                        f"neonia_target_audience_passport_{telegram_id}"
                    )
                    neona_drafts_key = (
                        f"neona_first_message_drafts_{telegram_id}"
                    )
                    sent_log_key = (
                        f"neona_first_message_sent_log_{telegram_id}"
                    )

                    candidate_results = st.session_state.get(
                        candidates_key,
                        [],
                    )
                    owner_contacts = st.session_state.get(
                        owner_contacts_key,
                        {},
                    )
                    if not isinstance(owner_contacts, dict):
                        owner_contacts = {}

                    # История знакомых хранится постоянно, но "сегодняшняя работа"
                    # должна начинаться заново каждый день.
                    today_work_date = datetime.now(
                        ZoneInfo("Europe/Berlin")
                    ).date().isoformat()
                    drafts_for_today = st.session_state.get(
                        neona_drafts_key,
                        {},
                    )
                    if not isinstance(drafts_for_today, dict):
                        drafts_for_today = {}
                    sent_log_for_today = st.session_state.get(
                        sent_log_key,
                        [],
                    )
                    if not isinstance(sent_log_for_today, list):
                        sent_log_for_today = []

                    def known_contact_is_active_today(contact_id, contact):
                        try:
                            normalized_contact_id = int(contact_id)
                        except (TypeError, ValueError):
                            return False

                        if str(contact.get("work_date") or "") == today_work_date:
                            return True

                        previous_draft = drafts_for_today.get(
                            normalized_contact_id,
                            drafts_for_today.get(str(normalized_contact_id), {}),
                        )
                        if isinstance(previous_draft, dict):
                            sent_at = str(previous_draft.get("sent_at") or "")
                            if sent_at[:10] == today_work_date:
                                return True

                        for event in sent_log_for_today:
                            if not isinstance(event, dict):
                                continue
                            try:
                                event_contact_id = int(
                                    event.get("telegram_id")
                                )
                            except (TypeError, ValueError):
                                continue
                            if (
                                event_contact_id == normalized_contact_id
                                and str(event.get("sent_at") or "")[:10]
                                == today_work_date
                            ):
                                return True

                        return False

                    active_owner_contacts = {
                        int(contact_id): contact
                        for contact_id, contact in owner_contacts.items()
                        if isinstance(contact, dict)
                        and known_contact_is_active_today(contact_id, contact)
                    }

                    all_contacts = st.session_state.get(
                        contacts_state_key,
                        [],
                    )
                    if not isinstance(all_contacts, list):
                        all_contacts = []

                    candidate_by_id = {}
                    for candidate in candidate_results:
                        try:
                            candidate_id = int(
                                candidate.get("telegram_id")
                            )
                        except (TypeError, ValueError):
                            continue
                        candidate_by_id[candidate_id] = candidate

                    for contact_id, contact in owner_contacts.items():
                        try:
                            normalized_id = int(contact_id)
                        except (TypeError, ValueError):
                            continue
                        candidate_by_id[normalized_id] = contact

                    selected_ids = []
                    for contact_id in st.session_state.get(
                        selected_candidates_key,
                        [],
                    ):
                        try:
                            normalized_id = int(contact_id)
                        except (TypeError, ValueError):
                            continue
                        if (
                            normalized_id in candidate_by_id
                            and normalized_id not in selected_ids
                        ):
                            selected_ids.append(normalized_id)

                    # В дневной лимит входят только знакомые,
                    # которых владелец выбрал для работы именно сегодня.
                    for contact_id in active_owner_contacts:
                        try:
                            normalized_id = int(contact_id)
                        except (TypeError, ValueError):
                            continue
                        if normalized_id not in selected_ids:
                            selected_ids.append(normalized_id)

                    selected_ids = selected_ids[:5]
                    selected_contacts = [
                        candidate_by_id[contact_id]
                        for contact_id in selected_ids
                        if contact_id in candidate_by_id
                    ]

                    st.divider()
                    st.markdown("### 🤝 Найти знакомого")
                    st.write(
                        "Найдите любого человека среди уже загруженных "
                        "Telegram-контактов — даже если Неония не "
                        "рекомендовала его по критериям ЦА."
                    )

                    if not all_contacts:
                        st.warning(
                            "Сначала загрузите контакты Telegram "
                            "в разделе Неонии."
                        )
                    else:
                        known_query = st.text_input(
                            "Имя, @username или номер телефона",
                            placeholder=(
                                "Например: Наталья, @username или +49..."
                            ),
                            key=f"known_contact_query_{telegram_id}",
                        )

                        if st.button(
                            "🔎 Найти знакомого",
                            key=f"known_contact_search_{telegram_id}",
                        ):
                            search_results = search_known_contacts(
                                all_contacts,
                                known_query,
                            )
                            st.session_state[
                                known_search_results_key
                            ] = search_results
                            if not search_results:
                                st.warning(
                                    "В загруженных Telegram-контактах "
                                    "совпадений не найдено."
                                )

                        known_results = st.session_state.get(
                            known_search_results_key,
                            [],
                        )

                        if known_results:
                            known_by_id = {
                                int(item["telegram_id"]): item
                                for item in known_results
                                if item.get("telegram_id") is not None
                            }
                            known_options = [None] + list(
                                known_by_id.keys()
                            )

                            chosen_known_id = st.selectbox(
                                "Выберите найденного человека",
                                options=known_options,
                                format_func=lambda contact_id: (
                                    "Выберите контакт"
                                    if contact_id is None
                                    else (
                                        f"{known_by_id[contact_id].get('name') or 'Без имени'} "
                                        + (
                                            f"@{known_by_id[contact_id].get('username')}"
                                            if known_by_id[contact_id].get("username")
                                            else ""
                                        )
                                    )
                                ),
                                key=(
                                    "known_contact_select_"
                                    f"{telegram_id}"
                                ),
                            )

                            if chosen_known_id is not None:
                                chosen_contact = known_by_id[
                                    chosen_known_id
                                ]
                                already_added = (
                                    chosen_known_id in active_owner_contacts
                                )
                                known_before = (
                                    chosen_known_id in owner_contacts
                                    or str(chosen_known_id) in owner_contacts
                                )
                                previous_known_draft = drafts_for_today.get(
                                    int(chosen_known_id),
                                    drafts_for_today.get(
                                        str(chosen_known_id),
                                        {},
                                    ),
                                )
                                dialog_already_started = bool(
                                    isinstance(previous_known_draft, dict)
                                    and previous_known_draft.get("sent")
                                )
                                already_selected = (
                                    chosen_known_id in selected_ids
                                )
                                limit_reached = len(selected_ids) >= 5

                                familiarity_note = st.text_area(
                                    "Откуда и как вы знакомы?",
                                    placeholder=(
                                        "Например: вместе работали "
                                        "в проекте два года назад."
                                    ),
                                    key=(
                                        "known_familiarity_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                )
                                owner_draft = st.text_area(
                                    "Ваш набросок первого сообщения",
                                    placeholder=(
                                        "Напишите простыми словами, "
                                        "что Вы хотите сказать."
                                    ),
                                    key=(
                                        "known_owner_draft_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                )
                                must_mention = st.text_input(
                                    "Что обязательно упомянуть?",
                                    key=(
                                        "known_must_mention_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                )
                                avoid = st.text_input(
                                    "Чего лучше не говорить?",
                                    key=(
                                        "known_avoid_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                )

                                if already_added:
                                    st.info(
                                        "Этот знакомый уже добавлен "
                                        "к сегодняшней работе."
                                    )
                                elif known_before and dialog_already_started:
                                    st.info(
                                        "С этим человеком диалог уже начинался ранее. "
                                        "Повторное первое сообщение не нужно. "
                                        "Можно снова добавить его к работе сегодня: "
                                        "после нового входящего Неона продолжит диалог."
                                    )
                                elif known_before:
                                    st.info(
                                        "Этот знакомый уже есть в истории, но сегодня "
                                        "ещё не добавлен к работе."
                                    )
                                elif already_selected:
                                    st.info(
                                        "Этот человек уже выбран "
                                        "среди текущих кандидатов."
                                    )
                                elif limit_reached:
                                    st.warning(
                                        "Лимит 5 человек уже заполнен."
                                    )

                                if st.button(
                                    "➕ Добавить знакомого к работе",
                                    disabled=(
                                        already_added
                                        or already_selected
                                        or limit_reached
                                    ),
                                    key=(
                                        "add_known_contact_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                ):
                                    if not owner_draft.strip():
                                        st.warning(
                                            "Добавьте хотя бы короткий "
                                            "набросок первого сообщения."
                                        )
                                    else:
                                        previous_known = owner_contacts.get(
                                            chosen_known_id,
                                            owner_contacts.get(
                                                str(chosen_known_id),
                                                {},
                                            ),
                                        )
                                        if not isinstance(previous_known, dict):
                                            previous_known = {}

                                        owner_contacts[
                                            chosen_known_id
                                        ] = {
                                            **previous_known,
                                            "telegram_id": int(
                                                chosen_known_id
                                            ),
                                            "name": (
                                                chosen_contact.get("name")
                                                or previous_known.get("name")
                                                or "Без имени"
                                            ),
                                            "first_name": (
                                                chosen_contact.get("first_name")
                                                or previous_known.get("first_name")
                                                or ""
                                            ),
                                            "username": (
                                                chosen_contact.get("username")
                                                or previous_known.get("username")
                                                or ""
                                            ),
                                            "source": (
                                                "Знакомый — выбран директором"
                                            ),
                                            "segment": "Выбран директором",
                                            "score": 0,
                                            "confidence": "решение владельца",
                                            "reasons": [
                                                familiarity_note.strip()
                                                or (
                                                    "Добавлен владельцем "
                                                    "как знакомый контакт"
                                                )
                                            ],
                                            "recommendation": (
                                                "Добавлен директором"
                                            ),
                                            "message_angle": owner_draft.strip(),
                                            "familiarity_note": (
                                                familiarity_note.strip()
                                            ),
                                            "owner_draft": owner_draft.strip(),
                                            "must_mention": must_mention.strip(),
                                            "avoid": avoid.strip(),
                                            "work_date": today_work_date,
                                            "status": (
                                                "Диалог уже начат"
                                                if dialog_already_started
                                                else "Выбран владельцем"
                                            ),
                                        }
                                        st.session_state[
                                            owner_contacts_key
                                        ] = owner_contacts
                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                                        if dialog_already_started:
                                            st.success(
                                                "Знакомый снова добавлен к работе сегодня. "
                                                "Первое сообщение уже отправлялось ранее, "
                                                "поэтому Неона ждёт нового входящего и "
                                                "продолжит существующий диалог."
                                            )
                                        else:
                                            st.success(
                                                "Знакомый добавлен. "
                                                "Неона получила его для работы."
                                            )
                                        st.rerun()

                    drafts = st.session_state.get(
                        neona_drafts_key,
                        {},
                    )
                    if not isinstance(drafts, dict):
                        drafts = {}

                    sent_log = st.session_state.get(
                        sent_log_key,
                        [],
                    )
                    if not isinstance(sent_log, list):
                        sent_log = []

                    # Старые черновики сохраняются в рабочем пространстве.
                    # После изменения регламента они не должны оставаться
                    # утверждёнными: помечаем их как устаревшие и предлагаем
                    # владельцу сформировать новый текст.
                    drafts_changed = False
                    for selected_contact_id in selected_ids:
                        existing_draft = drafts.get(
                            int(selected_contact_id),
                            drafts.get(str(selected_contact_id)),
                        )
                        if not isinstance(existing_draft, dict):
                            continue
                        if existing_draft.get("sent"):
                            continue

                        rule_errors = validate_neona_first_message(
                            existing_draft.get("message", ""),
                            first_name,
                        )
                        if rule_errors:
                            needs_update = (
                                bool(existing_draft.get("approved"))
                                or existing_draft.get("status")
                                != "Требуется сформировать заново"
                                or existing_draft.get("validation_errors")
                                != rule_errors
                            )
                            if needs_update:
                                existing_draft["approved"] = False
                                existing_draft["status"] = (
                                    "Требуется сформировать заново"
                                )
                                existing_draft["validation_errors"] = rule_errors
                                drafts[int(selected_contact_id)] = existing_draft
                                drafts.pop(str(selected_contact_id), None)
                                drafts_changed = True

                    if drafts_changed:
                        st.session_state[neona_drafts_key] = drafts
                        persist_workspace_if_changed(
                            telegram_id,
                            force=True,
                        )

                    prepared_count = sum(
                        1
                        for contact_id in selected_ids
                        if int(contact_id) in drafts
                        or str(contact_id) in drafts
                    )
                    approved_count = sum(
                        1
                        for contact_id in selected_ids
                        if bool(
                            drafts.get(
                                int(contact_id),
                                drafts.get(str(contact_id), {}),
                            ).get("approved")
                        )
                    )

                    metric_columns = st.columns(3)
                    metric_columns[0].metric(
                        "Выбрано владельцем",
                        len(selected_contacts),
                    )
                    metric_columns[1].metric(
                        "Сообщения подготовлены",
                        prepared_count,
                    )
                    metric_columns[2].metric(
                        "Утверждено",
                        approved_count,
                    )
                    st.caption(
                        "Сегодня отправлено первых сообщений: "
                        f"{count_first_messages_sent_today(sent_log)} из 5"
                    )

                    if not selected_contacts:
                        st.warning(
                            "Неона пока не получила выбранных людей. "
                            "Вернитесь к Неонии, отметьте до 5 кандидатов и "
                            "нажмите «Сохранить выбор и передать Неоне»."
                        )
                    else:
                        passport = st.session_state.get(passport_key)
                        if not passport:
                            st.warning(
                                "Паспорт ЦА не найден. Сначала сохраните анализ "
                                "проекта и целевой аудитории у Неонии."
                            )
                        else:
                            st.markdown(
                                "#### ✍️ Формирование первых сообщений"
                            )
                            st.info(
                                "Каждый текст создаётся персонально. Владелец "
                                "может изменить сообщение и только затем утвердить его."
                            )

                            editable_draft_ids = []
                            for selected_contact in selected_contacts:
                                selected_contact_id = int(
                                    selected_contact["telegram_id"]
                                )
                                selected_draft = drafts.get(
                                    selected_contact_id,
                                    drafts.get(str(selected_contact_id)),
                                )
                                if (
                                    isinstance(selected_draft, dict)
                                    and not selected_draft.get("sent")
                                ):
                                    editable_draft_ids.append(
                                        selected_contact_id
                                    )

                            if editable_draft_ids:
                                bulk_columns = st.columns(2)
                                regenerate_all = bulk_columns[0].button(
                                    "🔄 Сформировать все сообщения заново",
                                    type="primary",
                                    key=(
                                        "neona_regenerate_all_drafts_"
                                        f"{telegram_id}"
                                    ),
                                )
                                delete_all = bulk_columns[1].button(
                                    "🗑️ Удалить все черновики",
                                    key=(
                                        "neona_delete_all_drafts_"
                                        f"{telegram_id}"
                                    ),
                                )

                                if regenerate_all:
                                    with st.spinner(
                                        "Неона формирует новые сообщения по "
                                        "действующему регламенту..."
                                    ):
                                        try:
                                            generated = (
                                                generate_neona_first_messages(
                                                    first_name,
                                                    passport["analysis"],
                                                    selected_contacts,
                                                )
                                            )
                                            for generated_id, new_draft in (
                                                generated.items()
                                            ):
                                                previous = drafts.get(
                                                    int(generated_id),
                                                    drafts.get(
                                                        str(generated_id),
                                                        {},
                                                    ),
                                                )
                                                if previous.get("sent"):
                                                    continue
                                                new_draft = dict(new_draft)
                                                new_draft["revision"] = int(
                                                    previous.get(
                                                        "revision",
                                                        0,
                                                    )
                                                    or 0
                                                ) + 1
                                                new_draft["approved"] = False
                                                new_draft[
                                                    "validation_errors"
                                                ] = []
                                                new_draft["status"] = (
                                                    "Сообщение сформировано заново"
                                                )
                                                drafts[int(generated_id)] = (
                                                    new_draft
                                                )
                                                drafts.pop(
                                                    str(generated_id),
                                                    None,
                                                )
                                            st.session_state[
                                                neona_drafts_key
                                            ] = drafts
                                            persist_workspace_if_changed(
                                                telegram_id,
                                                force=True,
                                            )
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(
                                                "Не удалось сформировать новые "
                                                f"сообщения: {exc}"
                                            )

                                if delete_all:
                                    for draft_id in editable_draft_ids:
                                        drafts.pop(draft_id, None)
                                        drafts.pop(str(draft_id), None)
                                    st.session_state[
                                        neona_drafts_key
                                    ] = drafts
                                    for candidate in candidate_results:
                                        try:
                                            candidate_id = int(
                                                candidate.get("telegram_id")
                                            )
                                        except (TypeError, ValueError):
                                            continue
                                        if candidate_id in editable_draft_ids:
                                            candidate["status"] = (
                                                "Выбран владельцем"
                                            )
                                    st.session_state[
                                        candidates_key
                                    ] = candidate_results
                                    persist_workspace_if_changed(
                                        telegram_id,
                                        force=True,
                                    )
                                    st.rerun()

                            missing_contacts = []
                            for contact in selected_contacts:
                                contact_id = int(contact["telegram_id"])
                                existing_draft = drafts.get(
                                    contact_id,
                                    drafts.get(str(contact_id)),
                                )
                                if not existing_draft:
                                    missing_contacts.append(contact)

                            if missing_contacts and st.button(
                                "✨ Подготовить сообщения всем выбранным",
                                type="primary",
                                key=(
                                    "neona_prepare_all_selected_"
                                    f"{telegram_id}"
                                ),
                            ):
                                with st.spinner(
                                    "Неона готовит отдельное сообщение каждому "
                                    "выбранному человеку..."
                                ):
                                    try:
                                        generated = generate_neona_first_messages(
                                            first_name,
                                            passport["analysis"],
                                            missing_contacts,
                                        )
                                        for contact_id, draft in generated.items():
                                            previous = drafts.get(
                                                int(contact_id),
                                                drafts.get(str(contact_id), {}),
                                            )
                                            if previous.get("sent"):
                                                continue
                                            draft = dict(draft)
                                            draft["revision"] = int(
                                                previous.get("revision", 0) or 0
                                            ) + 1
                                            draft["approved"] = False
                                            draft["validation_errors"] = []
                                            drafts[int(contact_id)] = draft

                                        st.session_state[
                                            neona_drafts_key
                                        ] = drafts

                                        generated_ids = set(generated)
                                        for candidate in candidate_results:
                                            try:
                                                candidate_id = int(
                                                    candidate.get("telegram_id")
                                                )
                                            except (TypeError, ValueError):
                                                continue
                                            if candidate_id in generated_ids:
                                                candidate["status"] = (
                                                    "Сообщение подготовлено"
                                                )
                                        st.session_state[
                                            candidates_key
                                        ] = candidate_results

                                        for contact_id in generated_ids:
                                            if contact_id in owner_contacts:
                                                owner_contacts[contact_id][
                                                    "status"
                                                ] = "Сообщение подготовлено"
                                        st.session_state[
                                            owner_contacts_key
                                        ] = owner_contacts

                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(
                                            "Не удалось подготовить сообщения: "
                                            f"{exc}"
                                        )

                            for number, contact in enumerate(
                                selected_contacts,
                                start=1,
                            ):
                                contact_id = int(contact["telegram_id"])
                                draft = drafts.get(
                                    contact_id,
                                    drafts.get(str(contact_id)),
                                )
                                username = (
                                    f"@{contact['username']}"
                                    if contact.get("username")
                                    else "без username"
                                )
                                source_title = (
                                    contact.get("source_chat_title")
                                    or contact.get("source")
                                    or "Рекомендация Неонии"
                                )

                                with st.container(border=True):
                                    st.markdown(
                                        f"#### {number}. {contact.get('name', 'Кандидат')}"
                                    )
                                    st.caption(
                                        f"{username} · {contact.get('score', 0)}% · "
                                        f"{contact.get('segment', 'Сегмент не указан')} · "
                                        f"источник: {source_title}"
                                    )

                                    with st.expander(
                                        "Контекст для персонализации"
                                    ):
                                        reasons = contact.get("reasons", [])
                                        if reasons:
                                            st.write("**Почему выбран:**")
                                            for reason in reasons:
                                                st.write(f"• {reason}")
                                        st.write(
                                            "**Безопасная тема обращения:**"
                                        )
                                        st.write(
                                            contact.get(
                                                "message_angle",
                                                "Нейтральное знакомство",
                                            )
                                        )
                                        profile_about = str(
                                            contact.get("profile_about") or ""
                                        ).strip()
                                        if profile_about:
                                            st.write("**Bio:**")
                                            st.write(profile_about)

                                    if not draft:
                                        if st.button(
                                            "✍️ Подготовить сообщение",
                                            key=(
                                                "neona_prepare_one_"
                                                f"{telegram_id}_{contact_id}"
                                            ),
                                        ):
                                            with st.spinner(
                                                "Неона готовит персональный текст..."
                                            ):
                                                try:
                                                    message = (
                                                        generate_neona_first_message(
                                                            first_name,
                                                            passport["analysis"],
                                                            contact,
                                                        )
                                                    )
                                                    drafts[contact_id] = {
                                                        "message": message,
                                                        "approved": False,
                                                        "status": (
                                                            "Сообщение подготовлено"
                                                        ),
                                                        "revision": 1,
                                                        "validation_errors": [],
                                                    }
                                                    st.session_state[
                                                        neona_drafts_key
                                                    ] = drafts
                                                    contact["status"] = (
                                                        "Сообщение подготовлено"
                                                    )
                                                    st.session_state[
                                                        candidates_key
                                                    ] = candidate_results
                                                    persist_workspace_if_changed(
                                                        telegram_id,
                                                        force=True,
                                                    )
                                                    st.rerun()
                                                except Exception as exc:
                                                    st.error(
                                                        "Не удалось подготовить "
                                                        f"сообщение: {exc}"
                                                    )
                                    else:
                                        draft_revision = int(
                                            draft.get("revision", 0) or 0
                                        )
                                        message_key = (
                                            "neona_agent_message_text_"
                                            f"{telegram_id}_{contact_id}_"
                                            f"{draft_revision}"
                                        )
                                        edited_message = st.text_area(
                                            "Первое сообщение",
                                            value=str(
                                                draft.get("message") or ""
                                            ),
                                            height=180,
                                            disabled=bool(draft.get("sent")),
                                            key=message_key,
                                        )
                                        current_rule_errors = (
                                            validate_neona_first_message(
                                                edited_message.strip(),
                                                first_name,
                                            )
                                        )
                                        if current_rule_errors and not draft.get(
                                            "sent"
                                        ):
                                            st.error(
                                                "Этот текст создан по старым правилам "
                                                "или нарушает новый регламент. "
                                                "Нажмите «Сформировать заново» либо "
                                                "исправьте текст вручную."
                                            )

                                        action_columns = st.columns(4)
                                        save_message = action_columns[0].button(
                                            "💾 Сохранить",
                                            disabled=bool(draft.get("sent")),
                                            key=(
                                                "neona_save_agent_draft_"
                                                f"{telegram_id}_{contact_id}"
                                            ),
                                        )
                                        approve_message = action_columns[1].button(
                                            "✅ Утвердить",
                                            type="primary",
                                            disabled=bool(draft.get("sent")),
                                            key=(
                                                "neona_approve_agent_draft_"
                                                f"{telegram_id}_{contact_id}"
                                            ),
                                        )
                                        regenerate_message = action_columns[2].button(
                                            "🔄 Заново",
                                            disabled=bool(draft.get("sent")),
                                            key=(
                                                "neona_regenerate_agent_draft_"
                                                f"{telegram_id}_{contact_id}"
                                            ),
                                        )
                                        delete_message = action_columns[3].button(
                                            "🗑️ Удалить",
                                            disabled=bool(draft.get("sent")),
                                            key=(
                                                "neona_delete_agent_draft_"
                                                f"{telegram_id}_{contact_id}"
                                            ),
                                        )

                                        if regenerate_message:
                                            with st.spinner(
                                                "Неона формирует новый текст по "
                                                "действующему регламенту..."
                                            ):
                                                try:
                                                    new_message = (
                                                        generate_neona_first_message(
                                                            first_name,
                                                            passport["analysis"],
                                                            contact,
                                                        )
                                                    )
                                                    draft = {
                                                        "message": new_message,
                                                        "approved": False,
                                                        "status": (
                                                            "Сообщение сформировано заново"
                                                        ),
                                                        "revision": (
                                                            draft_revision + 1
                                                        ),
                                                        "validation_errors": [],
                                                    }
                                                    drafts[contact_id] = draft
                                                    drafts.pop(str(contact_id), None)
                                                    st.session_state[
                                                        neona_drafts_key
                                                    ] = drafts
                                                    contact["status"] = draft[
                                                        "status"
                                                    ]
                                                    st.session_state[
                                                        candidates_key
                                                    ] = candidate_results
                                                    persist_workspace_if_changed(
                                                        telegram_id,
                                                        force=True,
                                                    )
                                                    st.rerun()
                                                except Exception as exc:
                                                    st.error(
                                                        "Не удалось сформировать "
                                                        f"новый текст: {exc}"
                                                    )

                                        if delete_message:
                                            drafts.pop(contact_id, None)
                                            drafts.pop(str(contact_id), None)
                                            st.session_state[
                                                neona_drafts_key
                                            ] = drafts
                                            contact["status"] = (
                                                "Выбран владельцем"
                                            )
                                            st.session_state[
                                                candidates_key
                                            ] = candidate_results
                                            if contact_id in owner_contacts:
                                                owner_contacts[contact_id][
                                                    "status"
                                                ] = "Выбран владельцем"
                                                st.session_state[
                                                    owner_contacts_key
                                                ] = owner_contacts
                                            persist_workspace_if_changed(
                                                telegram_id,
                                                force=True,
                                            )
                                            st.rerun()

                                        if save_message or approve_message:
                                            # Владелец вручную отредактировал текст: сохраняем и утверждаем
                                            # ровно то, что видно в поле, без скрытых дописок.
                                            normalized_message = edited_message.strip()
                                            if not normalized_message:
                                                st.warning(
                                                    "Текст сообщения пуст."
                                                )
                                            else:
                                                validation_errors = (
                                                    validate_neona_first_message(
                                                        normalized_message,
                                                        first_name,
                                                    )
                                                )
                                                draft["message"] = (
                                                    normalized_message
                                                )
                                                draft["approved"] = bool(
                                                    approve_message
                                                    and not validation_errors
                                                )
                                                draft["validation_errors"] = (
                                                    validation_errors
                                                )
                                                if approve_message and validation_errors:
                                                    draft["status"] = (
                                                        "Нужно исправить перед утверждением"
                                                    )
                                                    st.error(
                                                        "Утвердить этот текст нельзя: "
                                                        + "; ".join(validation_errors)
                                                    )
                                                else:
                                                    draft["status"] = (
                                                        "Первое сообщение утверждено"
                                                        if approve_message
                                                        else "Сообщение отредактировано"
                                                    )
                                                drafts[contact_id] = draft
                                                drafts.pop(str(contact_id), None)
                                                st.session_state[
                                                    neona_drafts_key
                                                ] = drafts

                                                for candidate in candidate_results:
                                                    try:
                                                        candidate_id = int(
                                                            candidate.get(
                                                                "telegram_id"
                                                            )
                                                        )
                                                    except (
                                                        TypeError,
                                                        ValueError,
                                                    ):
                                                        continue
                                                    if candidate_id == contact_id:
                                                        candidate["status"] = (
                                                            draft["status"]
                                                        )
                                                st.session_state[
                                                    candidates_key
                                                ] = candidate_results

                                                if contact_id in owner_contacts:
                                                    owner_contacts[contact_id][
                                                        "status"
                                                    ] = draft["status"]
                                                    st.session_state[
                                                        owner_contacts_key
                                                    ] = owner_contacts

                                                persist_workspace_if_changed(
                                                    telegram_id,
                                                    force=True,
                                                )
                                                if not (
                                                    approve_message
                                                    and validation_errors
                                                ):
                                                    st.rerun()

                                        # Ошибка регламента всегда важнее старого
                                        # сохранённого статуса «утверждено».
                                        if current_rule_errors:
                                            if draft.get("approved"):
                                                draft["approved"] = False
                                                draft["status"] = (
                                                    "Нужно исправить перед утверждением"
                                                )
                                                draft["validation_errors"] = (
                                                    current_rule_errors
                                                )
                                                drafts[contact_id] = draft
                                                drafts.pop(str(contact_id), None)
                                                st.session_state[
                                                    neona_drafts_key
                                                ] = drafts
                                                persist_workspace_if_changed(
                                                    telegram_id,
                                                    force=True,
                                                )
                                            st.warning(
                                                "Черновик пока не утверждён. "
                                                "Исправьте текст или сформируйте его заново."
                                            )
                                            with st.expander(
                                                "Почему текст пока не утверждается"
                                            ):
                                                for rule_error in current_rule_errors:
                                                    st.write(f"• {rule_error}")
                                        elif draft.get("approved"):
                                            st.success(
                                                "✅ Сообщение утверждено владельцем"
                                            )
                                        else:
                                            st.warning(
                                                "Черновик ещё не утверждён."
                                            )

                                        if draft.get("sent"):
                                            sent_at = str(
                                                draft.get("sent_at", "")
                                            )
                                            st.success(
                                                "📨 Первое сообщение отправлено"
                                                + (
                                                    f" · {sent_at[11:16]}"
                                                    if len(sent_at) >= 16
                                                    else ""
                                                )
                                            )
                                            if (
                                                contact.get("source")
                                                == "Знакомый — выбран директором"
                                            ):
                                                st.info(
                                                    "Первое сообщение уже является частью "
                                                    "истории и не редактируется. "
                                                    "Повторно формировать его не нужно: "
                                                    "после нового входящего сообщения "
                                                    "Неона продолжает диалог сама."
                                                )
                                        elif (
                                            draft.get("approved")
                                            and not current_rule_errors
                                        ):
                                            sent_today = (
                                                count_first_messages_sent_today(
                                                    sent_log
                                                )
                                            )
                                            already_sent_to_contact = any(
                                                int(event.get("telegram_id", 0))
                                                == contact_id
                                                for event in sent_log
                                                if isinstance(event, dict)
                                            )

                                            if already_sent_to_contact:
                                                st.info(
                                                    "Первое сообщение этому человеку "
                                                    "уже отправлялось. Повторная отправка "
                                                    "заблокирована."
                                                )
                                            elif sent_today >= 5:
                                                st.warning(
                                                    "Дневной лимит достигнут: сегодня "
                                                    "уже отправлено 5 первых сообщений."
                                                )
                                            else:
                                                st.caption(
                                                    "Сообщение будет отправлено с "
                                                    "подключённого Telegram-аккаунта "
                                                    f"{first_name}. После отправки отменить "
                                                    "его нельзя."
                                                )
                                                if st.button(
                                                    "📨 Отправить первое сообщение",
                                                    type="primary",
                                                    key=(
                                                        "neona_agent_send_first_message_"
                                                        f"{telegram_id}_{contact_id}"
                                                    ),
                                                ):
                                                    try:
                                                        with st.spinner(
                                                            "Отправляем сообщение в Telegram..."
                                                        ):
                                                            send_result = (
                                                                run_telegram_async(
                                                                    send_telegram_first_message(
                                                                        telegram_id,
                                                                        contact_id,
                                                                        str(
                                                                            contact.get(
                                                                                "username",
                                                                                "",
                                                                            )
                                                                            or ""
                                                                        ),
                                                                        draft["message"],
                                                                        contact.get(
                                                                            "source_chat_id"
                                                                        ),
                                                                    )
                                                                )
                                                            )

                                                        draft["sent"] = True
                                                        draft["approved"] = True
                                                        draft["sent_at"] = (
                                                            send_result["sent_at"]
                                                        )
                                                        draft[
                                                            "telegram_message_id"
                                                        ] = send_result["message_id"]
                                                        draft["status"] = (
                                                            "Первое сообщение отправлено"
                                                        )
                                                        drafts[contact_id] = draft
                                                        drafts.pop(
                                                            str(contact_id),
                                                            None,
                                                        )
                                                        st.session_state[
                                                            neona_drafts_key
                                                        ] = drafts

                                                        sent_log.append(
                                                            {
                                                                "telegram_id": contact_id,
                                                                "recipient_name": contact.get(
                                                                    "name",
                                                                    "Кандидат",
                                                                ),
                                                                "sent_at": send_result[
                                                                    "sent_at"
                                                                ],
                                                                "message_id": send_result[
                                                                    "message_id"
                                                                ],
                                                                "kind": "first_message",
                                                            }
                                                        )
                                                        st.session_state[
                                                            sent_log_key
                                                        ] = sent_log

                                                        try:
                                                            initialize_dialog_after_first_message(
                                                                telegram_id,
                                                                contact_id,
                                                                baseline_incoming_id=int(
                                                                    send_result.get(
                                                                        "baseline_incoming_message_id",
                                                                        0,
                                                                    )
                                                                ),
                                                                sent_at=send_result[
                                                                    "sent_at"
                                                                ],
                                                            )
                                                        except Exception as dialog_exc:
                                                            draft[
                                                                "dialog_activation_error"
                                                            ] = str(dialog_exc)
                                                            drafts[contact_id] = draft
                                                            st.session_state[
                                                                neona_drafts_key
                                                            ] = drafts

                                                        for candidate in candidate_results:
                                                            try:
                                                                candidate_id = int(
                                                                    candidate.get(
                                                                        "telegram_id"
                                                                    )
                                                                )
                                                            except (
                                                                TypeError,
                                                                ValueError,
                                                            ):
                                                                continue
                                                            if candidate_id == contact_id:
                                                                candidate["status"] = (
                                                                    "Первое сообщение отправлено"
                                                                )
                                                        st.session_state[
                                                            candidates_key
                                                        ] = candidate_results

                                                        if contact_id in owner_contacts:
                                                            owner_contacts[contact_id][
                                                                "status"
                                                            ] = (
                                                                "Первое сообщение отправлено"
                                                            )
                                                            st.session_state[
                                                                owner_contacts_key
                                                            ] = owner_contacts

                                                        persist_workspace_if_changed(
                                                            telegram_id,
                                                            force=True,
                                                        )
                                                        st.rerun()

                                                    except Exception as exc:
                                                        st.error(
                                                            friendly_telegram_send_error(
                                                                exc
                                                            )
                                                        )

                            latest_drafts = st.session_state.get(
                                neona_drafts_key,
                                {},
                            )
                            all_approved = all(
                                bool(
                                    latest_drafts.get(
                                        int(contact["telegram_id"]),
                                        latest_drafts.get(
                                            str(contact["telegram_id"]),
                                            {},
                                        ),
                                    ).get("approved")
                                )
                                for contact in selected_contacts
                            )
                            if all_approved:
                                st.success(
                                    "Все выбранные сообщения утверждены. "
                                    "Теперь их можно отправить по одному с "
                                    "подключённого Telegram-аккаунта владельца."
                                )
                            else:
                                st.caption(
                                    "Кнопка отправки появляется только после "
                                    "утверждения конкретного сообщения владельцем."
                                )

                else:
                    st.caption(
                        "Подключение этого агента будет следующим этапом."
                    )

        elif main_section == "👥 Команда":
            render_team_center(
                telegram_id,
                member_code,
                first_name,
                partner_link,
            )

        elif main_section == "👤 Профиль":
            inviter_text = referral_code if referral_code else "не указан"

            st.markdown("### 👤 Профиль")

            with st.container(border=True):
                st.markdown(f"**Имя:** {first_name}")
                st.markdown(f"**Партнёрский код:** `{member_code}`")
                st.markdown(f"**Пригласитель:** `{inviter_text}`")
                st.markdown("**Статус:** 🟢 Активен")

            st.markdown("**Персональная партнёрская ссылка:**")
            st.code(partner_link, language=None)
    else:
        st.error(
            "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз."
        )
else:
    bot_username = st.secrets["TELEGRAM_BOT_USERNAME"]

    auth_url = "https://agency-w.streamlit.app/"
    if referral_code:
        auth_url += "?" + urlencode({"ref": referral_code})

    st.html(
        f"""
        <div style="display:flex; justify-content:center; margin:0.5rem 0 1rem;">
            <script async
                src="https://telegram.org/js/telegram-widget.js?22"
                data-telegram-login="{html.escape(bot_username, quote=True)}"
                data-size="large"
                data-radius="10"
                data-lang="ru"
                data-auth-url="{html.escape(auth_url, quote=True)}">
            </script>
        </div>
        """,
        unsafe_allow_javascript=True,
    )

    st.caption("Подтвердите вход в безопасном окне Telegram.")

if not st.query_params.get("hash"):
    st.divider()

    st.markdown(
        """
        **После регистрации вы получите:**

        🤖 доступ к ИИ-помощникам;  
        🔗 собственную партнёрскую ссылку;  
        🗂️ личный рабочий кабинет;  
        📊 инструменты поиска, общения и сопровождения партнёров.
        """
    )
