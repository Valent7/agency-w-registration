"""Project validation and durable editorial scheduling, independent of Streamlit."""
from __future__ import annotations

import re
import copy
import json
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from agency_access_control import is_agency_access_revoked

PLATFORMS = ("instagram", "vk", "threads")
FORMATS = ("post", "carousel", "reel")
UTC = timezone.utc


class FactoryError(ValueError):
    pass


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise FactoryError("Укажите часовой пояс времени публикации.")
    return value.astimezone(UTC)


def iso(value):
    return timestamp(value).isoformat()


def validate_project(data):
    result = dict(data)
    required = {"name": 120, "product": 6000, "audience": 3000}
    labels = {"name": "Название проекта", "product": "Продукт или услуга", "audience": "Аудитория",
              "language": "Язык", "tone": "Стиль общения", "goal": "Цель", "website": "Сайт", "cta": "Призыв к действию",
              "facts": "Проверенные сведения", "examples": "Примеры хороших материалов",
              "visual_style": "Визуальный стиль", "avoid": "Что нельзя обещать и использовать"}
    optional = {"language": ("Русский", 80), "tone": ("Ясно, доброжелательно, по делу", 1000),
                "goal": ("Привлекать заинтересованных клиентов", 2000), "website": ("", 500),
                "cta": ("Узнать подробнее", 500)}
    for name, limit in required.items():
        result[name] = str(data.get(name) or "").strip()
        if not result[name] or len(result[name]) > limit:
            raise FactoryError(f"Заполните поле «{labels[name]}»: от 1 до {limit} символов.")
    for name, (default, limit) in optional.items():
        result[name] = str(data.get(name) or default).strip()
        if len(result[name]) > limit:
            raise FactoryError(f"Слишком длинное поле «{labels[name]}».")
    for name, limit in (("facts", 6000), ("examples", 6000), ("visual_style", 2000), ("avoid", 2000)):
        value = data.get(name, "")
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise FactoryError(f"Поле «{labels[name]}» должно содержать текст до {limit} символов.")
        result[name] = value.strip()
    video_mode = data.get("video_mode", "illustrated")
    if not isinstance(video_mode, str) or video_mode not in {"live", "illustrated"}:
        raise FactoryError("Выберите поддерживаемый способ создания видео.")
    result["video_mode"] = video_mode
    if result["website"] and not re.fullmatch(r"https://[^\s/@]+(?:/[^\s]*)?", result["website"]):
        raise FactoryError("Ссылка проекта должна начинаться с https://.")
    for name, allowed in (("platforms", PLATFORMS), ("formats", FORMATS)):
        values = data.get(name, list(allowed))
        if not isinstance(values, list) or not values or any(value not in allowed for value in values):
            raise FactoryError(f"Выберите поддерживаемые значения {name}.")
        result[name] = list(dict.fromkeys(values))
    times = data.get("publish_times", ["10:00"])
    if not isinstance(times, list) or not 1 <= len(times) <= 6 or any(
        not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
        for value in times
    ):
        raise FactoryError("Укажите от 1 до 6 времён в формате ЧЧ:ММ.")
    result["publish_times"] = sorted(set(times))
    result["timezone"] = str(data.get("timezone") or "Europe/Berlin")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise FactoryError("Неизвестный часовой пояс. Например: Europe/Berlin или Europe/Moscow.") from None
    result["brand_color"] = str(data.get("brand_color") or "#D4AF37")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", result["brand_color"]):
        raise FactoryError("Цвет бренда должен иметь формат #RRGGBB.")
    for field, default, maximum in (("max_daily_generations", 6, 60), ("plan_days", 7, 30)):
        try:
            value = int(data.get(field, default))
        except (TypeError, ValueError):
            raise FactoryError(f"Поле {field} должно быть целым числом.") from None
        if not 1 <= value <= maximum:
            raise FactoryError(f"Допустимое значение {field}: от 1 до {maximum}.")
        result[field] = value
    for field, default in (("automation_enabled", False), ("review_required", False)):
        value = data.get(field, default)
        if not isinstance(value, bool):
            raise FactoryError(f"Поле {field} должно иметь значение да/нет.")
        result[field] = value
    return result


def _reference_video_personalization(reference):
    """Copy only bounded, owner-saved finishing controls into a production job."""
    checkpoint = reference.get("checkpoint") if isinstance(reference, dict) else None
    raw = checkpoint.get("video_personalization") if isinstance(checkpoint, dict) else None
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in ("intro_asset", "outro_asset", "music_asset"):
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, dict) or not isinstance(value.get("storage_path"), str):
                raise FactoryError("Сохранённое оформление видео повреждено. Выберите файлы заново.")
            result[key] = copy.deepcopy(value)
    for key, default, maximum in (("voice_volume", 1.0, 1.5), ("music_volume", 0.12, 0.40)):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= maximum:
            raise FactoryError("Сохранённая громкость видео имеет неверное значение.")
        result[key] = float(value)
    return result


class ContentFactoryService:
    def __init__(self, store, clock=utc_now):
        self.store = store
        self.clock = clock

    def _project(self, owner_id, project_id):
        if is_agency_access_revoked(owner_id):
            raise FactoryError("Доступ к Агентству W для этой учётной записи закрыт.")
        project = self.store.get_project(owner_id, project_id)
        if not project:
            raise FactoryError("Проект не найден в вашем кабинете.")
        return project

    def _job(self, owner_id, job_id):
        if is_agency_access_revoked(owner_id):
            raise FactoryError("Доступ к Агентству W для этой учётной записи закрыт.")
        job = self.store.get_job(owner_id, job_id)
        if not job:
            raise FactoryError("Материал не найден в вашем кабинете.")
        return job

    def save_project(self, owner_id, data):
        if is_agency_access_revoked(owner_id):
            raise FactoryError("Доступ к Агентству W для этой учётной записи закрыт.")
        existing = self._project(owner_id, data["id"]) if data.get("id") else {}
        project = self.store.save_project(owner_id, validate_project({**existing, **data}))
        # Pausing affects existing work as well as future planning. An active lease
        # is left to its worker, which rechecks project activation before publishing.
        if existing:
            for job in self.store.list_jobs(owner_id, project["id"], limit=1000):
                if job.get("lease_owner"):
                    continue
                changes = None
                allowed = job["platform"] in project["platforms"] and job["format"] in project["formats"]
                saved = job.get("checkpoint", {}).get("result", {})
                if saved.get("external_id") and job["state"] != "published":
                    changes = {"state": "published", "external_id": str(saved["external_id"]), "permalink": str(saved.get("permalink") or ""), "error": ""}
                elif job.get("checkpoint", {}).get("publish_started") and job["state"] != "published":
                    changes = {"state": "reconcile", "error": "Отправка уже начиналась. Проверьте её результат в аккаунте перед повтором."}
                elif (not allowed or (not project["automation_enabled"] and (
                    job["state"] == "scheduled" or not job.get("requested_by_owner")
                ))) and job["state"] in {"queued", "scheduled"}:
                    changes = {"state": "paused"}
                elif (project["review_required"] or job.get("review_required")) and job["state"] == "scheduled" and not job.get("checkpoint", {}).get("owner_approved"):
                    changes = {"state": "ready"}
                elif project["automation_enabled"] and allowed and job["state"] == "paused":
                    state = "queued"
                    incomplete = job.get("checkpoint", {}).get("pipeline_version") == 2 and job.get("checkpoint", {}).get("generation_complete") is not True
                    if job.get("content") and not incomplete:
                        state = "ready" if (project["review_required"] or job.get("review_required")) and not job.get("checkpoint", {}).get("owner_approved") else "scheduled"
                    changes = {"state": state, "available_at": iso(max(self.clock(), timestamp(job["scheduled_at"]))) if state == "scheduled" else iso(self.clock())}
                if changes:
                    self.store.update_job(owner_id, job["id"], changes, expected_updated_at=job.get("updated_at"))
        return project

    def create_material(self, owner_id, project_id, topic, content_format, platforms, *, request_id, video_style=None):
        """Queue an explicit brief without enabling scheduled publication.

        The request ID survives an ambiguous response or UI rerun. The worker
        may prepare these jobs while the project's publication schedule is off,
        but the usual publication and owner-review checks still apply.
        """
        return self._create_material(owner_id, project_id, topic, content_format, platforms,
                                     request_id=request_id, video_style=video_style)

    def create_from_video_reference(self, owner_id, project_id, reference_job_id, *, platforms, request_id):
        """Queue the owner's accepted original storyboard, never the source pixels."""
        from content_factory_editorial import build_reference_seed, EditorialError, validate_reference_plan
        project = self._project(owner_id, project_id)
        reference = self._job(owner_id, reference_job_id)
        content = reference.get("content")
        if (reference.get("owner_id") != owner_id or reference.get("project_id") != project["id"]
                or reference.get("state") != "studio" or reference.get("platform") != "studio"
                or reference.get("studio_tool") != "reference" or reference.get("studio_complete") is not True
                or not isinstance(content, dict)):
            raise FactoryError("Сначала сохраните собственный сценарий по видео-образцу в этом проекте.")
        if not isinstance(platforms, list) or not platforms:
            raise FactoryError("Выберите площадки для нового видео.")
        try:
            # Validate all publication limits before the first durable enqueue.
            for platform in platforms:
                validate_reference_plan(content.get("reference_plan"), platform=platform)
            seed = build_reference_seed(owner_id, project["id"], reference["id"],
                                        content.get("reference_style"), content.get("reference_plan"), project)
            personalization = _reference_video_personalization(reference)
        except EditorialError as error:
            raise FactoryError(str(error)) from error
        topic = f"Создать собственный ролик по принятому сценарию: {seed['plan']['title']}"
        return self._create_material(owner_id, project_id, topic, "reel", platforms,
                                     request_id=request_id, video_style=seed["style"],
                                     revision={"approved_reference": seed, "review_required": True,
                                               "checkpoint": {"video_personalization": personalization}})

    def create_master_from_video_reference(self, owner_id, project_id, reference_job_id, *, request_id):
        """Queue one owner-only 9:16 master video, independent of social connections."""
        from content_factory_editorial import build_reference_seed, EditorialError, validate_reference_plan
        project = self._project(owner_id, project_id)
        reference = self._job(owner_id, reference_job_id)
        content = reference.get("content")
        if (reference.get("owner_id") != owner_id or reference.get("project_id") != project["id"]
                or reference.get("state") != "studio" or reference.get("platform") != "studio"
                or reference.get("studio_tool") != "reference" or reference.get("studio_complete") is not True
                or not isinstance(content, dict)):
            raise FactoryError("Сначала утвердите сценарий по видео-образцу.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise FactoryError("Не удалось определить задачу. Обновите страницу и повторите создание.")
        try:
            # The master is vertical and reusable across Reels, Stories, Status and Threads.
            validate_reference_plan(content.get("reference_plan"), platform="instagram")
            seed = build_reference_seed(
                owner_id, project["id"], reference["id"],
                content.get("reference_style"), content.get("reference_plan"), project,
            )
            personalization = _reference_video_personalization(reference)
        except EditorialError as error:
            raise FactoryError(str(error)) from error

        topic = f"Создать мастер-ролик по утверждённому сценарию: {seed['plan']['title']}"
        video_style = seed["style"]
        revision = {
            "approved_reference": seed,
            "review_required": True,
            "studio_master": True,
            "checkpoint": {"video_personalization": personalization},
        }
        intent = [topic, "reel", "studio-master", revision, video_style]
        fingerprint = sha256(
            json.dumps(intent, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        for previous in self.store.list_jobs(owner_id, project_id, limit=1000):
            if previous.get("request_id") == request_id and previous.get("request_fingerprint") != fingerprint:
                raise FactoryError("Эта задача уже сохранена с другими условиями. Создайте новую задачу.")

        now = iso(self.clock())
        key = sha256(f"reference-master|{project_id}|{request_id}".encode()).hexdigest()
        job = self.store.enqueue_job(owner_id, project_id, {
            # 'instagram' is an internal 9:16 render profile here, not a publication target.
            "platform": "instagram",
            "format": "reel",
            "scheduled_at": now,
            "available_at": now,
            "state": "queued",
            "dedupe_key": key,
            "topic": topic,
            "requested_by_owner": True,
            "request_id": request_id,
            "request_fingerprint": fingerprint,
            "video_mode": "live",
            "video_style": video_style,
            "content": {},
            "assets": [],
            "checkpoint": revision["checkpoint"],
            "error": "",
            "attempts": 0,
            "approved_reference": seed,
            "review_required": True,
            "studio_master": True,
        })
        if job.get("request_fingerprint") != fingerprint:
            raise FactoryError("Эта задача уже сохранена с другими условиями. Создайте новую задачу.")
        return job

    def revise_material(self, owner_id, job_id, feedback, *, request_id):
        job = self._job(owner_id, job_id)
        if job.get("state") not in {"ready", "paused", "failed"} or not job.get("content"):
            raise FactoryError("Переработать можно подготовленный материал, который ещё не отправлен площадке.")
        if job.get("external_id") or job.get("checkpoint", {}).get("publish_started"):
            raise FactoryError("Материал уже передан площадке. Для новой публикации создайте отдельную задачу.")
        if _uncertain_video_creation(job.get("production")):
            raise FactoryError("Видеосервис мог принять прошлый заказ. Сначала проверьте его результат, чтобы не оплачивать повторно.")
        if not isinstance(feedback, str) or not 5 <= len(feedback.strip()) <= 3000 or "\x00" in feedback:
            raise FactoryError("Опишите, что изменить: от 5 до 3000 символов.")
        return self._create_material(
            owner_id, job["project_id"], job.get("topic") or job["content"].get("title") or "Переработать готовый материал",
            job["format"], [job["platform"]], request_id=request_id,
            revision={"source_job_id": job["id"], "previous_content": copy.deepcopy(job["content"]),
                      "revision_feedback": feedback.strip(), "review_required": True},
            video_style=job.get("video_style") if job["format"] == "reel" else None,
        )

    def _create_material(self, owner_id, project_id, topic, content_format, platforms, *, request_id, revision=None,
                         video_style=None):
        project = self._project(owner_id, project_id)
        if video_style is not None and (content_format != "reel" or not isinstance(video_style, str)
                                        or video_style not in {"live", "cartoon"}):
            raise FactoryError("Выберите живое видео или мультфильм для формата Видео / Reels.")
        if not isinstance(topic, str) or not 10 <= len(topic.strip()) <= 6000 or "\x00" in topic:
            raise FactoryError("Опишите задачу подробнее: от 10 до 6000 символов.")
        topic = topic.strip()
        if content_format not in FORMATS or content_format not in project.get("formats", []):
            raise FactoryError("Выберите формат, включённый в настройках проекта.")
        if not isinstance(platforms, list) or not platforms or any(
            value not in PLATFORMS or value not in project.get("platforms", []) for value in platforms
        ):
            raise FactoryError("Выберите площадки, включённые в настройках проекта.")
        platforms = list(dict.fromkeys(platforms))
        connected = {item.get("platform") for item in self.store.list_connections(owner_id, project_id)}
        if any(platform not in connected for platform in platforms):
            raise FactoryError("Сначала подключите выбранные площадки во вкладке «Площадки».")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise FactoryError("Не удалось определить задачу. Обновите страницу и повторите создание.")
        video_mode = ("live" if video_style is not None else project.get("video_mode", "illustrated")) if content_format == "reel" else None
        intent = [topic, content_format, sorted(platforms), revision, video_mode]
        if video_style is not None:
            intent.append(video_style)
        fingerprint = sha256(json.dumps(intent, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for previous in self.store.list_jobs(owner_id, project_id, limit=1000):
            if previous.get("request_id") == request_id and previous.get("request_fingerprint") != fingerprint:
                raise FactoryError("Эта задача уже сохранена с другими условиями. Создайте новую задачу.")
        now = iso(self.clock())
        jobs = []
        for platform in platforms:
            key = sha256(f"request|{project_id}|{request_id}|{platform}".encode()).hexdigest()
            job = self.store.enqueue_job(owner_id, project_id, {
                "platform": platform, "format": content_format, "scheduled_at": now,
                "available_at": now, "state": "queued", "dedupe_key": key,
                "topic": topic, "requested_by_owner": True, "request_id": request_id,
                "request_fingerprint": fingerprint,
                **({"video_mode": video_mode} if content_format == "reel" else {}),
                **({"video_style": video_style} if video_style is not None else {}),
                "content": {}, "assets": [], "checkpoint": {}, "error": "", "attempts": 0,
                **(revision or {}),
            })
            if job.get("request_fingerprint") != fingerprint:
                raise FactoryError("Эта задача уже сохранена с другими условиями. Создайте новую задачу.")
            jobs.append(job)
        return jobs

    def plan(self, owner_id, project_id, days=7, *, connected_only=False):
        project = self._project(owner_id, project_id)
        if not isinstance(days, int) or not 1 <= days <= 30:
            raise FactoryError("План можно создать на срок от 1 до 30 дней.")
        platforms = list(project["platforms"])
        if connected_only:
            connected = {item["platform"] for item in self.store.list_connections(owner_id, project_id)}
            platforms = [platform for platform in platforms if platform in connected]
        now = timestamp(self.clock())
        tz = ZoneInfo(project["timezone"])
        local_today = now.astimezone(tz).date()
        jobs = []
        for offset in range(days):
            day = local_today + timedelta(days=offset)
            for slot, clock_text in enumerate(project["publish_times"]):
                hour, minute = map(int, clock_text.split(":"))
                local = datetime.combine(day, time(hour, minute), tzinfo=tz)
                # Roundtrip moves a nonexistent spring-DST time forward. During
                # autumn's repeated hour fold=0 deterministically creates one slot.
                scheduled = local.astimezone(UTC)
                if scheduled < now:
                    continue
                format_ = project["formats"][(day.toordinal() * len(project["publish_times"]) + slot) % len(project["formats"])]
                for platform in platforms:
                    key = sha256(f"{project_id}|{day.isoformat()}|{clock_text}|{platform}".encode()).hexdigest()
                    jobs.append(self.store.enqueue_job(owner_id, project_id, {
                        "platform": platform, "format": format_, "scheduled_at": iso(scheduled),
                        "available_at": iso(max(now, scheduled - timedelta(hours=24))),
                        "state": "queued", "dedupe_key": key,
                        "topic": f"{project['goal']}. Материал для {day.isoformat()}, {clock_text}. "
                                 f"Покажи конкретную пользу проекта «{project['name']}» для его аудитории.",
                        **({"video_mode": project.get("video_mode", "illustrated")} if format_ == "reel" else {}),
                        "content": {}, "assets": [], "checkpoint": {}, "error": "", "attempts": 0,
                    }))
        return jobs

    def approve(self, owner_id, job_id, content=None):
        job = self._job(owner_id, job_id)
        if job["state"] not in {"ready", "paused", "scheduled"} or not job.get("content"):
            raise FactoryError("Утвердить можно только подготовленный материал.")
        checkpoint = dict(job.get("checkpoint") or {})
        if _uncertain_video_creation(job.get("production")):
            raise FactoryError("Создание видео ещё не подтверждено. Сначала проверьте результат в видеосервисе.")
        review = job["content"].get("quality_review")
        if (isinstance(review, dict) and review.get("approved") is False) or (
            checkpoint.get("pipeline_version") == 2 and checkpoint.get("generation_complete") is not True
        ):
            raise FactoryError("Создание или проверка качества ещё не завершены. Сначала передайте материал на доработку.")
        if checkpoint.get("publish_started"):
            raise FactoryError("Материал уже передан площадке; менять его во время публикации нельзя.")
        updated_content = dict(job["content"])
        if content is not None:
            text = content.get("text") if isinstance(content, dict) else content
            if not isinstance(text, str) or not text.strip():
                raise FactoryError("Текст публикации не может быть пустым.")
            limit = {"instagram": 2200, "threads": 500, "vk": 16000}[job["platform"]]
            if len(text) > limit:
                raise FactoryError(f"Для этой площадки допустимо до {limit} символов.")
            updated_content["text"] = text.strip()
        if any(checkpoint.get(key) for key in ("container_id", "children", "vk_attachments")) and updated_content != job["content"]:
            raise FactoryError("Медиа уже подготовлено площадкой. Можно утвердить исходный текст; для изменения создайте новый материал.")
        checkpoint["owner_approved"] = True
        project = self._project(owner_id, job["project_id"])
        return self.store.update_job(owner_id, job_id, {
            "content": updated_content, "checkpoint": checkpoint,
            "state": "scheduled" if project["automation_enabled"] else "paused",
            "available_at": iso(max(timestamp(self.clock()), timestamp(job["scheduled_at"]))), "error": "",
        }, expected_updated_at=job.get("updated_at"))

    def cancel(self, owner_id, job_id):
        job = self._job(owner_id, job_id)
        if job["state"] in {"published", "reconcile"} or job.get("checkpoint", {}).get("publish_started"):
            raise FactoryError("Публикация уже отправлена площадке. Проверьте её результат в аккаунте.")
        return self.store.update_job(owner_id, job_id, {"state": "cancelled", "error": ""}, expected_updated_at=job.get("updated_at"))

    def resume_stalled_video(self, owner_id, job_id):
        job = self._job(owner_id, job_id)
        try:
            return self.store.resume_stalled_video_job(
                owner_id, job_id, iso(self.clock()), stale_seconds=180
            )
        except Exception as exc:
            from content_factory_store import StoreError
            if isinstance(exc, StoreError):
                raise FactoryError(str(exc)) from None
            raise

    def retry(self, owner_id, job_id):
        job = self._job(owner_id, job_id)
        if job["state"] not in {"failed", "needs_connection"}:
            raise FactoryError("Повтор доступен для ошибки подготовки или отсутствующего подключения.")
        checkpoint = dict(job.get("checkpoint") or {})
        if checkpoint.get("publish_started"):
            raise FactoryError("Результат отправки неизвестен. Сначала проверьте аккаунт: повтор может создать дубликат.")
        production = copy.deepcopy(job.get("production"))
        if _uncertain_video_creation(production):
            raise FactoryError("Видеосервис мог принять прошлый заказ. Сначала проверьте его результат, чтобы не оплачивать повторно.")
        production_changes = {}
        if isinstance(production, dict) and production.get("failed_indices"):
            failed = production["failed_indices"]
            tasks, clips = production.get("tasks"), production.get("clips")
            if (not isinstance(failed, list) or not isinstance(tasks, list) or not isinstance(clips, list)
                    or len(tasks) != len(clips) or any(type(i) is not int or not 0 <= i < len(tasks) for i in failed)):
                raise FactoryError("Не удалось проверить предыдущие сцены. Нужна проверка администратора.")
            for index in failed:
                tasks[index], clips[index] = None, None
            production.update(phase="inputs_ready", starting_index=None, failed_indices=[], creation_uncertain=False)
            production_changes["production"] = production
        review = (job.get("content") or {}).get("quality_review")
        incomplete = checkpoint.get("pipeline_version") == 2 and checkpoint.get("generation_complete") is not True
        regenerate = incomplete or bool(production_changes) or (isinstance(review, dict) and review.get("approved") is False)
        # Retain already-created provider containers, but a deliberate retry may
        # start a failed generation again. It consumes a new daily reservation.
        checkpoint.pop("generation_started", None)
        checkpoint.pop("retry_count", None)
        if regenerate:
            checkpoint.pop("generation_complete", None)
        connection = self.store.get_connection(owner_id, job["project_id"], job["platform"])
        if connection and checkpoint.get("target_account_id") and (
            str(connection["account_id"]) != str(checkpoint["target_account_id"])
            or connection.get("id") != checkpoint.get("target_connection_id")
        ):
            # An explicit owner retry authorizes the replacement account. Old
            # unpublished containers are discarded; an uncertain write is never
            # allowed into this branch because publish_started was checked above.
            checkpoint = {key: value for key, value in checkpoint.items() if key in {"owner_approved"}}
        project = self._project(owner_id, job["project_id"])
        state = "queued" if not job.get("content") or regenerate else (
            "ready" if (project["review_required"] or job.get("review_required")) and not checkpoint.get("owner_approved") else "scheduled"
        )
        if not project["automation_enabled"] and state == "scheduled":
            state = "paused"
        return self.store.update_job(owner_id, job_id, {
            "state": state, "checkpoint": checkpoint, "error": "",
            **production_changes,
            "available_at": iso(max(timestamp(self.clock()), timestamp(job["scheduled_at"]))) if state == "scheduled" else iso(self.clock()),
        }, expected_updated_at=job.get("updated_at"))


def _uncertain_video_creation(production):
    if production is None:
        return False
    if not isinstance(production, dict):
        return bool(production)
    if production.get("creation_uncertain"):
        return True
    index = production.get("starting_index")
    if index is None:
        return False
    tasks = production.get("tasks")
    return (type(index) is not int or not isinstance(tasks, list) or not 0 <= index < len(tasks)
            or not isinstance(tasks[index], str) or not tasks[index].strip())
