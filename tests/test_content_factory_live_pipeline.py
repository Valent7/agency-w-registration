"""Durable live-video queue behavior with real SQLite leases and private files."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from content_factory_core import ContentFactoryService, FactoryError, iso
from content_factory_generation import can_resume_video
from content_factory_live_video import LiveVideoError, LiveVideoPending, LiveVideoUncertain
from content_factory_store import SQLiteStore, StoreError
from content_factory_worker import Worker


COPY = {
    "format": "reel", "title": "Верните себе вечер", "text": "Передайте повторяющуюся подготовку ИИ-помощнику.",
    "narration": " ".join(["подготовка"] * 50),
    "quality_review": {"approved": True, "issues": [], "brief_feedback": "Готово."},
}


class FakeLiveGenerator:
    """Emulate paid creation separately from safe reads, not the AI renderer."""

    supports_live_video_resume = True
    supports_progress = True
    can_resume_video = staticmethod(can_resume_video)

    def __init__(self, ledger=None, *, failure=None, attack_asset=None):
        self.ledger = ledger if ledger is not None else {"prepared": 0, "created": [], "read": [], "ready": False}
        self.failure = failure
        self.attack_asset = attack_asset

    def generate(self, project, job, *, production=None, save_production, save_source, load_source, progress):
        if self.attack_asset is not None:
            load_source(self.attack_asset)
            raise AssertionError("An unrelated or corrupt source must not be loaded")
        if production is None:
            self.ledger["prepared"] += 1
            sources = [save_source(f"source_{i}.jpg", f"image-{i}".encode(), "image/jpeg") for i in range(4)]
            speech = save_source("speech.mp3", b"spoken-words", "audio/mpeg")
            production = {
                "version": 1, "phase": "inputs_ready", "content": copy.deepcopy(COPY),
                "raw_sources": sources, "speech_source": speech, "duration": 35.0,
                "scene_seconds": [9] * 4, "tasks": [None] * 4, "clips": [None] * 4,
                "starting_index": None, "failed_indices": [],
            }
            save_production(production)
        state = copy.deepcopy(production)
        if state.get("creation_uncertain") or (
            state.get("starting_index") is not None and not state["tasks"][state["starting_index"]]
        ):
            raise LiveVideoUncertain("Прошлый заказ сцены требует проверки.")
        for index, source in enumerate(state["raw_sources"]):
            data = load_source(source)
            self.ledger["read"].append(data)
            if state["tasks"][index] or state["clips"][index]:
                continue
            state["starting_index"] = index
            save_production(state)
            task_id = f"scene-{index}-order-{len(self.ledger['created']) + 1}"
            self.ledger["created"].append(task_id)
            if self.failure == "uncertain":
                raise LiveVideoUncertain("Подтверждение оплаченной сцены не получено.")
            state["tasks"][index] = task_id
            state["starting_index"] = None
            state["phase"] = "clips"
            save_production(state)
        self.ledger["read"].append(load_source(state["speech_source"]))
        if self.failure == "read":
            raise LiveVideoError("Статус сцены временно недоступен.", retryable=True)
        if self.failure == "scene":
            for index in (0, 2, 3):
                state["clips"][index] = save_source(f"clip_{index}.mp4", f"motion-{index}".encode(), "video/mp4")
            state["failed_indices"] = [1]
            save_production(state)
            raise LiveVideoError("Одна сцена завершилась ошибкой; готовые сцены сохранены.")
        if not self.ledger["ready"]:
            progress({"stage": "video", "message": "Живые сцены создаются."})
            save_production(state)
            raise LiveVideoPending(retry_after=30)
        for index in range(4):
            if state["clips"][index] is None:
                state["clips"][index] = save_source(f"clip_{index}.mp4", f"motion-{index}".encode(), "video/mp4")
                save_production(state)
            self.ledger["read"].append(load_source(state["clips"][index]))
        state["phase"] = "complete"
        state["content"]["video_kind"] = "live"
        save_production(state)
        return state["content"], [{"filename": "live_reel.mp4", "data": b"completed-moving-video", "mime_type": "video/mp4"}]


class LiveVideoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "queue.sqlite3", Fernet.generate_key())
        self.now = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        self.service = ContentFactoryService(self.store, lambda: self.now)
        for name in ("content_factory_core.is_agency_access_revoked", "content_factory_worker.is_agency_access_revoked"):
            patcher = patch(name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = self.service.save_project(101, {
            "name": "Агентство W", "product": "Команда ИИ-помощников", "audience": "Предприниматели",
            "platforms": ["vk"], "formats": ["reel"], "publish_times": ["10:00"],
            "automation_enabled": False, "review_required": True, "video_mode": "live", "plan_days": 1,
        })
        self.publisher = Mock()

    def enqueue(self, dedupe="live-request"):
        return self.store.enqueue_job(101, self.project["id"], {
            "platform": "vk", "format": "reel", "video_mode": "live", "state": "queued",
            "requested_by_owner": True, "review_required": True, "dedupe_key": dedupe,
            "topic": "Как освободить вечер от повторяющихся задач", "scheduled_at": iso(self.now),
            "available_at": iso(self.now), "content": {}, "assets": [], "checkpoint": {},
        })

    def worker(self, generator, identifier="first-process"):
        return Worker(self.store, generator, publisher=self.publisher, clock=lambda: self.now, worker_id=identifier)

    def reservations(self):
        with self.store._db() as db:
            return db.execute("SELECT count(*) FROM content_factory_generation_reservations WHERE owner_id=? AND project_id=?",
                              (101, self.project["id"])).fetchone()[0]

    def test_pending_persists_inputs_task_ids_and_releases_lease(self):
        job = self.enqueue()
        generator = FakeLiveGenerator()
        result = self.worker(generator).run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["content"], COPY)
        self.assertEqual(len(saved["production"]["tasks"]), 4)
        self.assertTrue(all(saved["production"]["tasks"]))
        self.assertEqual(saved["progress"]["stage"], "video")
        self.assertIsNone(saved["lease_owner"])
        self.assertIsNone(saved["lease_until"])
        self.assertGreater(saved["available_at"], iso(self.now))
        self.assertEqual(self.reservations(), 1)
        self.assertEqual(generator.ledger["prepared"], 1)
        self.assertEqual(len(generator.ledger["created"]), 4)
        self.assertIn(b"spoken-words", generator.ledger["read"])
        self.publisher.assert_not_called()

    def test_restart_resumes_known_tasks_without_copy_images_or_budget_duplicate(self):
        job = self.enqueue()
        first = FakeLiveGenerator()
        self.worker(first).run_once()
        self.now += timedelta(seconds=31)
        first.ledger["ready"] = True
        restarted = FakeLiveGenerator(first.ledger)
        result = self.worker(restarted, "restarted-process").run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(saved["state"], "ready")
        self.assertTrue(saved["checkpoint"]["generation_complete"])
        self.assertEqual(saved["content"]["video_kind"], "live")
        self.assertEqual(self.store.read_asset(101, saved["assets"][0]), b"completed-moving-video")
        self.assertEqual(first.ledger["prepared"], 1)
        self.assertEqual(len(first.ledger["created"]), 4)
        self.assertEqual(self.reservations(), 1)
        self.assertEqual(saved["attempts"], 2)
        self.publisher.assert_not_called()

    def test_transient_status_read_defers_and_does_not_reorder_paid_scenes(self):
        job = self.enqueue()
        generator = FakeLiveGenerator(failure="read")
        result = self.worker(generator).run_once()
        self.assertEqual(result["pending"], 1)
        saved = self.store.get_job(101, job["id"])
        tasks = copy.deepcopy(saved["production"]["tasks"])
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["error"], "Статус сцены временно недоступен.")
        self.assertIsNone(saved["lease_owner"])
        self.now += timedelta(seconds=61)
        generator.ledger["ready"] = True
        self.worker(FakeLiveGenerator(generator.ledger), "after-status-outage").run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["production"]["tasks"], tasks)
        self.assertEqual(len(generator.ledger["created"]), 4)
        self.assertEqual(self.reservations(), 1)
        self.assertEqual(saved["state"], "ready")
        self.publisher.assert_not_called()

    def test_uncertain_paid_creation_is_saved_and_owner_retry_is_blocked(self):
        job = self.enqueue()
        generator = FakeLiveGenerator(failure="uncertain")
        result = self.worker(generator).run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(saved["state"], "failed")
        self.assertTrue(saved["production"]["creation_uncertain"])
        self.assertEqual(saved["production"]["starting_index"], 0)
        self.assertIsNone(saved["production"]["tasks"][0])
        self.assertIsNone(saved["lease_owner"])
        with self.assertRaises(FactoryError):
            self.service.retry(101, job["id"])
        self.worker(FakeLiveGenerator(generator.ledger), "subsequent-process").run_once()
        self.assertEqual(len(generator.ledger["created"]), 1)
        self.assertEqual(self.reservations(), 1)
        self.publisher.assert_not_called()

    def test_explicit_retry_only_reorders_a_known_failed_scene(self):
        job = self.enqueue()
        generator = FakeLiveGenerator(failure="scene")
        self.worker(generator).run_once()
        failed = self.store.get_job(101, job["id"])
        completed = {index: copy.deepcopy(failed["production"]["clips"][index]) for index in (0, 2, 3)}
        original_ids = copy.deepcopy(failed["production"]["tasks"])
        self.assertEqual(failed["production"]["failed_indices"], [1])
        retried = self.service.retry(101, job["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertIsNone(retried["production"]["tasks"][1])
        for index, reference in completed.items():
            self.assertEqual(retried["production"]["clips"][index], reference)
            self.assertEqual(retried["production"]["tasks"][index], original_ids[index])
        generator.ledger["ready"] = True
        result = self.worker(FakeLiveGenerator(generator.ledger), "explicit-retry-process").run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(len(generator.ledger["created"]), 5)
        self.assertEqual(generator.ledger["prepared"], 1)
        self.assertEqual(self.reservations(), 2)
        for index, reference in completed.items():
            self.assertEqual(saved["production"]["clips"][index], reference)
        self.publisher.assert_not_called()

    def test_cancelling_between_safe_polls_prevents_further_work(self):
        job = self.enqueue()
        generator = FakeLiveGenerator()
        self.worker(generator).run_once()
        self.service.cancel(101, job["id"])
        self.now += timedelta(minutes=2)
        result = self.worker(FakeLiveGenerator(generator.ledger), "after-owner-cancel").run_once()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.store.get_job(101, job["id"])["state"], "cancelled")
        self.assertEqual(len(generator.ledger["created"]), 4)
        self.publisher.assert_not_called()

    def test_source_callback_rejects_foreign_owner_project_and_material(self):
        another_project = self.service.save_project(101, {
            "name": "Другой проект", "product": "Иной продукт", "audience": "Иная аудитория",
        })
        foreign_project = self.service.save_project(202, {
            "name": "Другой владелец", "product": "Иной продукт", "audience": "Иная аудитория",
        })
        sources = []
        for owner, project_id, key in ((101, self.project["id"], "another-material"),
                                       (101, another_project["id"], "another-project"),
                                       (202, foreign_project["id"], "another-owner")):
            source_job = self.store.enqueue_job(owner, project_id, {
                "platform": "vk", "format": "reel", "state": "ready", "dedupe_key": key,
                "scheduled_at": iso(self.now), "available_at": iso(self.now),
            })
            sources.append(self.store.put_asset(owner, project_id, source_job["id"], "private.jpg", b"private-source", "image/jpeg"))
        for index, reference in enumerate(sources):
            job = self.enqueue(f"attacking-reference-{index}")
            generator = FakeLiveGenerator(attack_asset=reference)
            result = self.worker(generator, f"source-check-{index}").run_once(101, self.project["id"])
            saved = self.store.get_job(101, job["id"])
            self.assertEqual(result["failed"], 1)
            self.assertEqual(saved["state"], "failed")
            self.assertIn("не принадлежит этому материалу", saved["error"])
            self.assertEqual(generator.ledger["created"], [])
            self.assertIsNone(saved["lease_owner"])

    def test_owned_source_integrity_is_checked_before_it_reaches_provider(self):
        job = self.enqueue()
        asset = self.store.put_asset(101, self.project["id"], job["id"], "changed.jpg", b"original-source", "image/jpeg")
        path = Path(self.store.resolve_assets(101, [asset])[0]["path"])
        path.write_bytes(b"changed-source")
        generator = FakeLiveGenerator(attack_asset=asset)
        self.worker(generator).run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertIn("медиафайл изменился", saved["error"])
        self.assertEqual(generator.ledger["created"], [])

    def test_oversized_owned_source_is_rejected_without_reading_it_into_memory(self):
        job = self.enqueue()
        asset = self.store.put_asset(101, self.project["id"], job["id"], "too-large.mp4", b"tiny-original", "video/mp4")
        path = Path(self.store.resolve_assets(101, [asset])[0]["path"])
        with path.open("wb") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)  # sparse file; no 100 MiB allocation
        generator = FakeLiveGenerator(attack_asset=asset)
        self.worker(generator).run_once()
        saved = self.store.get_job(101, job["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertIn("медиафайл слишком велик", saved["error"])
        self.assertEqual(generator.ledger["created"], [])


if __name__ == "__main__":
    unittest.main()
