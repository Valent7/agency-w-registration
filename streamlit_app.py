import streamlit as st
from agency_values import render_agency_development
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from neonia_contacts import render_neonia_contacts
from neonia_chats import render_neonia_chats
from neona_reglament import (
    NEONA_FIRST_MESSAGE_OPT_OUT,
    NEONA_FORBIDDEN_AI_LABELS,
    NEONA_FORBIDDEN_CLAIMS,
    build_neona_first_message_system_prompt,
    build_neona_first_messages_system_prompt,
    choose_neona_magnet,
    NEONA_MAGNET_NAMES,
    neona_identity,
    neona_reglament_markdown,
)
from agency_calendar import (
    render_agency_calendar,
    render_today_meetings_compact,
)
from team_center import render_team_center
from personal_tasks import render_personal_tasks
from neola_partner_center import (
    activation_is_confirmed,
    activation_label,
    ensure_partner_activation,
    render_neola_agent,
    render_neola_quick_assistant,
    render_partner_center,
)
from neola_realtime_voice import render_neola_realtime_voice
import neola_role_policy  # фиксирует границы роли Неолы
from neonia_intelligence_v2 import (
    analyze_candidate_project_risk,
    analyze_owner_project_target_profile,
    render_project_risk,
    render_target_profile,
    target_profile_for_analysis,
)

from neona_dialog_policy import (
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



def split_contacts_by_analysis_value(contact_contexts):
    """Не отправляет в OpenAI контакты, где фактически нечего анализировать."""

    informative = []
    empty = []

    for source in contact_contexts:
        # Собираем только содержательные поля, которые реально могут помочь анализу.
        chunks = []

        for key in (
            "about",
            "bio",
            "description",
            "context",
            "dialogue",
            "messages",
            "recent_messages",
            "public_info",
        ):
            value = source.get(key)
            if isinstance(value, list):
                value = " ".join(
                    str(item) for item in value if str(item).strip()
                )
            if value is not None and str(value).strip():
                chunks.append(str(value).strip())

        # Имя, username и телефон сами по себе не считаем достаточными
        # основаниями для платного ИИ-анализа.
        useful_text = " ".join(chunks).strip()

        if len(useful_text) >= 40:
            informative.append(source)
        else:
            empty.append(source)

    return informative, empty


def build_no_data_candidate(source):
    """Бесплатная локальная характеристика пустого контакта."""

    return {
        "telegram_id": int(source["telegram_id"]),
        "name": source.get("name") or "Без имени",
        "first_name": source.get("first_name") or "",
        "username": source.get("username") or "",
        "potential_interest": "неясно",
        "actuality": "неясно",
        "warmth": "неясно",
        "obstacles": ["недостаточно данных"],
        "short_portrait": (
            "Данных о человеке пока недостаточно. "
            "Неония не видит признаков, по которым можно уверенно "
            "оценить его интерес к вашему предложению."
        ),
        "owner_hint": (
            "Если вы знаете этого человека лично, решение о разговоре "
            "лучше принять на основании вашего знакомства."
        ),
        "message_angle": (
            "Если владелец решит написать — начать с обычного "
            "человеческого обращения без предположений об интересах."
        ),
        "project_name": "",
        "project_url": "",
        "project_evidence": "",
        # Совместимость с уже работающей Неоной.
        "segment": "Недостаточно данных",
        "score": 0,
        "confidence": "данных недостаточно",
        "reasons": ["Недостаточно содержательных данных для ИИ-анализа"],
        "recommendation": "Решает владелец",
        "status": "Проанализирован без OpenAI",
        "analysis_cost_mode": "local_no_ai",
        "analyzed_at": datetime.now(
            ZoneInfo("Europe/Berlin")
        ).isoformat(),
    }


def analyze_contacts_for_target_audience(
    passport_analysis,
    contact_contexts,
):
    """Даёт краткую характеристику очередной партии контактов.

    ЦА используется как ориентир для оценки потенциального интереса,
    но Неония никого не исключает и не принимает решение за владельца.
    """

    system_prompt = """
Ты — Неония, аналитик людей Агентства W.

У тебя есть:
1) сохранённый портрет целевой аудитории проекта владельца;
2) данные очередных контактов Telegram.

Твоя задача НЕ состоит в том, чтобы решить, «подходит» человек или «не подходит».
ЦА — только один из ориентиров. Любой человек может оказаться хорошим
собеседником, поэтому окончательное решение всегда принимает владелец.

Для КАЖДОГО переданного контакта составь маленькую практичную характеристику
по четырём отдельным показателям:

1. potential_interest — потенциальный интерес к предложению:
   строго «высокий», «средний» или «низкий».
   Оценивай по совпадению ситуации, интересов, опыта и задач человека
   с тем, что реально может дать проект.

2. actuality — актуальность контакта сейчас:
   строго «активен сейчас», «неясно» или «давно неактивен».
   Свежая активность важнее старого опыта. Если данных о свежести нет —
   ставь «неясно», а не придумывай.

3. warmth — теплота контакта:
   строго «знакомый», «поверхностно знакомый» или «холодный».
   Опирайся только на имеющиеся признаки переписки/контекста.
   Если уверенно определить нельзя — «холодный».

4. obstacles — список из 0–3 реальных препятствий/рисков:
   например «активно развивает другой проект», «данные устарели»,
   «мало данных», «нет свежей активности».
   Не выдумывай препятствия.

Также верни:
- telegram_id;
- short_portrait — 1–2 простых предложения: кто этот человек по имеющимся данным;
- owner_hint — 1 короткая рекомендация владельцу:
  «стоит попробовать поговорить», «можно поговорить, но без высокого приоритета»,
  «лучше пока отложить» или другой спокойный вариант без категоричного запрета;
- message_angle — с какой человеческой стороны Неоне разумнее начать разговор;
- project_name — проект/компания человека, только если это явно следует из данных;
- project_url — только реально имеющаяся ссылка;
- project_evidence — коротко, откуда видна связь с проектом.

ВАЖНО:
- старый интерес к криптовалюте, ИИ, MLM или Telegram сам по себе
  НЕ делает человека приоритетным;
- человек, активно продвигающий другой проект, не запрещён для разговора,
  но это обязательно отражается в obstacles и owner_hint;
- давняя информация не равна актуальной;
- не оценивай по имени, полу, возрасту, национальности или фотографии;
- если сведений мало — прямо говори об этом;
- никого не исключай автоматически.

Верни ТОЛЬКО JSON-массив без Markdown и пояснений.
"""

    request = (
        "ПОРТРЕТ ЦЕЛЕВОЙ АУДИТОРИИ — ТОЛЬКО ОРИЕНТИР:\n"
        f"{passport_analysis}\n\n"
        "ОЧЕРЕДНЫЕ КОНТАКТЫ ДЛЯ ХАРАКТЕРИСТИКИ:\n"
        f"{json.dumps(contact_contexts, ensure_ascii=False)}"
    )

    answer = ask_openai(system_prompt, request)

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    raw_results = extract_json_array(answer)
    source_by_id = {
        int(item["telegram_id"]): item
        for item in contact_contexts
    }
    normalized = []

    allowed_interest = {"высокий", "средний", "низкий"}
    allowed_actuality = {"активен сейчас", "неясно", "давно неактивен"}
    allowed_warmth = {"знакомый", "поверхностно знакомый", "холодный"}

    for item in raw_results:
        try:
            contact_id = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue

        source = source_by_id.get(contact_id)
        if not source:
            continue

        potential_interest = str(
            item.get("potential_interest") or "средний"
        ).strip().lower()
        if potential_interest not in allowed_interest:
            potential_interest = "средний"

        actuality = str(
            item.get("actuality") or "неясно"
        ).strip().lower()
        if actuality not in allowed_actuality:
            actuality = "неясно"

        warmth = str(
            item.get("warmth") or "холодный"
        ).strip().lower()
        if warmth not in allowed_warmth:
            warmth = "холодный"

        obstacles = item.get("obstacles") or []
        if isinstance(obstacles, str):
            obstacles = [obstacles]
        if not isinstance(obstacles, list):
            obstacles = []
        obstacles = [
            str(value).strip()[:250]
            for value in obstacles[:3]
            if str(value).strip()
        ]

        # Старые поля оставляем только для совместимости с Неоной
        # и сохранённым рабочим пространством. Пользователю проценты не показываются.
        compatibility_score = {
            "высокий": 75,
            "средний": 50,
            "низкий": 25,
        }[potential_interest]

        normalized.append(
            {
                "telegram_id": contact_id,
                "name": source.get("name") or "Без имени",
                "first_name": source.get("first_name") or "",
                "username": source.get("username") or "",
                "potential_interest": potential_interest,
                "actuality": actuality,
                "warmth": warmth,
                "obstacles": obstacles,
                "short_portrait": str(
                    item.get("short_portrait") or "Данных пока немного."
                ).strip()[:700],
                "owner_hint": str(
                    item.get("owner_hint")
                    or "Решение о разговоре принимает владелец."
                ).strip()[:500],
                "message_angle": str(
                    item.get("message_angle")
                    or "Спокойное человеческое знакомство"
                ).strip()[:500],
                "project_name": str(item.get("project_name") or "")[:180],
                "project_url": str(item.get("project_url") or "")[:900],
                "project_evidence": str(
                    item.get("project_evidence") or ""
                )[:500],
                # compatibility fields for existing Neona code
                "segment": potential_interest.capitalize() + " потенциальный интерес",
                "score": compatibility_score,
                "confidence": "аналитическая оценка",
                "reasons": [
                    str(item.get("short_portrait") or "Данных пока немного.")[:300]
                ],
                "recommendation": "Решает владелец",
                "status": "Проанализирован",
                "analysis_cost_mode": "openai",
                "analyzed_at": datetime.now(
                    ZoneInfo("Europe/Berlin")
                ).isoformat(),
            }
        )

    # Если модель случайно пропустила контакт, мы всё равно показываем его владельцу.
    returned_ids = {int(item["telegram_id"]) for item in normalized}
    for source in contact_contexts:
        contact_id = int(source["telegram_id"])
        if contact_id in returned_ids:
            continue
        normalized.append(
            {
                "telegram_id": contact_id,
                "name": source.get("name") or "Без имени",
                "first_name": source.get("first_name") or "",
                "username": source.get("username") or "",
                "potential_interest": "средний",
                "actuality": "неясно",
                "warmth": "холодный",
                "obstacles": ["мало данных"],
                "short_portrait": "Недостаточно данных для уверенной характеристики.",
                "owner_hint": "Решение о разговоре принимает владелец.",
                "message_angle": "Спокойное человеческое знакомство",
                "project_name": "",
                "project_url": "",
                "project_evidence": "",
                "segment": "Средний потенциальный интерес",
                "score": 50,
                "confidence": "низкая",
                "reasons": ["Недостаточно данных"],
                "recommendation": "Решает владелец",
                "status": "Проанализирован",
                "analyzed_at": datetime.now(
                    ZoneInfo("Europe/Berlin")
                ).isoformat(),
            }
        )

    # Сохраняем исходный порядок десятки, а не строим псевдо-рейтинг.
    order = {
        int(item["telegram_id"]): index
        for index, item in enumerate(contact_contexts)
    }
    normalized.sort(key=lambda item: order.get(int(item["telegram_id"]), 9999))
    return normalized

def merge_candidate_results(existing, new_results):
    """Сохраняет результаты анализа без повторного анализа и без псевдо-рейтинга."""

    merged = []
    positions = {}

    for item in existing:
        try:
            contact_id = int(item["telegram_id"])
        except (KeyError, TypeError, ValueError):
            continue
        positions[contact_id] = len(merged)
        merged.append(item)

    for item in new_results:
        contact_id = int(item["telegram_id"])
        if contact_id in positions:
            previous = merged[positions[contact_id]]
            item["status"] = previous.get("status", item.get("status", "Проанализирован"))
            merged[positions[contact_id]] = item
        else:
            positions[contact_id] = len(merged)
            merged.append(item)

    return merged


NEONA_FIRST_MESSAGE_FORBIDDEN = NEONA_FORBIDDEN_CLAIMS


def candidate_first_name(contact):
    """Возвращает безопасное имя: никогда не подставляет @username."""

    raw_first_name = str(contact.get("first_name") or "").strip()
    if raw_first_name:
        # Telegram first_name sometimes contains initials/extra words (e.g. "Aigul SK").
        # For a human greeting use only the first clean name token.
        first_name = re.split(r"[\s|,/]+", raw_first_name, maxsplit=1)[0].strip(
            " ,.!?;:()[]{}\"'"
        )
        if (
            first_name
            and len(first_name) <= 40
            and not first_name.startswith("@")
            and not any(character.isdigit() for character in first_name)
        ):
            return first_name

    full_name = str(contact.get("name") or "").strip()
    if full_name:
        first_word = full_name.split()[0].strip(" ,.!?;:()[]{}\"'")
        if (
            first_word
            and len(first_word) <= 40
            and not first_word.startswith("@")
            and not any(character.isdigit() for character in first_word)
        ):
            return first_word

    # Если имя очевидно читается в username, используем обычное имя.
    # В остальных случаях лучше нейтральное «Здравствуйте!», чем обращение по нику.
    username = str(contact.get("username") or "").strip().lstrip("@").lower()
    possible_handles = [username]
    possible_handles.extend(
        handle.lower()
        for handle in re.findall(r"@([A-Za-zА-Яа-яЁё0-9_.-]+)", full_name)
    )
    obvious_names = {
        "larisa": "Лариса",
        "larissa": "Лариса",
        "natasha": "Наталья",
        "natalia": "Наталья",
        "natalya": "Наталья",
        "nadia": "Надежда",
        "nadezhda": "Надежда",
        "sveta": "Светлана",
        "svetlana": "Светлана",
        "elena": "Елена",
        "lena": "Елена",
        "irina": "Ирина",
        "marina": "Марина",
        "olga": "Ольга",
        "anna": "Анна",
        "anya": "Анна",
        "tatiana": "Татьяна",
        "tatyana": "Татьяна",
        "valentina": "Валентина",
        "lyubov": "Любовь",
        "lubov": "Любовь",
        "alexander": "Александр",
        "aleksandr": "Александр",
        "sergey": "Сергей",
        "sergei": "Сергей",
        "yuri": "Юрий",
        "yuriy": "Юрий",
        "ilya": "Илья",
        "ilnur": "Ильнур",
        "dmitry": "Дмитрий",
        "dmitriy": "Дмитрий",
    }
    for handle in possible_handles:
        handle_compact = re.sub(r"[^a-zа-яё]", "", handle)
        for latin_name, russian_name in obvious_names.items():
            if handle_compact.startswith(latin_name):
                return russian_name

    return ""


def normalize_neona_first_greeting(message, contact):
    """Всегда начинает первое сообщение с человеческого приветствия по имени."""

    message = str(message or "").strip()
    first_name = candidate_first_name(contact)
    username = str(contact.get("username") or "").strip().lstrip("@")

    if not message:
        return message

    safe_greeting = (
        f"{first_name}, здравствуйте!"
        if first_name
        else "Здравствуйте!"
    )

    # Убираем любое старое/модельное обращение, чтобы не получить два приветствия.
    if first_name:
        escaped_name = re.escape(first_name)
        message = re.sub(
            rf"^{escaped_name}\s*,?\s*(?:здравствуйте|привет)\s*[!,.]?\s*",
            "",
            message,
            count=1,
            flags=re.IGNORECASE,
        )
        # Старые версии могли начинать сразу с «Имя, если...». Имя уже будет в приветствии.
        message = re.sub(
            rf"^{escaped_name}\s*,\s*",
            "",
            message,
            count=1,
            flags=re.IGNORECASE,
        )

    if username:
        username_re = re.escape(username)
        message = re.sub(
            rf"^(?:привет|здравствуйте)?\s*,?\s*@{username_re}\s*[!,.]?\s*",
            "",
            message,
            count=1,
            flags=re.IGNORECASE,
        )
        message = re.sub(
            rf"^@{username_re}\s*[,!.-]?\s*(?:привет|здравствуйте)?\s*[!,.]?\s*",
            "",
            message,
            count=1,
            flags=re.IGNORECASE,
        )

    # Если модель начала с нейтрального приветствия, заменяем его единым стандартом.
    message = re.sub(
        r"^(?:здравствуйте|привет)\s*[!,.]?\s*",
        "",
        message,
        count=1,
        flags=re.IGNORECASE,
    )

    message = re.sub(r"\s{2,}", " ", message).strip()
    return f"{safe_greeting} {message}".strip()


def build_neona_safe_first_message(owner_name, contact):
    """Запасное человеческое первое сообщение для холодного или тёплого контакта."""

    first_name = candidate_first_name(contact)
    magnet = choose_neona_magnet(contact)
    identity = neona_identity(owner_name)
    is_known_contact = (
        contact.get("source") == "Знакомый — выбран директором"
    )

    greeting = (
        f"{first_name}, здравствуйте!"
        if first_name
        else "Здравствуйте!"
    )

    benefits = {
        "Вернуть человеку время": (
            "Если у вас много вещей, которые приходится делать самому каждый день, "
            "часть повторяющейся работы можно передать ИИ-помощникам и вернуть себе время."
        ),
        "Своя ИИ-команда": (
            "Мы создаём Агентство, где рядом с человеком работает несколько ИИ-помощников: "
            "у каждого своя задача, а решения всё равно остаются за человеком."
        ),
        "Усилить существующий проект": (
            "Если у вас уже есть своё дело, менять его ради другого не нужно. Гораздо "
            "интереснее посмотреть, какую часть рутины можно снять и усилить то, что вы уже делаете."
        ),
        "Найти подходящих людей": (
            "Не обязательно писать всем подряд. Сначала можно понять, с кем разговор "
            "действительно имеет смысл, и только потом начинать общение."
        ),
        "Начать разговор без навязывания": (
            "Мы не начинаем с рекламного предложения. Смысл в том, чтобы сначала понять, "
            "почему тема может быть полезна именно этому человеку, и начать нормальный разговор."
        ),
        "Сопровождение новичка": (
            "Когда приходит новый человек, ему нужно показать первые шаги. Часть такого "
            "сопровождения может взять на себя отдельный ИИ-помощник."
        ),
        "Агентство понимает конкретный проект": (
            "Сначала система разбирается в конкретном проекте и его аудитории, и только "
            "после этого помощники работают в этом контексте, а не одинаково для всех."
        ),
        "Человек остаётся главным": (
            "ИИ нужен не для того, чтобы заменить человека. Он снимает часть подготовки и "
            "рутины, а отношения, выбор и важные решения остаются за человеком."
        ),
    }

    benefit = benefits.get(
        magnet,
        benefits["Своя ИИ-команда"],
    )

    if is_known_contact:
        bridge = (
            f"Вы уже знакомы с моим руководителем, поэтому напишу без длинного предисловия. "
        )
    else:
        bridge = ""

    question = "Вам было бы интересно посмотреть, как это может пригодиться именно вам?"
    opt_out = (
        "Если тема вам сейчас неинтересна, просто скажите — "
        "я больше не буду вас беспокоить."
    )

    return re.sub(
        r"\s+",
        " ",
        f"{greeting} {identity} {bridge}{benefit} {question} {opt_out}",
    ).strip()


def ensure_neona_first_message_opt_out(message):
    """Добавляет согласованный уважительный выход, если модель его пропустила."""

    message = str(message or "").strip()
    lowered = message.lower()
    if (
        "больше не буду вас беспокоить" in lowered
        or "больше не буду вам писать" in lowered
    ):
        return message

    opt_out = (
        "Если тема вам сейчас неинтересна, просто скажите — "
        "я больше не буду вас беспокоить."
    )
    return f"{message} {opt_out}".strip()


def validate_neona_first_message(message, owner_name):
    """Проверяет обязательные правила человеческого первого сообщения."""

    message = str(message or "").strip()
    if not message:
        return ["сообщение пустое"]

    errors = []
    lowered = message.lower()
    identity = neona_identity(owner_name)

    # После приветствия Неона должна сразу представиться, а не вставлять
    # внутреннее название магнита, сегмента или рекламный заголовок.
    greeting_end = message.find("!")
    if greeting_end == -1:
        errors.append("нет человеческого приветствия")
    else:
        after_greeting = message[greeting_end + 1 :].lstrip()
        if not after_greeting.lower().startswith("меня зовут неона"):
            errors.append("после приветствия Неона должна сразу представиться")

    if identity.lower() not in lowered[:350]:
        errors.append("нет точного представления Неоны и владельца")

    if message.count("?") != 1:
        errors.append("в первом сообщении должен быть один простой вопрос")

    if not message.endswith(NEONA_FIRST_MESSAGE_OPT_OUT):
        errors.append("уважительный выход должен стоять в самом конце")

    awkward_old_phrases = (
        "понятный путь входа",
        "путь входа",
        "понятный старт для вашей сети",
        "своя ии-команда рядом даёт",
        "своя ии-команда рядом дает",
    )
    for phrase in awkward_old_phrases:
        if phrase in lowered:
            errors.append("осталась старая или неестественная формулировка")
            break

    return errors



def finalize_neona_first_message(message, owner_name, contact):
    """Нормализует текст и заменяет небезопасный вариант шаблоном."""

    message = normalize_neona_first_greeting(message, contact)
    message = ensure_neona_identity(message, owner_name)
    message = ensure_neona_first_message_opt_out(message)
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
            "familiarity_note": item.get("familiarity_note", ""),
            "owner_draft": item.get("owner_draft", ""),
            "must_mention": item.get("must_mention", ""),
            "avoid": item.get("avoid", ""),
            "known_contact": (
                item.get("source") == "Знакомый — выбран директором"
            ),
            "selected_magnet": choose_neona_magnet(item),
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
            "magnet": (
                str(item.get("magnet") or "").strip()
                or choose_neona_magnet(contact)
            ),
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
            "magnet": choose_neona_magnet(contact),
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
    """Гарантирует одно представление Неоны как секретаря-референта."""

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
        "selected_magnet": choose_neona_magnet(contact),
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
        
        telegram_connected = render_telegram_connection(telegram_id)
        st.session_state["neonia_telegram_connected"] = telegram_connected

        if not telegram_connected:
            st.stop()

        # ---------------------------------------------------------
        # ШЛЮЗ АКТИВАЦИИ НОВОГО ПАРТНЁРА
        # До подтверждения 5 лож Neonexa рабочий кабинет не открывается.
        # При этом партнёр обязательно должен иметь доступ к экрану,
        # где он сам загружает скриншот подтверждения.
        # ---------------------------------------------------------
        try:
            entry_activation = ensure_partner_activation(int(telegram_id))
        except Exception as exc:
            st.error(
                "Не удалось проверить активацию партнёра. "
                "Попробуйте обновить страницу."
            )
            st.caption(f"Техническая причина: {exc}")
            st.stop()

        if not activation_is_confirmed(entry_activation):
            st.markdown("## 🔐 Активация доступа к Агентству W")
            st.info(
                "Для начала работы подтвердите приобретение/активацию "
                "не менее 5 лож в Neonexa."
            )
            st.caption(
                "До подтверждения скриншота рабочие разделы Агентства W "
                "и Неола недоступны."
            )

            # render_neola_agent при неподтверждённой активации
            # показывает только безопасный экран загрузки скриншота.
            render_neola_agent(
                int(telegram_id),
                first_name,
                "🔐 Активация доступа",
                ask_openai,
            )
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
            ["☀️ День", "📅 Календарь", "🤖 Агенты", "👥 Команда", "🗺️ Развитие", "👤 Профиль"],
            default="☀️ День",
            required=True,
            label_visibility="collapsed",
            width="stretch",
            key="main_section",
        )

        # Постоянный быстрый вызов Неолы. Она получает контекст текущего экрана
        # и учитывает вложенную навигацию («матрёшки») Агентства W.
        neola_ui_context_parts = [str(main_section)]
        current_agent_context = st.session_state.get("selected_agent")
        current_neonia_mode = st.session_state.get("neonia_mode")
        if current_agent_context:
            neola_ui_context_parts.append(f"агент: {current_agent_context}")
        if current_neonia_mode:
            neola_ui_context_parts.append(f"режим Неонии: {current_neonia_mode}")
        neola_ui_context = " → ".join(neola_ui_context_parts)

        # Живой голосовой вызов Неолы.
        # Открывается только по желанию партнёра, поэтому Realtime-сессия
        # не создаётся при каждом обычном переходе по кабинету.
        try:
            neola_activation = ensure_partner_activation(int(telegram_id))
        except Exception:
            neola_activation = None

        if neola_activation and activation_is_confirmed(neola_activation):
            if "neola_live_open" not in st.session_state:
                st.session_state["neola_live_open"] = False

            neola_button_label = (
                "✕ Закрыть Неолу"
                if st.session_state["neola_live_open"]
                else "🎙 Неола рядом"
            )
            if st.button(
                neola_button_label,
                key="neola_live_toggle",
                type="primary",
            ):
                st.session_state["neola_live_open"] = not st.session_state[
                    "neola_live_open"
                ]
                st.rerun()

            if st.session_state["neola_live_open"]:
                neola_step = int(
                    (neola_activation or {}).get("onboarding_step") or 0
                )
                with st.container(border=True):
                    render_neola_realtime_voice(
                        int(telegram_id),
                        first_name,
                        neola_ui_context,
                        neola_step,
                    )
        else:
            st.caption(
                "🔒 Неола включится после подтверждения активации партнёра."
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
            candidate_results = st.session_state.get(
                candidates_key,
                [],
            )
            recommended_candidates = [
                candidate
                for candidate in candidate_results
                if candidate.get("recommendation") == "Передать Неоне"
            ]
            recommended_candidates.sort(
                key=lambda item: -int(item.get("score", 0))
            )
            top_candidates = recommended_candidates[:10]

            st.info(
                f"🎯 Неония: {len(top_candidates)} кандидатов на сегодня. "
                "Работа с кандидатами — в разделе «🤖 Агенты → Неония»."
            )

            with st.container(border=True):
                st.markdown("**📅 Встречи**")
                render_today_meetings_compact(int(telegram_id))

            with st.container(border=True):
                render_personal_tasks(int(telegram_id))

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

            # Архитектура «матрёшки»: специализированные агенты находятся
            # внутри Стагирита как главного координатора.
            with st.container(border=True):
                st.markdown("#### 🧭 Стагирит")
                st.caption(
                    "Главный координатор. Внутри него находятся специализированные "
                    "агенты Агентства W."
                )
                selected_agent = st.selectbox(
                    "Откройте нужного агента внутри Стагирита",
                    ["Стагирит", "Неония", "Неона", "Неола"],
                    key="selected_agent",
                )

            agent_descriptions = {
                "Стагирит": (
                    "Главный координатор и заместитель директора. "
                    "Распределяет задачи между агентами и контролирует результат."
                ),
                "Неония": (
                    "Анализирует проект и помогает владельцу "
                    "быстро разбираться в контактах и расставлять приоритеты."
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
                "Неония анализирует проект, формирует портрет ЦА и затем "
                "помогает владельцу разбирать контакты партиями по 10."
            )

                    neonia_mode = st.radio(
                "Выберите задачу Неонии:",
                [
                    "🎯 Определить мою целевую аудиторию",
                    "🔎 Поиск чатов",
                    "🎯 Поиск контактов в чатах по ЦА",
                    "👥 Поиск контактов",
                    "🧠 Анализ 10 контактов",
                ],
                horizontal=True,
                key="neonia_mode",
            )
                    if neonia_mode != "🎯 Определить мою целевую аудиторию":
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
                        "🧠 Анализ 10 контактов": (
                            "Неония берёт следующие 10 ещё не проанализированных контактов, "
                            "даёт каждому короткую характеристику, а решение принимает владелец."
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
                            if isinstance(passport, dict):
                                profile_for_search = passport.get("profile")
                                if not (
                                    passport.get("schema_version") == 2
                                    and isinstance(profile_for_search, dict)
                                    and profile_for_search.get("portrait")
                                ):
                                    passport = None

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
                                    "Сначала откройте «Определить мою целевую аудиторию» "
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
                                            "карточку, изучите основания и отметьте тех, "
                                            "с кем хотите работать. Количество определяет владелец."
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
                                        # Жёсткого дневного лимита нет.
                                        # Владелец сам определяет рабочий объём.
                                        recommended_limit = 10

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
                                        current_chat_limit = len(top_candidate_ids)

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
                                            st.caption(
                                                "В других источниках уже есть выбранные кандидаты. "
                                                "Это не ограничивает выбор в текущем ТОП-10."
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
                                            limit_reached = False
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
                                                    disabled=False,
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

                        elif neonia_mode == "🧠 Анализ 10 контактов":
                            passport_key = (
                                f"neonia_target_audience_passport_{telegram_id}"
                            )
                            contacts_state_key = (
                                f"neonia_telegram_contacts_{telegram_id}"
                            )
                            candidates_key = (
                                f"neonia_candidates_{telegram_id}"
                            )
                            offset_key = (
                                f"neonia_selection_offset_{telegram_id}"
                            )
                            selected_candidates_key = (
                                f"neonia_selected_candidates_{telegram_id}"
                            )

                            passport = st.session_state.get(passport_key)
                            contacts = st.session_state.get(
                                contacts_state_key,
                                [],
                            )

                            if not passport:
                                st.warning(
                                    "Сначала проведите анализ проекта "
                                    "и сохраните портрет целевой аудитории."
                                )
                            elif not contacts:
                                st.warning(
                                    "Сначала откройте «Поиск контактов» "
                                    "и загрузите контакты из Telegram."
                                )
                            else:
                                current_offset = int(
                                    st.session_state.get(offset_key, 0) or 0
                                )
                                candidate_results = list(
                                    st.session_state.get(candidates_key, []) or []
                                )

                                st.success("✅ Проект, ЦА и контакты готовы")
                                st.caption(
                                    "ЦА здесь — ориентир, а не фильтр. "
                                    "Неония никого не исключает автоматически."
                                )

                                c1, c2, c3 = st.columns(3)
                                c1.metric("Контактов", len(contacts))
                                c2.metric(
                                    "Уже проанализировано",
                                    min(current_offset, len(contacts)),
                                )
                                c3.metric(
                                    "Осталось",
                                    max(0, len(contacts) - current_offset),
                                )

                                with st.expander(
                                    "🎯 Посмотреть сохранённый портрет ЦА"
                                ):
                                    st.write(passport["analysis"])

                                # Текущая десятка — последняя обработанная партия.
                                batch_start = max(0, current_offset - 10)
                                current_batch_ids = {
                                    int(contact["telegram_id"])
                                    for contact in contacts[batch_start:current_offset]
                                }
                                current_batch_results = [
                                    item
                                    for item in candidate_results
                                    if int(item.get("telegram_id", 0))
                                    in current_batch_ids
                                ]

                                if not current_batch_results and current_offset < len(contacts):
                                    st.info(
                                        "Нажмите кнопку ниже. Неония возьмёт первые "
                                        "10 ещё не проанализированных контактов."
                                    )

                                if current_offset < len(contacts):
                                    label = (
                                        "🧠 Проанализировать первые 10 контактов"
                                        if current_offset == 0
                                        else "➡️ Проанализировать следующие 10"
                                    )
                                    if st.button(
                                        label,
                                        type="primary",
                                        key=(
                                            "neonia_analyze_next_ten_"
                                            f"{telegram_id}_{current_offset}"
                                        ),
                                    ):
                                        batch = contacts[
                                            current_offset:current_offset + 10
                                        ]
                                        with st.spinner(
                                            "Неония изучает очередные контакты "
                                            "и составляет короткие характеристики..."
                                        ):
                                            try:
                                                contact_contexts = run_telegram_async(
                                                    fetch_telegram_contact_contexts(
                                                        telegram_id,
                                                        batch,
                                                    )
                                                )
                                                (
                                                    informative_contexts,
                                                    empty_contexts,
                                                ) = split_contacts_by_analysis_value(
                                                    contact_contexts
                                                )

                                                batch_results = [
                                                    build_no_data_candidate(item)
                                                    for item in empty_contexts
                                                ]

                                                if informative_contexts:
                                                    ai_results = (
                                                        analyze_contacts_for_target_audience(
                                                            passport["analysis"],
                                                            informative_contexts,
                                                        )
                                                    )
                                                    for item in ai_results:
                                                        item[
                                                            "analysis_cost_mode"
                                                        ] = "openai"
                                                    batch_results.extend(ai_results)

                                                # Возвращаем исходный порядок десятки.
                                                source_order = {
                                                    int(item["telegram_id"]): pos
                                                    for pos, item in enumerate(
                                                        contact_contexts
                                                    )
                                                }
                                                batch_results.sort(
                                                    key=lambda item: source_order.get(
                                                        int(item["telegram_id"]),
                                                        9999,
                                                    )
                                                )

                                                candidate_results = (
                                                    merge_candidate_results(
                                                        candidate_results,
                                                        batch_results,
                                                    )
                                                )
                                                new_offset = current_offset + len(batch)
                                                st.session_state[candidates_key] = (
                                                    candidate_results
                                                )
                                                st.session_state[offset_key] = new_offset

                                                # Новая десятка = новый осознанный выбор.
                                                st.session_state[
                                                    selected_candidates_key
                                                ] = []
                                                for item in batch_results:
                                                    cid = int(item["telegram_id"])
                                                    st.session_state.pop(
                                                        "owner_select_candidate_"
                                                        f"{telegram_id}_{cid}",
                                                        None,
                                                    )

                                                persist_workspace_if_changed(
                                                    telegram_id,
                                                    force=True,
                                                )
                                                st.rerun()
                                            except Exception as exc:
                                                st.error(
                                                    "Не удалось выполнить анализ: "
                                                    f"{exc}"
                                                )

                                if current_batch_results:
                                    st.markdown(
                                        "### 👥 Текущие 10 контактов"
                                    )
                                    st.caption(
                                        "Посмотрите характеристики и выберите до 5 людей, "
                                        "с которыми хотите начать разговор. "
                                        "Даже низкий приоритет не блокирует выбор."
                                    )

                                    batch_by_id = {
                                        int(item["telegram_id"]): item
                                        for item in current_batch_results
                                    }

                                    selected_now = []
                                    for candidate in current_batch_results:
                                        contact_id = int(candidate["telegram_id"])
                                        checkbox_key = (
                                            "owner_select_candidate_"
                                            f"{telegram_id}_{contact_id}"
                                        )

                                        username = (
                                            f"@{candidate['username']}"
                                            if candidate.get("username")
                                            else "без username"
                                        )

                                        with st.container(border=True):
                                            st.checkbox(
                                                f"{candidate['name']} · {username}",
                                                key=checkbox_key,
                                            )

                                            if st.session_state.get(
                                                checkbox_key, False
                                            ):
                                                selected_now.append(contact_id)

                                            i1, i2 = st.columns(2)
                                            i1.write(
                                                "**🎯 Потенциальный интерес:** "
                                                f"{candidate.get('potential_interest', '—')}"
                                            )
                                            i1.write(
                                                "**🟢 Актуальность:** "
                                                f"{candidate.get('actuality', '—')}"
                                            )
                                            i2.write(
                                                "**🤝 Теплота:** "
                                                f"{candidate.get('warmth', '—')}"
                                            )
                                            obstacles = (
                                                candidate.get("obstacles") or []
                                            )
                                            i2.write(
                                                "**⚠️ Препятствия:** "
                                                + (
                                                    "; ".join(obstacles)
                                                    if obstacles
                                                    else "не выявлены"
                                                )
                                            )

                                            st.write(
                                                "**Короткая характеристика:** "
                                                f"{candidate.get('short_portrait', '—')}"
                                            )
                                            st.info(
                                                "💡 Мнение Неонии: "
                                                f"{candidate.get('owner_hint', '—')}"
                                            )

                                            if candidate.get(
                                                "analysis_cost_mode"
                                            ) == "local_no_ai":
                                                st.caption(
                                                    "💰 Пустой контакт: "
                                                    "OpenAI для этой оценки не вызывался."
                                                )

                                            with st.expander(
                                                "Как Неоне лучше начать разговор"
                                            ):
                                                st.write(
                                                    candidate.get(
                                                        "message_angle",
                                                        "Спокойное знакомство",
                                                    )
                                                )
                                                project_name = str(
                                                    candidate.get(
                                                        "project_name"
                                                    ) or ""
                                                ).strip()
                                                if project_name:
                                                    st.write(
                                                        "**Сейчас связан с проектом:** "
                                                        f"{project_name}"
                                                    )
                                                    evidence = str(
                                                        candidate.get(
                                                            "project_evidence"
                                                        ) or ""
                                                    ).strip()
                                                    if evidence:
                                                        st.caption(evidence)

                                    # Do not silently cut the sixth checkbox:
                                    # owner must clearly see that the allowed maximum is five.
                                    selected_now = [
                                        int(cid) for cid in selected_now
                                    ]
                                    if len(selected_now) > 5:
                                        st.error(
                                            "Выбрано больше 5 человек. "
                                            "Снимите лишние отметки — передать Неоне "
                                            "можно не более пяти."
                                        )

                                    st.caption(
                                        f"Выбрано: {len(selected_now)} из 5"
                                    )

                                    if st.button(
                                        "✅ Сохранить выбор и передать Неоне",
                                        type="primary",
                                        disabled=(
                                            not selected_now
                                            or len(selected_now) > 5
                                        ),
                                        key=(
                                            "confirm_contact_candidates_"
                                            f"{telegram_id}_{current_offset}"
                                        ),
                                    ):
                                        st.session_state[
                                            selected_candidates_key
                                        ] = selected_now

                                        selected_set = set(selected_now)
                                        for item in candidate_results:
                                            try:
                                                item_id = int(
                                                    item.get("telegram_id")
                                                )
                                            except (TypeError, ValueError):
                                                continue
                                            if item_id in selected_set:
                                                item["status"] = (
                                                    "Выбран владельцем"
                                                )

                                        st.session_state[candidates_key] = (
                                            candidate_results
                                        )
                                        persist_workspace_if_changed(
                                            telegram_id,
                                            force=True,
                                        )
                                        st.success(
                                            "✅ Выбор сохранён. "
                                            "Выбранные контакты уже доступны Неоне."
                                        )

                                    if current_offset < len(contacts):
                                        st.divider()
                                        st.caption(
                                            "Никого не выбрали или хотите посмотреть "
                                            "других людей? Переходите к следующей десятке. "
                                            "Уже сделанный анализ сохраняется и повторно "
                                            "не выполняется."
                                        )

                                if candidate_results:
                                    with st.expander(
                                        "📚 История уже проанализированных контактов"
                                    ):
                                        history_rows = []
                                        for item in candidate_results:
                                            history_rows.append(
                                                {
                                                    "Имя": item.get("name", "—"),
                                                    "Интерес": item.get(
                                                        "potential_interest",
                                                        "—",
                                                    ),
                                                    "Актуальность": item.get(
                                                        "actuality",
                                                        "—",
                                                    ),
                                                    "Теплота": item.get(
                                                        "warmth",
                                                        "—",
                                                    ),
                                                    "Статус": item.get(
                                                        "status",
                                                        "Проанализирован",
                                                    ),
                                                }
                                            )
                                        st.dataframe(
                                            history_rows,
                                            use_container_width=True,
                                            hide_index=True,
                                        )

                                if st.button(
                                    "Сбросить анализ контактов и начать заново",
                                    key=(
                                        "neonia_reset_selection_"
                                        f"{telegram_id}"
                                    ),
                                ):
                                    st.session_state[candidates_key] = []
                                    st.session_state[offset_key] = 0
                                    st.session_state.pop(
                                        selected_candidates_key,
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
                    passport_key = (
                        f"neonia_target_audience_passport_{telegram_id}"
                    )
                    saved_passport = st.session_state.get(passport_key)
                    saved_profile = (
                        saved_passport.get("profile")
                        if isinstance(saved_passport, dict)
                        else None
                    )
                    is_live_profile = bool(
                        isinstance(saved_profile, dict)
                        and saved_profile.get("portrait")
                        and (
                            saved_profile.get("who_is_this")
                            or saved_profile.get("current_situation")
                        )
                    )

                    if is_live_profile:
                        st.markdown("### ✅ Сохранённый портрет ЦА")
                        render_target_profile(saved_profile)
                        st.caption(
                            "Неония использует именно этот живой портрет "
                            "при следующем анализе Telegram-контактов."
                        )
                        st.divider()
                    elif isinstance(saved_profile, dict):
                        st.warning(
                            "Сохранён старый вариант ЦА. Он больше не используется: "
                            "в нём смешивались сведения о проекте и портрет человека. "
                            "Создайте новый живой портрет ниже."
                        )

                    st.markdown("### 🎯 Определить мою целевую аудиторию")
                    st.write(
                        "Добавьте материалы проекта. Неония изучит их внутри, "
                        "но на экран выведет только живой портрет человека, "
                        "которому этот проект действительно может быть нужен."
                    )

                    with st.form("neonia_source_form"):
                        project_links = st.text_area(
                            "🔗 Ссылки на проект",
                            placeholder=(
                                "Официальный сайт, страница продукта, "
                                "презентация или ролик — каждая ссылка с новой строки."
                            ),
                            height=110,
                        )

                        project_files = st.file_uploader(
                            "📄 Материалы проекта",
                            type=["pdf", "docx", "pptx", "txt", "csv", "xlsx"],
                            accept_multiple_files=True,
                        )

                        owner_note = st.text_area(
                            "📝 Что особенно важно учесть — необязательно",
                            placeholder=(
                                "Например: нам нужны партнёры, которые работают с людьми "
                                "и открыты дополнительному направлению."
                            ),
                            height=90,
                        )

                        neonia_submitted = st.form_submit_button(
                            "🎯 Определить мою ЦА"
                        )

                    if neonia_submitted:
                        if not project_links.strip() and not project_files:
                            st.warning(
                                "Добавьте хотя бы одну ссылку или материал проекта."
                            )
                        else:
                            with st.spinner(
                                "Неония изучает проект и формирует портрет ЦА..."
                            ):
                                try:
                                    profile = analyze_owner_project_target_profile(
                                        ask_openai,
                                        project_links,
                                        project_files,
                                        owner_note,
                                    )
                                except Exception as exc:
                                    st.error(f"Не удалось определить ЦА: {exc}")
                                else:
                                    file_names = ", ".join(
                                        file.name for file in project_files
                                    ) if project_files else "Файлы не загружены."

                                    st.session_state[passport_key] = {
                                        "schema_version": 2,
                                        "profile": profile,
                                        "analysis": target_profile_for_analysis(profile),
                                        "project_links": project_links.strip(),
                                        "file_names": file_names,
                                        "owner_note": owner_note.strip(),
                                        "saved_at": profile.get("saved_at"),
                                    }
                                    persist_workspace_if_changed(
                                        telegram_id,
                                        force=True,
                                    )
                                    st.success("✅ Портрет ЦА сохранён")
                                    render_target_profile(profile)
                                    st.info(
                                        "Следующий шаг: открыть поиск контактов. "
                                        "Неония будет сравнивать людей именно с этим портретом."
                                    )

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

                    selected_contacts = [
                        candidate_by_id[contact_id]
                        for contact_id in selected_ids
                        if contact_id in candidate_by_id
                    ]

                    st.divider()
                    st.markdown("### 👥 Кого взять в работу")
                    st.caption(
                        "У Неоны два независимых входа: холодные контакты из "
                        "списка Неонии и ваши знакомые — тёплые или полутёплые. "
                        "Неония рекомендует, но не блокирует личный выбор владельца."
                    )

                    cold_selected_contacts = [
                        candidate_by_id[contact_id]
                        for contact_id in selected_ids
                        if contact_id in candidate_by_id
                        and candidate_by_id[contact_id].get("source")
                        != "Знакомый — выбран директором"
                    ]
                    cold_selected_by_id = {
                        int(item["telegram_id"]): item
                        for item in cold_selected_contacts
                        if item.get("telegram_id") is not None
                    }
                    cold_options = [None] + list(cold_selected_by_id)

                    st.markdown("#### 1. 🔎 Список Неонии — холодные контакты")
                    if cold_selected_by_id:
                        focused_cold_id = st.selectbox(
                            "Найдите человека среди выбранных из списка Неонии",
                            options=cold_options,
                            format_func=lambda contact_id: (
                                "Начните вводить имя или выберите человека"
                                if contact_id is None
                                else (
                                    f"{cold_selected_by_id[contact_id].get('name') or 'Без имени'} "
                                    + (
                                        f"@{cold_selected_by_id[contact_id].get('username')}"
                                        if cold_selected_by_id[contact_id].get("username")
                                        else ""
                                    )
                                )
                            ),
                            key=f"neona_cold_contact_focus_{telegram_id}",
                        )
                        if focused_cold_id is not None:
                            selected_contacts.sort(
                                key=lambda item: (
                                    int(item.get("telegram_id") or 0)
                                    != int(focused_cold_id)
                                )
                            )
                            st.caption(
                                "Это холодный контакт. Неона использует контекст "
                                "Неонии, но никогда не сообщает человеку о внутреннем анализе."
                            )
                    else:
                        st.info(
                            "Здесь появятся люди, которых вы выбрали из списка Неонии "
                            "и передали Неоне."
                        )

                    st.markdown("#### 2. 🔎 Найти знакомого — тёплые контакты")
                    st.write(
                        "Найдите любого человека среди уже загруженных Telegram-контактов. "
                        "Даже если Неония его не рекомендовала или вообще не включила в свой "
                        "список, поиск и работа с ним не блокируются."
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
                                limit_reached = False

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
                                    "Что вы хотели бы ему сказать? — необязательно",
                                    placeholder=(
                                        "Можно оставить пустым. Неона сама подготовит "
                                        "человеческое первое сообщение."
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
                                        "Этот человек есть и в списке Неонии. Если вы "
                                        "знаете его лично, можно добавить его как знакомого — "
                                        "Неона будет общаться с ним как с тёплым контактом."
                                    )

                                if st.button(
                                    "➕ Добавить знакомого к работе",
                                    disabled=already_added,
                                    key=(
                                        "add_known_contact_"
                                        f"{telegram_id}_"
                                        f"{chosen_known_id}"
                                    ),
                                ):
                                    previous_known = owner_contacts.get(
                                        chosen_known_id,
                                        owner_contacts.get(
                                            str(chosen_known_id),
                                            {},
                                        ),
                                    )
                                    if not isinstance(previous_known, dict):
                                        previous_known = {}

                                    # Если знакомого ранее выбрала Неония, личное знание
                                    # владельца важнее: переводим контакт в тёплый режим.
                                    current_cold_ids = []
                                    for contact_id in st.session_state.get(
                                        selected_candidates_key,
                                        [],
                                    ):
                                        try:
                                            normalized_contact_id = int(contact_id)
                                        except (TypeError, ValueError):
                                            continue
                                        if normalized_contact_id != int(chosen_known_id):
                                            current_cold_ids.append(normalized_contact_id)
                                    st.session_state[selected_candidates_key] = current_cold_ids

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
                                        "message_angle": (
                                            owner_draft.strip()
                                            or familiarity_note.strip()
                                            or "Тёплое знакомство по выбору владельца"
                                        ),
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
                            "Неона пока не получила людей для работы. Вы можете "
                            "выбрать холодный контакт у Неонии или найти здесь "
                            "своего знакомого — даже если его нет в списке Неонии."
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
                                                        "magnet": choose_neona_magnet(contact),
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
                                        selected_magnet = str(
                                            draft.get("magnet")
                                            or choose_neona_magnet(contact)
                                        ).strip()
                                        if selected_magnet not in NEONA_MAGNET_NAMES:
                                            selected_magnet = choose_neona_magnet(contact)

                                        magnet_index = list(
                                            NEONA_MAGNET_NAMES
                                        ).index(selected_magnet)

                                        magnet_columns = st.columns([3, 1])
                                        chosen_magnet = magnet_columns[0].selectbox(
                                            "🧲 Магнит первого сообщения",
                                            options=list(NEONA_MAGNET_NAMES),
                                            index=magnet_index,
                                            key=(
                                                "neona_magnet_select_"
                                                f"{telegram_id}_{contact_id}_"
                                                f"{draft_revision}"
                                            ),
                                        )
                                        rewrite_for_magnet = magnet_columns[1].button(
                                            "✨ Переписать",
                                            disabled=bool(draft.get("sent")),
                                            key=(
                                                "neona_rewrite_for_magnet_"
                                                f"{telegram_id}_{contact_id}_"
                                                f"{draft_revision}"
                                            ),
                                        )

                                        if (
                                            chosen_magnet != selected_magnet
                                            and not draft.get("sent")
                                        ):
                                            st.caption(
                                                "Вы выбрали другой магнит. "
                                                "Нажмите «✨ Переписать», "
                                                "и Неона создаст новый текст под него."
                                            )

                                        if rewrite_for_magnet:
                                            with st.spinner(
                                                "Неона переписывает сообщение "
                                                "под выбранный магнит..."
                                            ):
                                                try:
                                                    contact[
                                                        "selected_magnet_override"
                                                    ] = chosen_magnet
                                                    new_message = (
                                                        generate_neona_first_message(
                                                            first_name,
                                                            passport["analysis"],
                                                            contact,
                                                        )
                                                    )
                                                    draft = {
                                                        "message": new_message,
                                                        "magnet": chosen_magnet,
                                                        "approved": False,
                                                        "status": (
                                                            "Сообщение переписано "
                                                            "под выбранный магнит"
                                                        ),
                                                        "revision": (
                                                            draft_revision + 1
                                                        ),
                                                        "validation_errors": [],
                                                    }
                                                    drafts[contact_id] = draft
                                                    drafts.pop(
                                                        str(contact_id),
                                                        None,
                                                    )
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
                                                        "Не удалось переписать "
                                                        f"сообщение: {exc}"
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
                                                    # Если владелец вручную выбрал магнит,
                                                    # повторная генерация его не меняет.
                                                    new_message = (
                                                        generate_neona_first_message(
                                                            first_name,
                                                            passport["analysis"],
                                                            contact,
                                                        )
                                                    )
                                                    draft = {
                                                        "message": new_message,
                                                        "magnet": choose_neona_magnet(contact),
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

                elif selected_agent == "Неола":
                    st.caption(
                        "Неола — живой голосовой наставник. "
                        "Говорите с ней естественно: можно перебить, попросить "
                        "повторить, говорить медленнее или объяснить проще."
                    )
                    try:
                        neola_agent_activation = ensure_partner_activation(
                            int(telegram_id)
                        )
                    except Exception:
                        neola_agent_activation = None

                    if (
                        neola_agent_activation
                        and activation_is_confirmed(neola_agent_activation)
                    ):
                        neola_agent_step = int(
                            (neola_agent_activation or {}).get(
                                "onboarding_step"
                            )
                            or 0
                        )
                        st.progress(
                            min(max(neola_agent_step / 7.0, 0.0), 1.0),
                            text=f"Прогресс Неолы: {neola_agent_step}/7",
                        )
                        render_neola_realtime_voice(
                            int(telegram_id),
                            first_name,
                            "🤖 Агенты → 🧭 Стагирит → Неола",
                            neola_agent_step,
                        )
                    else:
                        # До подтверждения 5 лож сохраняем прежний экран
                        # загрузки/подтверждения активации.
                        render_neola_agent(
                            int(telegram_id),
                            first_name,
                            neola_ui_context,
                            ask_openai,
                        )

        elif main_section == "👥 Команда":
            partner_center_tab, team_tools_tab = st.tabs(
                ["🌳 Центр партнёров", "🧰 Инструменты команды"]
            )

            with partner_center_tab:
                render_partner_center(
                    int(telegram_id),
                    member_code,
                    first_name,
                )

            with team_tools_tab:
                render_team_center(
                    telegram_id,
                    member_code,
                    first_name,
                    partner_link,
                )

        elif main_section == "🗺️ Развитие":
            render_agency_development()

        elif main_section == "👤 Профиль":
            inviter_text = referral_code if referral_code else "не указан"

            st.markdown("### 👤 Профиль")

            with st.container(border=True):
                st.markdown(f"**Имя:** {first_name}")
                st.markdown(f"**Партнёрский код:** `{member_code}`")
                st.markdown(f"**Пригласитель:** `{inviter_text}`")
                profile_activation = ensure_partner_activation(int(telegram_id))
                st.markdown(f"**Статус:** {activation_label(profile_activation)}")
                if activation_is_confirmed(profile_activation):
                    st.markdown("**Неола:** 🎙 доступна")
                else:
                    st.markdown("**Неола:** 🔒 после подтверждения 5 лож")

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
