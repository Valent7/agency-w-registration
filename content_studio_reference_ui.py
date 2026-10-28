"""Explicit, owner-scoped adaptation from a video reference to a new film."""
from __future__ import annotations

import copy
from hashlib import sha256
import json
import uuid

import streamlit as st


STYLES = {"live": "Живое видео", "cartoon": "Мультфильм"}
PLATFORMS = {"instagram": "Instagram / Reels", "vk": "ВКонтакте", "threads": "Threads"}
MAX_VIDEO_BYTES = 50 * 1024 * 1024


class ReferenceUiError(ValueError):
    """Only local, credential-free explanations may reach this page."""


def _key(owner_id, project):
    return f"studio_reference_{int(owner_id)}_{(project or {}).get('id', 'new')}"


def _report(value):
    from content_studio_reference import validate_reference_report
    return validate_reference_report(value)


def _fingerprint(project, video_bytes, goal, style):
    # A project correction or a different source deserves a new analysis. The
    # source hash never becomes permission to reuse another owner's report.
    context = {field: project.get(field) for field in (
        "id", "owner_id", "name", "product", "audience", "language", "tone", "goal",
        "facts", "examples", "visual_style", "avoid", "cta", "website",
    )}
    return sha256(json.dumps([context, sha256(video_bytes).hexdigest(), goal, style],
                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _save(owner_id, project, store, result):
    from content_studio_catalog import save_reference_result
    try:
        job = save_reference_result(store, owner_id, project["id"], result["report"],
                                    request_id=result["request_id"])
        if (not isinstance(job, dict) or not job.get("id")
                or job.get("studio_complete") is not True):
            raise ReferenceUiError("Сохранение разбора пока не подтверждено.")
        result.update(saved_job_id=job["id"], save_failed=False)
    except Exception:
        # The accepted report remains in this owner's session. Retrying this
        # write must not transcribe, analyze, or generate anything again.
        result["save_failed"] = True


def _load_saved(owner_id, project, store, key):
    incoming = st.session_state.pop(f"content_studio_{owner_id}_reference_job_id", None)
    if not incoming:
        return
    try:
        job = store.get_job(owner_id, incoming)
        if (not isinstance(job, dict) or job.get("owner_id") != owner_id
                or job.get("project_id") != project["id"] or job.get("state") != "studio"
                or job.get("platform") != "studio" or job.get("studio_tool") != "reference"
                or job.get("studio_complete") is not True):
            raise ReferenceUiError("Разбор не найден в этом проекте.")
        report = _report(job.get("content"))
        st.session_state[key + "_result"] = {
            "report": report, "request_id": uuid.uuid4().hex, "fingerprint": None,
            "saved_job_id": job["id"], "save_failed": False, "create_requests": {},
        }
    except Exception:
        st.error("Не удалось открыть сохранённый разбор в этом проекте. Выберите его в «Моих генерациях» ещё раз.")


def _show_report(owner_id, project, store, service, key, result):
    report = result["report"]
    plan = report["reference_plan"]
    st.subheader("Ваш новый сценарий")
    st.caption("Формат: " + STYLES[report["reference_style"]])
    with st.expander("Что взяли из образца", expanded=False):
        st.write(report["source_summary"])
        st.caption("Используем идею и приёмы. Герои, сцены, текст и оформление нового ролика создаются для вашего проекта.")
    st.markdown("**" + plan["title"] + "**")
    st.write(plan["narration"])
    with st.expander("План сцен", expanded=False):
        for index, frame in enumerate(plan["visual_plan"]["frames"], 1):
            st.write(f"**{index}.** {frame['subject']}")
        st.caption(plan["visual_plan"]["art_direction"])
    draft = plan["title"] + "\n\n" + plan["narration"] + "\n\n" + "\n".join(
        f"{index}. {frame['subject']}" for index, frame in enumerate(plan["visual_plan"]["frames"], 1))
    st.download_button("Скачать сценарий", draft, file_name="agency-w-new-video-script.txt",
                       mime="text/plain", key=key + "_download")
    if result.get("saved_job_id"):
        st.success("Разбор и сценарий сохранены в «Моих генерациях».")
    elif result.get("save_failed"):
        st.warning("Сценарий готов, но история пока не сохранилась. Повторите сохранение — новый разбор для этого не нужен.")
        if st.button("Повторить сохранение разбора", key=key + "_retry_save"):
            _save(owner_id, project, store, result)
            st.rerun()

    try:
        connections = store.list_connections(owner_id, project["id"])
        connected = {item.get("platform") for item in connections if isinstance(item, dict)}
        platforms = [value for value in project.get("platforms", []) if value in connected and value in PLATFORMS]
    except Exception:
        platforms = []
    try:
        from content_factory_runtime import get_live_video_capabilities
        video_configured = get_live_video_capabilities(owner_id, project["id"]).get("live_video_configured") is True
    except Exception:
        video_configured = False
    with st.form(key + "_create_form"):
        selected = st.multiselect("Где разместить новое видео", platforms, default=platforms,
                                  format_func=PLATFORMS.get, key=key + "_platforms")
        submitted = st.form_submit_button(
            "Создать моё видео", type="primary", width="stretch",
            disabled=not (result.get("saved_job_id") and platforms and video_configured
                          and "reel" in project.get("formats", [])) or bool(result.get("created_jobs")),
        )
        st.caption("Эта кнопка запускает платное создание новых сцен и озвучки. Готовый ролик сначала появится на вашу проверку.")
    if not platforms:
        st.info("Подключите площадку в разделе «Площадки», чтобы передать новый ролик в производство.")
    if "reel" not in project.get("formats", []):
        st.info("Включите формат «Видео / Reels» в настройках проекта.")
    if not video_configured:
        st.info("Для создания движущихся сцен подключите видеосервис в настройках проекта. Сценарий уже можно прочитать и скачать.")
    if submitted:
        if not selected:
            st.warning("Выберите хотя бы одну площадку.")
            return
        fingerprint = sha256(json.dumps([owner_id, project["id"], result["saved_job_id"], sorted(selected)]).encode()).hexdigest()
        requests = result.setdefault("create_requests", {})
        # The same accepted scenario keeps its identity even after closing the
        # browser and reopening history. Reading it cannot buy a second render.
        request_id = requests.setdefault(fingerprint, "reference-" + fingerprint[:48])
        try:
            jobs = service.create_from_video_reference(owner_id, project["id"], result["saved_job_id"],
                                                       platforms=selected, request_id=request_id)
            if not isinstance(jobs, list) or not jobs or any(not isinstance(job, dict) or not job.get("id") for job in jobs):
                raise ReferenceUiError("Создание задачи пока не подтверждено.")
            result["created_jobs"] = [job["id"] for job in jobs]
            st.session_state[f"cf_auto_{owner_id}_notice"] = "Новый ролик передан в производство. Результат появится в «Моих генерациях» и дождётся вашей проверки."
            st.session_state[f"content_studio_{owner_id}_next"] = "library"
            st.rerun()
        except Exception:
            st.error("Не удалось подтвердить задачу создания видео. Обновите материалы; повторная кнопка использует тот же запрос и не создаёт дубликат.")
    if result.get("created_jobs"):
        st.info("Новый ролик передан в производство. Откройте «Мои генерации», чтобы следить за результатом.")


def render_reference_tool(owner_id, project, store, service):
    """Rendering and storage retries never start a paid model operation."""
    from content_studio_tools import _ready
    from content_studio_reference import ReferenceError

    st.subheader("Создать своё видео по образцу")
    st.caption("Загрузите ролик, укажите свою тему и получите собственный сценарий для живого видео или мультфильма.")
    if not _ready(owner_id, project, store) or service is None:
        return
    owner_id = int(owner_id)
    key = _key(owner_id, project)
    _load_saved(owner_id, project, store, key)
    epoch = st.session_state.get(key + "_upload_epoch", 0)
    with st.form(key + "_analysis_form"):
        upload = st.file_uploader("Видео-образец", type=["mp4", "mov", "webm"], key=key + f"_file_{epoch}")
        goal = st.text_area("О чём должно быть ваше видео?", max_chars=3000, height=100,
                            placeholder="Например: покажи, как наш помощник освобождает вечер предпринимателя.", key=key + "_goal")
        style = st.radio("Какой результат нужен?", list(STYLES), format_func=STYLES.get,
                         horizontal=True, key=key + "_style")
        submitted = st.form_submit_button("Разобрать и подготовить свой сценарий", width="stretch", type="primary")
        st.caption("Файл до 50 МБ и до двух минут. Сначала оплачивается только разбор образца; создание видео запускается отдельной кнопкой.")
    with st.expander("Если у вас только ссылка на публикацию", expanded=False):
        st.caption("Автоматическое получение роликов по ссылкам соцсетей пока не подключено. Загрузите сам видеофайл.")
    if submitted:
        try:
            if upload is None:
                raise ReferenceUiError("Добавьте видеофайл, который нужно разобрать.")
            if getattr(upload, "size", 0) > MAX_VIDEO_BYTES:
                raise ReferenceUiError("Выберите видео размером до 50 МБ.")
            video_bytes = upload.getvalue()
            if not isinstance(video_bytes, bytes) or not video_bytes or len(video_bytes) > MAX_VIDEO_BYTES:
                raise ReferenceUiError("Выберите непустой видеофайл размером до 50 МБ.")
            if not 10 <= len(goal.strip()) <= 3000 or "\x00" in goal:
                raise ReferenceUiError("Опишите тему своего видео: от 10 до 3000 символов.")
            fingerprint = _fingerprint(project, video_bytes, goal.strip(), style)
            previous = st.session_state.get(key + "_result")
            if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
                st.info("Разбор этого образца с этой задачей уже готов ниже.")
            else:
                from content_factory_runtime import get_generator
                from content_studio_reference import analyze_reference_video

                with st.spinner("Изучаем образец и готовим новую историю…"):
                    report = _report(analyze_reference_video(get_generator(), project, video_bytes,
                                                             filename=str(getattr(upload, "name", "reference.mp4")),
                                                             goal=goal.strip(), style=style))
                if report["reference_style"] != style or report["source"]["sha256"] != sha256(video_bytes).hexdigest():
                    raise ReferenceUiError("Не удалось связать разбор с выбранным образцом и стилем. Новое видео не запускалось.")
                result = {"report": copy.deepcopy(report), "fingerprint": fingerprint,
                          "request_id": uuid.uuid4().hex, "saved_job_id": None,
                          "save_failed": False, "create_requests": {}}
                st.session_state[key + "_result"] = result
                _save(owner_id, project, store, result)
        except ReferenceUiError as error:
            st.error(str(error))
        except ReferenceError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось разобрать образец. Проверьте, что видео открывается и длится не более двух минут. Автоматический повтор платного запроса не запускался.")
    result = st.session_state.get(key + "_result")
    if isinstance(result, dict):
        _show_report(owner_id, project, store, service, key, result)
        if st.button("Взять другой образец", key=key + "_new"):
            st.session_state.pop(key + "_result", None)
            st.session_state[key + "_upload_epoch"] = epoch + 1
            st.rerun()
