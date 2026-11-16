"""Partner-facing video chat on the Agency's existing production worker."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from content_factory_autopilot_ui import _runtime
from content_factory_core import iso


def _render_library(store, owner_id, project_id, style):
    try:
        jobs = store.list_jobs(owner_id, project_id, limit=100)
    except Exception:
        st.warning("Не удалось загрузить готовые работы.")
        return
    found = False
    for job in jobs:
        if not job.get("studio_master") or job.get("video_style") != style or not job.get("assets"):
            continue
        if job.get("state") not in {"ready", "published", "scheduled", "paused"} or job.get("error"):
            continue
        try:
            assets = store.resolve_assets(owner_id, job["assets"])
        except Exception:
            st.caption("Один из файлов временно недоступен.")
            continue
        for asset in assets:
            url = asset.get("url")
            if url and str(asset.get("mime_type") or "").startswith("video/"):
                found = True
                st.video(url)
                st.link_button("Открыть MP4", url)
    if not found:
        st.info("Готовые файлы появятся здесь после сборки.")


def render_kadrian(owner_id, ask_ai):
    st.header("Кадриан")
    st.caption("Расскажите, какой ролик или мультфильм хотите. Сценарий можно поправить здесь же.")
    try:
        store, _, _ = _runtime()
        projects = store.list_projects(owner_id)
    except Exception:
        st.error("Не удалось открыть проекты студии. Обновите страницу и попробуйте снова.")
        return
    if not projects:
        st.info("Для производства нужен сохранённый проект Агентства W.")
        return
    project = st.selectbox("Проект", projects, format_func=lambda item: item["name"])
    project_id = project["id"]
    key = f"kadrian_{owner_id}_{project_id}"
    chat = st.session_state.setdefault(key, {"messages": [], "brief": "", "request": None})
    gallery_key = key + "_gallery"
    first, second = st.columns(2)
    if first.button("Готовые видеоролики", key=key + "_live_library"):
        st.session_state[gallery_key] = "live"
    if second.button("Готовые мультфильмы", key=key + "_cartoon_library"):
        st.session_state[gallery_key] = "cartoon"
    library = st.session_state.get(gallery_key)
    if library:
        if st.button("Вернуться в чат", key=key + "_return"):
            st.session_state.pop(gallery_key, None)
            st.rerun()
        _render_library(store, owner_id, project_id, library)
        return
    if chat.get("files"):
        with st.expander("Вложения в этом чате"):
            for name, mime, data in chat["files"]:
                st.caption(name)
                if mime.startswith("image/"):
                    st.image(data, width=180)
                elif mime.startswith("audio/"):
                    st.audio(data, format=mime)
                elif mime.startswith("video/"):
                    st.video(data, format=mime)
            if st.button("Убрать вложения", key=key + "_clear_files"):
                chat.pop("files", None)
                st.rerun()
    styles = {"Живое видео": "live", "Мультфильм": "cartoon"}
    style = styles[st.radio("Формат", list(styles), horizontal=True, key=key + "_style")]
    music_only = style == "cartoon" and st.checkbox("Только музыка", key=key + "_music")
    for role, message in chat["messages"]:
        with st.chat_message(role):
            st.markdown(message)

    message = st.chat_input("Напишите или наговорите, какой ролик хотите",
                            max_chars=5000, accept_audio=True, accept_file="multiple",
                            file_type=["png", "jpg", "jpeg", "webp", "mp3", "wav", "m4a", "ogg", "mp4", "mov", "webm"],
                            max_upload_size=15)
    if message:
        if message.files:
            files = [(item.name, item.type or "", item.getvalue()) for item in message.files]
            if len(files) > 8 or sum(len(data) for _, _, data in files) > 40 * 1024 * 1024:
                st.error("Прикрепите до 8 файлов общим размером до 40 МБ.")
                return
            chat["files"] = files
        prompt = message.text.strip()
        if message.audio:
            from content_factory_ui import _transcribe_content_audio
            try:
                with st.spinner("Распознаю запись…"):
                    spoken = _transcribe_content_audio(message.audio.getvalue())
            except Exception:
                st.error("Не удалось распознать запись. Попробуйте ещё раз или напишите пожелание.")
                return
            prompt = "\n".join(value for value in (prompt, spoken) if value)[:5000]
        if not prompt and message.files:
            st.rerun()
        if not prompt:
            st.warning("Запишите пожелание ещё раз или напишите его.")
            return
        history = "\n\n".join(f"{role}: {message}" for role, message in chat["messages"][-10:])
        with st.spinner("Кадриан готовит ответ…"):
            answer = ask_ai(
                "Ты Кадриан, кинопроизводитель Агентства W. Пиши по-русски простыми словами. "
                "Если без уточнения нельзя сделать качественно, задай ровно один вопрос. "
                "Иначе предложи короткий сценарий: кадры, озвучка, ритм и финал. "
                "Сохраняй утверждённые реплики дословно. Не обещай, что видео уже создано. "
                "Отвечай только по текущему видео или мультфильму.",
                f"Формат: {style}. Звук: {'только музыка, без диктора' if music_only else 'с озвучкой'}.\nПереписка:\n{history}\nПоследнее пожелание: {prompt}",
            )
        if not answer or answer.startswith(("Ошибка OpenAI", "Ключ OpenAI", "Не удалось")):
            st.error("Кадриан не получил сценарий. Попробуйте отправить пожелание ещё раз.")
            return
        chat["messages"].extend([("user", prompt), ("assistant", answer)])
        chat["brief"] = f"Пожелание партнёра: {prompt}\nПлан Кадриана: {answer}"[:6000]
        chat["request"] = uuid4().hex
        st.rerun()

    if chat.get("files"):
        st.caption("Вложения доступны для просмотра в этой переписке. Подключение к сборке пока недоступно.")
    if chat["brief"] and not chat.get("files") and st.button("Собрать по этому замыслу", type="primary", key=key + "_submit"):
        request = chat["request"]
        dedupe = sha256(f"kadrian|{owner_id}|{project_id}|{request}".encode()).hexdigest()
        fingerprint = sha256(f"{style}|{music_only}|{chat['brief']}".encode()).hexdigest()
        now = iso(datetime.now(timezone.utc))
        try:
            job = store.enqueue_job(owner_id, project_id, {
                "platform": "instagram", "format": "reel", "scheduled_at": now,
                "available_at": now, "state": "queued", "dedupe_key": dedupe,
                "request_id": request, "request_fingerprint": fingerprint,
                "topic": ("[Кадриан: только музыка]\n" + chat["brief"])[:6000] if music_only else chat["brief"], "requested_by_owner": True,
                "video_mode": "live", "video_style": style,
                "review_required": True, "studio_master": True,
                "content": {}, "assets": [], "checkpoint": {}, "error": "", "attempts": 0,
            })
            if job.get("request_fingerprint") != fingerprint:
                st.warning("Этот замысел уже отправлен с другим форматом. Опишите новую версию в чате.")
            else:
                chat["queued"] = True
        except Exception:
            st.error("Не удалось подтвердить постановку в очередь. Обновите список работ перед повтором.")

    if chat.get("queued"):
        st.caption("Ролик отправлен на сборку. Готовый файл появится в соответствующем разделе.")
