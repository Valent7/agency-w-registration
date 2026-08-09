from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests
import streamlit as st


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

        st.write(str(message.get("body") or ""))

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
            f"✍️ Написать",
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
                    placeholder=(
                        "Напишите сообщение партнёрам..."
                    ),
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
        if not inbox:
            st.caption("Новых сообщений пока нет.")
        else:
            for message in inbox:
                _render_message_card(
                    message,
                    owner_telegram_id,
                    "in",
                )

    with tabs[2]:
        outbox = _outbox(owner_telegram_id)
        if not outbox:
            st.caption("Вы ещё ничего не отправляли.")
        else:
            for message in outbox:
                _render_message_card(
                    message,
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


def render_team_center(
    owner_telegram_id: int | str,
    member_code: str,
    owner_name: str,
    partner_link: str,
) -> None:
    """
    Рабочий центр Директора:
    Партнёры | Сообщения | Инструкции | Объявления.
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
