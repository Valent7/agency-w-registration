"""Explicit, owner-scoped adaptation from a video reference to a new film."""
from __future__ import annotations

import copy
from hashlib import sha256
import json
from pathlib import Path
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


def _fingerprint(project, video_bytes, goal, style, target_seconds=45):
    # A project correction or a different source deserves a new analysis. The
    # source hash never becomes permission to reuse another owner's report.
    context = {field: project.get(field) for field in (
        "id", "owner_id", "name", "product", "audience", "language", "tone", "goal",
        "facts", "examples", "visual_style", "avoid", "cta", "website",
    )}
    values = [context, sha256(video_bytes).hexdigest(), goal, style]
    if target_seconds != 45:
        values.append(target_seconds)
    return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


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
            "video_personalization": copy.deepcopy((job.get("checkpoint") or {}).get("video_personalization") or {}),
        }
    except Exception:
        st.error("Не удалось открыть сохранённый разбор в этом проекте. Выберите его в «Моих генерациях» ещё раз.")



def _current_personalization(owner_id, store, result):
    saved = result.get("saved_job_id")
    if not saved:
        return copy.deepcopy(result.get("video_personalization") or {})
    try:
        job = store.get_job(owner_id, saved)
        checkpoint = job.get("checkpoint") if isinstance(job, dict) else None
        value = checkpoint.get("video_personalization") if isinstance(checkpoint, dict) else None
        if isinstance(value, dict):
            result["video_personalization"] = copy.deepcopy(value)
            return copy.deepcopy(value)
    except Exception:
        pass
    return copy.deepcopy(result.get("video_personalization") or {})


def _save_personalization(owner_id, project, store, result, *, intro, outro, music,
                          voice_volume, music_volume, clear_intro=False,
                          clear_outro=False, clear_music=False):
    saved = result.get("saved_job_id")
    if not saved:
        raise ReferenceUiError("Сначала сохраните сценарий, затем настройте оформление фильма.")
    job = store.get_job(owner_id, saved)
    if (not isinstance(job, dict) or job.get("owner_id") != owner_id
            or job.get("project_id") != project["id"] or job.get("studio_tool") != "reference"):
        raise ReferenceUiError("Не удалось найти сохранённый сценарий для оформления.")
    checkpoint = dict(job.get("checkpoint") or {})
    settings = dict(checkpoint.get("video_personalization") or {})
    assets = list(job.get("assets") or [])

    def upload_asset(upload, key_name, prefix, maximum, allowed_mime):
        if upload is None:
            return
        data = upload.getvalue()
        mime = str(getattr(upload, "type", "") or "").lower()
        if not isinstance(data, bytes) or not data or len(data) > maximum or mime not in allowed_mime:
            raise ReferenceUiError("Выбранный файл оформления имеет неподдерживаемый формат или слишком большой размер.")
        suffix = Path(str(getattr(upload, "name", "") or "")).suffix.lower()
        if not suffix or len(suffix) > 6:
            suffix = ".bin"
        asset = store.put_asset(
            owner_id, project["id"], saved, prefix + suffix, data,
            mime or "application/octet-stream",
        )
        settings[key_name] = asset
        if all(item.get("storage_path") != asset.get("storage_path") for item in assets if isinstance(item, dict)):
            assets.append(asset)

    if clear_intro:
        settings.pop("intro_asset", None)
    if clear_outro:
        settings.pop("outro_asset", None)
    if clear_music:
        settings.pop("music_asset", None)
    upload_asset(intro, "intro_asset", "film-intro", 10 * 1024 * 1024,
                 {"image/png", "image/jpeg", "image/webp"})
    upload_asset(outro, "outro_asset", "film-outro", 10 * 1024 * 1024,
                 {"image/png", "image/jpeg", "image/webp"})
    upload_asset(music, "music_asset", "film-music", 25 * 1024 * 1024,
                 {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/x-m4a"})

    settings["voice_volume"] = max(0.0, min(1.5, float(voice_volume)))
    settings["music_volume"] = max(0.0, min(0.40, float(music_volume)))
    checkpoint["video_personalization"] = settings
    updated = store.update_job(
        owner_id, saved, {"checkpoint": checkpoint, "assets": assets},
        expected_updated_at=job.get("updated_at"),
    )
    result["video_personalization"] = copy.deepcopy(settings)
    return updated


def _scenario_editor(owner_id, project, store, key, result):
    report = result["report"]
    plan = report["reference_plan"]
    editing_key = key + "_editing"
    left, right = st.columns([1, 3])
    with left:
        if st.button("✏️ Редактировать сценарий", key=key + "_edit"):
            st.session_state[editing_key] = not st.session_state.get(editing_key, False)
            st.rerun()
    with right:
        st.caption("Можно исправить текст и сцены без повторного платного разбора видео.")

    if not st.session_state.get(editing_key, False):
        return
    with st.form(key + "_edit_form"):
        title = st.text_input("Название ролика", value=plan["title"], max_chars=150)
        text = st.text_area("Подпись к ролику", value=plan["text"], max_chars=500, height=110)
        narration = st.text_area("Текст диктора", value=plan["narration"], height=260,
                                 max_chars=4000 if plan.get("target_seconds") == 120 else 1200)
        scene_values = []
        st.markdown("**Сцены**")
        for index, frame in enumerate(plan["visual_plan"]["frames"], 1):
            scene_values.append(st.text_area(
                f"Сцена {index}", value=frame["subject"], max_chars=600,
                key=key + f"_edit_scene_{index}", height=80,
            ))
        save_edit = st.form_submit_button("Сохранить изменения", type="primary", width="stretch")
    if save_edit:
        try:
            edited = copy.deepcopy(report)
            target = edited["reference_plan"]
            target["title"], target["text"], target["narration"] = title.strip(), text.strip(), narration.strip()
            for frame, subject in zip(target["visual_plan"]["frames"], scene_values):
                frame["subject"] = subject.strip()
            result["report"] = _report(edited)
            result["request_id"] = uuid.uuid4().hex
            result["saved_job_id"] = None
            result["save_failed"] = False
            result["create_requests"] = {}
            result.pop("created_jobs", None)
            result["video_personalization"] = {}
            _save(owner_id, project, store, result)
            st.session_state[editing_key] = False
            st.success("Изменения сохранены как новая версия сценария. Повторный разбор видео не запускался.")
            st.rerun()
        except Exception:
            st.error("Не удалось сохранить правки. Проверьте длину текста и заполнение всех сцен.")


def _film_finishing(owner_id, project, store, key, result):
    settings = _current_personalization(owner_id, store, result)
    st.subheader("Оформление фильма")
    logo_path = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
    logo_col, note_col = st.columns([1, 5], gap="large")
    with logo_col:
        if logo_path.is_file():
            st.image(str(logo_path), width=96)
    with note_col:
        st.markdown("**Логотип Агентства W**")
        st.write("Именно этот оригинальный логотип автоматически ставится в ролик. Обычная буква W не используется.")
        st.caption("Если свою заставку не загружать, начало и конец будут оформлены фирменными заставками Агентства W.")

    first, second = st.columns(2, gap="large")
    with first:
        intro = st.file_uploader(
            "Начальная заставка (по желанию)", type=["png", "jpg", "jpeg", "webp"],
            key=key + "_intro_upload", help="Если не загружать — используется фирменное начало Агентства W.",
        )
        if intro is not None and str(getattr(intro, "type", "") or "").startswith("image/"):
            st.image(intro.getvalue(), caption="Ваше начало фильма", width="stretch")
        clear_intro = st.checkbox(
            "Вернуть фирменное начало",
            value=False, disabled=not bool(settings.get("intro_asset")), key=key + "_clear_intro",
        )
    with second:
        outro = st.file_uploader(
            "Конечная заставка (по желанию)", type=["png", "jpg", "jpeg", "webp"],
            key=key + "_outro_upload", help="Если не загружать — используется фирменный финал Агентства W.",
        )
        if outro is not None and str(getattr(outro, "type", "") or "").startswith("image/"):
            st.image(outro.getvalue(), caption="Ваш конец фильма", width="stretch")
        clear_outro = st.checkbox(
            "Вернуть фирменный финал",
            value=False, disabled=not bool(settings.get("outro_asset")), key=key + "_clear_outro",
        )

    st.markdown("**Музыка и голос**")
    music_mode = st.radio(
        "Музыкальное сопровождение", ["Без музыки", "Загрузить свою музыку"],
        index=1 if settings.get("music_asset") else 0, horizontal=True, key=key + "_music_mode",
    )
    music = None
    if music_mode == "Загрузить свою музыку":
        music = st.file_uploader(
            "Музыкальный файл", type=["mp3", "wav", "m4a", "aac"],
            key=key + "_music_upload", help="До 25 МБ. Музыка будет тише речи.",
        )
        if music is not None:
            st.audio(music.getvalue())
    clear_music = music_mode == "Без музыки"
    voice_default = int(round(float(settings.get("voice_volume", 1.0)) * 100))
    music_default = int(round(float(settings.get("music_volume", 0.12)) * 100))
    volume_left, volume_right = st.columns(2, gap="large")
    with volume_left:
        voice_percent = st.slider("Голос", 60, 150, max(60, min(150, voice_default)), 5,
                                  format="%d%%", key=key + "_voice_volume")
    with volume_right:
        music_percent = st.slider("Музыка", 0, 40, max(0, min(40, music_default)), 1,
                                  format="%d%%", key=key + "_music_volume",
                                  disabled=music_mode == "Без музыки")
    st.caption("Музыка ограничена 40% от исходного уровня, чтобы она не заглушала диктора.")

    if st.button("Сохранить оформление фильма", type="primary", width="stretch", key=key + "_save_finishing"):
        try:
            _save_personalization(
                owner_id, project, store, result, intro=intro, outro=outro, music=music,
                voice_volume=voice_percent / 100, music_volume=music_percent / 100,
                clear_intro=clear_intro, clear_outro=clear_outro, clear_music=clear_music,
            )
            st.success("Оформление сохранено. Эти настройки будут применены при создании фильма.")
            st.rerun()
        except ReferenceUiError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось сохранить оформление. Проверьте выбранные файлы и повторите.")

    active = _current_personalization(owner_id, store, result)
    summary = []
    summary.append("своё начало" if active.get("intro_asset") else "фирменное начало W")
    summary.append("свой финал" if active.get("outro_asset") else "фирменный финал W")
    summary.append("музыка загружена" if active.get("music_asset") else "без музыки")
    st.info("Будет использовано: " + " · ".join(summary) + ".")


def _show_report(owner_id, project, store, service, key, result):
    report = result["report"]
    plan = report["reference_plan"]
    st.subheader("Ваш новый сценарий")
    st.caption("Формат: " + STYLES[report["reference_style"]])
    st.caption("Длительность нового ролика: около двух минут." if plan.get("target_seconds") == 120
               else "Длительность нового ролика: до одной минуты.")
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
    _scenario_editor(owner_id, project, store, key, result)
    if result.get("saved_job_id"):
        st.success("Разбор и сценарий сохранены в «Моих генерациях».")
    elif result.get("save_failed"):
        st.warning("Сценарий готов, но история пока не сохранилась. Повторите сохранение — новый разбор для этого не нужен.")
        if st.button("Повторить сохранение разбора", key=key + "_retry_save"):
            _save(owner_id, project, store, result)
            st.rerun()

    _film_finishing(owner_id, project, store, key, result)

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
        target_seconds = st.radio("Длительность вашего нового видео", [45, 120],
                                  format_func=lambda value: "Около двух минут" if value == 120 else "Короткое, до минуты",
                                  horizontal=True, key=key + "_target_seconds")
        submitted = st.form_submit_button("Разобрать и подготовить свой сценарий", width="stretch", type="primary")
        st.caption("Исходный ролик: до 50 МБ и до 15 минут. Завод разбирает речь всего ролика; длительность вашего нового видео выбирается отдельно. Сначала оплачивается разбор, создание видео запускается другой кнопкой.")
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
            fingerprint = _fingerprint(project, video_bytes, goal.strip(), style, target_seconds)
            previous = st.session_state.get(key + "_result")
            if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
                st.info("Разбор этого образца с этой задачей уже готов ниже.")
            else:
                from content_factory_runtime import get_generator
                from content_studio_reference import analyze_reference_video

                with st.spinner("Изучаем образец и готовим новую историю…"):
                    duration_options = {"target_seconds": 120} if target_seconds == 120 else {}
                    report = _report(analyze_reference_video(get_generator(), project, video_bytes,
                                                             filename=str(getattr(upload, "name", "reference.mp4")),
                                                             goal=goal.strip(), style=style, **duration_options))
                if (report["reference_style"] != style or report["source"]["sha256"] != sha256(video_bytes).hexdigest()
                        or report["reference_plan"].get("target_seconds", 45) != target_seconds):
                    raise ReferenceUiError("Не удалось связать разбор с выбранным образцом и стилем. Новое видео не запускалось.")
                result = {"report": copy.deepcopy(report), "fingerprint": fingerprint,
                          "request_id": uuid.uuid4().hex, "saved_job_id": None,
                          "save_failed": False, "create_requests": {}, "video_personalization": {}}
                st.session_state[key + "_result"] = result
                _save(owner_id, project, store, result)
        except ReferenceUiError as error:
            st.error(str(error))
        except ReferenceError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось разобрать образец. Проверьте, что видео открывается и длится не более 15 минут. Автоматический повтор платного запроса не запускался.")
    result = st.session_state.get(key + "_result")
    if isinstance(result, dict):
        _show_report(owner_id, project, store, service, key, result)
        if st.button("Взять другой образец", key=key + "_new"):
            st.session_state.pop(key + "_result", None)
            st.session_state[key + "_upload_epoch"] = epoch + 1
            st.rerun()
