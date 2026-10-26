"""Project validation and durable editorial scheduling, independent of Streamlit."""
from __future__ import annotations

import re
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
              "language": "Язык", "tone": "Стиль общения", "goal": "Цель", "website": "Сайт", "cta": "Призыв к действию"}
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
                elif (not project["automation_enabled"] or not allowed) and job["state"] in {"queued", "scheduled"}:
                    changes = {"state": "paused"}
                elif project["review_required"] and job["state"] == "scheduled" and not job.get("checkpoint", {}).get("owner_approved"):
                    changes = {"state": "ready"}
                elif project["automation_enabled"] and allowed and job["state"] == "paused":
                    state = "queued"
                    if job.get("content"):
                        state = "ready" if project["review_required"] and not job.get("checkpoint", {}).get("owner_approved") else "scheduled"
                    changes = {"state": state, "available_at": iso(max(self.clock(), timestamp(job["scheduled_at"]))) if state == "scheduled" else iso(self.clock())}
                if changes:
                    self.store.update_job(owner_id, job["id"], changes, expected_updated_at=job.get("updated_at"))
        return project

    def plan(self, owner_id, project_id, days=7):
        project = self._project(owner_id, project_id)
        if not isinstance(days, int) or not 1 <= days <= 30:
            raise FactoryError("План можно создать на срок от 1 до 30 дней.")
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
                for platform in project["platforms"]:
                    key = sha256(f"{project_id}|{day.isoformat()}|{clock_text}|{platform}".encode()).hexdigest()
                    jobs.append(self.store.enqueue_job(owner_id, project_id, {
                        "platform": platform, "format": format_, "scheduled_at": iso(scheduled),
                        "available_at": iso(max(now, scheduled - timedelta(hours=24))),
                        "state": "queued", "dedupe_key": key,
                        "topic": f"{project['goal']}. Материал для {day.isoformat()}, {clock_text}. "
                                 f"Покажи конкретную пользу проекта «{project['name']}» для его аудитории.",
                        "content": {}, "assets": [], "checkpoint": {}, "error": "", "attempts": 0,
                    }))
        return jobs

    def approve(self, owner_id, job_id, content=None):
        job = self._job(owner_id, job_id)
        if job["state"] not in {"ready", "paused", "scheduled"} or not job.get("content"):
            raise FactoryError("Утвердить можно только подготовленный материал.")
        checkpoint = dict(job.get("checkpoint") or {})
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

    def retry(self, owner_id, job_id):
        job = self._job(owner_id, job_id)
        if job["state"] not in {"failed", "needs_connection"}:
            raise FactoryError("Повтор доступен для ошибки подготовки или отсутствующего подключения.")
        checkpoint = dict(job.get("checkpoint") or {})
        if checkpoint.get("publish_started"):
            raise FactoryError("Результат отправки неизвестен. Сначала проверьте аккаунт: повтор может создать дубликат.")
        # Retain already-created provider containers, but a deliberate retry may
        # start a failed generation again. It consumes a new daily reservation.
        checkpoint.pop("generation_started", None)
        checkpoint.pop("retry_count", None)
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
        state = "queued" if not job.get("content") else (
            "ready" if project["review_required"] and not checkpoint.get("owner_approved") else "scheduled"
        )
        if not project["automation_enabled"] and state == "scheduled":
            state = "paused"
        return self.store.update_job(owner_id, job_id, {
            "state": state, "checkpoint": checkpoint, "error": "",
            "available_at": iso(max(timestamp(self.clock()), timestamp(job["scheduled_at"]))) if state == "scheduled" else iso(self.clock()),
        }, expected_updated_at=job.get("updated_at"))
