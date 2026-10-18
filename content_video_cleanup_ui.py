"""Simple, opt-in personal-video cleanup panel inside Agency W content factory."""
from __future__ import annotations

import hashlib

import streamlit as st

from content_video_cleanup import (
    VideoCleanupError, clean_user_video, MAX_VIDEO_BYTES,
    sample_video_frames, preview_mask_image,
)


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
    # There is no automatic watermark detector; show EXACTLY what will be
    # retouched before the button is enabled to prevent invisible misses.
    left_y, right_y = 82, 82
    mask_width, mask_height = 38, 16
    if mode != "none":
        st.markdown("**Передвиньте красные рамки на значок и имя автора**")
        if mode in ("both", "left"):
            left_y = st.slider(
                "Слева: переместить рамку выше или ниже",
                min_value=8, max_value=92, value=82,
                key=f"cf_cleanup_left_y_{owner_id}",
            )
        if mode in ("both", "right"):
            right_y = st.slider(
                "Справа: переместить рамку выше или ниже",
                min_value=8, max_value=92, value=82,
                key=f"cf_cleanup_right_y_{owner_id}",
            )
        with st.expander("📐 Изменить размер рамки (если ник не помещается)"):
            mask_width = st.slider(
                "Ширина рамки",
                min_value=10, max_value=58, value=38,
                key=f"cf_cleanup_mask_width_{owner_id}",
            )
            mask_height = st.slider(
                "Высота рамки",
                min_value=6, max_value=30, value=16,
                key=f"cf_cleanup_mask_height_{owner_id}",
            )
        st.caption(
            "Красная рамка должна целиком закрывать TikTok и ник. "
            "На втором кадре знак может переместиться — проверьте обе картинки."
        )
        source_hash = hashlib.sha256(uploaded.getvalue()).hexdigest()
        frames_key = f"cf_cleanup_frames_{owner_id}"
        preview_data = st.session_state.get(frames_key)
        if not preview_data or preview_data.get("hash") != source_hash:
            try:
                with st.spinner("Подготавливаем два кадра для проверки..."):
                    frames = sample_video_frames(uploaded.getvalue(), uploaded.name)
                st.session_state[frames_key] = {"hash": source_hash, "frames": frames}
                preview_data = st.session_state[frames_key]
            except VideoCleanupError as exc:
                st.error(str(exc))
                preview_data = None
        if preview_data:
            first, second = st.columns(2)
            with first:
                st.image(
                    preview_mask_image(
                        preview_data["frames"][0], mode,
                        left_y_pct=left_y, right_y_pct=right_y,
                        mask_width_pct=mask_width, mask_height_pct=mask_height,
                    ),
                    caption="Начало видео: красная зона",
                    width=250,
                )
            with second:
                st.image(
                    preview_mask_image(
                        preview_data["frames"][1], mode,
                        left_y_pct=left_y, right_y_pct=right_y,
                        mask_width_pct=mask_width, mask_height_pct=mask_height,
                    ),
                    caption="Середина видео: красная зона",
                    width=250,
                )
            st.info(
                "Если знак или ник находятся ВНЕ красной области, "
                "передвиньте её выше/ниже или увеличьте размер. "
                "Иначе программа их не удалит."
            )
        else:
            st.warning("Не удалось показать область обработки. Не запускайте очистку вслепую.")
    position = 82
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
    settings = (fingerprint, mode, left_y, right_y, mask_width, mask_height, float(cut))
    state_key = f"cf_cleanup_result_{owner_id}"
    current = st.session_state.get(state_key)
    if current and current.get("settings") != settings:
        st.session_state.pop(state_key, None)
        current = None
    st.markdown("**4. Получите готовый MP4**")
    if st.button(
        "🧹 Очистить видео",
        type="primary",
        disabled=not permitted or (mode == "none" and not trim) or (
            mode != "none" and not preview_data
        ),
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
                    left_y_pct=int(left_y), right_y_pct=int(right_y),
                    mask_width_pct=int(mask_width),
                    mask_height_pct=int(mask_height),
                    trim_end_seconds=float(cut),
                )
            current = {"settings": settings, "video": result, "details": details}
            st.session_state[state_key] = current
        except VideoCleanupError as exc:
            st.error(str(exc))
    if not current:
        return
    st.info("Видео обработано. Проверьте: исчезли ли значок, ник и сохранился ли звук?")
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
