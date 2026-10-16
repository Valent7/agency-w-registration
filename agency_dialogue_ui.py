"""Streamlit editor for two speaking characters, with explicit HeyGen payment gate."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st
import requests

from agency_dialogue_video import (
    SPEAKER_LABELS,
    assemble_dialogue_mp4,
    create_dialogue_video,
    dialogue_errors,
    dialogue_text,
    duration_warning,
    estimate_dialogue_seconds,
    list_heygen_catalog,
    suggest_partner_dialogue_cast,
    partner_dialogue_catalog,
    parse_dialogue_editor,
    validate_cast,
)


def _secret(*names: str) -> str:
    for name in names:
        value = st.secrets.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _cast_defaults(item: dict) -> dict:
    previous = item.get("dialogue_cast") or {}
    female_avatar = _secret(
        "HEYGEN_DIALOGUE_AVATAR_B_ID",
        "HEYGEN_NEONA_AVATAR_ID", "HEYGEN_AVATAR_ID", "NEONA_HEYGEN_AVATAR_ID"
    )
    female_voice = _secret(
        "HEYGEN_DIALOGUE_VOICE_B_ID",
        "HEYGEN_NEONA_VOICE_ID", "HEYGEN_VOICE_ID", "NEONA_HEYGEN_VOICE_ID"
    )
    return {
        "A": {
            "avatar_id": str((previous.get("A") or {}).get("avatar_id") or _secret("HEYGEN_DIALOGUE_AVATAR_A_ID")).strip(),
            "voice_id": str((previous.get("A") or {}).get("voice_id") or _secret("HEYGEN_DIALOGUE_VOICE_A_ID")).strip(),
        },
        "B": {
            "avatar_id": str((previous.get("B") or {}).get("avatar_id") or female_avatar).strip(),
            "voice_id": str((previous.get("B") or {}).get("voice_id") or female_voice).strip(),
        },
    }


def _cast_controls(prefix: str, item: dict) -> dict:
    """Friendly, private-by-default HeyGen selector; no technical ID fields."""
    st.markdown("### 4. Выберите героев и голоса")
    st.caption(
        "Начнём с предложенных героев и русских голосов. "
        "Если они подходят — ничего менять не нужно."
    )
    previous = _cast_defaults(item)
    api_key = _secret("HEYGEN_API_KEY", "HEYGEN_KEY")
    if not api_key:
        st.error("HeyGen не подключён администратором Агентства.")
        return {"A": {"avatar_id": "", "voice_id": ""},
                "B": {"avatar_id": "", "voice_id": ""}}

    catalog_key = f"{prefix}_catalog"
    if catalog_key not in st.session_state:
        with st.spinner("Загружаем героев и голоса..."):
            st.session_state[catalog_key] = list_heygen_catalog(api_key)
    catalog = st.session_state[catalog_key]
    if not isinstance(catalog, dict) or not catalog.get("ok"):
        st.warning("Не удалось открыть библиотеку HeyGen. Попробуйте обновить каталог.")
        if isinstance(catalog, dict) and catalog.get("error"):
            st.caption(str(catalog["error"]))
        if st.button("🔄 Обновить каталог", key=f"{prefix}_retry_catalog"):
            st.session_state.pop(catalog_key, None)
            st.rerun()
        return {"A": {"avatar_id": "", "voice_id": ""},
                "B": {"avatar_id": "", "voice_id": ""}}

    avatars = catalog.get("avatars") or []
    voices = catalog.get("voices") or []
    suggested = suggest_partner_dialogue_cast(catalog, preferred=previous)
    cast = {"A": {}, "B": {}}

    with st.expander("🎭 Выбрать аватар и голос", expanded=True):
        for speaker, title, initial_gender in (
            ("A", "Первый герой", "Мужской"),
            ("B", "Второй герой", "Женский"),
        ):
            st.markdown(f"**{title}**")
            gender_label = st.radio(
                f"Пол персонажа — {title.lower()}",
                ["Мужской", "Женский"],
                key=f"{prefix}_{speaker}_gender",
                horizontal=True,
                index=0 if initial_gender == "Мужской" else 1,
            )
            gender = "male" if gender_label == "Мужской" else "female"
            avatar_mode = st.radio(
                f"Аватар — {title.lower()}",
                ["Из библиотеки HeyGen", "Мой аватар в HeyGen", "Мой аватар (загрузить)"],
                key=f"{prefix}_{speaker}_avatar_mode",
                horizontal=True,
            )
            entries = [
                a for a in avatars
                if str(a.get("gender") or "").lower() in (
                    gender, "man" if gender == "male" else "woman",
                    "мужчина" if gender == "male" else "женщина",
                ) and a.get("id")
            ]
            # The partnership example must not silently use an Agency employee
            # as the prospective partner in the first role.
            if speaker == "A":
                staff = ("тео", "theo", "стагирит", "stagirit", "неона",
                         "neona", "неония", "neonia", "неола", "neola")
                entries = [
                    a for a in entries
                    if not any(name in str(a.get("name") or "").lower() for name in staff)
                ]
            if avatar_mode == "Мой аватар в HeyGen":
                entries = [a for a in entries if a.get("is_private")]
            entries.sort(key=lambda a: str(a.get("name") or "").casefold())
            avatar_key = f"{prefix}_{speaker}_avatar_id"
            if avatar_mode == "Мой аватар (загрузить)":
                upload = st.file_uploader(
                    "Загрузить своё фото (JPG или PNG)",
                    type=["png", "jpg", "jpeg"],
                    key=f"{prefix}_{speaker}_avatar_upload",
                )
                if upload:
                    st.image(upload, width=160)
                    st.info(
                        "Фото принято для подготовки. Чтобы оно заговорило, "
                        "сначала создайте личный аватар в HeyGen и подтвердите "
                        "согласие человека. Затем выберите «Мой аватар в HeyGen»."
                    )
                else:
                    st.caption("Добавьте фото человека, с его разрешения.")
                st.session_state.pop(avatar_key, None)
                cast[speaker]["avatar_id"] = ""
            else:
                options = [""] + [a["id"] for a in entries]
                best = suggested[speaker]["avatar_id"]
                if avatar_key not in st.session_state or st.session_state[avatar_key] not in options:
                    st.session_state[avatar_key] = best if best in options else ""
                mapping = {a["id"]: str(a.get("name") or "Персонаж") for a in entries}
                cast[speaker]["avatar_id"] = st.selectbox(
                    "Выбрать аватар", options, key=avatar_key,
                    format_func=lambda ident, names=mapping: names.get(ident, "Выберите персонажа"),
                )
                if not entries:
                    st.warning("В этой категории нет аватаров. Выберите библиотеку или другой пол.")

            voice_mode = st.radio(
                f"Голос — {title.lower()}",
                ["Из библиотеки HeyGen", "Мой голос в HeyGen", "Мой голос (загрузить)"],
                key=f"{prefix}_{speaker}_voice_mode", horizontal=True,
            )
            voice_entries = [
                v for v in voices if v.get("id")
                and str(v.get("gender") or "").lower() in (
                    gender, "man" if gender == "male" else "woman",
                    "мужчина" if gender == "male" else "женщина",
                )
                and str(v.get("language") or "").lower().replace("_", "-").strip()
                    in ("ru", "ru-ru", "russian", "русский", "русская")
            ]
            if voice_mode == "Мой голос в HeyGen":
                voice_entries = [v for v in voice_entries if v.get("is_private")]
            voice_entries.sort(key=lambda v: str(v.get("name") or "").casefold())
            voice_key = f"{prefix}_{speaker}_voice_id"
            if voice_mode == "Мой голос (загрузить)":
                audio = st.file_uploader(
                    "Загрузить образец своего голоса (MP3, WAV или M4A)",
                    type=["mp3", "wav", "m4a"],
                    key=f"{prefix}_{speaker}_voice_upload",
                )
                if audio:
                    st.audio(audio)
                    st.info(
                        "Запись подготовлена. Загрузка не создаёт голосовой клон: "
                        "нужно обучить свой голос в HeyGen с подтверждением согласия. "
                        "После этого выберите «Мой голос в HeyGen»."
                    )
                st.session_state.pop(voice_key, None)
                cast[speaker]["voice_id"] = ""
            else:
                options = [""] + [v["id"] for v in voice_entries]
                best = suggested[speaker]["voice_id"]
                if voice_key not in st.session_state or st.session_state[voice_key] not in options:
                    st.session_state[voice_key] = best if best in options else ""
                mapping = {v["id"]: str(v.get("name") or "Голос") for v in voice_entries}
                cast[speaker]["voice_id"] = st.selectbox(
                    "Выбрать русский голос", options, key=voice_key,
                    format_func=lambda ident, names=mapping: names.get(ident, "Выберите голос"),
                )
                if not voice_entries:
                    st.warning("Подходящего русского голоса пока нет в библиотеке.")
            if speaker == "A":
                st.divider()

    if not validate_cast(cast):
        st.success("✅ Герои и голоса выбраны. Можно запускать видеопробу.")
    else:
        st.caption(
            "Для генерации выберите по одному действующему аватару и голосу на роль. "
            "Загруженные фото и аудио нельзя использовать до регистрации в HeyGen."
        )
    if st.button("🔄 Обновить библиотеку HeyGen", key=f"{prefix}_refresh_cast"):
        st.session_state.pop(catalog_key, None)
        st.rerun()
    return cast


def _rewrite_dialogue(item: dict, ask_ai_fn) -> list[dict[str, str]]:
    """New text only: no paid images, video or external casting."""
    from content_factory_ui import _json_from_answer

    old = dialogue_text(item.get("dialogue") or [])
    prompt = (
        "Перепиши сценарий ИИ-диалога Агентства W понятным живым языком. "
        "Сохрани тему, главную мысль и призыв. Герои говорят попеременно: "
        "A — собеседник, B — представитель Агентства. Если B рассказывает "
        "о своей работе, используй 'Я помогаю', не третье лицо. "
        "Не придумывай доходность и гарантии. "
        "Всего 60–105 русских слов, длительность речи до 56 секунд. "
        "Верни только JSON вида "
        "{\\\"dialogue\\\":[{\\\"speaker\\\":\\\"A\\\",\\\"text\\\":\\\"...\\\"},"
        "{\\\"speaker\\\":\\\"B\\\",\\\"text\\\":\\\"...\\\"}]}. "
        "Текущий текст: " + old
    )
    response = ask_ai_fn("Ты режиссёр коротких человеческих диалогов для Агентства W.", prompt)
    data = _json_from_answer(response)
    from agency_dialogue_video import normalise_dialogue_turns
    turns = normalise_dialogue_turns(data.get("dialogue") or [])
    errors = dialogue_errors(turns)
    note = duration_warning(turns)
    if note:
        errors.append(note)
    if errors:
        raise ValueError("; ".join(errors))
    return turns


def render_dialogue_item(
    package: dict,
    item: dict,
    owner_id: int,
    ask_ai_fn,
    get_avatar_video_fn,
    *,
    review_fn,
    save_package_fn,
    download_video_fn,
    text_export_fn,
) -> None:
    """A distinct dialogue workflow that never interferes with regular Reels."""
    prefix = "cf_dialogue_" + str(owner_id) + "_" + str(package["package_id"])
    item_id = str(item.get("id") or "content_1")
    prefix += "_" + item_id
    approved = item.get("status") == "approved"
    revision = int(item.get("revision") or 0)

    if not approved:
        st.info(
            "🎭 Режим «ИИ-диалог»: два собеседника, как в видеозвонке. "
            "Сейчас только редактура. Никакие платные видео не запускаются."
        )
        item["title"] = st.text_input(
            "Название ролика", value=item.get("title") or "",
            key=f"{prefix}_title_r{revision}",
        )
        text = st.text_area(
            "Диалог: одна реплика на строку (Мужчина: ... / Женщина: ...)",
            value=dialogue_text(item.get("dialogue") or []),
            height=330, key=f"{prefix}_turns_r{revision}",
        )
        item["caption"] = st.text_area(
            "Подпись к ролику", value=item.get("caption") or "",
            height=120, key=f"{prefix}_caption_r{revision}",
        )
        item["cta"] = st.text_input(
            "Одно действие для зрителя",
            value=item.get("cta") or "", key=f"{prefix}_cta_r{revision}",
        )
        item["aspect_ratio"] = st.radio(
            "### 3. Размер видео",
            options=["9:16", "16:9"],
            format_func=lambda r: "9:16 — вертикальный Reels" if r == "9:16" else "16:9 — горизонтальный",
            index=0 if item.get("aspect_ratio", "9:16") == "9:16" else 1,
            horizontal=True, key=f"{prefix}_ratio_r{revision}",
        )
        try:
            draft_turns = parse_dialogue_editor(text)
            expected = estimate_dialogue_seconds(draft_turns)
            st.caption(f"⏱ Ориентировочная длительность со заставками: {expected:.0f} из 60 секунд")
            note = duration_warning(draft_turns)
            if note:
                st.warning(note)
        except ValueError:
            pass

        edit_col, rewrite_col = st.columns(2)
        edit_col.caption("✏️ Редактируйте слова прямо в поле выше.")
        if rewrite_col.button("🔄 Переписать сценарий", key=f"{prefix}_rewrite_r{revision}", use_container_width=True):
            try:
                with st.spinner("Переписываем сценарий без платной генерации видео..."):
                    item["dialogue"] = _rewrite_dialogue(item, ask_ai_fn)
                item["status"] = "draft"
                item["revision"] = revision + 1
                save_package_fn(package)
                st.rerun()
            except (ValueError, TypeError) as exc:
                st.error("Не удалось переписать: " + str(exc))
        if st.button(
            "✅ Утвердить сценарий",
            key=f"{prefix}_approve_r{revision}", type="primary",
            use_container_width=True,
        ):
            try:
                turns = parse_dialogue_editor(text)
                validation = dialogue_errors(turns)
                timing = duration_warning(turns)
                if timing:
                    validation.append(timing)
                if not item["title"].strip():
                    validation.append("Добавьте название ролика.")
                if not item["cta"].strip():
                    validation.append("Добавьте один призыв к действию.")
                if not item["caption"].strip():
                    validation.append("Добавьте подпись к Reels.")
                if validation:
                    st.warning("Исправьте сценарий:\n- " + "\n- ".join(validation))
                    return
                item["dialogue"] = turns
                # Existing Stagirite AI reviews meaning/style and can fix issues,
                # but paid video generation remains gated by human approval.
                with st.spinner("Стагирит проверяет реплики и смысл..."):
                    reviewed = review_fn(package, item, ask_ai_fn)
                timing = duration_warning(reviewed.get("dialogue") or [])
                if timing:
                    st.warning(timing)
                    return
                reviewed["aspect_ratio"] = item.get("aspect_ratio") or "9:16"
                item.clear()
                item.update(reviewed)
                package["status"] = "approved"
                save_package_fn(package)
                st.rerun()
            except (ValueError, TypeError, KeyError) as exc:
                st.error("Не удалось утвердить сценарий: " + str(exc))
        return

    st.success(
        "✅ Диалог согласован. Ни один запрос на платную видеогенерацию пока не отправлен."
    )
    with st.container(border=True):
        st.markdown("**" + (item.get("title") or "ИИ-диалог") + "**")
        for turn in item.get("dialogue") or []:
            st.markdown(f"**{SPEAKER_LABELS.get(turn.get('speaker'), 'Герой')}:** {turn.get('text') or ''}")
        if item.get("caption"):
            st.caption("Подпись к Reels: " + str(item["caption"]))
        st.caption("Действие зрителя: " + str(item.get("cta") or ""))

    edit, rewrite, export = st.columns(3)
    if edit.button("✏️ Редактировать", key=f"{prefix}_edit", use_container_width=True):
        item["status"] = "draft"
        item["revision"] = revision + 1
        item.pop("dialogue_video_id", None)
        item.pop("dialogue_video_test", None)
        package["status"] = "draft"
        save_package_fn(package)
        st.session_state.pop(f"{prefix}_video", None)
        st.rerun()
    if rewrite.button("🔄 Переписать", key=f"{prefix}_rewrite_approved", use_container_width=True):
        try:
            with st.spinner("Создаём новую версию сценария..."):
                item["dialogue"] = _rewrite_dialogue(item, ask_ai_fn)
            item["status"] = "draft"
            item["revision"] = revision + 1
            package["status"] = "draft"
            item.pop("dialogue_video_id", None)
            item.pop("dialogue_video_test", None)
            save_package_fn(package)
            st.session_state.pop(f"{prefix}_video", None)
            st.rerun()
        except (ValueError, TypeError) as exc:
            st.error("Не получилось переписать: " + str(exc))
    export.download_button(
        "⬇️ Скачать сценарий",
        data=(text_export_fn(item) + "\n").encode("utf-8"),
        file_name="agency_w_dialogue_script.txt", mime="text/plain",
        key=f"{prefix}_script_file", use_container_width=True,
    )

    aspect_ratio = item.get("aspect_ratio") or "9:16"
    st.markdown("### 3. Размер видео")
    st.info("📐 " + ("Вертикальный 9:16 (Reels)" if aspect_ratio == "9:16" else "Горизонтальный 16:9"))
    st.caption(f"⏱ Примерная длительность: {estimate_dialogue_seconds(item.get('dialogue') or []):.0f} секунд, максимум 60.")
    st.markdown("### 5. Утверждено — можно создать видео")
    st.markdown("### 🎬 Производство ИИ-диалога")
    st.caption(
        "HeyGen создаёт чередующиеся кадры с двумя говорящими аватарами. "
        "Мы добавляем оформление видеозвонка и знак Агентства W. "
        "Это отдельный режим: прежняя сборка Reels с Runway не меняется."
    )
    cast = _cast_controls(prefix, item)
    state_key = f"{prefix}_video"
    state = st.session_state.get(state_key)
    if not isinstance(state, dict):
        state = {}
        if item.get("dialogue_video_id"):
            state = {
                "video_id": item["dialogue_video_id"],
                "test": bool(item.get("dialogue_video_test")),
            }
            st.session_state[state_key] = state

    if not state.get("video_id"):
        test_mode = st.checkbox(
            "Тестовый запуск HeyGen (с водяным знаком, до финального производства)",
            value=True, key=f"{prefix}_test_mode",
        )
        if test_mode:
            st.info(
                "Начните с тестового видео. Когда убедимся, что обе роли говорят "
                "и смена кадров правильная, можно создать чистовую версию."
            )
        else:
            st.warning(
                "Это финальная генерация HeyGen. Она платная и запускается только "
                "по вашей кнопке; длительность зависит от голоса и объёма реплик."
            )
        if st.button(
            "🎬 Создать ИИ-диалог в HeyGen",
            key=f"{prefix}_generate", type="primary",
            use_container_width=True,
        ):
            errors = dialogue_errors(item.get("dialogue")) + validate_cast(cast)
            timing = duration_warning(item.get("dialogue") or [])
            if timing:
                errors.append(timing)
            if errors:
                st.error("Проверьте настройки:\n- " + "\n- ".join(errors))
                return
            api_key = _secret("HEYGEN_API_KEY", "HEYGEN_KEY")
            with st.spinner("HeyGen создаёт диалог двух героев..."):
                result = create_dialogue_video(
                    api_key, item["dialogue"], cast,
                    is_test=test_mode, title=item.get("title") or "Агентство W: диалог", aspect_ratio=aspect_ratio,
                )
            if not result.get("ok"):
                st.error(str(result.get("error") or "HeyGen не запустил видео."))
                return
            state.update(result)
            st.session_state[state_key] = state
            item["dialogue_video_id"] = result["video_id"]
            item["dialogue_video_test"] = bool(test_mode)
            item["dialogue_cast"] = cast
            item["aspect_ratio"] = aspect_ratio
            save_package_fn(package)
            st.rerun()
        return

    st.info(
        "Видео обрабатывается в HeyGen. Повторная платная генерация не запускается "
        "при обновлении страницы."
    )
    if st.button(
        "🔄 Проверить готовность диалога",
        key=f"{prefix}_check", use_container_width=True,
    ):
        with st.spinner("Проверяем ранее запущенное видео..."):
            status = get_avatar_video_fn(state["video_id"])
        if not status.get("ok"):
            st.error(str(status.get("error") or "HeyGen не вернул статус."))
        else:
            state["status"] = status.get("status") or ""
            if state["status"] == "completed" and status.get("video_url"):
                try:
                    with st.spinner("Сохраняем MP4 и оформление видеовстречи..."):
                        raw = download_video_fn(status["video_url"])
                        state["original_mp4"] = raw
                        logo = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
                        try:
                            state["final_mp4"] = assemble_dialogue_mp4(raw, logo_path=logo, title=item.get('title') or '', aspect_ratio=aspect_ratio)
                            state.pop("overlay_error", None)
                        except (OSError, RuntimeError, ValueError) as exc:
                            # Never make a successfully generated, paid video inaccessible.
                            state["overlay_error"] = str(exc)
                            state["final_mp4"] = raw
                except (OSError, RuntimeError, requests.RequestException) as exc:
                    st.error("HeyGen завершил видео, но MP4 не удалось загрузить: " + str(exc))
            elif state["status"] in ("failed", "error"):
                state["error"] = str(status.get("error") or "Генерация не удалась.")
            st.session_state[state_key] = state
            st.rerun()

    if state.get("error"):
        st.error(state["error"])
    if state.get("overlay_error"):
        st.warning(
            "HeyGen подготовил речь обоих героев, но оформление видеозвонка "
            "не удалось наложить. Можно скачать оригинальный MP4. "
            + state["overlay_error"][:360]
        )
    final = state.get("final_mp4")
    if final:
        # Rebuild the already-paid HeyGen footage without placing another order.
        if state.get("original_mp4") and st.button(
            "🛠 Убрать белые поля и пересобрать без оплаты HeyGen",
            key=f"{prefix}_repair_padding", use_container_width=True,
        ):
            logo = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
            try:
                with st.spinner("Убираем светлые поля, сохраняем озвучку и заставки..."):
                    state["final_mp4"] = assemble_dialogue_mp4(
                        state["original_mp4"], logo_path=logo,
                        title=item.get("title") or "", remove_pale_bars=True, aspect_ratio=aspect_ratio,
                    )
                state.pop("overlay_error", None)
                st.session_state[state_key] = state
                st.rerun()
            except (OSError, RuntimeError, ValueError) as exc:
                st.error("Не удалось исправить кадр: " + str(exc))
        st.video(final)
        st.download_button(
            "⬇️ Скачать готовый ИИ-диалог MP4",
            data=final, file_name="agency_w_ai_dialogue.mp4",
            mime="video/mp4", use_container_width=True,
            key=f"{prefix}_mp4",
        )
        st.caption(
            "Это тестовый ролик с водяным знаком HeyGen."
            if state.get("test") else
            "Видео готово к просмотру. Публикация не выполняется автоматически."
        )
        if state.get("test"):
            if st.button(
                "✨ Создать чистовую версию (платно)",
                key=f"{prefix}_final_version", use_container_width=True,
            ):
                # Deliberately reset the job only when the user explicitly requests
                # a paid final version. The next button is the paid confirmation.
                state.clear()
                st.session_state[state_key] = state
                item.pop("dialogue_video_id", None)
                item.pop("dialogue_video_test", None)
                st.session_state[f"{prefix}_test_mode"] = False
                save_package_fn(package)
                st.rerun()
    elif str(state.get("status") or "").lower() == "completed":
        st.info("Видео создано. Нажмите проверку ещё раз, чтобы скачать готовый MP4.")
