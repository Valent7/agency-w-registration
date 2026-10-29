"""Explicit, owner-scoped adaptation from a video reference to a new film."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
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
            "scenario_approved": True,
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
                          clear_outro=False, clear_music=False, music_library_asset=None):
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

    def upload_asset(upload, key_name, prefix, maximum, formats):
        if upload is None:
            return
        data = upload.getvalue()
        suffix = Path(str(getattr(upload, "name", "") or "")).suffix.lower()
        mime = formats.get(suffix)
        if not isinstance(data, bytes) or not data or len(data) > maximum or mime is None:
            raise ReferenceUiError("Выбранный файл оформления имеет неподдерживаемый формат или слишком большой размер.")
        asset = store.put_asset(
            owner_id, project["id"], saved, prefix + suffix, data, mime,
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
    image_formats = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    music_formats = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/mp4"}
    upload_asset(intro, "intro_asset", "film-intro", 10 * 1024 * 1024, image_formats)
    upload_asset(outro, "outro_asset", "film-outro", 10 * 1024 * 1024, image_formats)
    if music_library_asset is not None:
        if not isinstance(music_library_asset, dict):
            raise ReferenceUiError("Не удалось проверить выбранную музыку.")
        mime = str(music_library_asset.get("mime_type") or "")
        extension = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/mp4": ".m4a"}.get(mime)
        if extension is None:
            raise ReferenceUiError("Выбранная музыка имеет неподдерживаемый формат.")
        data = store.read_asset(owner_id, music_library_asset)
        if not 0 < len(data) <= 25 * 1024 * 1024:
            raise ReferenceUiError("Для фильма выберите музыкальный файл размером до 25 МБ.")
        asset = store.put_asset(owner_id, project["id"], saved, "film-music" + extension, data, mime)
        settings["music_asset"] = asset
        if all(item.get("storage_path") != asset.get("storage_path") for item in assets if isinstance(item, dict)):
            assets.append(asset)
    else:
        upload_asset(music, "music_asset", "film-music", 25 * 1024 * 1024, music_formats)

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
    try:
        music_jobs = [
            job for job in store.list_jobs(owner_id, project["id"], limit=100)
            if isinstance(job, dict) and job.get("state") == "studio"
            and job.get("studio_tool") == "music" and job.get("studio_complete") is True
            and isinstance(job.get("assets"), list) and job["assets"]
        ]
    except Exception:
        music_jobs = []

    selected_suno_job = st.session_state.get(f"studio_music_{owner_id}_selected_job")
    valid_suno_ids = {job["id"] for job in music_jobs}
    if selected_suno_job not in valid_suno_ids:
        selected_suno_job = None

    music_modes = ["Без музыки"]
    if settings.get("music_asset"):
        music_modes.append("Оставить сохранённую музыку")
    if music_jobs:
        music_modes.append("Музыка из Suno / Моих генераций")
    music_modes.append("Загрузить файл")

    if selected_suno_job:
        default_mode = "Музыка из Suno / Моих генераций"
    elif settings.get("music_asset"):
        default_mode = "Оставить сохранённую музыку"
    else:
        default_mode = "Без музыки"
    music_mode = st.radio(
        "Музыкальное сопровождение", music_modes,
        index=music_modes.index(default_mode), horizontal=True, key=key + "_music_mode",
    )

    music = None
    music_library_asset = None
    if music_mode == "Музыка из Suno / Моих генераций":
        labels = {
            job["id"]: str((job.get("content") or {}).get("title") or job.get("topic") or "Музыкальный трек")
            for job in music_jobs
        }
        ids = [job["id"] for job in music_jobs]
        default_id = selected_suno_job if selected_suno_job in ids else ids[0]
        selected_id = st.selectbox(
            "Музыка проекта", ids, index=ids.index(default_id),
            format_func=labels.get, key=key + "_suno_music_job",
        )
        st.session_state[f"studio_music_{owner_id}_selected_job"] = selected_id
        chosen = next(job for job in music_jobs if job["id"] == selected_id)
        music_library_asset = chosen["assets"][0]
        try:
            resolved = store.resolve_assets(owner_id, [music_library_asset])[0]
            source = resolved.get("url") or resolved.get("path")
            if source:
                st.audio(source, format=resolved.get("mime_type"))
        except Exception:
            st.caption("Предпросмотр музыки временно недоступен, но сохранённый трек можно использовать.")
    elif music_mode == "Загрузить файл":
        music = st.file_uploader(
            "Музыкальный файл", type=["mp3", "wav", "m4a", "aac"],
            key=key + "_music_upload", help="До 25 МБ. Музыка будет тише речи.",
        )
        if music is not None:
            st.audio(music.getvalue())

    if not music_jobs:
        if st.button("🎵 Создать или отредактировать музыку в Suno", key=key + "_open_music"):
            st.session_state[f"content_studio_{owner_id}_next"] = "music"
            st.rerun()

    clear_music = music_mode == "Без музыки"
    voice_default = int(round(float(settings.get("voice_volume", 1.0)) * 100))
    music_default = int(round(float(settings.get("music_volume", 0.12)) * 100))
    volume_left, volume_right = st.columns(2, gap="large")
    with volume_left:
        voice_percent = st.slider("Голос", 60, 150, max(60, min(150, voice_default)), 5,
                                  format="%d%%", key=key + "_voice_volume")
    with volume_right:
        music_enabled = music_mode != "Без музыки"
        music_percent = st.slider("Музыка", 0, 40, max(0, min(40, music_default)), 1,
                                  format="%d%%", key=key + "_music_volume",
                                  disabled=not music_enabled)
    st.caption("Музыка ограничена 40% от исходного уровня, чтобы она не заглушала диктора.")

    if st.button("Сохранить оформление фильма", type="primary", width="stretch", key=key + "_save_finishing"):
        try:
            _save_personalization(
                owner_id, project, store, result, intro=intro, outro=outro, music=music,
                voice_volume=voice_percent / 100, music_volume=music_percent / 100,
                clear_intro=clear_intro, clear_outro=clear_outro, clear_music=clear_music,
                music_library_asset=music_library_asset,
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
    summary.append("музыка выбрана" if active.get("music_asset") else "без музыки")
    st.info("Будет использовано: " + " · ".join(summary) + ".")


def _approved_scenario_form(owner_id, project, store, key, result):
    report = result["report"]
    plan = report["reference_plan"]

    st.markdown("### 2. Проверьте сценарий")
    st.caption("Исправьте всё, что хотите. Повторно разбирать видео для этого не нужно.")
    with st.form(key + "_approve_form"):
        title = st.text_input("Название ролика", value=plan["title"], max_chars=150)
        narration = st.text_area(
            "Текст диктора",
            value=plan["narration"],
            height=220,
            max_chars=4000 if plan.get("target_seconds") == 120 else 1200,
        )
        scene_values = []
        with st.expander("Сцены — можно изменить при необходимости", expanded=False):
            for index, frame in enumerate(plan["visual_plan"]["frames"], 1):
                scene_values.append(st.text_area(
                    f"Сцена {index}",
                    value=frame["subject"],
                    max_chars=600,
                    key=key + f"_approve_scene_{index}",
                    height=75,
                ))
        approved = st.form_submit_button(
            "✅ Утвердить сценарий",
            type="primary",
            width="stretch",
        )

    if approved:
        try:
            edited = copy.deepcopy(report)
            target = edited["reference_plan"]
            target["title"] = title.strip()
            target["narration"] = narration.strip()
            for frame, subject in zip(target["visual_plan"]["frames"], scene_values):
                frame["subject"] = subject.strip()
            result["report"] = _report(edited)
            # A changed/approved script is a new durable version, but never a
            # new paid analysis of the source video.
            result["request_id"] = uuid.uuid4().hex
            result["saved_job_id"] = None
            result["save_failed"] = False
            result["create_requests"] = {}
            result.pop("created_job_id", None)
            _save(owner_id, project, store, result)
            if result.get("saved_job_id"):
                result["scenario_approved"] = True
                st.success("Сценарий утверждён. Можно создавать видео.")
                st.rerun()
            else:
                st.error("Сценарий готов, но не удалось сохранить его. Видео не запускалось.")
        except Exception:
            st.error("Не удалось утвердить сценарий. Проверьте текст и сцены.")

    if result.get("scenario_approved") and result.get("saved_job_id"):
        st.success("✅ Сценарий утверждён.")


def _find_existing_master_job(store, owner_id, project_id, reference_job_id):
    """Recover a previously started master after refresh; never create a paid duplicate."""
    try:
        jobs = store.list_jobs(owner_id, project_id, limit=300)
    except Exception:
        return None
    candidates = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("studio_master") is not True:
            continue
        approved = job.get("approved_reference")
        if not isinstance(approved, dict) or approved.get("source_job_id") != reference_job_id:
            continue
        if job.get("state") == "cancelled":
            continue
        candidates.append(job)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return candidates[0]


def _master_video_asset(job):
    for asset in job.get("assets") or []:
        if isinstance(asset, dict) and (
            asset.get("kind") == "video"
            or str(asset.get("mime_type") or "").startswith("video/")
        ):
            return asset
    return None


@st.fragment(run_every=15)
def _render_master_status(owner_id, project, store, service, key, job_id):
    try:
        job = store.get_job(owner_id, job_id)
    except Exception:
        st.error("Не удалось прочитать состояние видео. Обновите страницу.")
        return
    if not isinstance(job, dict):
        st.error("Задача создания видео не найдена.")
        return

    state = str(job.get("state") or "")
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    progress_text = str(progress.get("message") or "").strip()
    error = str(job.get("error") or "").strip()

    if state in {"queued", "generating", "scheduled"}:
        # A live-video job should refresh its durable status every few minutes.
        # If it does not, do not leave the owner staring at an endless
        # "accepted" message: explain that the background continuation stalled.
        stale_seconds = 0.0
        updated_raw = str(job.get("updated_at") or "").strip()
        if updated_raw:
            try:
                updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                stale_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError):
                stale_seconds = 0.0

        if stale_seconds >= 180:
            minutes = max(3, int(stale_seconds // 60))
            st.error(
                "❌ Видео пока не создано. Причина: фоновая обработка перестала "
                f"обновлять статус после передачи видеосервису ({minutes} мин без обновлений)."
            )
            st.caption(
                "Это не нормальное длительное ожидание: задача зависла между проверками. "
                "Сценарий и уже созданные данные сохранены; повторно разбирать образец не нужно."
            )
            if st.button("▶️ Продолжить создание", key=key + "_resume_stalled_master", width="stretch"):
                try:
                    service.resume_stalled_video(owner_id, job_id)
                    st.success("Задача возвращена на проверку уже заказанных сцен.")
                    st.rerun()
                except Exception as exc:
                    from content_factory_core import FactoryError
                    if isinstance(exc, FactoryError):
                        st.error("Продолжение не запущено: " + str(exc))
                    else:
                        st.error("Продолжение не запущено: внутренняя ошибка производства.")
            return

        st.info("🎬 " + (progress_text or "Видео принято в работу. Создаём сцены и озвучку."))
        st.caption("Статус на этой странице обновляется автоматически.")
        return

    if state == "ready":
        asset = _master_video_asset(job)
        if not asset:
            st.warning("Видео подготовлено, но файл ещё не появился в хранилище. Обновите статус.")
            return
        try:
            resolved = store.resolve_assets(owner_id, [asset])[0]
            source = resolved.get("url") or resolved.get("path")
            st.success("✅ Видео готово.")
            if source:
                st.video(source)
            try:
                data = store.read_asset(owner_id, asset)
                st.download_button(
                    "⬇️ Скачать готовое видео",
                    data,
                    file_name="agency-w-master-video.mp4",
                    mime="video/mp4",
                    width="stretch",
                    key=key + "_master_download",
                )
            except Exception:
                st.caption("Видео готово к просмотру. Скачивание временно недоступно.")
        except Exception:
            st.error("Видео создано, но не удалось открыть готовый файл.")
        return

    if state == "failed":
        reason = error or progress_text or "Видеосервис не завершил создание."
        st.error("❌ Видео не создано. Причина: " + reason)
        if st.button("Повторить создание", key=key + "_retry_master", width="stretch"):
            try:
                service.retry(owner_id, job_id)
                st.rerun()
            except Exception as exc:
                from content_factory_core import FactoryError
                if isinstance(exc, FactoryError):
                    st.error("Повтор не запущен: " + str(exc))
                else:
                    st.error("Повтор не запущен: внутренняя ошибка производства.")
        return

    if state == "paused":
        st.error("⏸️ Создание остановлено. Причина: " + (error or progress_text or "задача приостановлена."))
        return

    if state == "needs_connection":
        st.error(
            "❌ Видео не создано. Внутренняя ошибка: мастер-ролик не должен требовать "
            "подключения соцсети."
        )
        return

    if state == "reconcile":
        st.error("❌ Результат создания не подтверждён. Причина: " + (error or progress_text or "неизвестный ответ видеосервиса."))
        return

    st.info(progress_text or f"Состояние видео: {state or 'неизвестно'}.")


def _show_report(owner_id, project, store, service, key, result):
    report = result["report"]
    plan = report["reference_plan"]

    st.markdown("### Сценарий по вашему образцу")
    st.caption(
        ("Живое видео" if report["reference_style"] == "live" else "Мультфильм")
        + " · короткий вертикальный ролик"
    )
    with st.expander("Что завод понял из образца", expanded=False):
        st.write(report["source_summary"])
        st.caption(
            "Берём структуру и приёмы. Лица, сцены, текст и бренд нового ролика "
            "создаются заново для Агентства W."
        )

    _approved_scenario_form(owner_id, project, store, key, result)

    st.markdown("### 3. Создать готовое видео")
    if not (result.get("scenario_approved") and result.get("saved_job_id")):
        st.info("Сначала утвердите сценарий выше.")
        return

    try:
        from content_factory_runtime import get_live_video_capabilities
        video_configured = (
            get_live_video_capabilities(owner_id, project["id"]).get("live_video_configured")
            is True
        )
    except Exception:
        video_configured = False

    if not video_configured:
        st.error(
            "Видео сейчас не создастся: не подключён рабочий видеосервис. "
            "Сценарий сохранён; после подключения повторно разбирать образец не потребуется."
        )
        return

    created_job_id = result.get("created_job_id")
    if not created_job_id:
        existing = _find_existing_master_job(
            store, owner_id, project["id"], result["saved_job_id"]
        )
        if existing and existing.get("id"):
            created_job_id = existing["id"]
            result["created_job_id"] = created_job_id

    if not created_job_id:
        if st.button(
            "🎬 Создать видео",
            type="primary",
            width="stretch",
            key=key + "_create_master",
        ):
            fingerprint = sha256(json.dumps([
                owner_id, project["id"], result["saved_job_id"], "master-video"
            ]).encode()).hexdigest()
            requests = result.setdefault("create_requests", {})
            request_id = requests.setdefault(
                fingerprint,
                "reference-master-" + fingerprint[:40],
            )
            try:
                job = service.create_master_from_video_reference(
                    owner_id,
                    project["id"],
                    result["saved_job_id"],
                    request_id=request_id,
                )
                if not isinstance(job, dict) or not job.get("id"):
                    raise ReferenceUiError("Задача создания видео не подтверждена.")
                result["created_job_id"] = job["id"]
                st.success("Видео принято в работу. Оставайтесь на этой странице — статус появится ниже.")
                st.rerun()
            except ReferenceUiError as error:
                st.error("Видео не запущено. Причина: " + str(error))
            except Exception as error:
                from content_factory_core import FactoryError
                if isinstance(error, FactoryError):
                    st.error("Видео не запущено. Причина: " + str(error))
                else:
                    st.error("Видео не запущено. Причина: не удалось создать задачу производства.")
        return

    _render_master_status(
        owner_id, project, store, service, key, created_job_id
    )


def render_reference_tool(owner_id, project, store, service):
    """Rendering and storage retries never start a paid model operation."""
    from content_studio_tools import _ready
    from content_studio_reference import ReferenceError

    st.subheader("Видео по образцу — 3 шага")
    st.caption("1. Загрузить образец → 2. Утвердить сценарий → 3. Создать готовое видео. Всё на одной странице.")
    if not _ready(owner_id, project, store) or service is None:
        return
    owner_id = int(owner_id)
    key = _key(owner_id, project)
    _load_saved(owner_id, project, store, key)
    epoch = st.session_state.get(key + "_upload_epoch", 0)
    st.markdown("### 1. Загрузите видео-образец")
    with st.form(key + "_analysis_form"):
        upload = st.file_uploader(
            "Видео-образец",
            type=["mp4", "mov", "webm"],
            key=key + f"_file_{epoch}",
        )
        goal = st.text_area(
            "Что должно говориться в вашем новом видео?",
            max_chars=3000,
            height=110,
            placeholder=(
                "Например: Агентство W освобождает время от рутины. "
                "Сегодня в 20:00 МСК презентация в Zoom. Напишите «Презентация»."
            ),
            key=key + "_goal",
        )
        style = st.radio(
            "Вид ролика",
            list(STYLES),
            format_func=STYLES.get,
            horizontal=True,
            key=key + "_style",
        )
        target_seconds = 45
        submitted = st.form_submit_button(
            "✨ Подготовить сценарий",
            width="stretch",
            type="primary",
        )
        st.caption("Короткий вертикальный ролик до минуты. Образец — до 50 МБ и 15 минут.")
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
                result = {
                    "report": copy.deepcopy(report),
                    "fingerprint": fingerprint,
                    "request_id": uuid.uuid4().hex,
                    "saved_job_id": None,
                    "save_failed": False,
                    "create_requests": {},
                    "scenario_approved": False,
                    "video_personalization": {},
                }
                st.session_state[key + "_result"] = result
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
