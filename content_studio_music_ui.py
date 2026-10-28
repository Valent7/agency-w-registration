"""Suno Studio bridge for Agency W music workflow.

The browser editor remains on Suno because it requires the owner's own Suno
session. Finished tracks are imported into the owner's private Agency project
and can be reused by video finishing without another upload.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import streamlit as st

SUNO_STUDIO_URL = "https://suno.com/studio-welcome"
_AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def _music_jobs(owner_id, project, store):
    if not store or not project:
        return []
    try:
        jobs = store.list_jobs(owner_id, project["id"], limit=100)
    except Exception:
        return []
    return [
        job for job in jobs
        if isinstance(job, dict)
        and job.get("state") == "studio"
        and job.get("studio_tool") == "music"
        and job.get("studio_complete") is True
        and isinstance(job.get("assets"), list)
        and job["assets"]
    ]


def _mime(upload):
    suffix = Path(str(getattr(upload, "name", "") or "")).suffix.lower()
    return _AUDIO_TYPES.get(suffix)


def render_music_tool(owner_id, project, store):
    st.subheader("Suno Studio — музыкальный редактор")
    st.write(
        "Создавайте и редактируйте музыку в Suno Studio, затем возвращайте готовый трек "
        "в Контент-завод Агентства W."
    )

    left, right = st.columns([1.2, 2.8], gap="large")
    with left:
        st.link_button("🎵 Открыть Suno Studio", SUNO_STUDIO_URL, type="primary", width="stretch")
    with right:
        st.caption(
            "Suno откроется в новой вкладке под вашим аккаунтом. После экспорта MP3/WAV "
            "вернитесь сюда и сохраните трек в проект."
        )

    if not store or not project:
        st.info("Сначала выберите или сохраните проект Агентства W.")
        return

    st.divider()
    st.markdown("### Вернуть готовую музыку в Агентство")
    title = st.text_input(
        "Название трека",
        placeholder="Например: Время жить — спокойный фон",
        key=f"studio_music_{owner_id}_{project['id']}_title",
        max_chars=150,
    )
    upload = st.file_uploader(
        "Готовый трек из Suno",
        type=["mp3", "wav", "m4a"],
        key=f"studio_music_{owner_id}_{project['id']}_file",
        help="Поддерживаются MP3, WAV и M4A до 100 МБ.",
    )
    if upload is not None:
        st.audio(upload.getvalue())

    if st.button(
        "Сохранить в «Мои генерации»",
        type="primary",
        width="stretch",
        key=f"studio_music_{owner_id}_{project['id']}_save",
        disabled=upload is None or not title.strip(),
    ):
        try:
            data = upload.getvalue()
            mime = _mime(upload)
            if mime is None or not isinstance(data, bytes) or not 0 < len(data) <= 100 * 1024 * 1024:
                raise ValueError("Выберите MP3, WAV или M4A размером до 100 МБ.")
            from content_studio_catalog import save_studio_result
            digest = sha256(title.strip().encode("utf-8") + b"\0" + data).hexdigest()
            job = save_studio_result(
                store, owner_id, project["id"], "music", title.strip(),
                text="Музыка создана или отредактирована в Suno Studio.",
                data=data, mime_type=mime, request_id="suno-" + digest[:48],
            )
            if not isinstance(job, dict) or job.get("studio_complete") is not True:
                raise ValueError("Сохранение ещё не подтверждено.")
            st.session_state[f"studio_music_{owner_id}_selected_job"] = job["id"]
            st.success("Музыка сохранена в «Моих генерациях» и готова для использования в фильме.")
            st.rerun()
        except ValueError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось сохранить трек. Сам файл остаётся у вас; повторите сохранение.")

    jobs = _music_jobs(owner_id, project, store)
    if not jobs:
        st.caption("Сохранённых музыкальных треков в этом проекте пока нет.")
        return

    st.divider()
    st.markdown("### Музыка проекта")
    labels = {
        job["id"]: str((job.get("content") or {}).get("title") or job.get("topic") or "Музыкальный трек")
        for job in jobs
    }
    current = st.session_state.get(f"studio_music_{owner_id}_selected_job")
    options = [job["id"] for job in jobs]
    if current not in options:
        current = options[0]
    selected_id = st.selectbox(
        "Выберите трек",
        options,
        index=options.index(current),
        format_func=labels.get,
        key=f"studio_music_{owner_id}_{project['id']}_library",
    )
    st.session_state[f"studio_music_{owner_id}_selected_job"] = selected_id
    selected = next(job for job in jobs if job["id"] == selected_id)
    try:
        resolved = store.resolve_assets(owner_id, selected["assets"][:1])[0]
        source = resolved.get("url") or resolved.get("path")
        if source:
            st.audio(source, format=resolved.get("mime_type"))
    except Exception:
        st.caption("Предпросмотр временно недоступен, но сохранённый файл остаётся в проекте.")

    if st.button(
        "Использовать этот трек в моём видео",
        type="primary",
        width="stretch",
        key=f"studio_music_{owner_id}_{project['id']}_use_video",
    ):
        st.session_state[f"studio_music_{owner_id}_selected_job"] = selected_id
        st.session_state[f"content_studio_{owner_id}_next"] = "reference"
        st.rerun()
