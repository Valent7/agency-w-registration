"""Owner-only, supervised video-cleanup test inside Agency W Content Factory."""
from __future__ import annotations
import hashlib
import streamlit as st
from video_cleanup_pilot import (
    MAX_UPLOAD_BYTES, PilotError, make_test_preview, recognized_reference,
)

def render_owner_video_cleanup_pilot(owner_id: int) -> None:
    # Server-side permission guard, not just hidden navigation.
    from neola_partner_center import _is_agency_owner
    if not _is_agency_owner(int(owner_id)):
        return
    with st.expander("🧪 Очистить видео — тест новой методики", expanded=False):
        st.caption(
            "Только для владельца Агентства. Проверяем способ из ролика "
            "«Русские немцы». Другие видео пока не принимаем — "
            "универсальное удаление TikTok ещё не доказано."
        )
        video = st.file_uploader(
            "1. Загрузите исходный ролик «Русские немцы.mp4»",
            type=["mp4"],
            key=f"w_pilot_upload_{owner_id}",
        )
        if video is None:
            return
        if video.size > MAX_UPLOAD_BYTES:
            st.error("Файл слишком большой для тестовой версии (максимум 95 МБ).")
            return
        raw = video.getvalue()
        if not recognized_reference(raw):
            st.warning(
                "Этот файл не совпадает с проверенным исходником. "
                "Мы не будем применять его индивидуальные настройки к другому видео."
            )
            return
        st.success("Контрольный исходный ролик распознан.")
        start_label = st.radio(
            "2. Какой участок проверим?",
            ["Начало (0 секунд)", "Середина (60 секунд)", "Далее (180 секунд)"],
            key=f"w_pilot_start_{owner_id}",
            horizontal=False,
        )
        start = {
            "Начало (0 секунд)": 0,
            "Середина (60 секунд)": 60,
            "Далее (180 секунд)": 180,
        }[start_label]
        seconds = st.slider(
            "Длительность видеопробы, секунд", min_value=2, max_value=12,
            value=8, key=f"w_pilot_len_{owner_id}",
        )
        st.caption(
            "Обрабатываем один короткий фрагмент со звуком; "
            "никаких платных API и скрытых запусков."
        )
        permission = st.checkbox(
            "Это мой ролик или у меня есть разрешение автора на обработку",
            key=f"w_pilot_rights_{owner_id}",
        )
        settings = (hashlib.sha256(raw).hexdigest(), start, seconds)
        state_key = f"w_pilot_result_{owner_id}"
        result = st.session_state.get(state_key)
        if result and result.get("settings") != settings:
            st.session_state.pop(state_key, None)
            result = None
        if st.button(
            "🧪 Создать пробу", disabled=not permission, type="primary",
            use_container_width=True, key=f"w_pilot_run_{owner_id}",
        ):
            try:
                with st.spinner("Обрабатываю выбранный участок и сохраняю звук..."):
                    out, detail = make_test_preview(
                        raw, start_second=start, duration_seconds=seconds,
                    )
                result = {"settings": settings, "mp4": out, "details": detail}
                st.session_state[state_key] = result
            except (PilotError, TimeoutError) as exc:
                st.error(str(exc))
        if result:
            st.markdown("**3. Проверьте результат до публикации**")
            st.video(result["mp4"])
            st.caption("Это экспериментальная версия: знак может остаться на отдельных кадрах.")
            st.download_button(
                "⬇️ Скачать тестовый MP4",
                data=result["mp4"], file_name=f"agency_w_cleanup_test_{start}s.mp4",
                mime="video/mp4", use_container_width=True,
                key=f"w_pilot_download_{owner_id}",
            )
            st.warning(
                "Не используйте эту пробу как подтверждение качества для других "
                "роликов. Полная автоматическая очистка пока не включена."
            )
