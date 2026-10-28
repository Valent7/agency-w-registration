"""A focused content workspace over the Agency's existing tools and archives."""
from __future__ import annotations

from pathlib import Path

import streamlit as st


TOOLS = {
    "texts": "Тексты",
    "images": "Изображения",
    "carousel": "Карусели",
    "video": "Видео",
    "reference": "Своё по видео-образцу",
    "speech": "Озвучка",
    "music": "Музыка",
    "ready": "Готовый контент",
    "library": "Мои генерации",
    "editor": "Редактор",
    "prompts": "Готовые задания",
    "search": "Поиск",
    "calendar": "Календарь публикаций",
    "connections": "Площадки",
    "project": "Настройки проекта",
    "help": "Как всё работает",
}


def _navigate(owner_id, tool, **values):
    st.session_state[f"content_studio_{owner_id}_next"] = tool
    for name, value in values.items():
        st.session_state[f"content_studio_{owner_id}_{name}"] = value
    st.rerun()


def _context(owner_id):
    """Share the established owner/project selection with publishing controls."""
    from content_factory_autopilot_ui import _runtime

    try:
        store, service, capabilities = _runtime()
        projects = store.list_projects(owner_id)
    except Exception:
        st.info("Не удалось открыть сохранённые проекты. Попробуйте обновить страницу.")
        return None, None, None
    if not projects:
        st.info("Сначала сохраните проект: его стиль и аудитория будут использоваться во всех инструментах.")
        if st.button("Создать проект", key=f"content_studio_{owner_id}_new_project"):
            _navigate(owner_id, "project")
        return store, service, None
    key = f"cf_auto_{owner_id}_selected_project"
    options = [item["id"] for item in projects]
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]
    labels = {item["id"]: item["name"] for item in projects}
    selected = st.selectbox("Проект", options, format_func=labels.get, key=key)
    return store, service, next(item for item in projects if item["id"] == selected)


def _kind(job):
    if job.get("studio_tool") in {"text", "image", "audio", "reference"}:
        return job["studio_tool"]
    return {"reel": "video", "carousel": "carousel"}.get(job.get("format"), "ready")


def _media(owner_id, job, store):
    assets = job.get("assets") or []
    if not assets:
        st.caption("Оформление ещё не создано." if job.get("state") == "queued" else "Текстовый материал")
        return
    try:
        resolved = store.resolve_assets(owner_id, assets[:1])
    except Exception:
        st.caption("Предпросмотр временно недоступен. Откройте материал, чтобы повторить.")
        return
    for asset in resolved:
        source = asset.get("url") or asset.get("path")
        if not source:
            continue
        mime = str(asset.get("mime_type") or "")
        if mime.startswith("image/"):
            st.image(source, width="stretch")
        elif mime.startswith("video/"):
            st.video(source)
        elif mime.startswith("audio/"):
            st.audio(source, format=mime)


def _studio_detail(owner_id, job, store):
    content = job.get("content") or {}
    if job.get("studio_tool") == "reference":
        st.subheader(content.get("title") or "Разбор видео-образца")
        st.write(content.get("source_summary") or "")
        plan = content.get("reference_plan") or {}
        st.markdown("**Ваш новый сценарий**")
        st.write(plan.get("narration") or "")
        if st.button("Продолжить создание своего видео", key=f"studio_reference_{owner_id}_{job['id']}_continue", type="primary"):
            _navigate(owner_id, "reference", reference_job_id=job["id"])
        return
    st.subheader(content.get("title") or "Материал")
    _media(owner_id, job, store)
    if content.get("text"):
        st.write(content["text"])
        st.download_button("Скачать текст", content["text"], file_name=f"content-{job['id']}.txt",
                           key=f"studio_detail_{owner_id}_{job['id']}_text")
    for asset in job.get("assets") or []:
        if st.button("Подготовить файл для скачивания", key=f"studio_detail_{owner_id}_{job['id']}_{asset.get('filename')}_prepare"):
            try:
                st.session_state[f"studio_download_{owner_id}_{job['id']}"] = (
                    store.read_asset(owner_id, asset), asset.get("filename") or "content", asset.get("mime_type") or "application/octet-stream")
            except Exception:
                st.error("Не удалось загрузить сохранённый файл. Повторите скачивание позже.")
        download = st.session_state.get(f"studio_download_{owner_id}_{job['id']}")
        if download:
            st.download_button("Скачать файл", download[0], file_name=download[1], mime=download[2],
                               key=f"studio_detail_{owner_id}_{job['id']}_{asset.get('filename')}_download")
    if job.get("error"):
        st.warning("Сохранение файла требует внимания. Исходный результат может оставаться в инструменте создания.")


def _manual_archive(owner_id, packages, query="", kind="all"):
    manual = [package for package in packages if (kind in {"all", "ready"}) and
              isinstance(package.get("items"), list) and package["items"] and
              all(isinstance(item, dict) for item in package["items"]) and
              package.get("package_id") and (not query or query in str(package["items"]).casefold())]
    if manual:
        st.subheader("Материалы из редактора")
        for package in manual:
            items = package["items"]
            with st.container(border=True):
                st.write(str(items[0].get("title") or package.get("week_title") or "Сохранённый материал"))
                st.caption(f"Материалов в комплекте: {len(items)}")
                if st.button("Открыть в редакторе", key=f"studio_manual_{owner_id}_{package['package_id']}"):
                    st.session_state[f"content_factory_package_{owner_id}"] = package
                    st.session_state[f"content_factory_stage_{owner_id}"] = "result"
                    st.session_state[f"content_factory_v2_loaded_{owner_id}"] = True
                    _navigate(owner_id, "editor")
    return bool(manual)


def _library(owner_id, owner_name, target_profile, render_manual):
    from content_factory_autopilot_ui import _materials, STATES
    from content_studio_catalog import list_manual_packages

    packages = list_manual_packages(owner_id)
    current = st.session_state.get(f"content_factory_package_{owner_id}")
    if isinstance(current, dict) and current.get("owner_telegram_id") == owner_id and current.get("items"):
        if not any(item.get("package_id") == current.get("package_id") for item in packages):
            packages.insert(0, current)
    store, service, project = _context(owner_id)
    if not store or not project:
        _manual_archive(owner_id, packages)
        return
    st.caption("Тексты, изображения, озвучка и материалы для публикации — в одном месте.")
    try:
        jobs = store.list_jobs(owner_id, project["id"], limit=100)
    except Exception:
        st.error("Не удалось загрузить материалы. Сохранённые файлы остаются в вашем проекте.")
        _manual_archive(owner_id, packages)
        return
    detail = st.session_state.get(f"content_studio_{owner_id}_detail")
    if detail:
        selected = next((item for item in jobs if item["id"] == detail), None)
        if selected:
            if st.button("← Все генерации", key=f"studio_library_{owner_id}_back"):
                st.session_state.pop(f"content_studio_{owner_id}_detail", None)
                st.rerun()
            if selected.get("state") == "studio":
                _studio_detail(owner_id, selected, store)
            else:
                _materials(owner_id, project, [selected], store, service, workspace=True)
            return
        st.session_state.pop(f"content_studio_{owner_id}_detail", None)
    search, filters = st.columns([2, 1])
    query = search.text_input("Поиск по материалам", key=f"studio_library_{owner_id}_search").strip().casefold()
    kind = filters.selectbox("Тип материала", ["all", "ready", "text", "image", "carousel", "video", "audio", "reference"],
                            format_func=lambda value: {"all": "Все", "ready": "Готовый контент", "text": "Тексты", "image": "Изображения", "carousel": "Карусели", "video": "Видео", "audio": "Озвучка", "reference": "Разборы видео"}[value],
                            key=f"studio_library_{owner_id}_kind")
    favorites_only = st.checkbox("Только избранное", key=f"studio_library_{owner_id}_favorites")
    show_cancelled = st.checkbox("Показать отменённые", key=f"studio_library_{owner_id}_cancelled")
    selected = [job for job in jobs if (show_cancelled or job.get("state") != "cancelled")
                and (not favorites_only or job.get("favorite") is True)
                and (kind == "all" or _kind(job) == kind)
                and (not query or query in str(job.get("content") or {}).casefold() or query in str(job.get("prompt") or job.get("topic") or "").casefold())]
    if selected:
        st.caption(f"Материалов: {len(selected)}")
    for start in range(0, len(selected), 3):
        for column, job in zip(st.columns(3), selected[start:start + 3]):
            with column, st.container(border=True):
                _media(owner_id, job, store)
                content = job.get("content") or {}
                st.markdown("**" + str(content.get("title") or job.get("topic") or "Новый материал")[:140] + "**")
                state_label = "Сохранено" if job.get("state") == "studio" and job.get("studio_complete") else STATES.get(job.get("state"), "Сохранение не завершено")
                st.caption(state_label)
                if st.button("Открыть", key=f"studio_library_{owner_id}_{job['id']}_open", width="stretch"):
                    st.session_state[f"content_studio_{owner_id}_detail"] = job["id"]
                    st.session_state[f"cf_auto_{owner_id}_{project['id']}_filter"] = "all"
                    st.rerun()
                favorite = job.get("favorite") is True
                if st.button("♥ В избранном" if favorite else "♡ В избранное", key=f"studio_library_{owner_id}_{job['id']}_favorite"):
                    try:
                        from content_studio_catalog import set_favorite
                        set_favorite(store, owner_id, job["id"], not favorite)
                        st.rerun()
                    except Exception:
                        st.error("Не удалось изменить избранное. Обновите материалы и повторите.")
    manual = False if favorites_only else _manual_archive(owner_id, packages, query, kind)
    if not selected and not manual:
        st.info("Здесь появятся ваши результаты. Выберите инструмент слева и создайте первый материал.")


def _help(owner_id):
    st.subheader("От идеи до готового материала")
    for title, description in (
        ("1. Выберите задачу", "Текст, изображение, карусель, видео или озвучка — каждый инструмент открывается отдельно."),
        ("2. Опишите результат", "Напишите обычными словами, что хотите получить. Настройки проекта помогают сохранить ваш стиль."),
        ("3. Найдите результат", "Все сохранённые результаты доступны в «Мои генерации». Там можно открыть материал, проверить и скачать его."),
        ("4. Утвердите публикацию", "Подключите площадки и задайте расписание. Правила проверки и автопилота находятся в настройках проекта."),
    ):
        with st.container(border=True):
            st.markdown("**" + title + "**")
            st.write(description)
    if st.button("Открыть мои генерации", key=f"studio_help_{owner_id}_library", type="primary"):
        _navigate(owner_id, "library")


def _preserve_drafts(owner_id):
    # Streamlit cleans widget keys when a page stops rendering them. Keep text
    # and settings while the owner moves between tools, without retaining file
    # upload widgets, button states, or credential inputs.
    suffixes = ("_prompt", "_voice", "_direction", "_size", "_name", "_product", "_audience",
                "_goal", "_website", "_language", "_tone", "_cta", "_facts", "_examples",
                "_visual_style", "_avoid", "_color", "_platforms", "_formats", "_video_mode",
                "_timezone", "_times", "_days", "_limit", "_review", "_enabled", "_topic",
                "_format", "_text", "_feedback", "_style", "_target_seconds")
    for key in list(st.session_state):
        scoped = key.startswith((f"studio_tool_{owner_id}_", f"cf_auto_{owner_id}_", f"studio_reference_{owner_id}_"))
        manual = key in {f"content_factory_request_{owner_id}", f"cf2_format_{owner_id}", f"content_studio_{owner_id}_video_style"} or (
            key.startswith("cf2_u_") and key.endswith(f"_{owner_id}"))
        if (scoped and key.endswith(suffixes)) or manual:
            st.session_state[key] = st.session_state[key]


def _mobile_navigation(owner_id, widget_key):
    st.session_state[f"content_studio_{owner_id}_next"] = st.session_state[widget_key]


def render_content_studio(owner_id, owner_name, ask_ai_fn, generate_illustration_fn,
                          create_avatar_video_fn, get_avatar_video_fn, *, target_profile=None, render_manual):
    from agency_access_control import is_agency_access_revoked
    from content_factory_autopilot_ui import render_content_autopilot
    from content_studio_tools import render_text_tool, render_image_tool, render_speech_tool

    try:
        owner_id = int(owner_id)
    except (ValueError, TypeError):
        return
    if owner_id <= 0 or is_agency_access_revoked(owner_id):
        return
    _preserve_drafts(owner_id)
    key = f"content_studio_{owner_id}_tool"
    next_tool = st.session_state.pop(f"content_studio_{owner_id}_next", None)
    if next_tool in TOOLS:
        st.session_state[key] = next_tool
    if st.session_state.get(key) not in TOOLS:
        st.session_state[key] = "ready"
    mobile_key = f"content_studio_{owner_id}_mobile_tool"
    st.session_state[mobile_key] = st.session_state[key]
    # Native controls retain keyboard/screen-reader behavior. Only visibility
    # changes with viewport width; the rest of the Agency theme is untouched.
    st.html(f"""<style>
      .st-key-studio_mobile_{owner_id} {{ display: none; }}
      @media (max-width: 700px) {{
        .st-key-studio_mobile_{owner_id} {{ display: block; }}
        .st-key-studio_nav_{owner_id} {{ display: none; }}
      }}
    </style>""")
    with st.container(key=f"studio_mobile_{owner_id}"):
        st.selectbox("Раздел завода", list(TOOLS), format_func=TOOLS.get,
                     key=mobile_key, on_change=_mobile_navigation, args=(owner_id, mobile_key))
    navigation, workspace = st.columns([1, 4], gap="large")
    with navigation, st.container(key=f"studio_nav_{owner_id}"):
        logo = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
        if logo.is_file():
            st.image(str(logo), width=82)
        st.markdown("**Контент-завод W**")
        st.caption("Инструменты и материалы")
        tool = st.radio("Раздел завода", list(TOOLS), format_func=TOOLS.get, key=key, label_visibility="collapsed")
    with workspace:
        st.markdown("## " + TOOLS[tool])
        if tool in {"texts", "images", "speech", "reference", "prompts"}:
            from content_studio_templates import apply_template_handoff
            store, _, project = _context(owner_id)
            if project:
                apply_template_handoff(owner_id, project, st.session_state)
            if tool == "reference":
                from content_studio_reference_ui import render_reference_tool
                from content_factory_core import ContentFactoryService
                render_reference_tool(owner_id, project, store, ContentFactoryService(store) if store else None)
            elif tool == "prompts":
                from content_studio_templates import render_templates
                render_templates(owner_id, project)
            else:
                {"texts": lambda: render_text_tool(owner_id, project, store, ask_ai_fn),
                 "images": lambda: render_image_tool(owner_id, project, store, generate_illustration_fn),
                 "speech": lambda: render_speech_tool(owner_id, project, store)}[tool]()
        elif tool in {"ready", "carousel", "video"}:
            if tool == "video":
                if st.button("Создать своё по видео-образцу", key=f"studio_video_{owner_id}_reference", type="primary"):
                    _navigate(owner_id, "reference")
                st.caption("Или опишите собственную идею ниже. Создание запускается только по вашей кнопке.")
            render_content_autopilot(owner_id, owner_name, target_profile=target_profile, section="create",
                                     format_filter={"carousel": ["carousel"], "video": ["reel"]}.get(tool),
                                     video_style=st.session_state.get(f"content_studio_{owner_id}_video_style") if tool == "video" else None)
        elif tool in {"library", "search"}:
            _library(owner_id, owner_name, target_profile, render_manual)
        elif tool == "editor":
            render_manual(owner_id, owner_name, ask_ai_fn, generate_illustration_fn,
                          create_avatar_video_fn, get_avatar_video_fn, target_profile=target_profile)
        elif tool in {"calendar", "connections", "project"}:
            render_content_autopilot(owner_id, owner_name, target_profile=target_profile, section=tool)
        elif tool == "music":
            st.subheader("Музыка для ваших материалов")
            st.info("Генератор музыки ещё не подключён к Агентству. Создание и оплата музыки здесь пока недоступны.")
        elif tool == "help":
            _help(owner_id)
