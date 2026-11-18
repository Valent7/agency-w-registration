"""Partner-facing video chat on the Agency's existing production worker."""
from base64 import b64decode, b64encode
import re
import os

from kadrian_films import FilmStore, films, create_film, change_film, save_drafts
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
            if role == "assistant" and isinstance(entry, str) and entry.strip() in {
                "Создаю сцену.", "Создаю ролик.", "Runway создаёт сцену."
            }:
                st.caption("История: задание было передано в производство. Актуальный статус — ниже.")
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
    base_key = f"kadrian_{owner_id}_{project_id}"
    raw_store = store
    # Flush responses before any navigation; never leave a paid job's reply behind.
    for state_key in list(st.session_state):
        if state_key.startswith(base_key + "_film_") and isinstance(st.session_state[state_key], dict):
            previous_chat = st.session_state[state_key]
            if previous_chat.get('film_id'):
                try:
                    save_drafts(raw_store, owner_id, project_id, previous_chat['film_id'], state_key, st.session_state)
                except Exception:
                    st.error('Изменения сценария ещё не сохранены. Переключение фильмов остановлено.')
                    return
            if previous_chat.get('unsaved_image'):
                unsaved = previous_chat['unsaved_image']
                try:
                    previous_store = FilmStore(raw_store, owner_id, project_id, previous_chat['film_id'])
                    asset = previous_store.put_asset(owner_id, project_id, None, 'kadrian-frame.png', unsaved['data'], 'image/png')
                    previous_chat['pending_save'] = {k: v for k, v in unsaved.items() if k != 'data'}
                    previous_chat['pending_save']['image_asset'] = asset
                    previous_chat.pop('unsaved_image')
                except Exception:
                    st.warning('Эскиз создан, но ещё не сохранён. Переключение фильмов остановлено до сохранения.')
                    st.image(unsaved['data'])
                    st.download_button('Скачать несохранённый эскиз', unsaved['data'], 'kadrian-frame.png', 'image/png',
                                       key=state_key + '_download_unsaved')
                    if st.button('Повторить сохранение эскиза', key=state_key + '_retry_image'):
                        st.rerun()
                    return
            if previous_chat.get("pending_save"):
                try:
                    previous_store = FilmStore(raw_store, owner_id, project_id, previous_chat['film_id'])
                    save_turn(previous_store, owner_id, project_id, **previous_chat['pending_save'])
                    previous_chat.pop('pending_save')
                except Exception:
                    st.warning("Сначала нужно сохранить ответ. Переключение фильмов временно остановлено.")
                    if st.button("Повторить сохранение", key=base_key + '_retry_save'):
                        st.rerun()
                    return
    try:
        project = raw_store.get_project(owner_id, project_id)
        catalog = films(project)
        # Preserve attachments still held by a pre-upgrade browser window.
        old_window = st.session_state.get(base_key)
        if isinstance(old_window, dict) and not old_window.get('films_preserved'):
            legacy_store = FilmStore(raw_store, owner_id, project_id, catalog['legacy_id'])
            if old_window.get('pending_save'):
                save_turn(legacy_store, owner_id, project_id, **old_window['pending_save'])
            if old_window.get('files') and not catalog['items'][catalog['legacy_id']].get('attachments'):
                legacy_store.save_attachments(old_window['files'])
            old_window['films_preserved'] = True
            project = raw_store.get_project(owner_id, project_id)
            catalog = films(project)
        selector_key = base_key + '_selected_film'
        if st.button("🎬 Новый фильм / мультфильм", key=base_key + '_new_film'):
            token_key = base_key + '_new_film_token'
            token = st.session_state.setdefault(token_key, str(uuid4()))
            created = create_film(raw_store, owner_id, project_id, token)
            st.session_state[selector_key] = created['id']
            st.session_state.pop(token_key, None)
            st.rerun()
        film_ids = sorted(catalog['items'], key=lambda fid: (
            fid != catalog['legacy_id'], catalog['items'][fid].get('created_at', ''), fid))
        if selector_key not in st.session_state and st.query_params.get('film') in film_ids:
            st.session_state[selector_key] = st.query_params['film']
        film_id = st.selectbox("Сохранённые фильмы", film_ids,
                               format_func=lambda value: catalog['items'][value]['title'], key=selector_key)
        st.query_params['film'] = film_id
        store = FilmStore(raw_store, owner_id, project_id, film_id)
        project = store.get_project(owner_id, project_id)
    except Exception:
        st.error("Не удалось открыть или сохранить фильм. История сохранённых фильмов не удалена. Повторите действие.")
        return
    key = base_key + '_film_' + film_id
    item = films(project)['items'][film_id]
    for suffix, value in item.get('drafts', {}).items():
        if key + suffix not in st.session_state:
            st.session_state[key + suffix] = value
    chat = restore_chat(project)
    chat['film_id'] = film_id
    try:
        chat['files'] = store.restore_attachments()
    except Exception:
        st.error("Вложения этого фильма временно недоступны. Производство остановлено, чтобы не потерять образцы.")
        return
    st.session_state[key] = chat
    st.caption("Фильм: " + film_id + ". Переключение не отменяет и не повторяет заказы.")
    title = st.text_input("Название фильма", value=item['title'], key=key + '_title', max_chars=120)
    if title.strip() != item['title'] and st.button("Сохранить название", key=key + '_rename'):
        try:
            change_film(raw_store, owner_id, project_id, film_id, lambda film: film.update(title=title.strip()))
        except Exception:
            st.error("Название не сохранено. Повторите действие.")
        else:
            st.rerun()
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
                try:
                    store.save_attachments([])
                except Exception:
                    st.error("Не удалось сохранить изменение вложений.")
                    return
                st.rerun()
    settings = item.get('settings') or {}
    for suffix, default in (('_style', 'Живое видео'), ('_music', False), ('_route', 'runway')):
        if key + suffix not in st.session_state:
            st.session_state[key + suffix] = settings.get(suffix, default)
    styles = {"Живое видео": "live", "Мультфильм": "cartoon"}
    style = styles[st.radio("Формат", list(styles), horizontal=True, key=key + "_style")]
    music_only = style == "cartoon" and st.checkbox("Только музыка", key=key + "_music")
    route = st.selectbox("Производство", list(ROUTES), format_func=ROUTES.get, key=key + "_route")
    current_settings = {suffix: st.session_state.get(key + suffix) for suffix in ('_style', '_music', '_route')}
    if current_settings != settings:
        try:
            change_film(raw_store, owner_id, project_id, film_id,
                        lambda film: film.update(settings=current_settings))
        except Exception:
            st.error("Настройки фильма не сохранены. Повторите действие.")
            return
    st.caption("Переписка — история обсуждения. Текущее состояние заказов показано в статусе производства.")
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
                            max_upload_size=15, key=key + "_input")
    if not message:
        with turn:
            for role, entry in latest:
                _render_chat_entry(role, entry, store, owner_id)
    if message:
        if message.files:
            files = [(item.name, item.type or "", item.getvalue()) for item in message.files]
            if len(files) > 8 or sum(len(data) for _, _, data in files) > 40 * 1024 * 1024:
                st.error("Прикрепите до 8 файлов общим размером до 40 МБ.")
                return
            try:
                store.save_attachments(files)
            except Exception:
                st.error("Вложения не сохранены. Задание не запущено. Повторите загрузку.")
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
                    chat["pending_save"] = {"turn_id": uuid4().hex, "prompt": prompt, "answer": error,
                                            "brief": chat['brief'], "request": chat['request']}
                    st.rerun()
            try:
                asset = store.put_asset(owner_id, project_id, None, "kadrian-frame.png", image, "image/png")
            except Exception:
                chat['unsaved_image'] = {'data': image, 'turn_id': uuid4().hex, 'prompt': prompt,
                                         'answer': 'Эскиз', 'brief': chat['brief'], 'request': chat['request']}
                st.rerun()
            chat["messages"].extend([("user", prompt), ("assistant", asset)])
            chat["pending_save"] = {"turn_id": uuid4().hex, "prompt": prompt, "answer": "Эскиз",
                                    "brief": chat["brief"], "request": chat["request"], "image_asset": asset}
            st.rerun()
        if route == "runway":
            from kadrian_actions import command, enqueue_runway, status_text
            try:
                with turn, st.spinner("Выполняю задание…"):
                    decision = command(ask_ai, project, prompt, history, chat['brief'], style)
                    if decision['action'] == 'status':
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
                        job, created = enqueue_runway(store, owner_id, project, decision, style, request, reference)
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
            error = answer or "Кадриан не получил ответ сервиса. Попробуйте отправить пожелание ещё раз."
            chat['pending_save'] = {"turn_id": uuid4().hex, "prompt": prompt, "answer": error,
                                    "brief": chat['brief'], "request": chat['request']}
            st.rerun()
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
    try:
        save_drafts(raw_store, owner_id, project_id, film_id, key, st.session_state)
    except Exception:
        st.warning('Изменения полей сценария пока не сохранены. Повторите сохранение перед закрытием окна.')
