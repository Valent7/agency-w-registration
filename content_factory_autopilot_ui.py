"""Partner-owned editorial calendar and social publishing controls."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st


PLATFORMS = {"instagram": "Instagram / Reels", "vk": "ВКонтакте", "threads": "Threads"}
FORMATS = {"post": "Пост", "carousel": "Карусель", "reel": "Видео / Reels"}
STATES = {
    "queued": "Ожидает создания",
    "generating": "Создаётся",
    "ready": "Готов к проверке",
    "scheduled": "В расписании",
    "publishing": "Публикуется",
    "pending": "Площадка обрабатывает медиа",
    "published": "Опубликован",
    "failed": "Нужна помощь",
    "needs_connection": "Требуется подключение площадки",
    "reconcile": "Проверяется результат отправки",
    "cancelled": "Отменён",
    "paused": "На паузе",
}
_NEW = "__new_project__"


def _runtime():
    # Keep loading the existing manual factory possible when a deployment has
    # not yet installed the autopilot's database or operator configuration.
    from content_factory_core import ContentFactoryService
    from content_factory_runtime import build_store, get_capabilities

    store = build_store()
    return store, ContentFactoryService(store), get_capabilities()


def _process_queue(store, owner_id, project_id):
    from content_factory_runtime import get_generator
    from content_factory_worker import Worker

    return Worker(store, get_generator()).run_once(
        owner_id=owner_id, project_id=project_id, max_jobs=1
    )


def _connect_url(owner_id, project_id, platform):
    try:
        import content_factory_runtime

        connect = getattr(content_factory_runtime, "get_connect_url", None)
        url = connect(owner_id, project_id, platform) if connect else None
        return url if isinstance(url, str) and url.startswith("https://") else None
    except Exception:
        return None


def _seed_project(target_profile=None):
    audience = "Предприниматели, эксперты и руководители, которым нужно освободить время от рутины."
    if isinstance(target_profile, str) and target_profile.strip():
        audience = target_profile.strip()[:2000]
    elif isinstance(target_profile, dict):
        for name in ("audience", "target_audience", "description", "summary"):
            if isinstance(target_profile.get(name), str) and target_profile[name].strip():
                audience = target_profile[name].strip()[:2000]
                break
    return {
        "name": "Агентство W",
        "product": "Цифровая команда ИИ-помощников для предпринимателя: подготовка контента, поиск подходящих людей и работа с диалогами. Миссия — возвращать человеку время.",
        "audience": audience,
        "language": "Русский",
        "tone": "Живой, понятный, профессиональный. Без давления и обещаний гарантированного результата.",
        "goal": "Познакомить целевую аудиторию с Агентством W и пригласить изучить его возможности.",
        "website": "https://agency-w.streamlit.app/",
        "cta": "Узнайте больше об Агентстве W на сайте.",
        "brand_color": "#D4AF37",
        "platforms": list(PLATFORMS),
        "formats": list(FORMATS),
        "timezone": "Europe/Berlin",
        "publish_times": ["10:00"],
        "automation_enabled": False,
        "review_required": False,
        "max_daily_generations": 6,
        "plan_days": 7,
    }


def _action_error(action, exc):
    # Only locally validated user input may be displayed verbatim. Provider and
    # storage errors can contain request details, so never render them here.
    detail = str(exc) if isinstance(exc, ValueError) else "Проверьте подключение сервиса и повторите действие."
    st.error(f"{action}: {detail}")


def _project_form(owner_id, project, service, target_profile):
    existing = bool(project)
    values = {**_seed_project(target_profile), **(project or {})}
    key = f"cf_auto_{owner_id}_{values.get('id') or 'new'}"
    st.caption("Расскажите о своём проекте один раз. ИИ будет опираться на эти настройки при подготовке каждого материала.")
    with st.form(key + "_project_form"):
        name = st.text_input("Название проекта", value=values["name"], max_chars=120, key=key + "_name")
        product = st.text_area("Что продвигаем", value=values["product"], height=120, key=key + "_product")
        audience = st.text_area("Для кого создаём контент", value=values["audience"], height=100, key=key + "_audience")
        goal = st.text_input("Цель публикаций", value=values["goal"], key=key + "_goal")
        website = st.text_input("Сайт проекта", value=values["website"], key=key + "_website")
        language = st.text_input("Язык публикаций", value=values["language"], key=key + "_language")
        tone = st.text_area("Голос бренда", value=values["tone"], height=90, key=key + "_tone")
        cta = st.text_input("Желаемое действие читателя", value=values["cta"], key=key + "_cta")
        brand_color = st.color_picker("Цвет оформления", value=values["brand_color"], key=key + "_color")
        platforms = st.multiselect("Площадки", list(PLATFORMS), default=values["platforms"], format_func=PLATFORMS.get, key=key + "_platforms")
        formats = st.multiselect("Форматы", list(FORMATS), default=values["formats"], format_func=FORMATS.get, key=key + "_formats")
        timezone = st.text_input("Часовой пояс расписания", value=values["timezone"], help="Например: Europe/Berlin или Europe/Moscow. Время публикаций учитывает переход на летнее и зимнее время.", key=key + "_timezone")
        publish_times = st.text_input("Время публикаций", value=", ".join(values["publish_times"]), help="В местном времени проекта, через запятую: 10:00, 18:30. Каждый слот создаёт материал для каждой выбранной площадки.", key=key + "_times")
        left, right = st.columns(2)
        days = left.number_input("Планировать на дней вперёд", min_value=1, max_value=30, value=int(values["plan_days"]), key=key + "_days")
        daily_limit = right.number_input("Лимит созданий в сутки", min_value=1, max_value=60, value=int(values["max_daily_generations"]), help="Общий предел генераций по всем площадкам этого проекта. Контент сверх лимита ждёт следующего дня.", key=key + "_limit")
        review = st.checkbox("Проверять каждый материал перед публикацией", value=bool(values["review_required"]), key=key + "_review")
        automatic = st.checkbox("Включить автопилот", value=bool(values["automation_enabled"]), help="ИИ создаёт материалы и публикует их в выбранные аккаунты по расписанию. При включённой проверке сначала ждёт вашего утверждения.", key=key + "_enabled")
        saved = st.form_submit_button("Сохранить проект" if existing else "Создать проект", type="primary", width="stretch")
    if saved:
        data = {
            "name": name, "product": product, "audience": audience, "goal": goal,
            "website": website, "language": language, "tone": tone, "cta": cta,
            "brand_color": brand_color, "platforms": platforms, "formats": formats,
            "timezone": timezone, "publish_times": [part.strip() for part in publish_times.split(",") if part.strip()],
            "plan_days": int(days), "max_daily_generations": int(daily_limit),
            "review_required": review, "automation_enabled": automatic,
        }
        if existing:
            data["id"] = project["id"]
        try:
            result = service.save_project(owner_id, data)
            st.session_state[f"cf_auto_{owner_id}_next_project"] = result["id"]
            st.session_state[f"cf_auto_{owner_id}_notice"] = "Настройки проекта сохранены. Подключите площадки и заполните контент-план."
            st.rerun()
        except Exception as exc:
            _action_error("Не удалось сохранить проект", exc)


def _connections(owner_id, project, store, capabilities):
    if not project:
        st.info("Сначала создайте проект во вкладке «Проект».")
        return
    project_id = project["id"]
    connections = {item["platform"]: item for item in store.list_connections(owner_id, project_id)}
    if not capabilities.get("encryption_configured"):
        st.info("Подключение аккаунтов пока недоступно. Администратору нужно завершить защищённую настройку сервиса публикаций.")
    st.caption("Подключите аккаунты, в которых этот проект будет публиковаться. Разрешения на публикацию нужны отдельно от входа в Агентство и поиска аудитории.")
    for platform in project.get("platforms", []):
        if platform not in PLATFORMS:
            continue
        connection = connections.get(platform)
        key = f"cf_auto_{owner_id}_{project_id}_{platform}"
        with st.container(border=True):
            st.markdown(f"#### {PLATFORMS[platform]}")
            if connection:
                st.success(f"Сохранено подключение: {connection.get('display_name') or connection.get('account_id') or PLATFORMS[platform]}")
                st.caption("Рабочий доступ к публикации проверяется при обращении к площадке. Сохранение подключения само по себе не подтверждает разрешения.")
                if st.button("Отключить аккаунт", key=key + "_disconnect"):
                    try:
                        store.delete_connection(owner_id, project_id, platform)
                        st.rerun()
                    except Exception as exc:
                        _action_error("Не удалось отключить аккаунт", exc)
            if platform in {"instagram", "threads"}:
                display = "Instagram" if platform == "instagram" else "Threads"
                connect_url = _connect_url(owner_id, project_id, platform)
                if connect_url:
                    verb = "Переподключить" if connection else "Подключить"
                    st.link_button(f"{verb} {display}", connect_url, type="primary", width="stretch")
                    st.caption(f"Откроется {display}. Войдите в свой аккаунт и разрешите Контент-заводу публиковать материалы.")
                elif not connection:
                    st.info(f"Подключение {display} через вход в аккаунт пока не настроено. Администратор может подготовить его или подключить аккаунт вручную.")
                if platform == "instagram":
                    st.caption("Для публикаций подходит профессиональный Instagram-аккаунт.")
                manual = st.expander("Подключить вручную", expanded=False)
            else:
                st.caption("Подключение сообщества ВК выполняет его администратор. Посты, фото и видео размещаются на стене сообщества.")
                manual = st.container()
            with manual:
                instructions = {
                    "instagram": "Разрешения: instagram_business_basic, instagram_business_content_publish.",
                    "threads": "Разрешения: threads_basic, threads_content_publish.",
                    "vk": "Нужен ключ сообщества с разрешениями на публикацию записей, фото и видео.",
                }
                st.caption(instructions[platform])
                with st.form(key + "_connection"):
                    account_label = "ID сообщества ВК (без минуса)" if platform == "vk" else "ID Instagram-аккаунта" if platform == "instagram" else "ID аккаунта Threads"
                    account_id = st.text_input(account_label, value=str((connection or {}).get("account_id") or ""), key=key + "_account")
                    display_name = st.text_input("Название для отображения", value=str((connection or {}).get("display_name") or ""), key=key + "_display")
                    token = st.text_input("Ключ доступа к публикациям", type="password", key=key + "_token", help="Введите новый действующий ключ. Сохранённые ключи здесь не отображаются.")
                    submitted = st.form_submit_button("Сохранить подключение", disabled=not capabilities.get("encryption_configured", False))
                if submitted:
                    try:
                        if not account_id.strip().isdigit() or int(account_id.strip()) <= 0:
                            raise ValueError("Укажите числовой ID аккаунта или сообщества.")
                        if not token.strip():
                            raise ValueError("Укажите действующий ключ с разрешением на публикацию.")
                        extra = {"group_id": int(account_id.strip())} if platform == "vk" else {}
                        store.save_connection(owner_id, project_id, platform, account_id.strip(), display_name.strip(), token.strip(), extra=extra)
                        st.session_state[key + "_clear_token"] = True
                        st.session_state[f"cf_auto_{owner_id}_notice"] = "Подключение сохранено. Доступ будет проверен при работе с площадкой."
                        st.rerun()
                    except Exception as exc:
                        _action_error("Не удалось сохранить подключение", exc)


def _local_time(value, timezone):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, KeyError):
        return "Время не задано"


def _calendar(owner_id, project, service, store, jobs, capabilities):
    if not project:
        st.info("Создайте проект, чтобы ИИ подготовил календарь публикаций.")
        return
    st.write(f"Время проекта: **{project.get('timezone', 'Europe/Berlin')}**. Форматы чередуются в выбранных слотах.")
    if not project.get("automation_enabled"):
        st.info("Автопилот на паузе. Контент можно готовить; для публикации включите автопилот во вкладке «Проект».")
    if capabilities.get("worker_recent") is True:
        st.caption("Фоновый запуск подтверждён: очередь обрабатывается без открытого кабинета.")
    else:
        st.caption("Фоновый запуск: состояние не подтверждено. Для работы без открытого кабинета нужен запущенный сервис обработки очереди.")
    plan_col, process_col = st.columns(2)
    if plan_col.button("📅 Заполнить контент-план", key=f"cf_auto_{owner_id}_{project['id']}_plan", width="stretch"):
        try:
            planned = service.plan(owner_id, project["id"], days=int(project.get("plan_days") or 7))
            st.session_state[f"cf_auto_{owner_id}_notice"] = f"Контент-план готов: {len(planned)} записей. Повторное заполнение сохраняет уже созданные слоты."
            st.rerun()
        except Exception as exc:
            _action_error("Не удалось заполнить план", exc)
    if process_col.button("⚙️ Обработать очередь сейчас", key=f"cf_auto_{owner_id}_{project['id']}_process", width="stretch", disabled=not capabilities.get("ai_configured", False)):
        try:
            with st.spinner("ИИ обрабатывает следующий материал…"):
                result = _process_queue(store, owner_id, project["id"])
            generated = int(result.get("generated") or 0)
            published = int(result.get("published") or 0)
            pending = int(result.get("pending") or 0)
            st.session_state[f"cf_auto_{owner_id}_notice"] = f"Создано: {generated}. Опубликовано: {published}. Ожидает обработки площадкой: {pending}."
            st.rerun()
        except Exception as exc:
            _action_error("Не удалось обработать очередь", exc)
    st.caption("Ручная обработка выполняет один готовый к работе пункт: создаёт материал либо отправляет запланированную публикацию. Все расходы на генерацию относятся к подключённому ИИ-сервису.")
    if not capabilities.get("ai_configured"):
        st.info("ИИ-сервис ещё не подключён. Проект и контент-план можно сохранить; генерация станет доступна после подключения.")
    if jobs:
        rows = [{
            "Когда": _local_time(job.get("scheduled_at"), project.get("timezone", "Europe/Berlin")),
            "Площадка": PLATFORMS.get(job.get("platform"), job.get("platform", "")),
            "Формат": FORMATS.get(job.get("format"), job.get("format", "")),
            "Материал": (job.get("content") or {}).get("title") or job.get("topic") or "ИИ подготовит тему",
            "Статус": STATES.get(job.get("state"), job.get("state", "")),
        } for job in sorted(jobs, key=lambda item: str(item.get("scheduled_at") or ""))]
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("План пока пуст. Нажмите «Заполнить контент-план».")


def _assets_preview(owner_id, job, store):
    assets = job.get("assets") or []
    if not assets:
        return
    try:
        resolved = store.resolve_assets(owner_id, assets)
    except Exception:
        st.caption("Предпросмотр медиа временно недоступен. Сохранённые материалы остаются в очереди.")
        return
    for asset in resolved:
        source = asset.get("url") or asset.get("path")
        if not source:
            continue
        mime = str(asset.get("mime_type") or "")
        if mime.startswith("video/"):
            st.video(source)
        elif mime.startswith("image/"):
            st.image(source, caption=asset.get("filename"), width="stretch")


def _materials(owner_id, project, jobs, store, service):
    if not project:
        st.info("После создания проекта здесь появятся тексты, карусели и видео.")
        return
    if not jobs:
        st.info("Готовых материалов пока нет. Начните с контент-плана.")
        return
    filter_value = st.selectbox("Показать материалы", ["all", "ready", "scheduled", "published", "attention"], format_func=lambda value: {"all": "Все", "ready": "Ждут проверки", "scheduled": "В расписании", "published": "Опубликованные", "attention": "Требуют внимания"}[value], key=f"cf_auto_{owner_id}_{project['id']}_filter")
    selected = [job for job in jobs if filter_value == "all" or (filter_value == "attention" and job.get("state") in {"failed", "needs_connection", "reconcile"}) or job.get("state") == filter_value]
    if not selected:
        st.caption("В этой группе пока нет материалов.")
    for job in selected:
        state = job.get("state") or "queued"
        content = job.get("content") or {}
        key = f"cf_auto_{owner_id}_{project['id']}_{job['id']}"
        title = content.get("title") or job.get("topic") or FORMATS.get(job.get("format"), "Материал")
        label = f"{PLATFORMS.get(job.get('platform'), job.get('platform'))} · {title} · {STATES.get(state, state)}"
        with st.expander(label, expanded=state in {"ready", "failed", "needs_connection", "reconcile"}):
            st.caption(_local_time(job.get("scheduled_at"), project.get("timezone", "Europe/Berlin")))
            if job.get("error"):
                # Persisted error messages are produced by sanitized worker errors.
                st.warning(str(job["error"])[:700])
            if state == "reconcile":
                st.info("Площадка могла принять публикацию, но ответ не был подтверждён. Проверьте аккаунт перед любым повтором; автоматическая повторная отправка остановлена.")
            if content:
                editable = state == "ready" and not (job.get("checkpoint") or {}).get("publish_started")
                text = st.text_area("Текст публикации", value=str(content.get("text") or ""), height=180, disabled=not editable, key=key + "_text")
                for index, slide in enumerate(content.get("slides") or [], 1):
                    value = slide if isinstance(slide, str) else "\n".join(str(slide.get(name) or "") for name in ("title", "text"))
                    st.write(f"**Слайд {index}**")
                    st.write(value)
                if content.get("narration"):
                    st.caption("Озвучка")
                    st.write(content["narration"])
                _assets_preview(owner_id, job, store)
                st.download_button("Скачать текст", data=text, file_name=f"content-{job['id']}.txt", mime="text/plain", key=key + "_download")
                if editable and st.button("✅ Утвердить и поставить в очередь", key=key + "_approve", type="primary"):
                    try:
                        service.approve(owner_id, job["id"], content={**content, "text": text})
                        st.rerun()
                    except Exception as exc:
                        _action_error("Не удалось утвердить материал", exc)
            if state == "published" and job.get("permalink"):
                permalink = str(job["permalink"])
                if permalink.startswith("https://"):
                    st.link_button("Открыть публикацию", permalink)
            if state in {"failed", "needs_connection"}:
                if st.button("Повторить после исправления", key=key + "_retry"):
                    try:
                        service.retry(owner_id, job["id"])
                        st.rerun()
                    except Exception as exc:
                        _action_error("Не удалось вернуть материал в работу", exc)
            if state in {"queued", "ready", "scheduled", "failed", "needs_connection", "paused"}:
                if st.button("Отменить материал", key=key + "_cancel"):
                    try:
                        service.cancel(owner_id, job["id"])
                        st.rerun()
                    except Exception as exc:
                        _action_error("Не удалось отменить материал", exc)


def render_content_autopilot(owner_id: int, owner_name: str, target_profile=None):
    """Render the authenticated partner's independent projects and queue."""
    try:
        owner_id = int(owner_id)
    except (TypeError, ValueError):
        return
    if owner_id <= 0:
        return
    with st.expander("🚀 Автопилот: контент и публикации", expanded=True):
        st.markdown("### Ваш контент-завод")
        st.write("Один проект — своя аудитория, голос бренда и площадки. ИИ готовит посты, карусели и вертикальные видео, затем публикует их по расписанию.")
        try:
            store, service, capabilities = _runtime()
            projects = store.list_projects(owner_id)
        except Exception:
            st.info("Автопилот пока недоступен. Администратору нужно завершить настройку сервиса; создание отдельных материалов ниже остаётся доступным.")
            return
        for platform in PLATFORMS:
            for project in projects:
                key = f"cf_auto_{owner_id}_{project['id']}_{platform}"
                if st.session_state.pop(key + "_clear_token", False):
                    st.session_state[key + "_token"] = ""
        notice = st.session_state.pop(f"cf_auto_{owner_id}_notice", None)
        if notice:
            st.success(notice)
        options = [item["id"] for item in projects] + [_NEW]
        labels = {item["id"]: item["name"] for item in projects}
        labels[_NEW] = "＋ Новый проект"
        selection_key = f"cf_auto_{owner_id}_selected_project"
        next_project = st.session_state.pop(f"cf_auto_{owner_id}_next_project", None)
        if next_project in options:
            st.session_state[selection_key] = next_project
        if st.session_state.get(selection_key) not in options:
            st.session_state[selection_key] = options[0]
        selected = st.selectbox("Проект", options, format_func=labels.get, key=selection_key)
        project = next((item for item in projects if item["id"] == selected), None)
        jobs = []
        if project:
            try:
                jobs = store.list_jobs(owner_id, project["id"], limit=100)
            except Exception:
                st.info("Очередь временно недоступна. Настройки проекта сохранены.")
            counts = Counter(job.get("state") for job in jobs)
            first, second, third = st.columns(3)
            first.metric("В работе", sum(counts[state] for state in ("queued", "scheduled", "generating", "publishing", "pending")))
            second.metric("Ждут проверки", counts["ready"])
            third.metric("Опубликовано", counts["published"])
            st.caption(f"Статистика по последним {len(jobs)} материалам проекта.")
            if project.get("automation_enabled") and st.button("⏸ Поставить автопилот на паузу", key=f"cf_auto_{owner_id}_{project['id']}_pause"):
                try:
                    service.save_project(owner_id, {**project, "automation_enabled": False})
                    st.session_state[f"cf_auto_{owner_id}_{project['id']}_enabled"] = False
                    st.rerun()
                except Exception as exc:
                    _action_error("Не удалось приостановить автопилот", exc)
        project_tab, accounts_tab, calendar_tab, materials_tab = st.tabs(["Проект", "Площадки", "Календарь", "Материалы"])
        with project_tab:
            _project_form(owner_id, project, service, target_profile)
        with accounts_tab:
            _connections(owner_id, project, store, capabilities)
        with calendar_tab:
            _calendar(owner_id, project, service, store, jobs, capabilities)
        with materials_tab:
            _materials(owner_id, project, jobs, store, service)
