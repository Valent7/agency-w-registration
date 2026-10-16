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
    list_heygen_catalog,
    suggest_dialogue_cast,
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
    """Ordinary users never need to copy HeyGen avatar/voice IDs."""
    st.markdown("#### 🎭 Герои и голоса")
    st.caption(
        "Агентство само подбирает мужчину, женщину и два русскоязычных голоса. "
        "Выбор героев не запускает видео и не расходует средства."
    )
    previous = _cast_defaults(item)
    api_key = _secret("HEYGEN_API_KEY", "HEYGEN_KEY")
    if not api_key:
        st.error(
            "Создание видео пока не подключено администратором Агентства. "
            "Не требуется регистрироваться в HeyGen самостоятельно."
        )
        return {"A": {"avatar_id": "", "voice_id": ""},
                "B": {"avatar_id": "", "voice_id": ""}}

    catalog_key = f"{prefix}_catalog"
    if catalog_key not in st.session_state:
        with st.spinner("Подбираем героев и голоса из HeyGen без оплаты..."):
            st.session_state[catalog_key] = list_heygen_catalog(api_key)
    catalog = st.session_state[catalog_key]
    if not isinstance(catalog, dict) or not catalog.get("ok"):
        st.warning(
            "Пока не получилось загрузить персонажей и голоса. "
            "Администратор может проверить подключение HeyGen, "
            "а участникам не нужно искать технические ID."
        )
        if isinstance(catalog, dict) and catalog.get("error"):
            st.caption(str(catalog["error"]))
        if st.button("🔄 Обновить каталог HeyGen", key=f"{prefix}_retry_catalog"):
            st.session_state.pop(catalog_key, None)
            st.rerun()
        return {"A": {"avatar_id": "", "voice_id": ""},
                "B": {"avatar_id": "", "voice_id": ""}}

    avatars = catalog.get("avatars") or []
    voices = catalog.get("voices") or []
    suggested = suggest_dialogue_cast(catalog, preferred=previous)
    cast = {"A": {}, "B": {}}
    labels = {}
    choices = {}
    for speaker, gender in (("A", "male"), ("B", "female")):
        for field in ("avatar_id", "voice_id"):
            if field == "avatar_id":
                entries = [a for a in avatars if str(a.get("gender") or "").lower() == gender]
            else:
                entries = [
                    v for v in voices
                    if str(v.get("gender") or "").lower() == gender
                    and (
                        str(v.get("language") or "").lower().strip().replace("_", "-")
                        in ("ru", "ru-ru", "russian", "русский", "русская")
                        or str(v.get("language") or "").lower().startswith(("ru-", "russian "))
                        or "русск" in str(v.get("language") or "").lower()
                    )
                ]
            entries = sorted(
                [e for e in entries if e.get("id")],
                key=lambda e: (str(e.get("name") or "").casefold(), str(e["id"])),
            )
            field_key = f"{prefix}_{speaker}_{field}"
            valid_ids = [e["id"] for e in entries]
            best = suggested[speaker][field]
            # Streamlit persists deliberate user choices across reruns.
            if field_key not in st.session_state or st.session_state[field_key] not in valid_ids:
                st.session_state[field_key] = best if best in valid_ids else ""
            cast[speaker][field] = st.session_state[field_key]
            choices[(speaker, field)] = [""] + valid_ids
            labels[(speaker, field)] = {
                e["id"]: str(e.get("name") or "Без названия") for e in entries
            }

    if not validate_cast(cast):
        st.success(
            "✅ Мужчина: " + labels[("A", "avatar_id")].get(cast["A"]["avatar_id"], "готов")
            + " · голос: " + labels[("A", "voice_id")].get(cast["A"]["voice_id"], "готов")
            + "\n\n✅ Женщина: " + labels[("B", "avatar_id")].get(cast["B"]["avatar_id"], "готов")
            + " · голос: " + labels[("B", "voice_id")].get(cast["B"]["voice_id"], "готов")
        )
    else:
        st.warning(
            "HeyGen пока не предоставил полную пару мужского и женского "
            "персонажей с русскими голосами. Видео не будет запущено "
            "с неподходящим голосом."
        )
    with st.expander("🎭 Изменить героев или голоса (необязательно)"):
        left, right = st.columns(2)
        for speaker, col in (("A", left), ("B", right)):
            with col:
                st.markdown(f"**{SPEAKER_LABELS[speaker]}**")
                for field, title in (("avatar_id", "Персонаж"), ("voice_id", "Русский голос")):
                    field_key = f"{prefix}_{speaker}_{field}"
                    options = choices[(speaker, field)]
                    mapping = labels[(speaker, field)]
                    st.selectbox(
                        title,
                        options=options,
                        key=field_key,
                        format_func=lambda value, m=mapping: m.get(value, "Выберите из списка"),
                    )
        if st.button("🔄 Подобрать автоматически", key=f"{prefix}_pick_auto"):
            for speaker in ("A", "B"):
                for field in ("avatar_id", "voice_id"):
                    st.session_state[f"{prefix}_{speaker}_{field}"] = suggested[speaker][field]
            st.rerun()
        st.caption(
            "Технические ID скрыты. Если каталог не предлагает подходящего героя "
            "или голос, обратитесь к администратору Агентства."
        )
    # The select boxes may have changed; re-read their current state.
    return {
        speaker: {
            field: str(st.session_state.get(f"{prefix}_{speaker}_{field}") or "").strip()
            for field in ("avatar_id", "voice_id")
        }
        for speaker in ("A", "B")
    }


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
        if st.button(
            "✅ Стагирит: проверить диалог и утвердить",
            key=f"{prefix}_approve_r{revision}", type="primary",
            use_container_width=True,
        ):
            try:
                turns = parse_dialogue_editor(text)
                validation = dialogue_errors(turns)
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

    edit, export = st.columns(2)
    if edit.button("✏️ Редактировать диалог", key=f"{prefix}_edit", use_container_width=True):
        item["status"] = "draft"
        item["revision"] = revision + 1
        item.pop("dialogue_video_id", None)
        item.pop("dialogue_video_test", None)
        package["status"] = "draft"
        save_package_fn(package)
        st.session_state.pop(f"{prefix}_video", None)
        st.rerun()
    export.download_button(
        "⬇️ Скачать сценарий",
        data=(text_export_fn(item) + "\n").encode("utf-8"),
        file_name="agency_w_dialogue_script.txt", mime="text/plain",
        key=f"{prefix}_script_file", use_container_width=True,
    )

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
            if errors:
                st.error("Проверьте настройки:\n- " + "\n- ".join(errors))
                return
            api_key = _secret("HEYGEN_API_KEY", "HEYGEN_KEY")
            with st.spinner("HeyGen создаёт диалог двух героев..."):
                result = create_dialogue_video(
                    api_key, item["dialogue"], cast,
                    is_test=test_mode, title=item.get("title") or "Агентство W: диалог",
                )
            if not result.get("ok"):
                st.error(str(result.get("error") or "HeyGen не запустил видео."))
                return
            state.update(result)
            st.session_state[state_key] = state
            item["dialogue_video_id"] = result["video_id"]
            item["dialogue_video_test"] = bool(test_mode)
            item["dialogue_cast"] = cast
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
                            state["final_mp4"] = assemble_dialogue_mp4(raw, logo_path=logo)
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
