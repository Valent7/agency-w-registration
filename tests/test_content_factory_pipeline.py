"""End-to-end queue tests use real transactions/media storage, no live APIs."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from content_factory_channels import PublishPending, PublishUncertain, publish
from content_factory_core import ContentFactoryService, FactoryError, iso, validate_project
from content_factory_store import SQLiteStore, StoreError
from content_factory_worker import Worker


class FakeGenerator:
    def __init__(self):
        self.calls = []

    def generate(self, project, job):
        self.calls.append((project["id"], job["id"]))
        return {"format": job["format"], "text": "Полезный материал для аудитории", "title": "Польза"}, [
            {"filename": "cover.jpg", "data": b"generated-image-fixture", "mime_type": "image/jpeg"}
        ]


class FactoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "queue.sqlite3", Fernet.generate_key())
        self.now = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        self.clock = lambda: self.now
        self.service = ContentFactoryService(self.store, self.clock)
        self.project = self.service.save_project(101, {
            "name": "Агентство W", "product": "Команда ИИ-помощников", "audience": "Предприниматели",
            "platforms": ["threads"], "formats": ["post"], "publish_times": ["23:00"],
            "automation_enabled": True, "plan_days": 1,
        })
        self.generator = FakeGenerator()
        self.publisher = Mock(return_value={"external_id": "123", "permalink": "https://www.threads.net/post/123"})
        self.worker = Worker(self.store, self.generator, publisher=self.publisher, clock=self.clock, worker_id="test-worker")

    def tearDown(self):
        self.directory.cleanup()

    def job(self, **changes):
        values = {"platform": "threads", "format": "post", "state": "queued", "dedupe_key": "fixture",
                  "scheduled_at": iso(self.now - timedelta(minutes=1)), "available_at": iso(self.now),
                  "content": {}, "assets": [], "checkpoint": {}}
        values.update(changes)
        return self.store.enqueue_job(101, self.project["id"], values)

    def connect(self):
        self.store.save_connection(101, self.project["id"], "threads", "456", "Мой аккаунт", "synthetic-test-token")

    def test_generation_persistence_publish_and_restart_do_not_duplicate(self):
        job = self.job()
        self.connect()
        result = self.worker.run_once(101, self.project["id"])
        self.assertEqual(result["generated"], 1)
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["state"], "scheduled")
        self.assertTrue(Path(self.store.resolve_assets(101, saved["assets"])[0]["path"]).is_file())
        self.assertEqual(self.worker.run_once(101, self.project["id"])["published"], 1)
        Worker(self.store, self.generator, publisher=self.publisher, clock=self.clock).run_once(101, self.project["id"], max_jobs=5)
        self.assertEqual(self.publisher.call_count, 1)
        self.assertEqual(self.store.get_job(101, job["id"])["external_id"], "123")

    def test_schedule_is_not_published_early(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"}, scheduled_at=iso(self.now + timedelta(hours=1)))
        self.worker.run_once(101, self.project["id"])
        self.publisher.assert_not_called()
        self.assertEqual(self.store.get_job(101, job["id"])["available_at"], iso(self.now + timedelta(hours=1)))

    def test_review_required_stops_before_publication(self):
        self.project = self.service.save_project(101, {"id": self.project["id"], "review_required": True})
        self.connect()
        job = self.job()
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "ready")
        self.service.approve(101, job["id"], {"text": "Отредактированный текст"})
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.publisher.call_args.args[2]["text"], "Отредактированный текст")

    def test_explicit_request_generates_in_background_with_schedule_off(self):
        self.store.save_project(101, {**self.project, "automation_enabled": False})
        job = self.job(requested_by_owner=True)
        result = self.worker.run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(saved["progress"]["stage"], "ready")
        self.assertEqual(self.worker.run_once()["published"], 0)
        self.publisher.assert_not_called()

    def test_background_request_flag_does_not_allow_publication_while_paused(self):
        self.store.save_project(101, {**self.project, "automation_enabled": False})
        job = self.job(state="scheduled", requested_by_owner=True, content={"format": "post", "text": "Готовый текст"})
        self.connect()
        self.worker.run_once()
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "paused")
        self.publisher.assert_not_called()

    def test_job_specific_review_survives_project_without_review(self):
        self.connect()
        job = self.job(review_required=True, requested_by_owner=True)
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "ready")
        self.worker.run_once(101, self.project["id"])
        self.publisher.assert_not_called()
        self.service.approve(101, job["id"])
        self.worker.run_once(101, self.project["id"])
        self.publisher.assert_called_once()

    def test_job_review_is_rechecked_at_public_write_barrier(self):
        self.connect()
        job = self.job(state="scheduled", review_required=True,
                       content={"format": "post", "text": "Готовый текст"})
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "ready")
        self.publisher.assert_not_called()

    def test_quality_failure_keeps_editable_context_and_never_publishes(self):
        from content_factory_generation import GenerationError
        draft = {"format": "post", "text": "Неудачный черновик", "title": "Черновик",
                 "quality_review": {"approved": False, "issues": ["Нет конкретного примера"], "brief_feedback": "Добавить пример"}}
        job = self.job()
        self.worker.generator = Mock()
        self.worker.generator.generate.side_effect = GenerationError("Требуется доработка", draft=draft)
        self.worker.run_once(101, self.project["id"])
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["content"], draft)
        self.assertEqual(saved["progress"]["stage"], "failed")
        self.assertEqual(saved["assets"], [])
        self.publisher.assert_not_called()

    def test_upload_failure_retains_generated_copy_but_never_schedules_it(self):
        job = self.job()
        with patch.object(self.store, "put_asset", side_effect=StoreError("Хранилище временно недоступно")):
            self.worker.run_once(101, self.project["id"])
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertTrue(saved["content"]["text"])
        self.assertNotIn("generation_complete", saved["checkpoint"])
        self.assertEqual(saved["checkpoint"]["pipeline_version"], 2)
        self.publisher.assert_not_called()
        self.assertEqual(self.service.retry(101, job["id"])["state"], "queued")

    def test_rejected_scheduled_content_cannot_bypass_the_publisher_gate(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Отклонённый текст",
                       "quality_review": {"approved": False, "issues": ["Выдуманный результат"], "brief_feedback": "Исправить"}})
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "failed")
        self.publisher.assert_not_called()

    def test_missing_connection_preserves_created_media(self):
        job = self.job()
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.worker.run_once(101, self.project["id"])["needs_connection"], 1)
        self.connect()
        self.service.retry(101, job["id"])
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(self.publisher.call_count, 1)

    def test_uncertain_publication_cannot_be_retried(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        def interrupted(platform, connection, content, assets, checkpoint, save):
            save({**checkpoint, "publish_started": True, "container_id": "789"})
            raise PublishUncertain("Нет подтверждённого результата")
        self.worker.publisher = interrupted
        self.assertEqual(self.worker.run_once(101, self.project["id"])["reconcile"], 1)
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "reconcile")
        with self.assertRaises(FactoryError):
            self.service.retry(101, job["id"])

    def test_processing_container_resumes_saved_checkpoint(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        def pending(platform, connection, content, assets, checkpoint, save):
            save({**checkpoint, "container_id": "789"})
            raise PublishPending("Обработка", retry_after=60)
        self.worker.publisher = pending
        self.assertEqual(self.worker.run_once(101, self.project["id"])["pending"], 1)
        self.now += timedelta(seconds=61)
        self.worker.publisher = self.publisher
        self.worker.run_once(101, self.project["id"], max_jobs=5)
        self.assertEqual(self.publisher.call_args.args[4]["container_id"], "789")
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "published")

    def test_daily_generation_budget_defers_without_paid_calls(self):
        self.project = self.service.save_project(101, {"id": self.project["id"], "max_daily_generations": 1})
        self.store.reserve_generation(101, self.project["id"], "previous-attempt", "2030-01-01", 1)
        job = self.job()
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "queued")
        self.assertGreater(self.store.get_job(101, job["id"])["available_at"], iso(self.now))

    def test_pausing_stops_existing_jobs_and_resuming_preserves_them(self):
        job = self.job()
        self.service.save_project(101, {"id": self.project["id"], "automation_enabled": False})
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "paused")
        self.service.save_project(101, {"id": self.project["id"], "automation_enabled": True})
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "queued")

    def test_another_owner_cannot_process_or_approve_job(self):
        job = self.job()
        with self.assertRaises(ValueError):
            self.worker.run_once(202, self.project["id"])
        with self.assertRaises(FactoryError):
            self.service.approve(202, job["id"])
        self.assertIsNone(self.store.claim_job("other", iso(self.now), owner_id=202))

    def test_interrupted_paid_generation_requires_explicit_retry(self):
        job = self.job(checkpoint={"generation_started": iso(self.now)})
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "failed")
        self.assertEqual(self.generator.calls, [])
        self.service.retry(101, job["id"])
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(len(self.generator.calls), 1)

    def test_saved_remote_success_recovers_even_after_disconnect_and_pause(self):
        job = self.job(state="scheduled", checkpoint={"result": {"external_id": "999", "permalink": ""}})
        self.store.save_project(101, {**self.project, "automation_enabled": False})
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "published")
        self.publisher.assert_not_called()

    def test_pause_during_preparation_prevents_public_write(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        def paused(platform, connection, content, assets, checkpoint, save):
            self.store.save_project(101, {**self.project, "automation_enabled": False})
            save({"publish_started": True})
            self.fail("Public write must not be reached")
        self.worker.publisher = paused
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "paused")

    def test_real_adapter_preserves_pause_through_checkpoint_wrapper(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {"id": "789"})
        def preparing(*args, **kwargs):
            self.store.save_project(101, {**self.project, "automation_enabled": False})
            return Mock(status_code=200, json=lambda: {"status": "FINISHED"})
        transport.get.side_effect = preparing
        self.worker.publisher = lambda *args: publish(*args, session=transport)
        result = self.worker.run_once(101, self.project["id"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(transport.post.call_count, 1)  # container only; no public write
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "paused")

    def test_disconnecting_account_during_media_preparation_stops_send(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {"id": "789"})
        def disconnected(*args, **kwargs):
            self.store.delete_connection(101, self.project["id"], "threads")
            return Mock(status_code=200, json=lambda: {"status": "FINISHED"})
        transport.get.side_effect = disconnected
        self.worker.publisher = lambda *args: publish(*args, session=transport)
        self.worker.run_once(101, self.project["id"])
        self.assertEqual(transport.post.call_count, 1)
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "needs_connection")
        self.connect()
        retried = self.service.retry(101, job["id"])
        self.assertNotIn("container_id", retried["checkpoint"])

    def test_token_refresh_cannot_restore_disconnected_account(self):
        self.connect()
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        def renewal(connection, now):
            self.store.delete_connection(101, self.project["id"], "threads")
            return {**connection, "access_token": "synthetic-new-token"}
        with patch("content_factory_credentials.refresh_connection", side_effect=renewal):
            self.worker.run_once(101, self.project["id"])
        self.publisher.assert_not_called()
        self.assertIsNone(self.store.get_connection(101, self.project["id"], "threads"))
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "needs_connection")

    def test_review_can_approve_unchanged_prepared_container(self):
        job = self.job(state="ready", content={"format": "post", "text": "Исходный текст"}, checkpoint={"container_id": "789"})
        with self.assertRaises(FactoryError):
            self.service.approve(101, job["id"], {"text": "Другой текст"})
        self.assertEqual(self.service.approve(101, job["id"], {"text": "Исходный текст"})["state"], "scheduled")

    def test_removed_platform_and_new_review_requirement_stop_old_jobs(self):
        job = self.job(state="scheduled", content={"format": "post", "text": "Текст"})
        self.project = self.service.save_project(101, {"id": self.project["id"], "review_required": True})
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "ready")
        self.service.approve(101, job["id"])
        self.service.save_project(101, {"id": self.project["id"], "platforms": ["vk"]})
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "paused")

    def test_project_pause_recovers_saved_success_as_published(self):
        job = self.job(state="scheduled", checkpoint={"result": {"external_id": "999", "permalink": ""}})
        self.service.save_project(101, {"id": self.project["id"], "automation_enabled": False})
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "published")

    def test_offboarded_owner_is_not_processed_by_background_worker(self):
        revoked = 291173537
        project = self.store.save_project(revoked, {**validate_project({"name": "Тест", "product": "Продукт", "audience": "Люди"}), "automation_enabled": True})
        job = self.store.enqueue_job(revoked, project["id"], {"platform": "threads", "format": "post", "dedupe_key": "revoked", "available_at": iso(self.now), "scheduled_at": iso(self.now)})
        self.worker.run_once(max_jobs=1)
        self.assertEqual(self.store.get_job(revoked, job["id"])["state"], "cancelled")
        self.assertEqual(self.generator.calls, [])


class SchedulingTests(unittest.TestCase):
    def test_invalid_project_schedule_and_platform_are_rejected(self):
        base = {"name": "Проект", "product": "Услуга", "audience": "Клиенты"}
        for field, value in (("platforms", ["unknown"]), ("publish_times", ["25:00"]),
                             ("timezone", "No/SuchZone"), ("automation_enabled", "false")):
            with self.subTest(field=field), self.assertRaises(FactoryError):
                validate_project({**base, field: value})

    def test_plan_is_idempotent_and_all_formats_reach_every_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "queue.sqlite3")
            service = ContentFactoryService(store, lambda: datetime(2030, 1, 1, 0, tzinfo=timezone.utc))
            project = service.save_project(101, {"name": "Проект", "product": "Услуга", "audience": "Клиенты", "publish_times": ["10:00"]})
            first = service.plan(101, project["id"], 3)
            second = service.plan(101, project["id"], 3)
            self.assertEqual({j["id"] for j in first}, {j["id"] for j in second})
            self.assertEqual(len(store.list_jobs(101, project["id"])), 9)
            self.assertEqual({(j["platform"], j["format"]) for j in first},
                             {(platform, format_) for platform in ("instagram", "vk", "threads") for format_ in ("post", "carousel", "reel")})

    def test_spring_dst_slot_is_once_and_moves_to_valid_local_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "queue.sqlite3")
            service = ContentFactoryService(store, lambda: datetime(2030, 3, 31, 0, tzinfo=timezone.utc))
            project = service.save_project(101, {"name": "Проект", "product": "Услуга", "audience": "Клиенты", "publish_times": ["02:30"], "platforms": ["threads"]})
            jobs = service.plan(101, project["id"], 1)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(datetime.fromisoformat(jobs[0]["scheduled_at"]), datetime(2030, 3, 31, 1, 30, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
