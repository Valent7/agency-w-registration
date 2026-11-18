"""Partner-facing video chat on the Agency's existing production worker."""
from base64 import b64decode, b64encode
import re
import os

from kadrian_memory import director_instructions, project_context, restore_chat, save_turn, production_plan
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from content_factory_autopilot_ui import _runtime
from content_factory_core import iso
from kadrian_production_ui import ROUTES, connection_status, render_production, render_provider_progress


def _render_library(store, owner_id, project_id, style):
    try:
        jobs = store.list_jobs(owner_id, project_id, limit=100)
    except Exception:
        st.warning("Не удалось загрузить готовые работы.")
        return
    found = False
    for job in jobs:
        if not job.get("studio_master") or job.get("video_style") != style :
            continue
        if job.get("state") not in {"ready", "published", "scheduled", "paused"} or job.get("error"):
            continue
        try:
            if job.get("external_delivery"):
                from kadrian_production import provider_preview_url
                url = provider_preview_url(job)
                st.video(url)
                st.link_button("Скачать готовое видео", url)
                st.caption("Большой файл хранится у видеосервиса. Скачайте его для своего архива.")
                found = True
                continue
            assets = store.resolve_assets(owner_id, job.get("assets") or [])
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


def _draw_chat_image(prompt, context, style, files):
    import os
    from openai import OpenAI

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        from streamlit.errors import StreamlitSecretNotFoundError
        try:
            api_key = str(st.secrets.get("OPENAI_API_KEY") or "").strip()
        except StreamlitSecretNotFoundError:
            pass
    if not api_key:
        raise RuntimeError("В студии не настроен ключ OpenAI для изображений.")
    client = OpenAI(api_key=api_key, timeout=100, max_retries=0)
    options = {
        "model": os.getenv("CONTENT_FACTORY_IMAGE_MODEL", "gpt-image-2.5-sunburst"),
        "prompt": (
            "Создай одно изображение по последней просьбе пользователя. "
            "Возьми описание героя и обстановки из переписки. "
            "Сохрани согласованные детали и используй приложенные изображения как образцы. "
            "Явно указанный стиль в последней просьбе важнее режима студии и прежних формулировок. "
            f"Режим студии по умолчанию: {style}.\nПереписка и сценарий:\n{context[-12000:]}\nПросьба: {prompt}"
        ),
        "size": "1024x1024",
        "quality": "medium",
    }
    references = [(name, data, mime) for name, mime, data in files
                  if mime in {"image/png", "image/jpeg", "image/webp"}]
    result = (
        client.images.edit(image=references, **options)
        if references else client.images.generate(**options)
    )
    return b64decode(result.data[0].b64_json, validate=True)


def _render_chat_entry(role, entry, store=None, owner_id=None):
    with st.chat_message(role):
        if isinstance(entry, dict) and entry.get("storage_path"):
            try:
                st.image(store.read_asset(owner_id, entry))
            except Exception:
                st.warning("Сохранённый эскиз временно недоступен.")
        elif isinstance(entry, bytes):
            st.image(entry)
        else:
            if role == 'assistant' and isinstance(entry,str) and entry.strip() in {'Создаю сцену.','Создаю ролик.','Runway создаёт сцену.'}:
                st.caption('История: задание было передано в производство. Текущий статус показан ниже.')
            else:
                st.markdown(entry)


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
    chat = st.session_state.setdefault(key, restore_chat(project))
    if chat.get("pending_save"):
        try:
            save_turn(store, owner_id, project_id, **chat["pending_save"])
            chat.pop("pending_save")
        except Exception:
            st.warning("Ответ сохранён в открытом окне. Сохранение истории на сервер временно недоступно.")
            if st.button("Повторить сохранение", key=key + "_retry_save"):
                st.rerun()
            return
    # Preserve a soundtrack still present in a browser session from before the fix.
    cached_files = chat.get('files') or []
    cached_music_key = sha256(b''.join(name.encode() + data for name,mime,data in cached_files if name.lower().endswith(('.mp3','.wav','.m4a')))).hexdigest()
    if any(name.lower().endswith(('.mp3','.wav','.m4a')) for name,mime,data in cached_files) and chat.get('saved_music_key') != cached_music_key:
        from kadrian_editing import remember_music_uploads
        try:
            chat['files'] = remember_music_uploads(store,owner_id,project_id,cached_files)
            chat['saved_music_key'] = cached_music_key
            chat['refresh_music_selection'] = True
        except Exception:
            st.warning('Прикреплённая музыка ещё не сохранена. Новые сцены по просьбе о музыке не запускаются.')
    with st.expander("Память проекта"):
        st.caption("Согласованные факты, стиль, решения и замечания для будущих разговоров.")
        notes = st.text_area("Что Кадриану нужно помнить", value=project.get("kadrian_notes", ""),
                             max_chars=20000, key=key + "_notes")
        if st.button("Сохранить память", key=key + "_save_notes"):
            try:
                store.update_project_if_current(owner_id, project_id, {"kadrian_notes": notes}, project["updated_at"])
            except Exception:
                st.error("Память не сохранена. Возможно, проект изменён в другом окне. Обновите страницу.")
            else:
                st.success("Память сохранена.")
                st.rerun()
    with st.expander("Подключения студии"):
        st.caption("Изображения и речь — OpenAI. Сцены — Runway и HeyGen Video. Ведущие — HeyGen. Музыка — ElevenLabs. Сборка — FFmpeg и Shotstack.")
        for service, configured in connection_status(owner_id, project_id).items():
            st.caption(service + (" — настройки найдены" if configured else " — настройки подключения не найдены"))
        if st.button("Проверить доступ к видеосервису", key=key + "_check_video"):
            try:
                from content_factory_runtime import resolve_live_video_client
                client = resolve_live_video_client(project)
                balance = client.preflight().get("credit_balance")
                st.success(f"Runway доступен. Модель {client.model}. Кредиты: {balance}.")
            except Exception:
                st.error("Доступ к Runway не подтверждён. Проверьте ключ и баланс видеосервиса в настройках студии.")
    st.link_button("Прямая ссылка на Кадриана", "https://agency-w-web.onrender.com/?studio=kadrian")
    from kadrian_editing_ui import render_edit_controls
    try:
        edit_sources, edit_music, edit_replace_audio = render_edit_controls(store, owner_id, project, key, chat)
    except Exception:
        st.warning("Материалы монтажа временно недоступны. Новые сцены по просьбе о музыке не запускаются.")
        edit_sources, edit_music, edit_replace_audio = [], None, False
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
    route = st.selectbox("Производство", list(ROUTES), format_func=ROUTES.get, key=key + "_route")
    previous = chat["messages"][:-2] if len(chat["messages"]) > 2 else []
    latest = chat["messages"][len(previous):]
    if previous:
        with st.expander("Предыдущие сообщения"):
            for role, entry in previous:
                _render_chat_entry(role, entry, store, owner_id)
    turn = st.container()

    with st.container():
        message = st.chat_input("Напишите или наговорите, какой ролик хотите",
                            max_chars=5000, accept_audio=True, accept_file="multiple",
                            file_type=["png", "jpg", "jpeg", "webp", "mp3", "wav", "m4a", "ogg", "mp4", "mov", "webm"],
                            max_upload_size=15)
    if not message:
        with turn:
            for role, entry in latest:
                _render_chat_entry(role, entry, store, owner_id)
    if message:
        from kadrian_editing import local_command, pause_production
        early_command = local_command(message.text.strip())
        if early_command and early_command['action'] == 'stop':
            try:
                pause_production(store,owner_id,project_id)
                st.info(early_command['reply'])
            except Exception:
                st.error('Не удалось подтвердить остановку. Новые заказы не отправлены.')
                return
        if message.files:
            files = [(item.name, item.type or "", item.getvalue()) for item in message.files]
            if len(files) > 8 or sum(len(data) for _, _, data in files) > 40 * 1024 * 1024:
                st.error("Прикрепите до 8 файлов общим размером до 40 МБ.")
                return
            from kadrian_editing import remember_music_uploads
            try:
                files = remember_music_uploads(store,owner_id,project_id,files)
                if any(mime.startswith('audio/') for _,mime,_ in files):
                    chat['refresh_music_selection'] = True
                    edit_music = None
            except Exception:
                st.error('Музыка не сохранена. Нужен MP3, WAV или M4A. Новые сцены не запускаются.')
                return
            chat["files"] = files
        prompt = message.text.strip()
        if message.audio:
            from content_factory_ui import _transcribe_content_audio
            try:
                with turn, st.spinner("Распознаю запись…"):
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
        history = "\n\n".join(f"{role}: {message}" for role, message in chat["messages"][-60:] if isinstance(message, str))
        with turn, st.chat_message("user"):
            st.markdown(prompt)
        draw = re.match(
            r"^(?:пожалуйста[,]?\s+)?(?:нарисуй|перерисуй|покажи|создай|сделай|пришли)\b", prompt.strip(), re.I
        ) and re.search(r"\b(?:портрет\w*|картинк\w*|эскиз\w*|иллюстраци\w*)", prompt, re.I) and not re.search(
            r"\b(?:ролик\w*|видеоролик\w*|видео\w*|мультфильм\w*|ожив\w*|анимиру\w*)", prompt, re.I
        )
        from kadrian_editing import local_command, pause_production, STOP_REPLY
        safe_command = local_command(prompt)
        if safe_command:
            try:
                answer = str(safe_command.get('reply') or '')
                if safe_command['action'] == 'stop':
                    pause_production(store,owner_id,project_id)
                    answer = STOP_REPLY
                if safe_command['action'] == 'music_edit' or safe_command.get('edit_music'):
                    from kadrian_editing_ui import execute_edit_command
                    try:
                        with st.spinner('Накладываю музыку на готовые сцены…'):
                            edited = execute_edit_command(store,owner_id,project_id,prompt,edit_sources,edit_music,edit_replace_audio)
                        answer = (answer + '\n\n' + edited).strip()
                    except Exception as exc:
                        answer = (answer + '\n\n' + (str(exc) if isinstance(exc, ValueError) else 'Монтаж не завершён. Исходники сохранены, новые сцены не создавались.')).strip()
            except Exception:
                st.error('Не удалось подтвердить остановку. Новые видеозапросы этим сообщением не запускались.')
                return
            chat['pending_save'] = {'turn_id':uuid4().hex,'prompt':prompt,'answer':answer,
                                    'brief':chat['brief'],'request':chat['request']}
            st.rerun()
        if draw:
            with turn, st.chat_message("assistant"):
                try:
                    with st.spinner("Кадриан рисует…"):
                        image = _draw_chat_image(prompt, chat["brief"] + "\n" + history, style, chat.get("files", []))
                except Exception as exc:
                    import logging
                    status = getattr(exc, "status_code", None)
                    code = getattr(exc, "code", None)
                    logging.getLogger(__name__).warning("Kadrian image failed: %s status=%s code=%s", type(exc).__name__, status, code)
                    if status in (401, 403):
                        error = "OpenAI отклонил доступ студии к генерации изображений."
                    elif code in ("billing_hard_limit_reached", "insufficient_quota"):
                        error = "OpenAI сообщил, что исчерпан лимит оплаты или квота изображений."
                    elif status == 429:
                        error = "OpenAI ограничил частоту запросов к генератору изображений."
                    elif status == 400:
                        error = "OpenAI отклонил параметры изображения или приложенный образец."
                    elif isinstance(exc, RuntimeError):
                        error = "В студии не настроен ключ OpenAI для изображений."
                    else:
                        error = "Генератор изображений не вернул результат."
                    chat["messages"].extend([("user", prompt), ("assistant", error)])
                    st.rerun()
            try:
                asset = store.put_asset(owner_id, project_id, None, "kadrian-frame.png", image, "image/png")
            except Exception:
                st.error("Эскиз создан, но не сохранён на сервере. Скачайте изображение из предпросмотра перед закрытием окна.")
                chat["messages"].extend([("user", prompt), ("assistant", image)])
                return
            chat["messages"].extend([("user", prompt), ("assistant", asset)])
            chat["pending_save"] = {"turn_id": uuid4().hex, "prompt": prompt, "answer": "Эскиз",
                                    "brief": chat["brief"], "request": chat["request"], "image_asset": asset}
            st.rerun()
        if route == "runway":
            from kadrian_actions import command, enqueue_runway, status_text
            try:
                with turn, st.spinner("Выполняю задание…"):
                    decision = command(ask_ai, project, prompt, history, chat['brief'], style)
                    if decision['action'] == 'stop':
                        pause_production(store,owner_id,project_id)
                        answer = STOP_REPLY
                    elif decision['action'] == 'music_edit':
                        from kadrian_editing_ui import execute_edit_command
                        answer = execute_edit_command(store,owner_id,project_id,prompt,edit_sources,edit_music,edit_replace_audio)
                    elif decision['action'] == 'status':
                        answer = status_text(store, owner_id, project)
                    elif decision['action'] == 'create':
                        decision['music_only'] = music_only
                        chat['brief'] = decision['brief']
                        reference = None
                        images = [(name, mime, data) for name, mime, data in chat.get('files', [])
                                  if mime in {'image/png', 'image/jpeg', 'image/webp'}]
                        if images:
                            name, mime, data = images[-1]
                            reference = store.put_asset(owner_id, project_id, None, name, data, mime)
                        else:
                            reference = next((entry for role, entry in reversed(chat['messages'])
                                              if isinstance(entry, dict) and entry.get('storage_path')), None)
                        request = chat.setdefault('auto_request', uuid4().hex)
                        job, created = enqueue_runway(store, owner_id, project, decision, style, request, reference, request_prompt=prompt)
                        answer = job['progress']['message'] if created else ((job.get('progress') or {}).get('message') or 'Задание уже выполняется. Готовое видео появится здесь.')
                        chat.pop('auto_request', None)
                        chat['brief'] = decision['brief']
                    else:
                        answer = str(decision.get('reply') or 'Готов принять задание.')[:2000]
            except ValueError as exc:
                answer = str(exc)
            except Exception:
                answer = 'Не удалось связаться с сервисом. Задание не продублировано. Повторите запрос позже.'
        else:
            with turn, st.chat_message("assistant"), st.spinner("Кадриан готовит ответ…"):
                answer = ask_ai(
                    director_instructions(),
                    f"Данные выбранного проекта (исходные материалы):\n{project_context(project)}\n"
                    f"Производство: {ROUTES[route]}. В интервью HeyGen два участника показаны сменой отдельных планов.\n"
                    f"Формат: {style}. Звук: {'только музыка, без диктора' if music_only else 'с озвучкой'}.\n"
                    f"Вложения: {[name for name, _, _ in chat.get('files', [])]}. Изображения переданы для просмотра. Аудио и видео доступны только в интерфейсе.\n"
                    f"Предыдущий замысел:\n{chat['brief']}\nПереписка:\n{history}\nПоследнее пожелание: {prompt}",
                    model=os.getenv("KADRIAN_CHAT_MODEL", "gpt-6.1-terra"),
                    reasoning_effort="medium",
                    input_images=["data:" + mime + ";base64," + b64encode(data).decode("ascii")
                                  for _, mime, data in chat.get("files", [])
                                  if mime in {"image/png", "image/jpeg", "image/webp"}],
                )
        if not answer or answer.startswith(("Ошибка OpenAI", "Ключ OpenAI", "Не удалось")):
            st.error(answer or "Кадриан не получил ответ сервиса. Попробуйте отправить пожелание ещё раз.")
            return
        chat["messages"].extend([("user", prompt), ("assistant", answer)])
        if route != "runway":
            chat["brief"] = f"Пожелание партнёра: {prompt}\nПлан Кадриана: {answer}"[:6000]
        chat["request"] = uuid4().hex
        chat["pending_save"] = {"turn_id": chat["request"], "prompt": prompt, "answer": answer,
                                "brief": chat["brief"], "request": chat["request"]}
        st.rerun()

    render_provider_progress(store, owner_id, project_id)
    if chat.get("files"):
        st.caption("Изображения можно использовать как опорный кадр сцены.")
    if route != "runway":
        render_production(owner_id, project, store, ask_ai, chat, route, style, key + "_" + route)
        return
