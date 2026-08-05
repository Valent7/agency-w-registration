import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from neonia_contacts import render_neonia_contacts
from workspace_persistence import (
    hydrate_workspace_state_once,
    persist_workspace_if_changed,
)
import asyncio
import json
import re

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
)
from cryptography.fernet import Fernet, InvalidToken

st.set_page_config(
    page_title="Агентство W",
    page_icon="🏛️",
    layout="centered",
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


def generate_neona_first_messages(
    owner_name,
    passport_analysis,
    selected_candidates,
):
    """Создаёт первые сообщения для выбранных кандидатов."""

    system_prompt = f"""
Ты — Неона, виртуальный секретарь-референт {owner_name}.

Подготовь первое персональное сообщение каждому выбранному кандидату.

Обязательные правила:
- обращайся по имени, если имя надёжно известно;
- обязательно назови себя по имени: «Меня зовут Неона, я виртуальный секретарь-референт {owner_name}»;
- никогда не представляйся именем владельца и не говори «я — виртуальный секретарь-референт {owner_name}» без имени Неона;
- пиши уважительно, естественно и без давления;
- не сообщай, что человек был проанализирован или отобран ИИ;
- не обещай доход, гарантированный результат или лёгкие деньги;
- не отправляй ссылки в первом сообщении;
- не используй одинаковый текст для всех;
- опирайся только на переданные сведения;
- закончи одним простым вопросом, на который удобно ответить;
- сообщение должно быть коротким: до 650 знаков.

Верни ТОЛЬКО JSON-массив:
[
  {{
    "telegram_id": 123,
    "message": "готовый текст"
  }}
]
Без пояснений и без Markdown.
"""

    safe_candidates = [
        {
            "telegram_id": item["telegram_id"],
            "name": item.get("name", ""),
            "first_name": item.get("first_name", ""),
            "username": item.get("username", ""),
            "segment": item.get("segment", ""),
            "score": item.get("score", 0),
            "reasons": item.get("reasons", []),
            "message_angle": item.get(
                "message_angle",
                "",
            ),
        }
        for item in selected_candidates
    ]

    request = (
        "ПАСПОРТ ЦА:\n"
        f"{passport_analysis}\n\n"
        "ВЫБРАННЫЕ КАНДИДАТЫ:\n"
        f"{json.dumps(safe_candidates, ensure_ascii=False)}"
    )

    answer = ask_openai(
        system_prompt,
        request,
    )

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    raw_messages = extract_json_array(answer)
    allowed_ids = {
        int(item["telegram_id"])
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

        message = ensure_neona_identity(
            message,
            owner_name,
        )

        result[contact_id] = {
            "message": message[:1000],
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
    """Гарантирует, что первое сообщение представлено от имени Неоны."""

    message = str(message or "").strip()
    identity = (
        f"Меня зовут Неона, я виртуальный "
        f"секретарь-референт {owner_name}."
    )

    # Исправляем типичную ошибку:
    # «Я — виртуальный секретарь-референт Валентины/Valentina».
    owner_pattern = re.escape(str(owner_name).strip())
    without_name_pattern = (
        rf"\bя\s*[—–-]?\s*виртуальн(?:ый|ая)\s+"
        rf"секретарь[\s‑-]*референт\s+{owner_pattern}\.?"
    )
    message = re.sub(
        without_name_pattern,
        identity,
        message,
        count=1,
        flags=re.IGNORECASE,
    )

    # Если имя Неона всё равно отсутствует в начале,
    # вставляем представление сразу после приветствия.
    if "неона" not in message[:350].lower():
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

    return message[:1000]

def generate_neona_first_message(
    owner_name,
    passport_analysis,
    contact,
):
    """Создаёт одно первое сообщение выбранному контакту."""

    is_known_contact = (
        contact.get("source")
        == "Знакомый — выбран владельцем"
    )

    system_prompt = f"""
Ты — Неона, виртуальный секретарь-референт {owner_name}.

Подготовь ОДНО первое персональное сообщение выбранному человеку.

Правила:
- верни только готовый текст сообщения, без пояснений и Markdown;
- обращайся по имени, только если имя выглядит надёжным;
- обязательно назови себя по имени: «Меня зовут Неона, я виртуальный секретарь-референт {owner_name}»;
- никогда не представляйся именем владельца и не говори «я — виртуальный секретарь-референт {owner_name}» без имени Неона;
- пиши естественно, тепло, уважительно и без давления;
- не говори, что человека анализировал или выбирал ИИ;
- не обещай доход, гарантированный результат или лёгкие деньги;
- не отправляй ссылку в первом сообщении;
- не выдумывай факты о человеке;
- закончи одним простым вопросом, на который удобно ответить;
- длина сообщения — до 650 знаков.

Если это знакомый контакт, выбранный владельцем:
- главным основанием являются набросок и пояснения владельца;
- сохрани смысл, тон и границы, заданные владельцем;
- не изображай холодное знакомство, если люди уже общались;
- обязательно учти, что упомянуть и чего избегать.
"""

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

    answer = ask_openai(
        system_prompt,
        request,
    ).strip()

    if answer.startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)

    if not answer:
        raise RuntimeError(
            "Неона не сформировала сообщение."
        )

    if (
        answer.startswith('"')
        and answer.endswith('"')
    ):
        answer = answer[1:-1].strip()

    answer = ensure_neona_identity(
        answer,
        owner_name,
    )

    return answer[:1000]

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

        .subtitle {
            text-align: center;
            font-size: 1.25rem;
            color: #d8c9b0;
            margin-bottom: 2rem;
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

            .subtitle {
                font-size: 1rem;
                line-height: 1.4;
                margin-bottom: 1.2rem;
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
    </style>
    """,
    unsafe_allow_html=True,
)

if not st.query_params.get("hash"):
    st.markdown(
        '<div class="main-title">🏛 Агентство W</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="subtitle">ИИ-агентство нового поколения</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="registration-card">
            <h2>Добро пожаловать</h2>
            <p>
                Здесь вы сможете зарегистрироваться, получить личный кабинет
                и подключить интеллектуальных помощников для развития своего проекта.
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
        st.markdown(f"## Добро пожаловать, {first_name}!")
        st.markdown("### 🟢 Мы строим своё будущее")
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
            ["☀️ День", "🤖 Агенты", "👥 Команда", "👤 Профиль"],
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
                "Ваше будущее создаётся решениями, которые вы принимаете сегодня.",
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
            passport_key = (
                f"neonia_target_audience_passport_{telegram_id}"
            )
            contacts_state_key = (
                f"neonia_telegram_contacts_{telegram_id}"
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
            analyzed_count = min(
                st.session_state.get(offset_key, 0),
                len(all_contacts),
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
                            == "Знакомый — выбран владельцем"
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

                if all_contacts:
                    st.write(
                        f"Всего контактов: **{len(all_contacts)}** · "
                        f"Проанализировано: **{analyzed_count}** · "
                        f"Соответствует ЦА: "
                        f"**{len(recommended_candidates)}**"
                    )
                    if analyzed_count < len(all_contacts):
                        st.caption(
                            "Результат предварительный: Неония ещё не "
                            "проанализировала все контакты."
                        )
                else:
                    st.caption(
                        "Сначала загрузите контакты в разделе Неонии."
                    )

                if not top_candidates:
                    st.caption(
                        "Подходящих кандидатов пока нет. "
                        "Запустите селекцию в разделе Неонии."
                    )
                else:
                    st.info(
                        "Неония предлагает до 10 лучших кандидатов "
                        "из уже проанализированных. Владелец может "
                        "выбрать из них людей для первого сообщения."
                    )
                    if len(top_candidates) < 10:
                        st.warning(
                            f"Пока найдено {len(top_candidates)} из 10 "
                            "кандидатов. Продолжите анализ контактов "
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
                        "#### ✅ Выбор владельца из списка Неонии"
                    )
                    st.info(
                        "Неония только сформировала список до 10 "
                        "подходящих кандидатов. Кого взять в работу, "
                        "решает владелец: поставьте галочку прямо "
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
                        f"Выбрано владельцем: "
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
                            "#### ✍️ Люди, выбранные владельцем"
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
                                            "Знакомый — выбран владельцем"
                                        ),
                                        "segment": (
                                            "Выбран владельцем"
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
                                            "Добавлен владельцем"
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
                                            "Знакомый добавлен владельцем"
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
                                "Добавлен владельцем — "
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

            if neona_drafts:
                with st.container(border=True):
                    st.markdown(
                        "**✍️ Первые сообщения Неоны "
                        "на утверждение**"
                    )
                    st.caption(
                        "Владелец утверждает только первое "
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

                    for contact_id, draft in neona_drafts.items():
                        candidate = candidate_lookup.get(
                            int(contact_id),
                            {},
                        )
                        candidate_name = candidate.get(
                            "name",
                            "Кандидат",
                        )

                        st.markdown(
                            f"#### {candidate_name}"
                        )
                        edited_message = st.text_area(
                            "Текст первого сообщения",
                            value=draft["message"],
                            height=150,
                            key=(
                                "neona_draft_text_"
                                f"{telegram_id}_{contact_id}"
                            ),
                        )

                        if (
                            edited_message.strip()
                            and edited_message.strip()
                            != str(draft.get("message", "")).strip()
                        ):
                            draft["message"] = (
                                edited_message.strip()
                            )
                            neona_drafts[
                                int(contact_id)
                            ] = draft
                            st.session_state[
                                neona_drafts_key
                            ] = neona_drafts
                            persist_workspace_if_changed(
                                telegram_id
                            )

                        if st.button(
                            "✅ Утвердить первое сообщение",
                            key=(
                                "neona_approve_first_message_"
                                f"{telegram_id}_{contact_id}"
                            ),
                        ):
                            draft["message"] = (
                                edited_message.strip()
                            )
                            draft["approved"] = True
                            draft["status"] = (
                                "Первое сообщение утверждено"
                            )
                            neona_drafts[
                                int(contact_id)
                            ] = draft
                            st.session_state[
                                neona_drafts_key
                            ] = neona_drafts

                            for candidate_item in candidate_results:
                                if int(
                                    candidate_item["telegram_id"]
                                ) == int(contact_id):
                                    candidate_item["status"] = (
                                        "Первое сообщение утверждено"
                                    )

                            st.session_state[
                                candidates_key
                            ] = candidate_results

                            if int(contact_id) in owner_contacts:
                                owner_contacts[
                                    int(contact_id)
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

                            st.success(
                                "Первое сообщение утверждено. "
                                "Отправка будет подключена "
                                "отдельным безопасным шагом."
                            )

                        if draft.get("approved"):
                            st.success(
                                "✅ Первое сообщение утверждено"
                            )

            with st.container(border=True):
                st.markdown("**📅 Встречи**")
                st.caption("Сегодняшние встречи появятся здесь.")

            with st.container(border=True):
                st.markdown("**✅ Задачи на сегодня**")
                st.caption("Главные задачи дня появятся здесь.")

            with st.container(border=True):
                st.markdown("**📊 Итоги**")
                st.caption("Здесь будут итоги недели и месяца.")

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
                    "👥 Поиск контактов",
                    "🧠 Анализ контактов по ЦА",
                ],
                horizontal=True,
                key="neonia_mode",
            )
                    if neonia_mode != "🎯 Анализ проекта и ЦА":
                        mode_messages = {
                        "🔎 Поиск чатов": (
                            "Здесь Неония будет находить подходящие чаты "
                            "по критериям целевой аудитории."
                        ),
                        "👥 Поиск контактов": (
                            "Здесь Неония будет работать с доступными контактами "
                            "и выделять потенциальных клиентов и партнёров."
                        ),
                        "🧠 Анализ контактов по ЦА": (
                            "Здесь Неония будет анализировать найденных людей, "
                            "создавать карточки и готовить выбранные контакты для Неоны."
                        ),
                        }
                        st.info(mode_messages[neonia_mode])
                        if neonia_mode == "👥 Поиск контактов":
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
                                        "«Передать Неоне». Из них владелец "
                                        "сам выбирает не более 5."
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
                    st.info(
                        "Неону подключаем следующим шагом сегодня. "
                        "Она будет вести диалог от первого сообщения до встречи."
                    )

                else:
                    st.caption(
                        "Подключение этого агента будет следующим этапом."
                    )

        elif main_section == "👥 Команда":
            st.markdown("### 👥 Команда")

            with st.container(border=True):
                st.markdown("**Лично приглашённые партнёры**")
                st.caption("Список партнёров появится после подключения базы.")

            with st.container(border=True):
                st.markdown("**Структура и активность**")
                st.caption("Здесь будут новые регистрации и результаты команды.")

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
