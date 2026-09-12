from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from vk_scout_oauth import (
    begin_vk_scout_authorization,
    get_vk_scout_connection,
)

from vk_scout import (
    VKScoutError,
    _sb_patch,
    add_known_vk_contact,
    load_known_vk_contacts,
    load_today_vk_assignments,
    load_vk_sources,
    mark_vk_invited,
    prepare_vk_invitation,
    skip_vk_assignment,
    upsert_vk_source,
)

UTC = timezone.utc


def _disable_vk_source(owner_id: int, source_id: int) -> None:
    """Мягко отключает источник, не удаляя историю поиска."""
    now = datetime.now(UTC).isoformat()
    _sb_patch(
        "agency_vk_sources",
        {
            "id": f"eq.{int(source_id)}",
            "owner_telegram_id": f"eq.{int(owner_id)}",
        },
        {
            "active": False,
            "updated_at": now,
        },
    )



def _render_today_vk_candidates(
    owner_id: int,
    member_code: str,
    ask_openai_fn=None,
) -> None:
    """Показывает владельцу его сегодняшнюю VK-пятёрку и действия Неоны."""
    st.markdown("### 💙 VK 5 на сегодня")

    try:
        assignments = load_today_vk_assignments(owner_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить сегодняшних VK-кандидатов: {exc}")
        return

    st.caption(f"Готово кандидатов: {len(assignments)}/5")

    if not assignments:
        st.info(
            "Пока сегодняшняя VK-пятёрка не сформирована. После авторизации VK Scout "
            "фоновый worker должен выполнить новый цикл: прочитать участников источников, "
            "передать их Неонии на анализ и зарезервировать до 5 лучших кандидатов."
        )
        st.caption(
            "Для первого теста можно перезапустить neona-worker в Render вручную; "
            "дальше поиск будет идти автоматически по расписанию."
        )
        return

    for item in assignments:
        assignment_id = int(item.get("id") or 0)
        position = int(item.get("daily_position") or 0)
        first_name = str(item.get("first_name") or "").strip()
        last_name = str(item.get("last_name") or "").strip()
        full_name = " ".join(x for x in (first_name, last_name) if x) or "VK-кандидат"
        score = int(item.get("score") or 0)
        city = str(item.get("city_name") or "").strip()
        country = str(item.get("country_name") or "").strip()
        place = ", ".join(x for x in (city, country) if x)
        fit = str(item.get("fit_summary") or "").strip()
        profile_url = str(item.get("profile_url") or "").strip()
        status = str(item.get("status") or "reserved").strip()
        invitation_text = str(item.get("invitation_text") or "").strip()
        legacy_link_message = bool(
            invitation_text
            and (
                "vk.me/club" in invitation_text
                or "ref_source=agency_w" in invitation_text
            )
        )

        title = f"{position}. {full_name}" if position else full_name
        with st.container(border=True):
            st.markdown(f"#### {title}")
            meta = [f"совпадение с ЦА: {score}/100"]
            if place:
                meta.append(place)
            st.caption(" · ".join(meta))

            if fit:
                st.write(f"**Почему Неония выбрала:** {fit}")

            if profile_url:
                st.link_button(
                    "Открыть профиль VK",
                    profile_url,
                    use_container_width=False,
                )

            editing_key = f"vk_editing_message_{owner_id}_{assignment_id}"
            if invitation_text:
                st.markdown("**Сообщение Неоны:**")

                if st.session_state.get(editing_key, False):
                    edited_text = st.text_area(
                        "Отредактируйте сообщение перед отправкой",
                        value=invitation_text,
                        key=f"vk_edit_text_{owner_id}_{assignment_id}",
                        height=190,
                        label_visibility="collapsed",
                    )
                    edit_cols = st.columns(2)
                    with edit_cols[0]:
                        if st.button(
                            "💾 Сохранить",
                            key=f"vk_save_edit_{owner_id}_{assignment_id}",
                            type="primary",
                            use_container_width=True,
                        ):
                            cleaned = str(edited_text or "").strip()
                            if not cleaned:
                                st.warning("Сообщение не может быть пустым.")
                            else:
                                try:
                                    _sb_patch(
                                        "agency_vk_assignments",
                                        {
                                            "id": f"eq.{assignment_id}",
                                            "owner_telegram_id": f"eq.{owner_id}",
                                        },
                                        {
                                            "invitation_text": cleaned,
                                            "status": "prepared",
                                            "updated_at": datetime.now(UTC).isoformat(),
                                        },
                                    )
                                    st.session_state[editing_key] = False
                                    st.success("Сообщение сохранено.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Не удалось сохранить сообщение: {exc}")
                    with edit_cols[1]:
                        if st.button(
                            "Отмена",
                            key=f"vk_cancel_edit_{owner_id}_{assignment_id}",
                            use_container_width=True,
                        ):
                            st.session_state[editing_key] = False
                            st.rerun()

                    # Пока текст редактируется, не показываем кнопки
                    # «Отправлено» и «Пропустить», чтобы не отметить
                    # сообщение до сохранения правок.
                    continue

                st.code(invitation_text, language=None)

                if legacy_link_message:
                    if status == "invited":
                        st.caption(
                            "ℹ️ Это старое сообщение, уже отмеченное как отправленное. "
                            "Оно сохранено в истории как было отправлено. Все новые первые "
                            "сообщения Неоны создаются без обязательной ссылки на сообщество."
                        )
                    elif assignment_id:
                        st.warning(
                            "Это сообщение было подготовлено до нового правила и всё ещё содержит "
                            "старую ссылку. Обновите его одним нажатием."
                        )
                        if st.button(
                            "🔄 Обновить без ссылки",
                            key=f"vk_refresh_legacy_{owner_id}_{assignment_id}",
                            type="primary",
                            use_container_width=False,
                        ):
                            try:
                                prepare_vk_invitation(
                                    assignment_id,
                                    member_code,
                                    ask_openai_fn=ask_openai_fn,
                                )
                                st.success("Сообщение обновлено по новым правилам.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Не удалось обновить сообщение: {exc}")

                if status != "invited" and assignment_id:
                    if st.button(
                        "✏️ Редактировать",
                        key=f"vk_edit_{owner_id}_{assignment_id}",
                        use_container_width=False,
                    ):
                        st.session_state[editing_key] = True
                        st.rerun()
            elif status in {"reserved", "prepared"} and assignment_id:
                if st.button(
                    "✍️ Подготовить сообщение Неоны",
                    key=f"vk_prepare_{owner_id}_{assignment_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        prepared = prepare_vk_invitation(
                            assignment_id,
                            member_code,
                            ask_openai_fn=ask_openai_fn,
                        )
                        st.success("Сообщение Неоны готово.")
                        st.session_state[
                            f"vk_prepared_message_{assignment_id}"
                        ] = prepared.get("invitation_text") or ""
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Не удалось подготовить сообщение: {exc}")

            cols = st.columns(2)
            with cols[0]:
                if status != "invited" and assignment_id:
                    if st.button(
                        "✅ Отправлено",
                        key=f"vk_invited_{owner_id}_{assignment_id}",
                        use_container_width=True,
                    ):
                        try:
                            mark_vk_invited(assignment_id)
                            st.success("Отмечено как отправленное.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось сохранить статус: {exc}")
                elif status == "invited":
                    st.success("✅ Отправлено")

            with cols[1]:
                if status != "invited" and assignment_id:
                    if st.button(
                        "⏭ Пропустить",
                        key=f"vk_skip_{owner_id}_{assignment_id}",
                        use_container_width=True,
                    ):
                        try:
                            skip_vk_assignment(assignment_id)
                            st.success("Кандидат пропущен.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось пропустить кандидата: {exc}")

    st.caption(
        "Первое сообщение пока отправляется из личного VK вручную: откройте профиль, "
        "скопируйте утверждённый текст Неоны и после отправки нажмите «Отправлено». "
        "Обязательной ссылки на сообщество в первом сообщении больше нет."
    )


def _render_known_vk_contacts(
    owner_id: int,
    member_code: str,
    ask_openai_fn=None,
) -> None:
    """Ручное добавление знакомых владельца — отдельно от ежедневной пятёрки Неонии."""
    st.markdown("### 👥 Свои знакомые VK")
    st.caption(
        "Можно добавить друга или знакомого по ссылке на личный VK-профиль. "
        "Он не занимает место в пятёрке Неонии. Неона подготовит отдельное первое сообщение, "
        "а вы сможете его отредактировать перед отправкой."
    )

    with st.form(f"vk_known_contact_add_{owner_id}", clear_on_submit=True):
        profile = st.text_input(
            "Ссылка или ID VK-профиля",
            placeholder="Например: https://vk.com/id123456 или https://vk.com/username",
        )
        familiarity_note = st.text_input(
            "Что Неоне важно знать о вашем знакомстве — необязательно",
            placeholder="Например: знакомы по прежнему проекту; давно не общались",
        )
        submitted = st.form_submit_button(
            "➕ Добавить знакомого",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not str(profile or "").strip():
            st.warning("Вставьте ссылку или ID личного VK-профиля.")
        else:
            try:
                added = add_known_vk_contact(
                    owner_id,
                    member_code,
                    str(profile).strip(),
                    familiarity_note=str(familiarity_note or "").strip(),
                )
                name = " ".join(
                    x for x in (
                        str(added.get("first_name") or "").strip(),
                        str(added.get("last_name") or "").strip(),
                    ) if x
                ) or "VK-контакт"
                st.success(f"✅ {name} добавлен в список знакомых.")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось добавить знакомого: {exc}")

    try:
        contacts = load_known_vk_contacts(owner_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить знакомых VK: {exc}")
        return

    if not contacts:
        st.info("Пока знакомые VK не добавлены.")
        return

    for item in contacts:
        assignment_id = int(item.get("assignment_id") or item.get("id") or 0)
        first_name = str(item.get("first_name") or "").strip()
        last_name = str(item.get("last_name") or "").strip()
        full_name = " ".join(x for x in (first_name, last_name) if x) or "VK-контакт"
        profile_url = str(item.get("profile_url") or "").strip()
        status = str(item.get("status") or "reserved").strip()
        invitation_text = str(item.get("invitation_text") or "").strip()
        legacy_link_message = bool(
            invitation_text
            and (
                "vk.me/club" in invitation_text
                or "ref_source=agency_w" in invitation_text
            )
        )
        fit = str(item.get("fit_summary") or "").strip()
        note = fit.split(":", 1)[1].strip() if fit.startswith("Знакомый владельца:") else ""

        with st.container(border=True):
            st.markdown(f"#### {full_name}")
            st.caption("Знакомый владельца")
            if note:
                st.write(f"**Заметка:** {note}")
            if profile_url:
                st.link_button("Открыть профиль VK", profile_url, use_container_width=False)

            editing_key = f"vk_known_editing_{owner_id}_{assignment_id}"
            if invitation_text:
                st.markdown("**Сообщение Неоны:**")
                if st.session_state.get(editing_key, False):
                    edited_text = st.text_area(
                        "Отредактируйте сообщение перед отправкой",
                        value=invitation_text,
                        key=f"vk_known_edit_text_{owner_id}_{assignment_id}",
                        height=190,
                        label_visibility="collapsed",
                    )
                    edit_cols = st.columns(2)
                    with edit_cols[0]:
                        if st.button(
                            "💾 Сохранить",
                            key=f"vk_known_save_{owner_id}_{assignment_id}",
                            type="primary",
                            use_container_width=True,
                        ):
                            cleaned = str(edited_text or "").strip()
                            if not cleaned:
                                st.warning("Сообщение не может быть пустым.")
                            else:
                                try:
                                    _sb_patch(
                                        "agency_vk_assignments",
                                        {
                                            "id": f"eq.{assignment_id}",
                                            "owner_telegram_id": f"eq.{owner_id}",
                                        },
                                        {
                                            "invitation_text": cleaned,
                                            "status": "prepared",
                                            "updated_at": datetime.now(UTC).isoformat(),
                                        },
                                    )
                                    st.session_state[editing_key] = False
                                    st.success("Сообщение сохранено.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Не удалось сохранить сообщение: {exc}")
                    with edit_cols[1]:
                        if st.button(
                            "Отмена",
                            key=f"vk_known_cancel_{owner_id}_{assignment_id}",
                            use_container_width=True,
                        ):
                            st.session_state[editing_key] = False
                            st.rerun()
                    continue

                st.code(invitation_text, language=None)

                if legacy_link_message:
                    if status == "invited":
                        st.caption(
                            "ℹ️ Это старое сообщение, уже отмеченное как отправленное. "
                            "Оно сохранено в истории как было отправлено. Все новые первые "
                            "сообщения Неоны создаются без обязательной ссылки на сообщество."
                        )
                    elif assignment_id:
                        st.warning(
                            "Это сообщение было подготовлено до нового правила и всё ещё содержит "
                            "старую ссылку. Обновите его одним нажатием."
                        )
                        if st.button(
                            "🔄 Обновить без ссылки",
                            key=f"vk_refresh_legacy_{owner_id}_{assignment_id}",
                            type="primary",
                            use_container_width=False,
                        ):
                            try:
                                prepare_vk_invitation(
                                    assignment_id,
                                    member_code,
                                    ask_openai_fn=ask_openai_fn,
                                )
                                st.success("Сообщение обновлено по новым правилам.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Не удалось обновить сообщение: {exc}")

                if status != "invited" and assignment_id:
                    if st.button(
                        "✏️ Редактировать",
                        key=f"vk_known_edit_{owner_id}_{assignment_id}",
                    ):
                        st.session_state[editing_key] = True
                        st.rerun()
            elif status in {"reserved", "prepared"} and assignment_id:
                if st.button(
                    "✍️ Подготовить сообщение Неоны",
                    key=f"vk_known_prepare_{owner_id}_{assignment_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        prepare_vk_invitation(
                            assignment_id,
                            member_code,
                            ask_openai_fn=ask_openai_fn,
                        )
                        st.success("Сообщение Неоны готово.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Не удалось подготовить сообщение: {exc}")

            cols = st.columns(2)
            with cols[0]:
                if status != "invited" and assignment_id:
                    if st.button(
                        "✅ Отправлено",
                        key=f"vk_known_invited_{owner_id}_{assignment_id}",
                        use_container_width=True,
                    ):
                        try:
                            mark_vk_invited(assignment_id)
                            st.success("Отмечено как отправленное.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось сохранить статус: {exc}")
                elif status == "invited":
                    st.success("✅ Отправлено")
            with cols[1]:
                if status != "invited" and assignment_id:
                    if st.button(
                        "⏭ Убрать",
                        key=f"vk_known_skip_{owner_id}_{assignment_id}",
                        use_container_width=True,
                    ):
                        try:
                            skip_vk_assignment(assignment_id)
                            st.success("Контакт убран из активной работы.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось убрать контакт: {exc}")


def render_vk_sources(
    owner_id: int,
    member_code: str = "",
    ask_openai_fn=None,
) -> None:
    """Интерфейс Неонии для управления тематическими VK-сообществами."""
    owner_id = int(owner_id)

    st.markdown("### 💙 Источники поиска VK")
    st.caption(
        "Добавьте публичные тематические VK-сообщества, где может находиться ваша ЦА. "
        "Неония будет анализировать только доступные публичные данные. "
        "Холодные сообщения автоматически не отправляются."
    )

    # VK user authorization for the background scanner. Tokens are encrypted
    # in Supabase and refresh automatically; they are never shown in the UI.
    try:
        connection = get_vk_scout_connection(owner_id)
    except Exception as exc:
        connection = {"connected": False}
        st.warning(f"Не удалось проверить авторизацию VK Scout: {exc}")

    if connection.get("connected"):
        st.success("🟢 VK Scout авторизован через VK ID")
        expires_at = str(connection.get("access_expires_at") or "").strip()
        if expires_at:
            st.caption("Access token обновляется worker автоматически. Ручная замена каждый час не нужна.")
    else:
        st.info(
            "Чтобы читать участников публичных VK-сообществ, один раз авторизуйте "
            "VK Scout через ваш VK ID."
        )
        auth_key = f"vk_scout_auth_url_{owner_id}"
        if st.button(
            "🔐 Подключить VK Scout",
            key=f"vk_scout_auth_start_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            try:
                st.session_state[auth_key] = begin_vk_scout_authorization(owner_id)
            except Exception as exc:
                st.error(f"Не удалось начать авторизацию VK Scout: {exc}")
        auth_url = str(st.session_state.get(auth_key) or "").strip()
        if auth_url:
            st.link_button(
                "Продолжить авторизацию в VK",
                auth_url,
                use_container_width=True,
            )
            st.caption("После разрешения VK вернёт вас обратно в Агентство W.")

    if connection.get("connected"):
        # Знакомые владельца показываем СРАЗУ сверху, чтобы блок не терялся
        # после пяти карточек ежедневной подборки Неонии.
        _render_known_vk_contacts(
            owner_id,
            str(member_code or "").strip(),
            ask_openai_fn=ask_openai_fn,
        )
        st.divider()
        _render_today_vk_candidates(
            owner_id,
            str(member_code or "").strip(),
            ask_openai_fn=ask_openai_fn,
        )
        st.divider()

    with st.form(
        f"neonia_vk_source_add_{owner_id}",
        clear_on_submit=True,
    ):
        community = st.text_input(
            "Ссылка или ID VK-сообщества",
            placeholder="Например: https://vk.com/имя_сообщества",
        )
        community_name = st.text_input(
            "Название — необязательно",
            placeholder="Например: Предприниматели и ИИ",
        )
        submitted = st.form_submit_button(
            "➕ Добавить источник",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not community.strip():
            st.warning("Вставьте ссылку или ID VK-сообщества.")
        else:
            try:
                saved = upsert_vk_source(
                    owner_id,
                    community.strip(),
                    community_name=community_name.strip(),
                )
                label = (
                    str(saved.get("community_name") or "").strip()
                    or str(saved.get("community_url") or "").strip()
                    or str(saved.get("community_id") or "VK-сообщество")
                )
                st.success(f"✅ Источник добавлен: {label}")
                st.rerun()
            except VKScoutError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Не удалось добавить источник VK: {exc}")

    st.divider()
    st.markdown("#### Сохранённые источники")

    try:
        sources = load_vk_sources(owner_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить источники VK: {exc}")
        return

    if not sources:
        st.info(
            "Пока нет ни одного источника. Добавьте первое тематическое VK-сообщество — "
            "после этого фоновый VK Scout сможет начать поиск кандидатов."
        )
        return

    st.caption(f"Активных источников: {len(sources)}")

    for source in sources:
        source_id = source.get("id")
        community_id = source.get("community_id")
        name = str(source.get("community_name") or "").strip()
        url = str(source.get("community_url") or "").strip()
        title = name or (f"VK-сообщество {community_id}" if community_id else "VK-сообщество")

        with st.container(border=True):
            cols = st.columns([5, 1.5])
            with cols[0]:
                st.markdown(f"**{title}**")
                if url:
                    st.link_button(
                        "Открыть VK-сообщество",
                        url,
                        use_container_width=False,
                    )
                if community_id:
                    st.caption(f"ID сообщества: {community_id}")

            with cols[1]:
                if source_id is None:
                    st.caption("Источник активен")
                elif st.button(
                    "Отключить",
                    key=f"disable_vk_source_{owner_id}_{source_id}",
                    use_container_width=True,
                ):
                    try:
                        _disable_vk_source(owner_id, int(source_id))
                        st.success("Источник отключён.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Не удалось отключить источник: {exc}")

    st.caption(
        "Фоновый worker заберёт активные источники в следующем цикле. "
        "Поиск выполняется отдельно от страницы Агентства W."
    )
