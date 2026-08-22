from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import base64
import re
import uuid

import requests
import streamlit as st

from agency_publisher import (
    list_publisher_destinations,
    remove_publisher_destination,
    sync_publisher_destinations,
    verify_publisher_destination,
)


UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")


def _config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise RuntimeError(
            "Не найдены SUPABASE_URL или SUPABASE_SECRET_KEY."
        )
    return url, key


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _get(
    table: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    url, _ = _config()
    response = requests.get(
        f"{url}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _post(
    table: str,
    payload: dict[str, Any] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    url, _ = _config()
    response = requests.post(
        f"{url}/rest/v1/{table}",
        headers=_headers("return=representation"),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _patch(
    table: str,
    filters: dict[str, str],
    payload: dict[str, Any],
) -> None:
    url, _ = _config()
    response = requests.patch(
        f"{url}/rest/v1/{table}",
        headers=_headers("return=minimal"),
        params=filters,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def _format_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(BERLIN).strftime("%d.%m.%Y · %H:%M")
    except (TypeError, ValueError):
        return str(value)


def _direct_partners(member_code: str) -> list[dict[str, Any]]:
    if not member_code:
        return []
    return _get(
        "agency_members",
        {
            "referrer_code": f"eq.{member_code}",
            "select": (
                "telegram_id,first_name,username,member_code,"
                "referrer_code,created_at"
            ),
            "order": "created_at.desc",
        },
    )


def _member_by_telegram(
    telegram_id: int,
) -> dict[str, Any] | None:
    rows = _get(
        "agency_members",
        {
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": (
                "telegram_id,first_name,username,member_code,"
                "referrer_code,created_at"
            ),
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _member_by_code(
    member_code: str | None,
) -> dict[str, Any] | None:
    if not member_code:
        return None
    rows = _get(
        "agency_members",
        {
            "member_code": f"eq.{member_code}",
            "select": (
                "telegram_id,first_name,username,member_code,"
                "referrer_code,created_at"
            ),
            "limit": 1,
        },
    )
    return rows[0] if rows else None



def _all_members() -> list[dict[str, Any]]:
    """Читает весь реестр участников постранично."""
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 1000

    while True:
        batch = _get(
            "agency_members",
            {
                "select": (
                    "telegram_id,first_name,username,member_code,"
                    "referrer_code,created_at"
                ),
                "order": "created_at.asc",
                "limit": page,
                "offset": offset,
            },
        )
        rows.extend(batch)

        if len(batch) < page:
            break

        offset += page
        if offset > 100000:
            break

    return rows


def structure_members(
    owner_telegram_id: int,
) -> list[dict[str, Any]]:
    """
    Вся нижестоящая структура владельца на любой глубине.

    Используем реферальное дерево agency_members:
    владелец -> его партнёры -> партнёры партнёров -> ...
    """
    owner_telegram_id = int(owner_telegram_id)

    owner = _member_by_telegram(owner_telegram_id)
    if not owner:
        return []

    root_code = str(owner.get("member_code") or "").strip()
    if not root_code:
        return []

    members = _all_members()

    children: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        referrer_code = str(
            member.get("referrer_code") or ""
        ).strip()
        if not referrer_code:
            continue
        children.setdefault(
            referrer_code,
            [],
        ).append(member)

    result: list[dict[str, Any]] = []
    seen_ids: set[int] = {owner_telegram_id}
    seen_codes: set[str] = set()
    queue: list[str] = [root_code]

    while queue:
        parent_code = queue.pop(0)

        if parent_code in seen_codes:
            continue
        seen_codes.add(parent_code)

        for member in children.get(parent_code, []):
            try:
                telegram_id = int(
                    member.get("telegram_id")
                )
            except (TypeError, ValueError):
                continue

            if telegram_id in seen_ids:
                continue

            seen_ids.add(telegram_id)
            result.append(member)

            member_code = str(
                member.get("member_code") or ""
            ).strip()
            if member_code:
                queue.append(member_code)

    return result


def structure_member_ids(
    owner_telegram_id: int,
) -> list[int]:
    """Telegram ID всех зарегистрированных людей ниже владельца."""
    ids: list[int] = []

    for member in structure_members(
        int(owner_telegram_id)
    ):
        try:
            telegram_id = int(
                member.get("telegram_id")
            )
        except (TypeError, ValueError):
            continue

        if telegram_id not in ids:
            ids.append(telegram_id)

    return ids



MEDIA_MARKER_RE = re.compile(
    r"\[\[AGENCY_W_MEDIA:([A-Za-z0-9_-]+)\]\]"
)


def _normalize_image_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if hasattr(value, "getvalue"):
        data = value.getvalue()
        if isinstance(data, bytes):
            return data
    try:
        data = bytes(value)
        if data:
            return data
    except Exception:
        pass
    raise ValueError("Не удалось прочитать данные изображения.")


def _media_marker(media_id: str) -> str:
    return f"[[AGENCY_W_MEDIA:{str(media_id).strip()}]]"


def _media_ids_from_body(body: str) -> list[str]:
    result: list[str] = []
    for media_id in MEDIA_MARKER_RE.findall(str(body or "")):
        clean = str(media_id or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def _body_without_media_markers(body: str) -> str:
    clean = MEDIA_MARKER_RE.sub("", str(body or ""))
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def _save_team_media(
    sender_telegram_id: int,
    image_bytes: Any,
    *,
    mime_type: str = "image/png",
    file_name: str = "",
) -> str:
    """
    Сохраняет ОДНУ копию изображения.
    В сообщения партнёров кладётся только media_id.
    """
    raw = _normalize_image_bytes(image_bytes)
    if not raw:
        raise ValueError("Изображение пустое.")

    media_id = "awimg_" + uuid.uuid4().hex
    encoded = base64.b64encode(raw).decode("ascii")

    _post(
        "agency_team_media",
        {
            "id": media_id,
            "sender_telegram_id": int(sender_telegram_id),
            "mime_type": str(mime_type or "image/png").strip(),
            "file_name": (
                str(file_name or "").strip()
                or f"{media_id}.png"
            ),
            "image_base64": encoded,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return media_id


def _team_media(media_id: str) -> dict[str, Any] | None:
    """
    Читает одно внутреннее изображение.
    Ошибка медиа не должна ломать весь раздел «Команда».
    """
    try:
        rows = _get(
            "agency_team_media",
            {
                "id": f"eq.{str(media_id).strip()}",
                "select": "id,mime_type,file_name,image_base64,created_at",
                "limit": 1,
            },
        )
    except Exception:
        return None
    return rows[0] if rows else None


def _render_message_media(media_id: str) -> None:
    media = _team_media(media_id)
    if not media:
        st.caption("🖼️ Иллюстрация временно недоступна.")
        return

    encoded = str(media.get("image_base64") or "").strip()
    if not encoded:
        st.caption("🖼️ Иллюстрация временно недоступна.")
        return

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        st.caption("🖼️ Не удалось открыть иллюстрацию.")
        return

    st.image(
        image_bytes,
        caption="Иллюстрация к публикации",
        use_container_width=True,
    )


def publish_structure_material(
    sender_telegram_id: int,
    body: str = "",
    *,
    image_bytes: Any | None = None,
    subject: str = "",
    zoom_url: str = "",
    mime_type: str = "image/png",
    file_name: str = "",
) -> dict[str, Any]:
    """
    Размещает текст + иллюстрацию как ОДНО внутреннее сообщение.

    Изображение хранится один раз в agency_team_media.
    Каждый получатель получает маленькую ссылку-маркер на media_id.
    """
    sender_telegram_id = int(sender_telegram_id)
    clean_body = str(body or "").strip()

    media_id = ""
    if image_bytes is not None:
        media_id = _save_team_media(
            sender_telegram_id,
            image_bytes,
            mime_type=mime_type,
            file_name=file_name,
        )

    if not clean_body and not media_id:
        raise ValueError("Публикация пуста.")

    message_body = clean_body
    if media_id:
        marker = _media_marker(media_id)
        message_body = (
            f"{clean_body}\n\n{marker}"
            if clean_body
            else marker
        )

    recipients = structure_member_ids(sender_telegram_id)
    if not recipients:
        return {
            "count": 0,
            "media_id": media_id,
            "mode": "combined",
        }

    created_at = datetime.now(UTC).isoformat()
    payload: list[dict[str, Any]] = []

    for recipient_id in recipients:
        if int(recipient_id) == sender_telegram_id:
            continue
        payload.append(
            {
                "sender_telegram_id": sender_telegram_id,
                "recipient_telegram_id": int(recipient_id),
                "subject": str(subject or "").strip() or None,
                "body": message_body,
                "zoom_url": str(zoom_url or "").strip() or None,
                "delivery_status": "stored",
                "created_at": created_at,
            }
        )

    total = 0
    chunk_size = 250
    for start in range(0, len(payload), chunk_size):
        chunk = payload[start:start + chunk_size]
        _post("agency_team_messages", chunk)
        total += len(chunk)

    return {
        "count": total,
        "media_id": media_id,
        "mode": "combined",
    }


def _parse_db_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def attach_structure_image_to_published_message(
    sender_telegram_id: int,
    source_body: str,
    image_bytes: Any,
    *,
    subject: str = "Сообщение Агентства W",
    published_at: str = "",
    mime_type: str = "image/png",
    file_name: str = "",
) -> dict[str, Any]:
    """
    Если текст уже размещён, НЕ дублирует пост.

    Находит последнюю копию этого поста у каждого получателя структуры
    и добавляет к ней ссылку на одну общую иллюстрацию.
    Тогда партнёр видит картинку прямо в карточке исходного поста.

    Если старые строки найти невозможно, безопасно размещает только
    иллюстрацию отдельным внутренним сообщением — текст не дублируется.
    """
    sender_telegram_id = int(sender_telegram_id)
    source_body = str(source_body or "").strip()
    if not source_body:
        raise ValueError("Не найден текст ранее размещённого поста.")

    recipients = set(
        structure_member_ids(sender_telegram_id)
    )
    if not recipients:
        return {
            "count": 0,
            "media_id": "",
            "mode": "attached",
        }

    try:
        rows = _get(
            "agency_team_messages",
            {
                "sender_telegram_id": f"eq.{sender_telegram_id}",
                "select": (
                    "id,recipient_telegram_id,subject,body,created_at"
                ),
                "order": "created_at.desc",
                "limit": 2000,
            },
        )
    except Exception:
        rows = []

    threshold = None
    published_dt = _parse_db_datetime(published_at)
    if published_dt is not None:
        threshold = published_dt - timedelta(minutes=5)

    chosen: dict[int, dict[str, Any]] = {}
    expected_subject = str(subject or "").strip()

    for row in rows:
        try:
            recipient_id = int(row.get("recipient_telegram_id"))
        except (TypeError, ValueError):
            continue

        if recipient_id not in recipients or recipient_id in chosen:
            continue

        if _body_without_media_markers(
            str(row.get("body") or "")
        ) != source_body:
            continue

        row_subject = str(row.get("subject") or "").strip()
        if expected_subject and row_subject != expected_subject:
            continue

        if threshold is not None:
            row_dt = _parse_db_datetime(row.get("created_at"))
            if row_dt is None or row_dt < threshold:
                continue

        chosen[recipient_id] = row

    # Идеальный путь: прикрепляем к уже размещённому посту.
    if chosen:
        media_id = _save_team_media(
            sender_telegram_id,
            image_bytes,
            mime_type=mime_type,
            file_name=file_name,
        )
        marker = _media_marker(media_id)

        updated = 0
        for row in chosen.values():
            message_id = row.get("id")
            if message_id is None:
                continue

            clean_body = _body_without_media_markers(
                str(row.get("body") or "")
            )
            new_body = f"{clean_body}\n\n{marker}"

            _patch(
                "agency_team_messages",
                {"id": f"eq.{int(message_id)}"},
                {"body": new_body},
            )
            updated += 1

        return {
            "count": updated,
            "media_id": media_id,
            "mode": "attached",
        }

    # Запасной путь: не дублируем сам текст.
    fallback = publish_structure_material(
        sender_telegram_id,
        "",
        image_bytes=image_bytes,
        subject="Иллюстрация к публикации",
        mime_type=mime_type,
        file_name=file_name,
    )
    fallback["mode"] = "image_only"
    return fallback


def publish_structure_message(
    sender_telegram_id: int,
    body: str,
    *,
    subject: str = "",
    zoom_url: str = "",
) -> int:
    """
    Размещает утверждённый ТЕКСТ во внутренних сообщениях
    всей нижестоящей структуры.

    delivery_status='stored' означает внутреннюю доставку Агентства W,
    а не подтверждение Telegram-доставки.
    """
    result = publish_structure_material(
        int(sender_telegram_id),
        str(body or ""),
        subject=subject,
        zoom_url=zoom_url,
    )
    return int(result.get("count") or 0)


def _display_name(member: dict[str, Any] | None) -> str:
    if not member:
        return "Партнёр"
    name = str(member.get("first_name") or "").strip()
    username = str(member.get("username") or "").strip()
    if name:
        return name
    if username:
        return f"@{username}"
    return f"Telegram {member.get('telegram_id', '')}".strip()


def _partner_label(member: dict[str, Any]) -> str:
    name = _display_name(member)
    username = str(member.get("username") or "").strip()
    if username and not name.startswith("@"):
        return f"{name} · @{username}"
    return name


def _send_team_message(
    sender_telegram_id: int,
    recipients: list[int],
    body: str,
    subject: str = "",
    zoom_url: str = "",
    reply_to_id: int | None = None,
) -> int:
    body = str(body or "").strip()
    if not body:
        raise ValueError("Сообщение пустое.")

    now = datetime.now(UTC).isoformat()
    payload = []
    for recipient_id in sorted(set(int(x) for x in recipients)):
        if recipient_id == int(sender_telegram_id):
            continue
        payload.append(
            {
                "sender_telegram_id": int(sender_telegram_id),
                "recipient_telegram_id": recipient_id,
                "subject": str(subject or "").strip() or None,
                "body": body,
                "zoom_url": str(zoom_url or "").strip() or None,
                "reply_to_id": (
                    int(reply_to_id)
                    if reply_to_id is not None
                    else None
                ),
                "created_at": now,
            }
        )
    if not payload:
        return 0
    _post("agency_team_messages", payload)
    return len(payload)


def _inbox(telegram_id: int) -> list[dict[str, Any]]:
    return _get(
        "agency_team_messages",
        {
            "recipient_telegram_id": f"eq.{int(telegram_id)}",
            "select": "*",
            "order": "created_at.desc",
            "limit": 100,
        },
    )


def _outbox(telegram_id: int) -> list[dict[str, Any]]:
    return _get(
        "agency_team_messages",
        {
            "sender_telegram_id": f"eq.{int(telegram_id)}",
            "select": "*",
            "order": "created_at.desc",
            "limit": 100,
        },
    )


def _mark_read(message_id: int) -> None:
    _patch(
        "agency_team_messages",
        {"id": f"eq.{int(message_id)}", "read_at": "is.null"},
        {"read_at": datetime.now(UTC).isoformat()},
    )


def _render_partners(
    owner_telegram_id: int,
    member_code: str,
    partner_link: str,
) -> None:
    partners = _direct_partners(member_code)

    c1, c2 = st.columns(2)
    c1.metric("Лично приглашено", len(partners))
    c2.metric(
        "С Telegram username",
        sum(bool(p.get("username")) for p in partners),
    )

    if not partners:
        st.info(
            "Пока нет зарегистрированных партнёров по вашей ссылке."
        )
        st.markdown("**Ваша партнёрская ссылка:**")
        st.code(partner_link, language=None)
        return

    st.caption(
        "Здесь видны только люди, зарегистрированные по вашей "
        "персональной ссылке."
    )

    for partner in partners:
        with st.container(border=True):
            name = _display_name(partner)
            username = str(partner.get("username") or "").strip()

            st.markdown(f"#### 👤 {name}")
            cols = st.columns([1, 1])
            cols[0].caption(
                f"Код: {partner.get('member_code') or '—'}"
            )
            cols[1].caption(
                "Регистрация: "
                + (
                    _format_dt(partner.get("created_at"))
                    if partner.get("created_at")
                    else "—"
                )
            )
            if username:
                st.markdown(f"Telegram: `@{username}`")
            else:
                st.caption("Telegram username не указан.")


def _render_message_card(
    message: dict[str, Any],
    current_telegram_id: int,
    direction: str,
) -> None:
    other_id = (
        int(message["sender_telegram_id"])
        if direction == "in"
        else int(message["recipient_telegram_id"])
    )
    other = _member_by_telegram(other_id)
    other_name = _display_name(other)

    unread = (
        direction == "in"
        and not message.get("read_at")
    )
    marker = "🔴 " if unread else ""

    with st.container(border=True):
        if direction == "in":
            st.markdown(f"**{marker}От: {other_name}**")
        else:
            st.markdown(f"**Кому: {other_name}**")

        st.caption(_format_dt(message.get("created_at")))

        subject = str(message.get("subject") or "").strip()
        if subject:
            st.markdown(f"**{subject}**")

        raw_body = str(message.get("body") or "")
        clean_body = _body_without_media_markers(raw_body)
        media_ids = _media_ids_from_body(raw_body)

        if clean_body:
            st.write(clean_body)

        for media_id in media_ids:
            _render_message_media(media_id)

        zoom_url = str(message.get("zoom_url") or "").strip()
        if zoom_url:
            st.link_button(
                "🎥 Открыть Zoom",
                zoom_url,
                use_container_width=True,
            )

        if unread:
            try:
                _mark_read(int(message["id"]))
            except Exception:
                pass

        if direction == "in":
            with st.expander("↩️ Ответить"):
                with st.form(
                    f"reply_team_message_{message['id']}"
                ):
                    reply_text = st.text_area(
                        "Ответ",
                        key=f"reply_text_{message['id']}",
                        height=100,
                    )
                    reply_zoom = st.text_input(
                        "Ссылка Zoom — необязательно",
                        key=f"reply_zoom_{message['id']}",
                    )
                    submitted = st.form_submit_button(
                        "Отправить ответ",
                        use_container_width=True,
                    )
                    if submitted:
                        if not reply_text.strip():
                            st.warning("Напишите сообщение.")
                        else:
                            _send_team_message(
                                current_telegram_id,
                                [other_id],
                                reply_text,
                                subject=(
                                    f"Re: {subject}"
                                    if subject
                                    else "Ответ"
                                ),
                                zoom_url=reply_zoom,
                                reply_to_id=int(message["id"]),
                            )
                            st.success("Ответ отправлен.")
                            st.rerun()


def _message_counterparty_id(
    message: dict[str, Any],
    direction: str,
) -> int:
    field = (
        "sender_telegram_id"
        if direction == "in"
        else "recipient_telegram_id"
    )
    return int(message[field])


def _conversation_member_info(
    telegram_id: int,
) -> tuple[dict[str, Any] | None, str, str]:
    member = _member_by_telegram(int(telegram_id))
    name = _display_name(member)
    username = str(
        (member or {}).get("username") or ""
    ).strip().lstrip("@")
    return member, name, username


def _conversation_latest_dt(
    messages: list[dict[str, Any]],
) -> datetime:
    dates = [
        _parse_db_datetime(item.get("created_at"))
        for item in messages
    ]
    valid = [item for item in dates if item is not None]
    if valid:
        return max(valid)
    return datetime.min.replace(tzinfo=UTC)


def _conversation_search_text(
    name: str,
    username: str,
) -> str:
    return " ".join(
        part
        for part in [name, username, f"@{username}" if username else ""]
        if part
    ).casefold()


def _render_grouped_messages(
    messages: list[dict[str, Any]],
    owner_telegram_id: int,
    direction: str,
) -> None:
    """
    Показывает сообщения компактно:
    одна строка на человека, переписка скрыта внутри.
    """
    if not messages:
        if direction == "in":
            st.caption("Новых сообщений пока нет.")
        else:
            st.caption("Вы ещё ничего не отправляли.")
        return

    grouped: dict[int, list[dict[str, Any]]] = {}
    for message in messages:
        try:
            other_id = _message_counterparty_id(
                message,
                direction,
            )
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(other_id, []).append(message)

    if not grouped:
        st.caption("Сообщений пока нет.")
        return

    people: list[dict[str, Any]] = []
    for other_id, person_messages in grouped.items():
        _, name, username = _conversation_member_info(other_id)
        latest_dt = _conversation_latest_dt(person_messages)

        unread_count = 0
        if direction == "in":
            unread_count = sum(
                1
                for item in person_messages
                if not item.get("read_at")
            )

        people.append(
            {
                "telegram_id": other_id,
                "name": name,
                "username": username,
                "messages": person_messages,
                "latest_dt": latest_dt,
                "unread_count": unread_count,
            }
        )

    controls = st.columns([2.2, 1.3])
    search = controls[0].text_input(
        "Поиск",
        placeholder="Имя, фамилия или @username",
        key=f"team_message_search_{direction}_{owner_telegram_id}",
        label_visibility="collapsed",
    ).strip().casefold()

    sort_mode = controls[1].selectbox(
        "Сортировка",
        [
            "Последняя активность",
            "Имя",
            "@username",
        ],
        key=f"team_message_sort_{direction}_{owner_telegram_id}",
        label_visibility="collapsed",
    )

    if search:
        people = [
            item
            for item in people
            if search in _conversation_search_text(
                str(item["name"]),
                str(item["username"]),
            )
        ]

    if sort_mode == "Имя":
        people.sort(
            key=lambda item: (
                str(item["name"]).casefold(),
                str(item["username"]).casefold(),
            )
        )
    elif sort_mode == "@username":
        people.sort(
            key=lambda item: (
                0 if str(item["username"]).strip() else 1,
                str(item["username"]).casefold(),
                str(item["name"]).casefold(),
            )
        )
    else:
        people.sort(
            key=lambda item: item["latest_dt"],
            reverse=True,
        )

    if not people:
        st.caption("По этому поиску сообщений не найдено.")
        return

    st.caption(
        f"Диалогов: {len(people)}. "
        "Нажмите на человека, чтобы открыть сообщения."
    )

    for person in people:
        name = str(person["name"])
        username = str(person["username"]).strip()
        person_messages = list(person["messages"])
        latest_dt = person["latest_dt"]
        unread_count = int(person["unread_count"] or 0)

        person_messages.sort(
            key=lambda item: (
                _parse_db_datetime(item.get("created_at"))
                or datetime.min.replace(tzinfo=UTC)
            )
        )

        username_text = f" · @{username}" if username else ""
        unread_text = (
            f" · 🔴 непрочитано {unread_count}"
            if unread_count
            else ""
        )
        latest_text = (
            _format_dt(latest_dt.isoformat())
            if latest_dt.year > 1
            else "дата не указана"
        )

        title = (
            f"👤 {name}{username_text}"
            f" · {len(person_messages)} сообщ."
            f" · последнее {latest_text}"
            f"{unread_text}"
        )

        with st.expander(title, expanded=False):
            for index, message in enumerate(person_messages):
                if index:
                    st.divider()

                st.caption(
                    _format_dt(message.get("created_at"))
                )

                subject = str(
                    message.get("subject") or ""
                ).strip()
                if subject:
                    st.markdown(f"**{subject}**")

                raw_body = str(message.get("body") or "")
                clean_body = _body_without_media_markers(
                    raw_body
                )
                media_ids = _media_ids_from_body(raw_body)

                if clean_body:
                    st.write(clean_body)

                for media_id in media_ids:
                    _render_message_media(media_id)

                zoom_url = str(
                    message.get("zoom_url") or ""
                ).strip()
                if zoom_url:
                    st.link_button(
                        "🎥 Открыть Zoom",
                        zoom_url,
                        use_container_width=True,
                    )

                if (
                    direction == "in"
                    and not message.get("read_at")
                ):
                    try:
                        _mark_read(int(message["id"]))
                    except Exception:
                        pass

            if direction == "in":
                latest_message = person_messages[-1]
                latest_subject = str(
                    latest_message.get("subject") or ""
                ).strip()

                st.divider()
                with st.form(
                    "reply_team_conversation_"
                    f"{owner_telegram_id}_"
                    f"{person['telegram_id']}"
                ):
                    reply_text = st.text_area(
                        "↩️ Ответить",
                        key=(
                            "reply_conversation_text_"
                            f"{owner_telegram_id}_"
                            f"{person['telegram_id']}"
                        ),
                        height=100,
                        placeholder="Напишите ответ...",
                    )
                    reply_zoom = st.text_input(
                        "Ссылка Zoom — необязательно",
                        key=(
                            "reply_conversation_zoom_"
                            f"{owner_telegram_id}_"
                            f"{person['telegram_id']}"
                        ),
                    )
                    submitted = st.form_submit_button(
                        "Отправить ответ",
                        use_container_width=True,
                    )

                    if submitted:
                        if not reply_text.strip():
                            st.warning("Напишите сообщение.")
                        else:
                            _send_team_message(
                                owner_telegram_id,
                                [int(person["telegram_id"])],
                                reply_text,
                                subject=(
                                    f"Re: {latest_subject}"
                                    if latest_subject
                                    else "Ответ"
                                ),
                                zoom_url=reply_zoom,
                                reply_to_id=int(
                                    latest_message["id"]
                                ),
                            )
                            st.success("Ответ отправлен.")
                            st.rerun()


def _render_messages(
    owner_telegram_id: int,
    member_code: str,
) -> None:
    partners = _direct_partners(member_code)

    inbox = _inbox(owner_telegram_id)
    unread_count = sum(
        not item.get("read_at") for item in inbox
    )

    tabs = st.tabs(
        [
            "✍️ Написать",
            f"📥 Входящие ({unread_count})",
            "📤 Отправленные",
        ]
    )

    with tabs[0]:
        if not partners:
            st.info(
                "Чтобы отправлять сообщения команде, сначала должен "
                "зарегистрироваться хотя бы один партнёр по вашей ссылке."
            )
        else:
            partner_map = {
                _partner_label(p): int(p["telegram_id"])
                for p in partners
            }
            labels = list(partner_map)

            with st.form("team_compose_message"):
                send_to_all = st.checkbox(
                    "Отправить всей моей команде"
                )

                selected_labels = st.multiselect(
                    "Получатели",
                    labels,
                    disabled=send_to_all,
                    placeholder="Выберите одного или нескольких партнёров",
                )

                subject = st.text_input(
                    "Тема — необязательно",
                    placeholder="Например: Встреча команды",
                )
                body = st.text_area(
                    "Сообщение",
                    height=160,
                    placeholder="Напишите сообщение партнёрам...",
                )
                zoom_url = st.text_input(
                    "Ссылка Zoom — необязательно",
                    placeholder="https://zoom.us/j/...",
                )

                submitted = st.form_submit_button(
                    "📨 Отправить",
                    use_container_width=True,
                )

                if submitted:
                    recipients = (
                        list(partner_map.values())
                        if send_to_all
                        else [
                            partner_map[label]
                            for label in selected_labels
                        ]
                    )

                    if not recipients:
                        st.warning("Выберите получателя.")
                    elif not body.strip():
                        st.warning("Напишите сообщение.")
                    else:
                        count = _send_team_message(
                            owner_telegram_id,
                            recipients,
                            body,
                            subject=subject,
                            zoom_url=zoom_url,
                        )
                        st.success(
                            f"Отправлено: {count}."
                        )
                        st.rerun()

    with tabs[1]:
        _render_grouped_messages(
            inbox,
            owner_telegram_id,
            "in",
        )

    with tabs[2]:
        outbox = _outbox(owner_telegram_id)
        _render_grouped_messages(
            outbox,
            owner_telegram_id,
            "out",
        )


def _incoming_owner(
    current_telegram_id: int,
) -> dict[str, Any] | None:
    current = _member_by_telegram(current_telegram_id)
    if not current:
        return None
    return _member_by_code(
        str(current.get("referrer_code") or "").strip()
    )


def _render_instructions(
    owner_telegram_id: int,
) -> None:
    referrer = _incoming_owner(owner_telegram_id)

    st.markdown("#### 📚 Инструкции")

    # Инструкции от пригласившего Директора
    if referrer:
        incoming = _get(
            "agency_team_instructions",
            {
                "owner_telegram_id": (
                    f"eq.{int(referrer['telegram_id'])}"
                ),
                "is_active": "eq.true",
                "select": "*",
                "order": "created_at.desc",
            },
        )

        st.caption(
            f"Материалы от Директора: {_display_name(referrer)}"
        )

        if not incoming:
            st.caption("Инструкций пока нет.")

        for item in incoming:
            with st.container(border=True):
                st.markdown(
                    f"**{item.get('title') or 'Инструкция'}**"
                )

                if item.get("body"):
                    st.write(item["body"])

                if item.get("url"):
                    st.link_button(
                        "🔗 Открыть материал",
                        item["url"],
                        use_container_width=True,
                    )
    else:
        st.caption(
            "У вашего кабинета пока не определён пригласивший Директор."
        )

    st.divider()

    # Создание новой инструкции
    st.markdown("#### ➕ Добавить инструкцию своей команде")

    with st.form(
        "team_instruction_create",
        clear_on_submit=True,
    ):
        title = st.text_input("Название")

        body = st.text_area(
            "Короткое пояснение",
            height=100,
        )

        url = st.text_input(
            "Ссылка на материал — необязательно"
        )

        submitted = st.form_submit_button(
            "Сохранить инструкцию",
            use_container_width=True,
        )

        if submitted:
            if not title.strip():
                st.warning("Укажите название.")
            else:
                _post(
                    "agency_team_instructions",
                    {
                        "owner_telegram_id": int(
                            owner_telegram_id
                        ),
                        "title": title.strip(),
                        "body": body.strip() or None,
                        "url": url.strip() or None,
                        "is_active": True,
                    },
                )

                st.success("Инструкция добавлена.")
                st.rerun()

    # Собственные инструкции Директора
    own = _get(
        "agency_team_instructions",
        {
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "is_active": "eq.true",
            "select": "*",
            "order": "created_at.desc",
        },
    )

    if own:
        st.markdown("#### Ваши инструкции")

        for item in own:
            item_id = int(item["id"])

            with st.container(border=True):
                st.markdown(
                    f"🟢 **{item.get('title') or 'Инструкция'}**"
                )

                if item.get("body"):
                    st.caption(str(item["body"]))

                material_url = str(
                    item.get("url") or ""
                ).strip()

                if material_url:
                    st.link_button(
                        "🔗 Открыть материал",
                        material_url,
                        use_container_width=True,
                    )

                # Редактирование
                with st.expander("✏️ Редактировать"):
                    with st.form(
                        f"team_instruction_edit_{item_id}"
                    ):
                        edit_title = st.text_input(
                            "Название",
                            value=str(
                                item.get("title") or ""
                            ),
                            key=f"instruction_title_{item_id}",
                        )

                        edit_body = st.text_area(
                            "Короткое пояснение",
                            value=str(
                                item.get("body") or ""
                            ),
                            height=100,
                            key=f"instruction_body_{item_id}",
                        )

                        edit_url = st.text_input(
                            "Ссылка на материал — необязательно",
                            value=material_url,
                            key=f"instruction_url_{item_id}",
                        )

                        save_edit = st.form_submit_button(
                            "💾 Сохранить изменения",
                            use_container_width=True,
                        )

                        if save_edit:
                            if not edit_title.strip():
                                st.warning(
                                    "Укажите название."
                                )
                            else:
                                _patch(
                                    "agency_team_instructions",
                                    {
                                        "id": f"eq.{item_id}"
                                    },
                                    {
                                        "title": edit_title.strip(),
                                        "body": (
                                            edit_body.strip()
                                            or None
                                        ),
                                        "url": (
                                            edit_url.strip()
                                            or None
                                        ),
                                    },
                                )

                                st.success(
                                    "Изменения сохранены."
                                )
                                st.rerun()

                # Удаление
                if st.button(
                    "🗑️ Удалить",
                    key=f"instruction_delete_{item_id}",
                    use_container_width=True,
                ):
                    _patch(
                        "agency_team_instructions",
                        {
                            "id": f"eq.{item_id}"
                        },
                        {
                            "is_active": False
                        },
                    )

                    st.success(
                        "Инструкция удалена."
                    )
                    st.rerun()

    else:
        st.caption(
            "У вас пока нет собственных инструкций."
        )


def _render_announcements(
    owner_telegram_id: int,
) -> None:
    referrer = _incoming_owner(owner_telegram_id)

    st.markdown("#### 🔔 Объявления")

    if referrer:
        incoming = _get(
            "agency_team_announcements",
            {
                "owner_telegram_id": (
                    f"eq.{int(referrer['telegram_id'])}"
                ),
                "is_active": "eq.true",
                "select": "*",
                "order": "created_at.desc",
                "limit": 50,
            },
        )
        st.caption(
            f"Объявления от Директора: {_display_name(referrer)}"
        )
        if not incoming:
            st.caption("Объявлений пока нет.")
        for item in incoming:
            with st.container(border=True):
                st.markdown(
                    f"**{item.get('title') or 'Объявление'}**"
                )
                st.write(str(item.get("body") or ""))
                if item.get("url"):
                    st.link_button(
                        "🔗 Открыть ссылку",
                        item["url"],
                        use_container_width=True,
                    )
    # Мои опубликованные объявления
    own_announcements = _get(
        "agency_team_announcements",
        {
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "is_active": "eq.true",
            "select": "*",
            "order": "created_at.desc",
            "limit": 50,
        },
    )

    if own_announcements:
        st.markdown("#### 📌 Мои объявления")
        for item in own_announcements:
            with st.container(border=True):
                st.markdown(
                    f"**{item.get('title') or 'Объявление'}**"
                )
                st.write(str(item.get("body") or ""))

                if item.get("url"):
                    st.link_button(
                        "🔗 Открыть ссылку",
                        item["url"],
                        use_container_width=True,
                    )
                item_id = item.get("id")

                if item_id is not None:
                    # Редактирование объявления
                    with st.expander("✏️ Редактировать"):
                        with st.form(
                            f"team_announcement_edit_{item_id}"
                        ):
                            edit_title = st.text_input(
                                "Заголовок",
                                value=str(
                                    item.get("title") or ""
                                ),
                                key=f"announcement_title_{item_id}",
                            )

                            edit_body = st.text_area(
                                "Текст объявления",
                                value=str(
                                    item.get("body") or ""
                                ),
                                height=120,
                                key=f"announcement_body_{item_id}",
                            )

                            edit_url = st.text_input(
                                "Ссылка — необязательно",
                                value=str(
                                    item.get("url") or ""
                                ),
                                key=f"announcement_url_{item_id}",
                            )

                            save_edit = st.form_submit_button(
                                "💾 Сохранить изменения",
                                use_container_width=True,
                            )

                        if save_edit:
                            if (
                                not edit_title.strip()
                                or not edit_body.strip()
                            ):
                                st.warning(
                                    "Укажите заголовок и текст объявления."
                                )
                            else:
                                _patch(
                                    "agency_team_announcements",
                                    {
                                        "id": f"eq.{item_id}"
                                    },
                                    {
                                        "title": edit_title.strip(),
                                        "body": edit_body.strip(),
                                        "url": (
                                            edit_url.strip()
                                            or None
                                        ),
                                    },
                                )

                                st.success(
                                    "Изменения сохранены."
                                )
                                st.rerun()

                    # Удаление объявления
                    if st.button(
                        "🗑 Удалить",
                        key=f"announcement_delete_{item_id}",
                        use_container_width=True,
                    ):
                        _patch(
                            "agency_team_announcements",
                            {
                                "id": f"eq.{item_id}"
                            },
                            {
                                "is_active": False
                            },
                        )

                        st.success(
                            "Объявление удалено."
                        )
                        st.rerun()                
    else:
        st.caption("У вас пока нет опубликованных объявлений.")
    st.divider()
    st.markdown("#### ➕ Новое объявление для моей команды")
    with st.form("team_announcement_create"):
        title = st.text_input("Заголовок")
        body = st.text_area(
            "Текст объявления",
            height=120,
        )
        url = st.text_input(
            "Ссылка — необязательно",
            key="announcement_url",
        )
        submitted = st.form_submit_button(
            "Опубликовать",
            use_container_width=True,
        )
        if submitted:
            if not title.strip() or not body.strip():
                st.warning(
                    "Укажите заголовок и текст объявления."
                )
            else:
                _post(
                    "agency_team_announcements",
                    {
                        "owner_telegram_id": int(
                            owner_telegram_id
                        ),
                        "title": title.strip(),
                        "body": body.strip(),
                        "url": url.strip() or None,
                        "is_active": True,
                    },
                )
                st.success("Объявление опубликовано.")
                st.rerun()



def _publisher_type_label(chat_type: str) -> str:
    return {
        "group": "Группа",
        "supergroup": "Группа",
        "channel": "Канал",
    }.get(str(chat_type or ""), "Telegram-площадка")


def _render_publishing_destinations(
    owner_telegram_id: int,
) -> None:
    """Личный реестр Telegram-групп и каналов владельца."""
    owner_telegram_id = int(owner_telegram_id)

    st.markdown("#### 📣 Мои площадки")
    st.caption(
        "Здесь будут только ваши Telegram-группы и каналы. "
        "Технические chat_id скрыты: их Агентство определяет само."
    )

    with st.container(border=True):
        st.markdown("**Подключить новую площадку**")
        st.write(
            "1. Добавьте `@AgencyWPublisherBot` администратором своей "
            "группы или канала.\n\n"
            "2. В группе можно отправить "
            "`/start@AgencyWPublisherBot`. Для канала достаточно добавить "
            "бота администратором.\n\n"
            "3. Вернитесь сюда и нажмите кнопку ниже."
        )

        if st.button(
            "🔎 Найти мои новые площадки",
            key=f"publisher_sync_{owner_telegram_id}",
            use_container_width=True,
            type="primary",
        ):
            try:
                result = sync_publisher_destinations()
                st.session_state[
                    f"publisher_sync_result_{owner_telegram_id}"
                ] = dict(result or {})
                st.rerun()
            except Exception as exc:
                st.error(
                    "Не удалось проверить Telegram. "
                    f"{type(exc).__name__}: {exc}"
                )

    sync_result = st.session_state.pop(
        f"publisher_sync_result_{owner_telegram_id}",
        None,
    )
    if isinstance(sync_result, dict):
        connected = int(sync_result.get("connected") or 0)
        if connected:
            st.success(
                "Telegram-площадки найдены и сохранены. "
                "Ниже показаны площадки, принадлежащие вашему кабинету."
            )
        else:
            st.info(
                "Новых площадок пока не найдено. Если бота только что "
                "добавили, подождите несколько секунд и нажмите ещё раз."
            )

    try:
        destinations = list_publisher_destinations(
            owner_telegram_id
        )
    except Exception as exc:
        st.error(
            "Реестр площадок пока не открылся. "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if not destinations:
        st.caption("Подключённых площадок пока нет.")
        return

    st.markdown("#### Подключено")

    for item in destinations:
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue

        title = str(
            item.get("chat_title")
            or "Telegram-площадка"
        ).strip()
        chat_type = str(item.get("chat_type") or "")
        username = str(item.get("chat_username") or "").strip()
        verified_at = str(item.get("verified_at") or "").strip()

        with st.container(border=True):
            st.markdown(f"**🟢 {title}**")
            label = _publisher_type_label(chat_type)
            subtitle = label
            if username:
                subtitle += f" · @{username}"
            st.caption(subtitle)

            if verified_at:
                st.caption(
                    "Последняя проверка: "
                    + _format_dt(verified_at)
                )

            c1, c2 = st.columns(2)
            if c1.button(
                "✅ Проверить",
                key=f"publisher_verify_{owner_telegram_id}_{chat_id}",
                use_container_width=True,
            ):
                try:
                    check = verify_publisher_destination(
                        owner_telegram_id,
                        chat_id,
                    )
                    if bool(check.get("ok")):
                        st.success(
                            "Площадка доступна. Publisher остаётся "
                            "администратором и может быть использован "
                            "для публикаций."
                        )
                    else:
                        st.warning(
                            "Площадка больше не доступна для публикаций."
                        )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        "Проверка не удалась. "
                        f"{type(exc).__name__}: {exc}"
                    )

            if c2.button(
                "🗑️ Убрать из реестра",
                key=f"publisher_remove_{owner_telegram_id}_{chat_id}",
                use_container_width=True,
            ):
                try:
                    remove_publisher_destination(
                        owner_telegram_id,
                        chat_id,
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        "Не удалось убрать площадку. "
                        f"{type(exc).__name__}: {exc}"
                    )

    st.caption(
        "Подключённые площадки доступны Стагириту для массовой публикации. "
        "Готовый пост с иллюстрацией или MP4-ролик с анонсом можно отправить "
        "сразу в несколько выбранных Telegram-групп и каналов."
    )

def render_team_center(
    owner_telegram_id: int | str,
    member_code: str,
    owner_name: str,
    partner_link: str,
) -> None:
    """
    Рабочий центр Директора:
    Партнёры | Сообщения | Инструкции | Объявления | Мои площадки.
    """
    owner_telegram_id = int(owner_telegram_id)

    st.markdown("### 👥 Команда")

    section = st.segmented_control(
        "Раздел команды",
        [
            "👥 Партнёры",
            "💬 Сообщения",
            "📚 Инструкции",
            "🔔 Объявления",
            "📣 Мои площадки",
        ],
        default="👥 Партнёры",
        required=True,
        label_visibility="collapsed",
        width="stretch",
        key=f"team_section_{owner_telegram_id}",
    )

    try:
        if section == "👥 Партнёры":
            _render_partners(
                owner_telegram_id,
                member_code,
                partner_link,
            )
        elif section == "💬 Сообщения":
            _render_messages(
                owner_telegram_id,
                member_code,
            )
        elif section == "📚 Инструкции":
            _render_instructions(owner_telegram_id)
        elif section == "🔔 Объявления":
            _render_announcements(owner_telegram_id)
        elif section == "📣 Мои площадки":
            _render_publishing_destinations(owner_telegram_id)

    except requests.HTTPError as exc:
        details = ""
        if exc.response is not None:
            details = exc.response.text[:500]
        st.error(
            "Раздел «Команда» пока не подключён к базе данных. "
            "Сначала выполните TEAM_CENTER_SETUP.sql в Supabase."
        )
        if details:
            with st.expander("Техническая информация"):
                st.code(details)
    except Exception as exc:
        st.error(
            "Не удалось открыть раздел «Команда»."
        )
        with st.expander("Техническая информация"):
            st.code(str(exc))
