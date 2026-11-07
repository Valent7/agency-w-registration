from __future__ import annotations

import os
from datetime import datetime, timezone

import streamlit as st

from vk_scout_oauth import (
    begin_vk_scout_authorization,
    get_vk_scout_connection,
)

import vk_scout as _vk_scout
from vk_scout_worker import top_up_vk_owner_now

from vk_scout import (
    VKScoutError,
    _sb_get,
    _sb_patch,
    add_known_vk_contact,
    load_known_vk_contacts,
    load_today_vk_assignments,
    load_vk_sources,
    mark_vk_invited,
    prepare_vk_feed_links,
    prepare_vk_feed_manual_text,
    prepare_vk_invitation,
    prepare_vk_warmup_comment,
    register_published_vk_comment,
    load_vk_comment_thread,
    check_vk_comment_thread,
    send_pending_vk_comment_reply,
    process_manual_vk_comment_reply,
    mark_manual_vk_reply_published,
    load_manual_vk_private_dialog,
    process_manual_vk_private_reply,
    mark_manual_vk_private_reply_sent,
    mark_vk_partner,
    skip_vk_assignment,
    search_vk_communities,
    upsert_vk_source,
)

# Защита от краткого рассинхрона файлов во время Streamlit Cloud deploy:
# старый vk_scout.py не должен уронить всё Агентство W, пока новый файл
# ещё подхватывается контейнером.
load_vk_queue_progress = getattr(
    _vk_scout,
    "load_vk_queue_progress",
    lambda owner_id: {
        "source_name": "VK-сообщество",
        "community_id": None,
        "total": 0,
        "processed": 0,
        "remaining": 0,
        "today": len(load_today_vk_assignments(int(owner_id))),
        "completed_sources": 0,
        "sources_total": len(load_vk_sources(int(owner_id))),
        "complete": False,
    },
)
vk_queue_assignment_meta = getattr(
    _vk_scout,
    "vk_queue_assignment_meta",
    lambda value: {},
)

UTC = timezone.utc


VK_AGENCY_MATERIAL_URL = (
    "https://vk.ru/club241237638?act=s&id=241237638#section=short_videos"
)


def _render_vk_partner_mark(
    owner_id: int,
    assignment_id: int,
    first_name: str = "",
    *,
    scope: str,
) -> None:
    """Manual handoff: this VK person is already an Agency W partner."""
    if not assignment_id:
        return

    label = "✅ Стал партнёром — исключить из поиска"
    if st.button(
        label,
        key=f"vk_mark_partner_{scope}_{int(owner_id)}_{int(assignment_id)}",
        use_container_width=True,
    ):
        try:
            result = mark_vk_partner(int(assignment_id), int(owner_id))
            name = str(result.get("name") or first_name or "Человек").strip()
            st.success(
                f"✅ {name} отмечен как партнёр. Неония больше не будет "
                "включать этот VK-профиль в подбор кандидатов."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Не удалось отметить партнёра: {exc}")

    st.caption(
        "Нажимайте только после фактической регистрации человека. Отметка постоянная: "
        "Неония исключит этот VK-профиль из будущих подборок и Радара."
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


def _render_vk_private_dialog_followup(
    owner_id: int,
    assignment_id: int,
    first_name: str,
    status: str,
    *,
    scope: str,
) -> None:
    """Personal VK DM handoff: one pasted inbound turn -> the shared Neona brain."""
    if str(status or "").strip() not in {"invited", "dialogue", "meeting"} or not assignment_id:
        return

    st.divider()
    st.markdown("**💬 Личный диалог VK — Неона**")
    st.caption(
        "Первое сообщение уже отправлено вручную. Когда человек ответит в личном VK, "
        "вставьте сюда только его новую реплику. Неона помнит своё первое сообщение и "
        "дальше ведёт этот же разговор по тем же правилам, что в Telegram."
    )

    try:
        dialog = load_manual_vk_private_dialog(int(assignment_id), int(owner_id))
    except Exception as exc:
        st.warning(f"Не удалось открыть память VK-диалога: {exc}")
        return

    stage = str(dialog.get("stage") or "idle")
    if stage == "scheduled":
        st.success("📅 Встреча согласована Неоной и зафиксирована в календаре.")
    elif stage in {
        "invited_to_meeting",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "awaiting_slot_choice",
    }:
        st.info("📅 Неона уже ведёт согласование даты и времени встречи.")

    pending_inbound = str(dialog.get("pending_inbound") or "").strip()
    pending_reply = str(dialog.get("pending_reply") or "").strip()
    widget_scope = f"{scope}_{int(owner_id)}_{int(assignment_id)}"

    if pending_reply:
        if pending_inbound:
            st.markdown("**Последняя реплика человека:**")
            st.write(pending_inbound)

        st.markdown("**Ответ Неоны готов:**")
        edit_key = f"vk_private_neona_reply_{widget_scope}_{dialog.get('last_incoming_message_id') or 'pending'}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = pending_reply
        edited_reply = st.text_area(
            "Можно отредактировать перед отправкой в VK",
            key=edit_key,
            height=120,
            label_visibility="collapsed",
        )
        st.caption(
            "Скопируйте этот ответ в личный VK. После фактической отправки нажмите кнопку ниже — "
            "тогда Неона запомнит ход и будет ждать следующую реплику человека."
        )
        if st.button(
            "✅ Ответ Неоны отправлен в VK",
            key=f"vk_private_reply_sent_{widget_scope}",
            type="primary",
            use_container_width=True,
        ):
            try:
                mark_manual_vk_private_reply_sent(
                    int(assignment_id),
                    int(owner_id),
                    str(edited_reply or ""),
                )
                st.success("Ответ сохранён в памяти диалога. Неона ждёт следующую реплику.")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось сохранить отправленный ответ: {exc}")
        return

    inbound_key = f"vk_private_inbound_{widget_scope}"
    reset_key = f"vk_private_inbound_reset_{widget_scope}"
    if st.session_state.pop(reset_key, False):
        st.session_state[inbound_key] = ""

    st.text_area(
        "Новая реплика человека из личного VK",
        key=inbound_key,
        height=100,
        placeholder="Вставьте только последнее сообщение кандидата",
    )
    inbound_text = str(st.session_state.get(inbound_key, "") or "").strip()

    if st.button(
        "🧠 Передать Неоне",
        key=f"vk_private_to_neona_{widget_scope}",
        type="primary",
        use_container_width=True,
        disabled=not bool(inbound_text),
    ):
        try:
            process_manual_vk_private_reply(
                int(assignment_id),
                int(owner_id),
                inbound_text,
            )
            st.session_state[reset_key] = True
            st.rerun()
        except Exception as exc:
            st.error(f"Неона не смогла обработать ответ: {exc}")

    st.caption(
        "Важно: обычная личная страница VK сейчас не передаёт Агентству такие входящие "
        "сообщения автоматически. Поэтому новую реплику нужно вставить сюда; весь контекст, "
        "аргументацию и согласование встречи Неона ведёт сама."
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
    ui_scope: str = "",
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
    widget_scope = f"{thread_id}_{str(ui_scope).strip()}" if str(ui_scope).strip() else str(thread_id)
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

    mode_key = f"vk_manual_input_mode_{widget_scope}"
    if mode_key not in st.session_state:
        st.session_state[mode_key] = "text"
    mode = str(st.session_state.get(mode_key) or "text")

    st.markdown("**Что прислал человек**")
    mode_cols = st.columns(2)
    with mode_cols[0]:
        if st.button(
            "💬 Текст",
            key=f"vk_manual_mode_text_{widget_scope}",
            type="primary" if mode == "text" else "secondary",
            use_container_width=True,
        ):
            st.session_state[mode_key] = "text"
            st.rerun()
    with mode_cols[1]:
        if st.button(
            "😊 Смайл / реакция",
            key=f"vk_manual_mode_reaction_{widget_scope}",
            type="primary" if mode == "reaction" else "secondary",
            use_container_width=True,
        ):
            st.session_state[mode_key] = "reaction"
            st.rerun()

    inbound_key = f"vk_manual_inbound_{widget_scope}"
    reaction_choice_key = f"vk_manual_reaction_choice_{widget_scope}"
    custom_reaction_key = f"vk_manual_custom_reaction_{widget_scope}"
    reset_inbound_key = f"vk_manual_reset_inbound_{widget_scope}"
    reset_custom_key = f"vk_manual_reset_custom_{widget_scope}"

    # Streamlit forbids changing a widget value after that widget has already
    # been instantiated in the same run. We therefore schedule clearing for
    # the next rerun, before the widget is created.
    if st.session_state.pop(reset_inbound_key, False):
        st.session_state[inbound_key] = ""
    if st.session_state.pop(reset_custom_key, False):
        st.session_state[custom_reaction_key] = ""

    if mode == "text":
        st.text_area(
            "Ответ человека",
            key=inbound_key,
            height=100,
            placeholder="Вставьте точную реплику из VK — например: нейробабушка прикольно",
        )
        inbound_value = str(st.session_state.get(inbound_key, "") or "").strip()
        event_kind = "comment"
    else:
        reaction = st.selectbox(
            "Смайл / реакция",
            options=("👍", "❤️", "😂", "😊", "👏", "🔥", "🤔", "😮", "😢", "😡", "Другая"),
            key=reaction_choice_key,
        )
        if reaction == "Другая":
            st.text_input(
                "Вставьте смайл или коротко опишите реакцию",
                key=custom_reaction_key,
                placeholder="Например: 🙏  или  поставил сердечко",
            )
            inbound_value = str(st.session_state.get(custom_reaction_key, "") or "").strip()
        else:
            inbound_value = str(reaction or "").strip()
        event_kind = "reaction"
        st.caption(
            "Неона воспримет это как реакцию человека и не будет приписывать смайлу лишний смысл."
        )

    if st.button(
        "🧠 Передать Неоне",
        key=f"vk_manual_to_neona_{widget_scope}",
        type="primary",
        use_container_width=True,
    ):
        try:
            process_manual_vk_comment_reply(
                thread_id,
                inbound_value,
                event_kind=event_kind,
                ask_openai_fn=ask_openai_fn,
            )
            if event_kind == "comment":
                st.session_state[reset_inbound_key] = True
            elif st.session_state.get(reaction_choice_key) == "Другая":
                st.session_state[reset_custom_key] = True
            st.rerun()
        except Exception as exc:
            st.error(f"Неона не смогла обработать ответ: {exc}")

    last_inbound = str(thread.get("last_inbound_text") or "").strip()
    pending_kind = str(thread.get("pending_event_kind") or "").strip()
    if last_inbound and pending_kind in {"manual_comment", "manual_reaction"}:
        if pending_kind == "manual_reaction":
            st.markdown("**Последняя реакция человека:**")
        else:
            st.markdown("**Последняя реплика человека:**")
        st.write(last_inbound)

    reply_text = str(thread.get("neona_reply_text") or "").strip()
    if pending_kind in {"manual_comment", "manual_reaction"} and reply_text:
        st.markdown("**Ответ Неоны готов:**")
        edit_key = f"vk_manual_neona_reply_{widget_scope}_{thread.get('pending_event_key') or 'pending'}"
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
            key=f"vk_manual_reply_published_{widget_scope}",
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


def _load_vk_dialog_threads(owner_id: int, limit: int = 100) -> list[dict]:
    """Loads saved VK comment threads independently of today's Neonia lists."""
    rows = _sb_get(
        "agency_vk_comment_threads",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "select": "*",
            "order": "updated_at.desc.nullslast,created_at.desc",
            "limit": max(1, min(300, int(limit))),
        },
    )
    return [row for row in rows if isinstance(row, dict)]


def _vk_thread_label(thread: dict) -> str:
    name = str(thread.get("first_name") or "").strip() or "VK-контакт"
    pending_kind = str(thread.get("pending_event_kind") or "").strip()
    reply_ready = bool(str(thread.get("neona_reply_text") or "").strip()) and pending_kind in {
        "manual_comment",
        "manual_reaction",
    }
    last_inbound = str(thread.get("last_inbound_text") or "").strip()
    if reply_ready:
        status = "🟠 ответ Неоны готов"
    elif last_inbound:
        status = "🟢 диалог идёт"
    else:
        status = "🟡 ждём ответа"
    return f"{name} · {status}"


def _render_vk_dialog_center(
    owner_id: int,
    ask_openai_fn=None,
) -> None:
    """Persistent VK dialogue desk for all saved comment threads."""
    st.markdown("### 💬 Диалоги VK")
    st.caption(
        "Здесь остаются все начатые ветки после опубликованных комментариев. "
        "Они не исчезают, даже если человек больше не попал в сегодняшнюю подборку Неонии."
    )

    try:
        threads = _load_vk_dialog_threads(int(owner_id))
    except Exception as exc:
        st.warning(f"Не удалось загрузить сохранённые VK-диалоги: {exc}")
        return

    active_threads = [
        thread for thread in threads
        if str(thread.get("status") or "").strip().casefold() != "closed"
    ]
    if not active_threads:
        st.info("Пока нет сохранённых VK-диалогов. Они появятся после кнопки «Комментарий опубликован».")
        st.caption("📚 История диалогов пока пуста.")
        return

    waiting = sum(
        1 for thread in active_threads
        if not str(thread.get("last_inbound_text") or "").strip()
    )
    replied = len(active_threads) - waiting
    ready = sum(
        1 for thread in active_threads
        if str(thread.get("pending_event_kind") or "").strip() in {"manual_comment", "manual_reaction"}
        and str(thread.get("neona_reply_text") or "").strip()
    )
    st.caption(
        f"Активных веток: {len(active_threads)} · ответили: {replied} · "
        f"ждём ответа: {waiting} · ответ Неоны готов: {ready}"
    )

    current_threads = active_threads[:20]
    history_threads = active_threads[20:]

    st.caption(
        f"На рабочем экране: {len(current_threads)} · "
        f"в истории: {len(history_threads)}"
    )

    def _render_vk_dialog_thread(thread: dict) -> None:
        thread_id = int(thread.get("id") or 0)
        post_owner_id = int(thread.get("post_owner_id") or 0)
        post_id = int(thread.get("post_id") or 0)
        if not thread_id or not post_owner_id or not post_id:
            return

        with st.expander(_vk_thread_label(thread), expanded=False):
            post_url = str(thread.get("post_url") or "").strip()
            if post_url:
                st.link_button("Открыть пост VK", post_url, use_container_width=False)

            history = thread.get("dialogue_history")
            if isinstance(history, list) and history:
                st.markdown("**История ветки:**")
                for event in history:
                    if not isinstance(event, dict):
                        continue
                    body = str(event.get("text") or "").strip()
                    if not body:
                        continue
                    direction = str(event.get("direction") or "").strip()
                    who = "Неона / вы" if direction == "out" else "Человек"
                    st.markdown(f"**{who}:** {body}")

            thread_assignment_id = (
                int(thread.get("assignment_id"))
                if thread.get("assignment_id") not in (None, "")
                else 0
            )
            if thread_assignment_id:
                _render_vk_partner_mark(
                    int(owner_id),
                    thread_assignment_id,
                    str(thread.get("first_name") or "").strip(),
                    scope=f"dialog_{thread_id}",
                )
                st.divider()

            _render_vk_comment_followup(
                int(owner_id),
                post_owner_id,
                post_id,
                str(thread.get("initial_comment_text") or "").strip(),
                str(thread.get("first_name") or "").strip(),
                post_url=post_url,
                post_text=str(thread.get("post_text") or "").strip(),
                source=str(thread.get("source") or "radar").strip() or "radar",
                assignment_id=(
                    int(thread.get("assignment_id"))
                    if thread.get("assignment_id") not in (None, "")
                    else None
                ),
                ask_openai_fn=ask_openai_fn,
                ui_scope="center",
            )

    for thread in current_threads:
        _render_vk_dialog_thread(thread)

    history_key = f"vk_dialog_history_open_{int(owner_id)}"
    history_open = bool(st.session_state.get(history_key, False))
    label = (
        f"📚 История диалогов ({len(history_threads)}) · "
        + ("Скрыть" if history_open else "Показать")
    )

    if st.button(
        label,
        key=f"vk_dialog_history_toggle_{int(owner_id)}",
        use_container_width=True,
        disabled=not bool(history_threads),
    ):
        st.session_state[history_key] = not history_open
        st.rerun()

    if history_threads and history_open:
        st.caption(
            "Здесь сохранены более старые VK-диалоги. "
            "Если человек снова отвечает, ветка обновляется и автоматически "
            "поднимается в текущие 20."
        )
        for thread in history_threads:
            _render_vk_dialog_thread(thread)
    elif not history_threads:
        st.caption("Сейчас в истории нет дополнительных VK-диалогов.")


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
    """No-OAuth helper for comments under posts copied from the user's VK feed."""
    state_key = f"vk_feed_links_{int(owner_id)}"
    result = st.session_state.get(state_key)

    st.markdown("### 🌿 Лента VK · комментарии для утепления")
    st.caption(
        "VK больше не даёт новым приложениям читать персональную ленту автоматически. "
        "Поэтому здесь нет VK ID и нет заблокированного приложения: откройте свою обычную "
        "ленту VK, скопируйте ссылки на 1–5 подходящих постов — Неона сама прочитает их "
        "через уже работающий сервисный доступ и подготовит комментарии."
    )

    st.link_button(
        "🌐 Открыть мою ленту VK",
        "https://vk.com/feed",
        use_container_width=True,
    )

    raw_key = f"vk_feed_links_input_{int(owner_id)}"
    raw = st.text_area(
        "Ссылки на посты, клипы или видео из моей ленты VK",
        key=raw_key,
        height=150,
        placeholder=(
            "Вставьте по одной ссылке на строку, например:\n"
            "https://vk.com/wall-123456_789\n"
            "https://vk.ru/clip-227478486_456239079"
        ),
        help="Можно вставить от 1 до 5 ссылок на обычные публичные посты, клипы или видео из Вашей ленты.",
    )

    cols = st.columns([2.2, 1])
    with cols[0]:
        if st.button(
            "💬 Подготовить комментарии",
            key=f"vk_feed_links_run_{int(owner_id)}",
            type="primary",
            use_container_width=True,
        ):
            values = [
                line.strip()
                for line in str(raw or "").replace(",", "\n").splitlines()
                if line.strip()
            ]
            try:
                with st.spinner("Неона читает выбранные посты и пишет комментарии…"):
                    result = prepare_vk_feed_links(
                        int(owner_id),
                        values,
                        ask_openai_fn=ask_openai_fn,
                        limit=5,
                    )
                st.session_state[state_key] = result
                for idx, item in enumerate(result.get("recommendations") or []):
                    st.session_state[f"vk_feed_link_comment_{int(owner_id)}_{idx}"] = str(
                        item.get("comment") or ""
                    )
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось прочитать выбранные посты VK: {exc}")
    with cols[1]:
        if isinstance(result, dict) and st.button(
            "🧹 Очистить",
            key=f"vk_feed_links_clear_{int(owner_id)}",
            use_container_width=True,
        ):
            st.session_state.pop(state_key, None)
            st.session_state[raw_key] = ""
            for key in list(st.session_state.keys()):
                if str(key).startswith(f"vk_feed_link_comment_{int(owner_id)}_"):
                    st.session_state.pop(key, None)
            st.rerun()

    if not isinstance(result, dict):
        st.info(
            "Два шага: 1) откройте свою ленту VK; 2) вставьте сюда ссылки на посты, "
            "которые хотите прокомментировать. Никакой повторной авторизации VK не требуется."
        )
        return

    message = str(result.get("message") or "").strip()
    recommendations = list(result.get("recommendations") or [])
    invalid = list(result.get("invalid") or [])

    if invalid:
        st.warning(
            "Не удалось распознать некоторые строки как ссылки на VK-посты: "
            + "; ".join(invalid[:3])
        )
    if message and not recommendations:
        st.info(message)

        # Some feed items are visible to the logged-in user but hidden from the
        # server-side VK API. In that case keep the original link for navigation
        # and let Neona work from text the user can actually see.
        first_link = ""
        for line in str(raw or "").splitlines():
            if line.strip():
                first_link = line.strip()
                break
        if first_link:
            manual_key = f"vk_feed_manual_text_{int(owner_id)}"
            manual_text = st.text_area(
                "Текст или подпись выбранной публикации",
                key=manual_key,
                height=150,
                placeholder=(
                    "Скопируйте сюда текст поста или подпись к клипу/видео, "
                    "которые Вы видите в VK."
                ),
            )
            if st.button(
                "💬 Подготовить комментарий по тексту",
                key=f"vk_feed_manual_run_{int(owner_id)}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    manual_result = prepare_vk_feed_manual_text(
                        first_link,
                        manual_text,
                        ask_openai_fn=ask_openai_fn,
                    )
                    st.session_state[state_key] = manual_result
                    for idx, item in enumerate(manual_result.get("recommendations") or []):
                        st.session_state[
                            f"vk_feed_link_comment_{int(owner_id)}_{idx}"
                        ] = str(item.get("comment") or "")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Не удалось подготовить комментарий по тексту: {exc}")

    for idx, item in enumerate(recommendations):
        author = str(item.get("author_name") or "VK").strip()
        post_url = str(item.get("post_url") or "").strip()
        profile_url = str(item.get("profile_url") or "").strip()
        preview = str(item.get("material") or "").strip()
        reason = str(item.get("reason") or "").strip()
        source_type = str(item.get("source_type") or "").strip()
        edit_key = f"vk_feed_link_comment_{int(owner_id)}_{idx}"

        with st.container(border=True):
            st.markdown(f"#### {author}")
            if source_type:
                st.caption("Человек" if source_type == "person" else "Сообщество")

            links = st.columns(2)
            with links[0]:
                if profile_url:
                    st.link_button("Открыть автора", profile_url, use_container_width=True)
            with links[1]:
                if post_url:
                    st.link_button("Открыть пост VK", post_url, use_container_width=True)

            if reason:
                st.caption(f"Почему здесь уместен комментарий: {reason}")
            if preview:
                short = preview if len(preview) <= 520 else preview[:517].rstrip() + "..."
                st.caption(f"Публикация: {short}")

            comment = st.text_area(
                "Комментарий Неоны",
                value=str(st.session_state.get(edit_key) or item.get("comment") or ""),
                key=edit_key,
                height=110,
            )

            content_kind = str(item.get("content_kind") or "wall").strip().casefold()
            if content_kind == "wall":
                _render_vk_comment_followup(
                    int(owner_id),
                    int(item.get("owner_id") or 0),
                    int(item.get("post_id") or 0),
                    str(comment or "").strip(),
                    author,
                    post_url=post_url,
                    post_text=preview,
                    source="personal_feed_link",
                    assignment_id=None,
                    ask_openai_fn=ask_openai_fn,
                    ui_scope=f"personal_feed_link_{idx}",
                )
            else:
                st.caption(
                    "Для клипа/видео: скопируйте комментарий и опубликуйте его под роликом. "
                    "Наблюдение за веткой ответов пока включено только для обычных VK-постов."
                )

    if recommendations:
        st.success(
            "✅ Комментарии подготовлены по постам из Вашей настоящей ленты VK — "
            "без VK ID и без заблокированного приложения."
        )


def _render_today_vk_candidates(
    owner_id: int,
    member_code: str,
    ask_openai_fn=None,
) -> None:
    """Показывает владельцу его сегодняшнюю VK-пятёрку и действия Неоны."""
    st.markdown("### 💙 VK 5 на сегодня · отбор Неонии")

    try:
        assignments = load_today_vk_assignments(owner_id)
        progress = load_vk_queue_progress(owner_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить VK-очередь: {exc}")
        return

    if isinstance(progress, dict):
        with st.container(border=True):
            st.markdown("#### 📊 Очередь VK")
            st.caption(
                "Текущий источник: "
                + str(progress.get("source_name") or "VK-сообщество")
            )
            available_value = progress.get("available")
            remaining_value = progress.get("remaining")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Всего в VK", int(progress.get("total") or 0))
            c2.metric(
                "Доступно Неонии",
                (
                    int(available_value)
                    if available_value is not None
                    else "—"
                ),
            )
            c3.metric("Отработано", int(progress.get("processed") or 0))
            c4.metric(
                "Осталось",
                (
                    int(remaining_value)
                    if remaining_value is not None
                    else "—"
                ),
            )
            c5.metric("Сегодня", int(progress.get("today") or 0))
            st.caption(
                "«Доступно Неонии» — реальные профили этого сообщества, которые "
                "можно передать в работу после технических исключений. "
                "Показан один раз = больше не возвращается в ежедневную пятёрку."
            )
            capacity_error = str(
                progress.get("capacity_error") or ""
            ).strip()
            if capacity_error and available_value is None:
                st.caption(
                    "VK пока не дал полный расчёт доступных профилей. "
                    "Очередь продолжит работать, а число будет пересчитано автоматически."
                )

    today_count = int(progress.get("today") or len(assignments)) if isinstance(progress, dict) else len(assignments)
    st.caption(
        f"Сегодня выдано: {today_count}/5"
        + (
            f" · активных карточек сейчас: {len(assignments)}"
            if len(assignments) != today_count
            else ""
        )
    )

    if today_count < 5:
        st.caption(
            "Если текущих кандидатов мало или прошлые не отвечают, можно не ждать "
            "фонового цикла — Неония сразу продолжит поиск по Вашим VK-источникам."
        )
        if st.button(
            "🔄 Добрать кандидатов до 5",
            key=f"vk_top_up_now_{int(owner_id)}",
            type="primary",
            use_container_width=True,
        ):
            try:
                with st.spinner("Неония проверяет следующие VK-профили и добирает пятёрку…"):
                    result = top_up_vk_owner_now(
                        int(owner_id),
                        str(member_code or "").strip(),
                        daily_limit=5,
                        min_score=50,
                        analysis_limit=30,
                    )
                count = int(result.get("count") or 0)
                if count >= 5:
                    st.success("✅ Готово: сегодняшняя VK-пятёрка собрана 5/5.")
                else:
                    st.info(
                        f"Неония продолжила поиск. Сейчас собрано {count}/5; "
                        f"проверено дополнительно: {int(result.get('analyzed') or 0)}."
                    )
                st.rerun()
            except Exception as exc:
                error_text = str(exc or "")
                lowered = error_text.casefold()
                if (
                    "openai временно недоступен" in lowered
                    or "503" in lowered
                    or "502" in lowered
                    or "429" in lowered
                ):
                    st.warning(
                        "OpenAI временно не ответил. Очередь VK сохранена — "
                        "нажмите «Добрать кандидатов до 5» ещё раз."
                    )
                else:
                    st.error(f"Не удалось выполнить добор VK-кандидатов: {error_text}")

    if not assignments:
        st.info(
            "Сегодня качественных VK-кандидатов пока нет. Неония продолжает "
            "проверять людей из Ваших настроенных VK-сообществ по портрету ЦА."
        )
        return

    for item in assignments:
        assignment_id = int(item.get("id") or 0)
        position = int(item.get("daily_position") or 0)
        first_name = str(item.get("first_name") or "").strip()
        last_name = str(item.get("last_name") or "").strip()
        full_name = " ".join(x for x in (first_name, last_name) if x) or "VK-кандидат"
        queue_meta = vk_queue_assignment_meta(item)
        city = str(item.get("city_name") or "").strip()
        country = str(item.get("country_name") or "").strip()
        place = ", ".join(x for x in (city, country) if x)
        source_name = str(
            queue_meta.get("community_name")
            or "VK-сообщество"
        ).strip()
        profile_url = str(item.get("profile_url") or "").strip()
        status = str(item.get("status") or "reserved").strip()
        invitation_text = str(item.get("invitation_text") or "").strip()
        auto_neona_link = bool(
            invitation_text
            and (
                "vk.me/club" in invitation_text
                or "ref_source=agency_w" in invitation_text
            )
        )

        title = f"{position}. {full_name}" if position else full_name
        with st.container(border=True):
            st.markdown(f"#### {title}")
            meta = [f"источник: {source_name}"]
            if place:
                meta.append(place)
            st.caption(" · ".join(meta))
            score = int(item.get("score") or 0)
            st.write(
                "**Почему сегодня:** профиль прошёл отбор Неонии по Вашей ЦА"
                + (f" · оценка {score}/100." if score else ".")
            )

            if profile_url:
                st.link_button(
                    "Открыть профиль VK",
                    profile_url,
                    use_container_width=False,
                )

            if status == "skipped":
                st.info(
                    "⏭ Кандидат пропущен. Он уже считается отработанным "
                    "и повторно в ежедневную пятёрку не вернётся."
                )
                continue

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

                if auto_neona_link:
                    st.caption(
                        "🔗 Персональная ссылка в конце сообщения — это автоматический вход к Неоне. "
                        "После первого сообщения человека в открывшемся диалоге Неона продолжит сама."
                    )
                elif assignment_id and status != "invited":
                    st.warning(
                        "У этого ранее подготовленного сообщения ещё нет автоматического перехода к Неоне."
                    )
                    if st.button(
                        "🔄 Добавить автоматический переход к Неоне",
                        key=f"vk_restore_neona_bridge_{owner_id}_{assignment_id}",
                        type="primary",
                        use_container_width=False,
                    ):
                        try:
                            prepare_vk_invitation(
                                assignment_id,
                                member_code,
                                ask_openai_fn=ask_openai_fn,
                            )
                            st.success("Автоматический переход к Неоне добавлен.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось обновить сообщение: {exc}")
                elif status == "invited":
                    st.caption(
                        "ℹ️ Это сообщение уже было отправлено без автоматического перехода. "
                        "Для него ниже остаётся резервный ручной режим."
                    )
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

            if assignment_id:
                st.divider()
                _render_vk_partner_mark(
                    owner_id,
                    assignment_id,
                    first_name,
                    scope="daily",
                )

            if status == "invited" and not auto_neona_link:
                _render_vk_private_dialog_followup(
                    owner_id,
                    assignment_id,
                    first_name,
                    status,
                    scope="daily",
                )
            _render_vk_material_followup(
                owner_id,
                assignment_id,
                first_name,
                status,
                scope="daily",
            )

    st.caption(
        "Первое сообщение отправляется из личного VK вручную. В конце текста есть персональная "
        "ссылка: человек открывает её и пишет ответ — после этого Неона автоматически подхватывает "
        "диалог и ведёт его дальше до встречи."
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
        auto_neona_link = bool(
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

                if auto_neona_link:
                    st.caption(
                        "🔗 Персональная ссылка в конце сообщения — автоматический вход к Неоне. "
                        "После первого сообщения человека в открывшемся диалоге Неона продолжит сама."
                    )
                elif assignment_id and status != "invited":
                    st.warning(
                        "У этого ранее подготовленного сообщения ещё нет автоматического перехода к Неоне."
                    )
                    if st.button(
                        "🔄 Добавить автоматический переход к Неоне",
                        key=f"vk_known_restore_neona_bridge_{owner_id}_{assignment_id}",
                        type="primary",
                        use_container_width=False,
                    ):
                        try:
                            prepare_vk_invitation(
                                assignment_id,
                                member_code,
                                ask_openai_fn=ask_openai_fn,
                            )
                            st.success("Автоматический переход к Неоне добавлен.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось обновить сообщение: {exc}")
                elif status == "invited":
                    st.caption(
                        "ℹ️ Это сообщение уже было отправлено без автоматического перехода. "
                        "Для него ниже остаётся резервный ручной режим."
                    )
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

            if assignment_id:
                st.divider()
                _render_vk_partner_mark(
                    owner_id,
                    assignment_id,
                    first_name,
                    scope="known",
                )

            if status == "invited" and not auto_neona_link:
                _render_vk_private_dialog_followup(
                    owner_id,
                    assignment_id,
                    first_name,
                    status,
                    scope="known",
                )
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
        "Добавьте публичные VK-сообщества в нужном порядке. Неония отрабатывает "
        "участников последовательно: 5 новых человек в день, без повторов и без "
        "отбора по ЦА. Закончилось одно сообщество — очередь переходит к следующему."
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
    service_mode = str(os.getenv("VK_SCOUT_SERVICE_MODE", "") or "").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    vk_ready = (connected and not needs_reconnect) or service_mode
    auth_key = f"vk_scout_auth_url_{owner_id}"

    if service_mode:
        st.success("🟢 Поиск кандидатов VK подключён через сервисный ключ")
        st.caption(
            "VK 5/5 работает через сервисный доступ. Заблокированное приложение VK ID "
            "больше не используется и повторная авторизация не требуется."
        )
        st.info(
            "🌿 Для комментариев в Вашей обычной ленте используйте блок «Лента VK» ниже: "
            "откройте ленту, скопируйте ссылки на 1–5 постов и передайте их Неоне."
        )
    elif vk_ready:
        st.success("🟢 VK Scout авторизован через VK ID")
        expires_at = str(connection.get("access_expires_at") or "").strip()
        if expires_at:
            st.caption("Access token обновляется worker автоматически. Ручная замена каждый час не нужна.")
    else:
        blocked_client = False
        if connected and needs_reconnect:
            last_error = str(connection.get("last_error") or "").strip()
            blocked_client = "client is blocked" in last_error.casefold()
            if blocked_client:
                st.error("🔴 Приложение VK ID, через которое работает VK Scout, заблокировано")
                st.caption(
                    "Ваш личный профиль VK не заблокирован. Повторное нажатие "
                    "«Переподключить» не поможет: сначала нужно восстановить само "
                    "приложение VK ID или заменить его рабочим приложением."
                )
                if last_error:
                    st.caption(f"Последний ответ VK: {last_error}")
            else:
                st.error("🔴 VK Scout нужно переподключить через VK ID")
                st.caption(
                    "Сохранённый refresh token больше не действует. Это не ошибка Радара: "
                    "нужно один раз заново подтвердить доступ VK, после чего новая пара токенов сохранится автоматически."
                )
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

        if not blocked_client:
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
        _render_vk_dialog_center(
            owner_id,
            ask_openai_fn=ask_openai_fn,
        )
        st.divider()
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
            "Пока нет ни одного источника. Добавьте первое VK-сообщество — "
            "после этого Неония начнёт последовательную очередь участников."
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
        "Фоновый worker продолжает очередь автоматически. Вступать в открытое "
        "сообщество ради этой очереди не требуется: используется доступный VK API список участников."
    )


def render_today_vk_candidates(
    owner_id: int,
    member_code: str = "",
    ask_openai_fn=None,
) -> None:
    """Public compact entry for the Agency W Day page."""
    _render_today_vk_candidates(
        int(owner_id),
        str(member_code or ""),
        ask_openai_fn=ask_openai_fn,
    )
