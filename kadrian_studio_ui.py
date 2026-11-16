"""Partner-facing video chat on the Agency's existing production worker."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from content_factory_autopilot_ui import _runtime
from content_factory_core import iso


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
    styles = {"Живое видео": "live", "Мультфильм": "cartoon"}
    style = styles[st.radio("Формат", list(styles), horizontal=True, key=key + "_style")]
    music_only = style == "cartoon" and st.checkbox("Только музыка", key=key + "_music")
    for role, message in chat["messages"]:
        with st.chat_message(role):
            st.markdown(message)

    message = st.chat_input("Напишите или наговорите, какой ролик хотите",
                            max_chars=5000, accept_audio=True, max_upload_size=15)
    if message:
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

    if chat["brief"] and st.button("Собрать по этому замыслу", type="primary", key=key + "_submit"):
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
        except Exception:
            st.error("Не удалось подтвердить постановку в очередь. Обновите список работ перед повтором.")

    try:
        jobs = store.list_jobs(owner_id, project_id, limit=100)
        jobs = [job for job in jobs if job.get("studio_master") and
                job.get("video_style") in {"live", "cartoon"}]
    except Exception:
        st.warning("Не удалось обновить список работ.")
        return
    errors = [job for job in jobs if job.get("error")]
    if errors:
        with st.expander("История ошибок"):
            for job in errors:
                st.caption(job["error"])
    jobs = [job for job in jobs if not job.get("error") or job.get("state") == "queued"]
    if jobs:
        st.subheader("Мои ролики")
        st.button("Обновить статус", key=key + "_refresh")
    for job in jobs:
        st.markdown(("**Мультфильм**" if job["video_style"] == "cartoon" else "**Живое видео**"))
        if job.get("error"):
            st.caption(job["error"])
        elif job.get("assets"):
            try:
                assets = store.resolve_assets(owner_id, job["assets"])
            except Exception:
                st.caption("Не удалось получить ссылку на файл. Обновите статус.")
                continue
            for asset in assets:
                if str(asset.get("mime_type") or "").startswith("video/") and asset.get("url"):
                    st.video(asset["url"])
                    st.link_button("Открыть MP4", asset["url"])
        else:
            st.caption("В очереди или в работе" if job["state"] == "queued" else job["state"])
