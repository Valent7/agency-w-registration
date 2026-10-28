"""Simple-first music workspace for Agency W.

The owner describes the desired result inside Agency W. Suno Create remains the
generation surface for a personal Pro account; finished tracks return to the
private Agency project and can be reused in video finishing.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import streamlit as st

SUNO_CREATE_URL = "https://suno.com/create"
SUNO_STUDIO_URL = "https://suno.com/studio-welcome"
_AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

MOODS = (
    "Спокойное",
    "Вдохновляющее",
    "Энергичное",
    "Торжественное",
    "Тёплое",
    "Современное",
)


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


def _clean(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _simple_prompt(description, kind, mood):
    description = _clean(description, 1200)
    if not description:
        return ""
    parts = [description]
    if mood:
        parts.append("Настроение: " + mood.lower())
    if kind == "Инструментальная":
        parts.append("Без вокала, инструментальная музыка")
    else:
        parts.append("С вокалом")
    parts.append("Чистое современное звучание, выразительная мелодия, качественная аранжировка")
    return ". ".join(parts) + "."


def _advanced_prompt(style, mood, instruments, vocals, idea):
    fields = []
    for label, value in (
        ("Стиль и жанр", style),
        ("Настроение", mood),
        ("Инструменты", instruments),
        ("Вокал", vocals),
        ("Идея / содержание", idea),
    ):
        value = _clean(value, 1500)
        if value:
            fields.append(f"{label}: {value}")
    if not fields:
        return ""
    fields.append("Сделай цельную, профессионально звучащую композицию с ясной структурой и чистым миксом")
    return ". ".join(fields) + "."


def _render_suno_handoff(owner_id, project, prompt):
    st.markdown("### 2. Создайте музыку")
    st.caption("Сейчас создание выполняет Suno. Мы оставляем человеку только три действия внутри Suno.")

    if prompt:
        st.markdown("**Описание уже готово. Скопируйте его:**")
        st.code(prompt, language="none")
        st.info("В Suno сделайте только это: **Simple → вставить описание → Create**. Если открылся Advanced — нажмите **Simple** вверху слева.")
    else:
        st.info("Сначала заполните шаг 1 — тогда Агентство подготовит описание для Suno.")

    st.link_button(
        "Шаг 2 — открыть Suno",
        SUNO_CREATE_URL,
        type="primary",
        width="stretch",
        disabled=not bool(prompt),
    )
    st.caption("После нажатия не выбирайте Audio, Voice, Inspo и другие инструменты. Нужен только режим Simple.")


def _render_import(owner_id, project, store):
    st.divider()
    st.markdown("### 3. Верните понравившийся трек")
    st.caption("В Suno прослушайте варианты, скачайте понравившийся трек и загрузите его сюда. Всё остальное Агентство сохранит само.")

    title = st.text_input(
        "Название трека",
        placeholder="Например: Время жить — спокойный фон",
        key=f"studio_music_{owner_id}_{project['id']}_title",
        max_chars=150,
    )
    upload = st.file_uploader(
        "Готовый трек",
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
                text="Музыкальный трек для материалов Агентства W.",
                data=data, mime_type=mime, request_id="music-" + digest[:48],
            )
            if not isinstance(job, dict) or job.get("studio_complete") is not True:
                raise ValueError("Сохранение ещё не подтверждено.")
            st.session_state[f"studio_music_{owner_id}_selected_job"] = job["id"]
            st.success("Трек сохранён. Теперь его можно выбрать при оформлении фильма.")
            st.rerun()
        except ValueError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось сохранить трек. Сам файл остаётся у вас; повторите сохранение.")


def _render_library(owner_id, project, store):
    jobs = _music_jobs(owner_id, project, store)
    if not jobs:
        st.caption("Сохранённых музыкальных треков в этом проекте пока нет.")
        return

    st.divider()
    st.markdown("### Моя музыка")
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


def render_music_tool(owner_id, project, store):
    st.markdown("## Музыка — всего 3 шага")
    st.caption("Без музыкальных терминов и без лишних настроек.")

    if not store or not project:
        st.info("Сначала выберите или сохраните проект Агентства W.")
        return

    st.markdown("### 1. Опишите, что хотите услышать")
    description = st.text_area(
        "Описание музыки",
        placeholder="Например: лёгкая музыка в стиле «Вальса цветов», спокойная и воздушная",
        height=150,
        key=f"studio_music_{owner_id}_{project['id']}_prompt",
        label_visibility="collapsed",
    )
    type_col, mood_col = st.columns(2, gap="large")
    with type_col:
        kind = st.radio(
            "Тип",
            ["Инструментальная", "С вокалом"],
            horizontal=True,
            key=f"studio_music_{owner_id}_{project['id']}_kind",
        )
    with mood_col:
        mood = st.selectbox(
            "Настроение",
            MOODS,
            key=f"studio_music_{owner_id}_{project['id']}_mood",
        )

    prompt = _simple_prompt(description, kind, mood)

    with st.expander("Настроить подробнее — только если хочется", expanded=False):
        style = st.text_input(
            "Стиль или жанр",
            placeholder="Например: cinematic ambient, soft piano, light strings",
            key=f"studio_music_{owner_id}_{project['id']}_style",
        )
        instruments = st.text_input(
            "Инструменты",
            placeholder="Например: фортепиано, струнные, мягкий синтезатор",
            key=f"studio_music_{owner_id}_{project['id']}_instruments",
        )
        idea = st.text_area(
            "Дополнительные пожелания",
            placeholder="Например: плавное вступление, лёгкое развитие, мягкий финал",
            height=110,
            key=f"studio_music_{owner_id}_{project['id']}_idea",
        )
        additions = []
        if _clean(style):
            additions.append("Стиль: " + _clean(style, 500))
        if _clean(instruments):
            additions.append("Инструменты: " + _clean(instruments, 500))
        if _clean(idea):
            additions.append("Дополнительно: " + _clean(idea, 800))
        if prompt and additions:
            prompt = prompt + " " + ". ".join(additions) + "."

    _render_suno_handoff(owner_id, project, prompt)
    _render_import(owner_id, project, store)
    _render_library(owner_id, project, store)

    with st.expander("Хочу создавать вообще без перехода в Suno", expanded=False):
        st.write(
            "Для этого Агентству нужен отдельный официальный Suno Platform API. "
            "После его подключения шаг 2 станет одной кнопкой «Создать», а готовые варианты появятся прямо здесь."
        )
        st.link_button("Открыть Suno Platform API", "https://platform.suno.com/auth/login?returnTo=%2Fauth", width="stretch")
