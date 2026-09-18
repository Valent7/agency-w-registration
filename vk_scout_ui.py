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
    prepare_vk_feed_radar,
    prepare_vk_invitation,
    prepare_vk_warmup_comment,
    register_published_vk_comment,
    load_vk_comment_thread,
    check_vk_comment_thread,
    send_pending_vk_comment_reply,
    process_manual_vk_comment_reply,
    mark_manual_vk_reply_published,
    skip_vk_assignment,
    upsert_vk_source,
)

UTC = timezone.utc

VK_AGENCY_MATERIAL_URL = (
    "https://vk.ru/club241237638?act=s&id=241237638#section=short_videos"
)


def _render_vk_material_message(
    owner_id: int,
    contact_key: str,
    first_name: str,
    *,
    intro: str = "Материал после согласия человека",
) -> None:
    """Показывает готовый текст со ссылкой только после явного согласия человека."""
    st.markdown(f"**📎 {intro}**")
    st.caption(
        "Ссылку даём только если человек сам попросил материал или явно согласился "
        "его посмотреть. Не используем её как рекламу в первом касании или в комментарии."
    )

    consent_key = f"vk_material_consent_{contact_key}_{int(owner_id)}"
    consent = st.checkbox(
        "Человек попросил или согласился получить материал",
        key=consent_key,
    )
    if not consent:
        return

    name_prefix = f"{str(first_name).strip()}, " if str(first_name or "").strip() else ""
    default_message = (
        f"{name_prefix}вот обещанный материал об Агентстве W. "
        "Здесь собраны короткие видео и примеры: "
        f"{VK_AGENCY_MATERIAL_URL}\n\n"
        "Посмотрите, когда будет удобно. Что из этого Вам покажется самым полезным?"
    )
    message_key = f"vk_material_message_{contact_key}_{int(owner_id)}"
    if message_key not in st.session_state:
        st.session_state[message_key] = default_message

    st.text_area(
        "Сообщение Неоны со ссылкой",
        key=message_key,
        height=170,
    )
    st.link_button(
        "Открыть материалы Агентства W во VK",
        VK_AGENCY_MATERIAL_URL,
        use_container_width=False,
    )
    st.caption(
        "Отправьте подготовленный текст вручную из своего VK. После отправки Неона "
        "сама не напоминает «Вы посмотрели?» — ждём нового сообщения человека."
    )


def _render_vk_material_followup(
    owner_id: int,
    assignment_id: int,
    first_name: str,
    status: str,
    *,
    scope: str,
) -> None:
    """Материал после уже отправленного первого личного сообщения."""
    if str(status or "").strip() != "invited" or not assignment_id:
        return

    st.divider()
    _render_vk_material_message(
        owner_id,
        f"{scope}_{int(assignment_id)}",
        first_name,
    )


def _render_vk_comment_followup(
    owner_id: int,
    post_owner_id: int,
    post_id: int,
    comment_text: str,
    first_name: str = "",
    *,
    post_url: str = "",
    post_text: str = "",
    source: str = "radar",
    assignment_id: int | None = None,
    ask_openai_fn=None,
) -> None:
    """Registers a real published VK comment and shows its live dialogue state."""
    st.markdown("**💬 Что дальше после комментария**")

    try:
        thread = load_vk_comment_thread(
            int(owner_id),
            int(post_owner_id),
            int(post_id),
        )
    except Exception as exc:
        thread = None
        st.warning(f"Не удалось проверить наблюдение за комментарием: {exc}")

    if not thread:
        st.caption(
            "Сначала опубликуйте подготовленный комментарий под этим постом в VK. "
            "После этого нажмите кнопку ниже — Агентство найдёт реальный ID комментария "
            "и Неона начнёт следить именно за этой веткой."
        )
        if st.button(
            "✅ Комментарий опубликован",
            key=f"vk_comment_published_{int(owner_id)}_{int(post_owner_id)}_{int(post_id)}",
            type="primary",
            use_container_width=True,
        ):
            try:
                register_published_vk_comment(
                    int(owner_id),
                    post_owner_id=int(post_owner_id),
                    post_id=int(post_id),
                    comment_text=str(comment_text or "").strip(),
                    post_url=str(post_url or "").strip(),
                    post_text=str(post_text or "").strip(),
                    first_name=str(first_name or "").strip(),
                    source=str(source or "radar"),
                    assignment_id=int(assignment_id) if assignment_id else None,
                )
                st.success("Комментарий найден в VK. Неона взяла ветку под наблюдение.")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось зарегистрировать опубликованный комментарий: {exc}")
        return

    thread_id = int(thread.get("id") or 0)
    st.success("💬 Ветка VK сохранена. Неона помнит контекст разговора")
    st.caption(
        "VK сейчас не даёт Агентству автоматически читать ответы под чужими личными постами. "
        "Поэтому ответ человека вставляем сюда вручную. Неона уже знает исходный пост, "
        "свой предыдущий комментарий и историю этой ветки."
    )

    # Keep this thread out of the background watcher while VK rejects the
    # required read methods for the current auth profile. This also clears the
    # noisy historic 1051/27 error once the user starts the manual path.
    if str(thread.get("status") or "") not in {"paused", "closed"} or thread.get("last_error"):
        try:
            _sb_patch(
                "agency_vk_comment_threads",
                {"id": f"eq.{thread_id}"},
                {
                    "status": "paused",
                    "last_error": None,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            thread = {**thread, "status": "paused", "last_error": None}
        except Exception:
            pass

    inbound_key = f"vk_manual_inbound_{thread_id}"
    st.text_area(
        "Ответ человека",
        key=inbound_key,
        height=100,
        placeholder="Вставьте точную реплику из VK — например: нейробабушка прикольно  или  🙂",
    )
    if st.button(
        "🧠 Передать Неоне",
        key=f"vk_manual_to_neona_{thread_id}",
        type="primary",
        use_container_width=True,
    ):
        try:
            process_manual_vk_comment_reply(
                thread_id,
                st.session_state.get(inbound_key, ""),
                ask_openai_fn=ask_openai_fn,
            )
            st.session_state[inbound_key] = ""
            st.rerun()
        except Exception as exc:
            st.error(f"Неона не смогла обработать ответ: {exc}")

    last_inbound = str(thread.get("last_inbound_text") or "").strip()
    pending_kind = str(thread.get("pending_event_kind") or "").strip()
    if last_inbound and pending_kind == "manual_comment":
        st.markdown("**Последняя реплика человека:**")
        st.write(last_inbound)

    reply_text = str(thread.get("neona_reply_text") or "").strip()
    if pending_kind == "manual_comment" and reply_text:
        st.markdown("**Ответ Неоны готов:**")
        edit_key = f"vk_manual_neona_reply_{thread_id}_{thread.get('pending_event_key') or 'pending'}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = reply_text
        edited_reply = st.text_area(
            "Можно отредактировать перед публикацией",
            key=edit_key,
            height=120,
        )
        st.caption(
            "Скопируйте этот ответ и опубликуйте вручную в VK именно ответом человеку. "
            "После публикации нажмите кнопку ниже — тогда Неона сохранит продолжение в памяти ветки."
        )
        if st.button(
            "✅ Ответ Неоны опубликован в VK",
            key=f"vk_manual_reply_published_{thread_id}",
            use_container_width=True,
        ):
            try:
                mark_manual_vk_reply_published(thread_id, edited_reply)
                st.success("Продолжение сохранено. Следующий ответ человека можно снова вставить сюда.")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось сохранить продолжение диалога: {exc}")
    elif thread.get("replied_at") and thread.get("last_outbound_text"):
        st.markdown("**Последний опубликованный ответ:**")
        st.write(str(thread.get("last_outbound_text") or ""))
        st.caption("✅ Отмечен как опубликованный вручную; контекст сохранён у Неоны.")

    st.caption(
        "Автоматическую проверку VK не удаляем из системы: вернём её, когда VK даст подходящий "
        "доступ к чтению комментариев. Сейчас фоновые ошибки 1051/27 для этой ветки отключены."
    )

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



def _render_vk_warmup(
    owner_id: int,
    assignment_id: int,
    first_name: str = "",
    ask_openai_fn=None,
) -> None:
    """Мягкое знакомство через свежую публичную публикацию кандидата."""
    if not assignment_id:
        return

    state_key = f"vk_warmup_{owner_id}_{assignment_id}"
    edit_key = f"vk_warmup_comment_{owner_id}_{assignment_id}"
    result = st.session_state.get(state_key)

    st.markdown("**🌿 Утепление**")
    if not isinstance(result, dict):
        st.caption(
            "Неона найдёт свежую публичную публикацию и подготовит тёплый комментарий — "
            "без предложения бизнеса и без рекламы."
        )
        if st.button(
            "🌿 Найти пост и подготовить комментарий",
            key=f"vk_warmup_prepare_{owner_id}_{assignment_id}",
            use_container_width=True,
        ):
            try:
                result = prepare_vk_warmup_comment(
                    assignment_id,
                    ask_openai_fn=ask_openai_fn,
                )
                st.session_state[state_key] = result
                st.session_state[edit_key] = str(result.get("comment") or "")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось подготовить утепление: {exc}")
        return

    post_url = str(result.get("post_url") or "").strip()
    preview = str(result.get("post_preview") or "").strip()
    if post_url:
        st.link_button("Открыть выбранную публикацию VK", post_url, use_container_width=False)
    if preview:
        short_preview = preview if len(preview) <= 320 else preview[:317].rstrip() + "..."
        st.caption(f"Неона выбрала: {short_preview}")

    comment = st.text_area(
        "Комментарий Неоны",
        value=str(st.session_state.get(edit_key) or result.get("comment") or ""),
        key=edit_key,
        height=110,
    )
    if comment.strip() != str(result.get("comment") or "").strip():
        result = {**result, "comment": comment.strip()}
        st.session_state[state_key] = result

    cols = st.columns(2)
    with cols[0]:
        if st.button(
            "🔄 Другой вариант",
            key=f"vk_warmup_refresh_{owner_id}_{assignment_id}",
            use_container_width=True,
        ):
            try:
                fresh = prepare_vk_warmup_comment(
                    assignment_id,
                    ask_openai_fn=ask_openai_fn,
                )
                st.session_state[state_key] = fresh
                st.session_state[edit_key] = str(fresh.get("comment") or "")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось подготовить другой вариант: {exc}")
    with cols[1]:
        if st.button(
            "🧹 Очистить",
            key=f"vk_warmup_clear_{owner_id}_{assignment_id}",
            use_container_width=True,
        ):
            st.session_state.pop(state_key, None)
            st.session_state.pop(edit_key, None)
            st.rerun()

    st.caption(
        "Комментарий пока публикуется вручную из вашего VK. Неона ничего не отправляет автоматически."
    )

    _render_vk_comment_followup(
        owner_id,
        int(result.get("vk_user_id") or 0),
        int(result.get("post_id") or 0),
        str(comment or "").strip(),
        first_name,
        post_url=post_url,
        post_text=preview,
        source="warmup",
        assignment_id=int(assignment_id),
        ask_openai_fn=ask_openai_fn,
    )



def _render_vk_feed_radar(
    owner_id: int,
    ask_openai_fn=None,
) -> None:
    """Ручной тест нашей ленты из свежих постов отобранных Неонией людей."""
    state_key = f"vk_feed_radar_{int(owner_id)}"
    result = st.session_state.get(state_key)

    st.markdown("### 🌿 Радар свежих постов VK")
    st.caption(
        "Неона собирает до 10 самых свежих публичных постов людей, которых уже отобрала по ЦА. "
        "10 просмотренных постов не означают 10 комментариев: она выбирает только естественные касания."
    )

    cols = st.columns([2.2, 1])
    with cols[0]:
        if st.button(
            "🔎 Собрать 10 свежих постов",
            key=f"vk_feed_radar_run_{int(owner_id)}",
            type="primary",
            use_container_width=True,
        ):
            try:
                result = prepare_vk_feed_radar(
                    int(owner_id),
                    ask_openai_fn=ask_openai_fn,
                    count=10,
                )
                st.session_state[state_key] = result
                for idx, item in enumerate(result.get("recommendations") or []):
                    st.session_state[f"vk_feed_radar_comment_{int(owner_id)}_{idx}"] = str(
                        item.get("comment") or ""
                    )
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось собрать радар свежих постов VK: {exc}")
    with cols[1]:
        if isinstance(result, dict) and st.button(
            "🧹 Очистить",
            key=f"vk_feed_radar_clear_{int(owner_id)}",
            use_container_width=True,
        ):
            st.session_state.pop(state_key, None)
            for key in list(st.session_state.keys()):
                if str(key).startswith(f"vk_feed_radar_comment_{int(owner_id)}_"):
                    st.session_state.pop(key, None)
            st.rerun()

    if not isinstance(result, dict):
        st.caption(
            "Сейчас это безопасный тест нашей собственной ленты: Неона читает публичные стены "
            "отобранных людей и предлагает комментарии, сама в VK ничего не публикует."
        )
        return

    checked = int(result.get("checked") or 0)
    people = int(result.get("people") or 0)
    commentable = int(result.get("commentable") or 0)
    recommendations = list(result.get("recommendations") or [])

    st.caption(
        f"Свежих постов собрано: {checked} · авторов: {people} · "
        f"доступны для комментария: {commentable} · Неона выбрала: {len(recommendations)}"
    )

    message = str(result.get("message") or "").strip()
    if message and not recommendations:
        st.info(message)

    for idx, item in enumerate(recommendations):
        author = str(item.get("author_name") or "VK-контакт").strip()
        post_url = str(item.get("post_url") or "").strip()
        profile_url = str(item.get("profile_url") or "").strip()
        preview = str(item.get("material") or "").strip()
        signal = str(item.get("business_signal") or "").strip()
        reason = str(item.get("reason") or "").strip()
        edit_key = f"vk_feed_radar_comment_{int(owner_id)}_{idx}"

        with st.container(border=True):
            st.markdown(f"#### {author}")
            links = st.columns(2)
            with links[0]:
                if profile_url:
                    st.link_button("Открыть профиль", profile_url, use_container_width=True)
            with links[1]:
                if post_url:
                    st.link_button("Открыть пост", post_url, use_container_width=True)

            if signal:
                st.write(f"**Почему человек интересен:** {signal}")
            if reason:
                st.caption(f"Почему Неона выбрала этот пост: {reason}")
            if preview:
                short = preview if len(preview) <= 420 else preview[:417].rstrip() + "..."
                st.caption(f"Публикация: {short}")

            comment = st.text_area(
                "Комментарий Неоны",
                value=str(st.session_state.get(edit_key) or item.get("comment") or ""),
                key=edit_key,
                height=110,
            )

            _render_vk_comment_followup(
                int(owner_id),
                int(item.get("owner_id") or 0),
                int(item.get("post_id") or 0),
                str(comment or "").strip(),
                str(item.get("first_name") or ""),
                post_url=post_url,
                post_text=preview,
                source="radar",
                assignment_id=None,
                ask_openai_fn=ask_openai_fn,
            )

    st.caption(
        "Первый комментарий по-прежнему публикуете вы. После кнопки «Комментарий опубликован» "
        "Неона следит за этой веткой и подхватывает реальные ответы человека."
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

            _render_vk_warmup(owner_id, assignment_id, first_name, ask_openai_fn=ask_openai_fn)
            st.divider()

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

            _render_vk_material_followup(
                owner_id,
                assignment_id,
                first_name,
                status,
                scope="daily",
            )

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

            _render_vk_warmup(owner_id, assignment_id, first_name, ask_openai_fn=ask_openai_fn)
            st.divider()

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

            _render_vk_material_followup(
                owner_id,
                assignment_id,
                first_name,
                status,
                scope="known",
            )


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

    connected = bool(connection.get("connected"))
    needs_reconnect = bool(connection.get("needs_reconnect"))
    vk_ready = connected and not needs_reconnect
    auth_key = f"vk_scout_auth_url_{owner_id}"

    if vk_ready:
        st.success("🟢 VK Scout авторизован через VK ID")
        expires_at = str(connection.get("access_expires_at") or "").strip()
        if expires_at:
            st.caption("Access token обновляется worker автоматически. Ручная замена каждый час не нужна.")
    else:
        if connected and needs_reconnect:
            st.error("🔴 VK Scout нужно переподключить через VK ID")
            st.caption(
                "Сохранённый refresh token больше не действует. Это не ошибка Радара: "
                "нужно один раз заново подтвердить доступ VK, после чего новая пара токенов сохранится автоматически."
            )
            last_error = str(connection.get("last_error") or "").strip()
            if last_error:
                st.caption(f"Последний ответ VK: {last_error}")
            button_label = "🔄 Переподключить VK Scout"
            button_key = f"vk_scout_reauth_start_{owner_id}"
        else:
            st.info(
                "Чтобы читать участников публичных VK-сообществ, один раз авторизуйте "
                "VK Scout через ваш VK ID."
            )
            button_label = "🔐 Подключить VK Scout"
            button_key = f"vk_scout_auth_start_{owner_id}"

        if st.button(
            button_label,
            key=button_key,
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

    if vk_ready:
        _render_vk_feed_radar(
            owner_id,
            ask_openai_fn=ask_openai_fn,
        )
        st.divider()
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
