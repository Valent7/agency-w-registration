"""Durable AI generation and publication worker; never depends on a browser session."""
from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from content_factory_channels import PublishError, PublishPending, PublishUncertain, publish
from content_factory_core import ContentFactoryService, iso, timestamp, utc_now
from content_factory_live_video import LiveVideoPending, LiveVideoError, LiveVideoUncertain
from content_factory_live_media import LiveMediaError
from agency_access_control import is_agency_access_revoked

LOG = logging.getLogger(__name__)


class PauseRequested(Exception):
    def __init__(self, state="paused"):
        self.state = state


class Worker:
    def __init__(self, store, generator=None, *, publisher=publish, clock=utc_now, worker_id=None):
        self.store = store
        self.generator = generator
        self.publisher = publisher
        self.clock = clock
        self.worker_id = worker_id or str(uuid.uuid4())

    def run_once(self, owner_id=None, project_id=None, max_jobs=1):
        if project_id and owner_id is None:
            raise ValueError("Выбор проекта требует владельца.")
        if owner_id is not None and is_agency_access_revoked(owner_id):
            raise ValueError("Доступ к Агентству W для этой учётной записи закрыт.")
        if not isinstance(max_jobs, int) or not 1 <= max_jobs <= 20:
            raise ValueError("За один проход можно обработать от 1 до 20 материалов.")
        result = {key: 0 for key in ("planned", "processed", "generated", "published", "pending", "failed", "needs_connection", "reconcile")}
        result["errors"] = []
        service = ContentFactoryService(self.store, self.clock)
        if owner_id is None:
            projects = self.store.list_active_projects()
        elif project_id:
            project = self.store.get_project(owner_id, project_id)
            if not project:
                raise ValueError("Проект не найден в вашем кабинете.")
            projects = [project]
        else:
            projects = self.store.list_projects(owner_id)
        for project in projects:
            if project.get("automation_enabled") and not is_agency_access_revoked(project["owner_id"]):
                existing = {job["id"] for job in self.store.list_jobs(project["owner_id"], project["id"], limit=1000)}
                planned = service.plan(project["owner_id"], project["id"], int(project.get("plan_days", 7)), connected_only=True)
                result["planned"] += sum(job["id"] not in existing for job in planned)
        for _ in range(max_jobs):
            job = self.store.claim_job(self.worker_id, iso(self.clock()), lease_seconds=7200,
                                       owner_id=owner_id, project_id=project_id)
            if not job:
                break
            result["processed"] += 1
            try:
                outcome = self._process(job, manual=owner_id is not None)
                if outcome in result:
                    result[outcome] += 1
            except PauseRequested as error:
                self._finish(job, state=error.state, error="")
                result["pending"] += 1
            except LiveVideoPending as error:
                self._finish(job, state="queued", available_at=iso(self.clock() + timedelta(seconds=error.retry_after)), error="")
                result["pending"] += 1
            except LiveVideoError as error:
                fresh = self.store.get_job(job["owner_id"], job["id"]) or job
                production = dict(fresh.get("production") or {})
                if isinstance(error, LiveVideoUncertain):
                    production["creation_uncertain"] = True
                if error.retryable and production and not isinstance(error, LiveVideoUncertain):
                    self._finish(job, state="queued", production=production, available_at=iso(self.clock() + timedelta(seconds=60)), error=str(error))
                    result["pending"] += 1
                else:
                    self._finish(job, state="failed", production=production, error=str(error), progress={"stage": "failed", "message": "Живое видео требует внимания. Откройте материал, чтобы посмотреть причину."})
                    result["failed"] += 1
                    result["errors"].append(str(error))
            except PublishPending as error:
                self._finish(job, state="scheduled", available_at=iso(self.clock() + timedelta(seconds=max(15, min(int(error.retry_after), 3600)))), error="")
                result["pending"] += 1
            except PublishUncertain as error:
                self._finish(job, state="reconcile", error=str(error))
                result["reconcile"] += 1
                result["errors"].append(str(error))
            except PublishError as error:
                if isinstance(error.__cause__, PauseRequested):
                    self._finish(job, state=error.__cause__.state, error="")
                    result["pending"] += 1
                    continue
                fresh = self.store.get_job(job["owner_id"], job["id"]) or job
                checkpoint = dict(fresh.get("checkpoint") or {})
                if checkpoint.get("publish_started"):
                    self._finish(job, state="reconcile", error=str(error))
                    result["reconcile"] += 1
                elif error.retryable and int(checkpoint.get("retry_count", 0)) < 3:
                    checkpoint["retry_count"] = int(checkpoint.get("retry_count", 0)) + 1
                    self._finish(job, state="scheduled", checkpoint=checkpoint, error=str(error),
                                 available_at=iso(self.clock() + timedelta(seconds=60 * 2 ** checkpoint["retry_count"])))
                    result["pending"] += 1
                else:
                    self._finish(job, state="failed", error=str(error))
                    result["failed"] += 1
                result["errors"].append(str(error))
            except Exception as error:
                # Never echo requests exceptions: their strings may contain a token
                # in the URL. Providers/store supply their own sanitized messages.
                from content_factory_generation import GenerationError
                from content_factory_store import StoreError
                message = str(error) if isinstance(error, (GenerationError, StoreError, LiveMediaError, ValueError)) else (
                    "Обработка прервана. Проверьте настройки и состояние материала. " + type(error).__name__
                )
                fresh = self.store.get_job(job["owner_id"], job["id"]) or job
                uncertain = bool(fresh.get("checkpoint", {}).get("publish_started"))
                state = "reconcile" if uncertain else "failed"
                changes = {"state": state, "error": message, "progress": {"stage": "failed", "message": "Материал требует внимания. Откройте его, чтобы посмотреть причину и задать правки."}}
                if isinstance(error, GenerationError) and isinstance(error.draft, dict):
                    changes["content"] = error.draft
                if isinstance(error, GenerationError) and error.provider_details:
                    changes["provider_diagnostic"] = dict(error.provider_details)
                self._finish(job, **changes)
                result[state] += 1
                result["errors"].append(message)
        return result

    def _update(self, job, **changes):
        return self.store.update_job(job["owner_id"], job["id"], changes, lease_owner=self.worker_id)

    def _finish(self, job, **changes):
        return self._update(job, **changes, lease_owner=None, lease_until=None)

    def _process(self, job, manual=False):
        self._update(job, processing_version=2,
                     progress={"stage": "accepted", "message": "Фоновый сервис принял материал в работу."})
        checkpoint = dict(job.get("checkpoint") or {})
        if checkpoint.get("result", {}).get("external_id"):
            saved = checkpoint["result"]
            self._finish(job, state="published", external_id=str(saved["external_id"]),
                         permalink=str(saved.get("permalink") or ""), error="")
            return "published"
        if checkpoint.get("publish_started"):
            raise PublishUncertain("Отправка уже начиналась. Проверьте её результат в аккаунте перед повтором.")
        if is_agency_access_revoked(job["owner_id"]):
            self._finish(job, state="cancelled", error="Доступ владельца к Агентству W закрыт.")
            return "pending"
        project = self.store.get_project(job["owner_id"], job["project_id"])
        if not project:
            raise ValueError("Проект больше недоступен.")
        if job["platform"] not in project["platforms"] or job["format"] not in project["formats"]:
            self._finish(job, state="paused", error="Площадка или формат отключены в проекте.")
            return "pending"
        explicitly_requested = job["state"] == "queued" and job.get("requested_by_owner") is True
        if not project.get("automation_enabled") and (job["state"] == "scheduled" or not (manual or explicitly_requested)):
            self._finish(job, state="paused", progress={"stage": "paused", "message": "Материал приостановлен вместе с автопубликацией."})
            return "pending"
        if job["state"] == "queued":
            return self._generate(job, project)
        if job["state"] != "scheduled":
            raise ValueError("Этот материал не ожидает обработки.")
        quality = (job.get("content") or {}).get("quality_review") or {}
        if quality.get("approved") is False or (
            checkpoint.get("pipeline_version") == 2 and checkpoint.get("generation_complete") is not True
        ):
            from content_factory_generation import GenerationError
            raise GenerationError("Материал не прошёл полный цикл подготовки. Задайте правки или повторите создание.",
                                  draft=job.get("content"))
        if timestamp(job["scheduled_at"]) > timestamp(self.clock()):
            self._finish(job, available_at=job["scheduled_at"])
            return "pending"
        if (project.get("review_required") or job.get("review_required")) and not checkpoint.get("owner_approved"):
            self._finish(job, state="ready")
            return "pending"
        # Only the Agency's own videos require its mark. The proof is emitted
        # after local rendering and bound to the stored MP4, never to the copy.
        from agency_video_branding import is_agency_w_brand, branding_proof_matches
        video_assets = [asset for asset in (job.get("assets") or [])
                        if asset.get("kind") == "video" or str(asset.get("mime_type") or "").startswith("video/")]
        if is_agency_w_brand(project.get("name")) and (job["format"] == "reel" or video_assets):
            if len(video_assets) != 1 or not branding_proof_matches(
                (job.get("content") or {}).get("video_branding"), video_assets[0].get("sha256")
            ):
                raise ValueError("У видео не подтверждён оригинальный логотип Агентства W. "
                                 "Сначала пересоберите фирменное оформление; публикация остановлена.")
        connection = self.store.get_connection(job["owner_id"], job["project_id"], job["platform"])
        if not connection:
            self._finish(job, state="needs_connection", error="Подключите аккаунт для публикаций на этой площадке.")
            return "needs_connection"
        if checkpoint.get("target_account_id") and (
            str(checkpoint["target_account_id"]) != str(connection["account_id"])
            or checkpoint.get("target_connection_id") != connection.get("id")
        ):
            self._finish(job, state="needs_connection", error="Аккаунт проекта изменён. Подтвердите повтор для нового подключения.")
            return "needs_connection"
        from content_factory_credentials import CredentialError, refresh_connection
        try:
            renewed = refresh_connection(connection, timestamp(self.clock()))
        except CredentialError as error:
            self._finish(job, state="needs_connection", error=str(error))
            return "needs_connection"
        if renewed is not connection:
            if self.store.refresh_connection_if_unchanged(job["owner_id"], job["project_id"], job["platform"], connection, renewed):
                connection = renewed
            else:
                current = self.store.get_connection(job["owner_id"], job["project_id"], job["platform"])
                if not current or current["id"] != connection["id"] or current["account_id"] != connection["account_id"]:
                    self._finish(job, state="needs_connection", error="Подключение изменено во время обновления доступа. Подтвердите повтор.")
                    return "needs_connection"
                connection = current
        checkpoint["target_account_id"] = str(connection["account_id"])
        checkpoint["target_connection_id"] = connection.get("id")
        self._update(job, checkpoint=dict(checkpoint))
        assets = self.store.resolve_assets(job["owner_id"], job.get("assets") or [])

        def save_checkpoint(value):
            # Recheck a partner's pause before crossing the public-write barrier.
            if value.get("publish_started") and not checkpoint.get("publish_started"):
                latest = self.store.get_project(job["owner_id"], job["project_id"])
                if not latest or not latest.get("automation_enabled") or is_agency_access_revoked(job["owner_id"]) or job["platform"] not in latest["platforms"] or job["format"] not in latest["formats"]:
                    raise PauseRequested()
                if (latest.get("review_required") or job.get("review_required")) and not checkpoint.get("owner_approved"):
                    raise PauseRequested("ready")
                current = self.store.get_connection(job["owner_id"], job["project_id"], job["platform"])
                if not current or current.get("id") != connection.get("id") or current["account_id"] != connection["account_id"]:
                    raise PauseRequested("needs_connection")
            checkpoint.clear()
            checkpoint.update(value)
            self._update(job, checkpoint=dict(checkpoint))

        published = self.publisher(job["platform"], connection, job["content"], assets,
                                   dict(checkpoint), save_checkpoint)
        if not isinstance(published, dict) or not published.get("external_id"):
            raise PublishUncertain("Площадка не подтвердила идентификатор публикации. Проверьте аккаунт перед повтором.")
        self._finish(job, state="published", external_id=str(published["external_id"]),
                     permalink=str(published.get("permalink") or ""), published_at=iso(self.clock()), error="")
        return "published"

    def _generate(self, job, project):
        checkpoint = dict(job.get("checkpoint") or {})
        production = job.get("production")
        live_resume = getattr(self.generator, "supports_live_video_resume", False) is True and self.generator.can_resume_video(production)
        if checkpoint.get("generation_started") and not live_resume:
            raise ValueError("Прошлая генерация прервалась. Автоматический повтор остановлен; проверьте материал и повторите вручную.")
        if self.generator is None:
            raise ValueError("Не подключён ИИ для создания материалов.")
        local_now = timestamp(self.clock()).astimezone(ZoneInfo(project["timezone"]))
        reserved = bool(checkpoint.get("generation_started")) or self.store.reserve_generation(
            job["owner_id"], job["project_id"], f"{job['id']}:{job['attempts']}",
            local_now.date().isoformat(), int(project.get("max_daily_generations", 6)))
        if not reserved:
            tomorrow = datetime.combine(local_now.date() + timedelta(days=1), datetime.min.time(), tzinfo=local_now.tzinfo)
            self._finish(job, available_at=iso(tomorrow), error="Дневной лимит создания материалов достигнут.")
            return "pending"
        checkpoint["generation_started"] = iso(self.clock())
        checkpoint["pipeline_version"] = 2
        checkpoint.pop("generation_complete", None)
        self._update(job, checkpoint=checkpoint, error="")
        recent_titles = [str(item["content"].get("title") or "") for item in
                         self.store.list_jobs(job["owner_id"], job["project_id"], limit=50)
                         if item.get("content") and item["id"] != job["id"]][:10]
        request = dict(job)
        if recent_titles and not job.get("requested_by_owner"):
            request["topic"] = str(job.get("topic") or "") + " Не повторяй недавние темы: " + "; ".join(recent_titles)
        def progress(value):
            self._update(job, progress=dict(value))

        def save_production(value):
            import copy
            if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 256000:
                raise ValueError("Состояние создания видео превышает допустимый размер.")
            changes = {"production": copy.deepcopy(value)}
            if isinstance(value.get("content"), dict):
                changes["content"] = copy.deepcopy(value["content"])
            self._update(job, **changes)

        def save_source(filename, data, mime_type):
            return self.store.put_asset(job["owner_id"], job["project_id"], job["id"], filename, data, mime_type)

        def load_source(asset):
            from pathlib import PurePosixPath
            parts = PurePosixPath(str(asset.get("storage_path") or "")).parts
            allowed = {(str(job["owner_id"]), job["project_id"], job["id"])}
            approved = job.get("approved_reference")
            if isinstance(approved, dict) and isinstance(approved.get("source_job_id"), str):
                allowed.add((str(job["owner_id"]), job["project_id"], approved["source_job_id"]))
            if len(parts) < 4 or parts[:3] not in allowed:
                raise ValueError("Исходный файл не принадлежит этому материалу или принятому сценарию.")
            return self.store.read_asset(job["owner_id"], asset)

        if getattr(self.generator, "supports_live_video_resume", False) is True:
            content, generated_assets = self.generator.generate(project, request, progress=progress, production=production,
                save_production=save_production, save_source=save_source, load_source=load_source)
        elif getattr(self.generator, "supports_progress", False) is True:
            content, generated_assets = self.generator.generate(project, request, progress=progress)
        else:
            content, generated_assets = self.generator.generate(project, request)
        if not isinstance(content, dict) or not str(content.get("text") or "").strip():
            raise ValueError("ИИ не подготовил текст материала.")
        # Keep the paid editorial result even if the subsequent media upload
        # fails. Incomplete production is never eligible for publication.
        self._update(job, content=content, assets=[], checkpoint=dict(checkpoint))
        assets = []
        for asset in generated_assets:
            assets.append(self.store.put_asset(job["owner_id"], job["project_id"], job["id"],
                                               asset["filename"], asset["data"], asset["mime_type"]))
            self._update(job, assets=list(assets))
        checkpoint["generation_complete"] = True
        latest = self.store.get_project(job["owner_id"], job["project_id"])
        state = "ready" if (latest.get("review_required") or job.get("review_required") or not latest.get("automation_enabled")) else "scheduled"
        self._finish(job, content=content, assets=assets, state=state, checkpoint=checkpoint,
                     progress={"stage": "ready", "message": "Материал готов к проверке." if state == "ready" else "Материал подготовлен и ждёт времени публикации."},
                     generated_at=iso(self.clock()), available_at=iso(max(timestamp(self.clock()), timestamp(job["scheduled_at"]))), error="")
        return "generated"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Контент-завод W: генерация и публикации по расписанию")
    parser.add_argument("--once", action="store_true", help="Один проход очереди")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-jobs", type=int, default=2)
    options = parser.parse_args(argv)
    if options.interval < 30 or not 1 <= options.max_jobs <= 20:
        parser.error("interval должен быть >=30 секунд, max-jobs — от 1 до 20")
    from content_factory_runtime import build_store, get_generator
    store = build_store()
    try:
        generator = get_generator()
    except ValueError:
        generator = None
    worker = Worker(store, generator)
    while True:
        try:
            summary = worker.run_once(max_jobs=options.max_jobs)
        except Exception as error:
            print(json.dumps({"worker_error": type(error).__name__, "message": "Не удалось обработать очередь. Проверьте подключение хранилища и применённую миграцию."}, ensure_ascii=False), flush=True)
            if options.once:
                return 1
            time.sleep(options.interval)
            continue
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if options.once:
            return 1 if summary["failed"] or summary["reconcile"] else 0
        time.sleep(options.interval)


if __name__ == "__main__":
    raise SystemExit(main())
