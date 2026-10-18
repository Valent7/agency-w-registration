"""Simple, opt-in personal-video cleanup panel inside Agency W content factory."""
from __future__ import annotations

import hashlib

import streamlit as st

from content_video_cleanup import VideoCleanupError, clean_user_video, MAX_VIDEO_BYTES


def render_video_cleanup(owner_id: int) -> None:
    """A partner needs only Upload → Preview → Clean → Download."""
    owner_id = int(owner_id)
    st.markdown("### 🧹 Очистить видео")
    st.caption(
        "Для ваших видеороликов или роликов, на обработку которых есть разрешение. "
        "Файл обрабатывается внутри Агентства, без загрузки в TikTok или сторонний сервис."
    )
    uploaded = st.file_uploader(
        "1. Загрузите свой видеоролик (MP4, MOV или M4V, до 120 МБ)",
        type=["mp4", "mov", "m4v"],
        key=f"cf_cleanup_upload_{owner_id}",
    )
    if uploaded is None:
        return
    if uploaded.size > MAX_VIDEO_BYTES:
        st.error("Файл больше 120 МБ. Выберите ролик поменьше.")
        return
    st.markdown("**2. Посмотрите исходный ролик**")
    st.video(uploaded)
    mask_labels = {
        "both": "Слева и справа",
        "left": "Только слева",
        "right": "Только справа",
        "none": "Не скрывать — только обрезать конец",
    }
    mode = st.radio(
        "3. Где расположен водяной знак TikTok?",
        list(mask_labels),
        format_func=lambda key: mask_labels[key],
        horizontal=True,
        key=f"cf_cleanup_mode_{owner_id}",
    )
    position = 82
    if mode != "none":
        position = st.slider(
            "Высота знака (от верха кадра, %)",
            min_value=10, max_value=90, value=82, step=2,
            help="Например, 80% — ближе к низу. При необходимости измените после первой пробы.",
            key=f"cf_cleanup_y_{owner_id}",
        )
        st.caption(
            "Скрываем небольшой участок кадра приблизительной ретушью. "
            "Если знак перемещается или попадает на лицо, могут оставаться следы."
        )
    trim = st.checkbox(
        "✂️ Убрать лишнюю заставку в конце",
        key=f"cf_cleanup_trim_{owner_id}",
    )
    cut = 0.0
    if trim:
        cut = st.number_input(
            "Сколько последних секунд удалить?",
            min_value=0.5, max_value=20.0, value=2.0, step=0.5,
            key=f"cf_cleanup_trim_sec_{owner_id}",
        )
    permitted = st.checkbox(
        "Это мой ролик или у меня есть разрешение автора на обработку",
        key=f"cf_cleanup_rights_{owner_id}",
    )
    fingerprint = hashlib.sha256(uploaded.getvalue()).hexdigest()
    settings = (fingerprint, mode, position, float(cut))
    state_key = f"cf_cleanup_result_{owner_id}"
    current = st.session_state.get(state_key)
    if current and current.get("settings") != settings:
        st.session_state.pop(state_key, None)
        current = None
    st.markdown("**4. Получите готовый MP4**")
    if st.button(
        "🧹 Очистить видео",
        type="primary",
        disabled=not permitted or (mode == "none" and not trim),
        use_container_width=True,
        key=f"cf_cleanup_go_{owner_id}",
    ):
        try:
            with st.spinner("Аккуратно обрабатываем видео и сохраняем звук..."):
                result, details = clean_user_video(
                    uploaded.getvalue(),
                    filename=uploaded.name,
                    mask_mode=mode,
                    position_pct=int(position),
                    trim_end_seconds=float(cut),
                )
            current = {"settings": settings, "video": result, "details": details}
            st.session_state[state_key] = current
        except VideoCleanupError as exc:
            st.error(str(exc))
    if not current:
        return
    st.success("Готово. Проверьте кадры и звук перед публикацией.")
    st.video(current["video"])
    st.caption(
        "Исходный ролик не изменён. "
        "После скачивания новый MP4 можно отправить вручную."
    )
    st.download_button(
        "⬇️ Скачать очищенный MP4",
        current["video"],
        file_name="agency_w_video_cleaned.mp4",
        mime="video/mp4",
        use_container_width=True,
        key=f"cf_cleanup_download_{owner_id}",
    )
