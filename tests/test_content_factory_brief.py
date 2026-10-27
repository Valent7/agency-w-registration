"""Owner briefs stay durable, idempotent, and separate from publication consent."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from content_factory_core import ContentFactoryService, FactoryError, validate_project
from content_factory_store import SQLiteStore


class FactoryBriefTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.access = patch("content_factory_core.is_agency_access_revoked", return_value=False)
        self.access.start()
        self.addCleanup(self.access.stop)
        self.store = SQLiteStore(Path(self.directory.name) / "brief.sqlite3", Fernet.generate_key())
        self.now = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        self.service = ContentFactoryService(self.store, lambda: self.now)
        self.project = self.service.save_project(77, {
            "name": "Агентство W", "product": "Команда ИИ-помощников", "audience": "Предприниматели",
            "platforms": ["vk", "threads"], "formats": ["post", "carousel"],
            "automation_enabled": False,
        })
        self.store.save_connection(77, self.project["id"], "vk", "123", "Моё сообщество", "synthetic-test-token")

    def create(self, **changes):
        values = {"owner_id": 77, "project_id": self.project["id"],
                  "topic": "Как освободить вечер от повторяющихся задач", "content_format": "post",
                  "platforms": ["vk"], "request_id": "request-test-001"}
        values.update(changes)
        return self.service.create_material(**values)

    def test_old_projects_get_empty_memory_and_new_memory_is_bounded(self):
        values = validate_project({"name": "W", "product": "Команда", "audience": "Эксперты"})
        for field in ("facts", "examples", "visual_style", "avoid"):
            self.assertEqual(values[field], "")
        saved = self.service.save_project(77, {"id": self.project["id"], "facts": " Реальная возможность ",
                                               "examples": "Пример текста", "visual_style": "Тёплый свет", "avoid": "Гарантии дохода"})
        self.assertEqual(self.store.get_project(77, saved["id"])["facts"], "Реальная возможность")
        for field, maximum in (("facts", 6000), ("examples", 6000), ("visual_style", 2000), ("avoid", 2000)):
            for value in ("x" * (maximum + 1), {"text": "not a string"}, "text\x00"):
                with self.subTest(field=field, value_type=type(value).__name__), self.assertRaises(FactoryError):
                    self.service.save_project(77, {"id": self.project["id"], field: value})

    def test_explicit_brief_is_queued_while_publication_remains_off(self):
        job = self.create()[0]
        self.assertEqual(job["owner_id"], 77)
        self.assertEqual(job["platform"], "vk")
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["requested_by_owner"])
        self.assertEqual(job["content"], {})
        self.assertFalse(job["checkpoint"].get("owner_approved", False))
        self.assertFalse(self.store.get_project(77, self.project["id"])["automation_enabled"])

    def test_retry_same_request_and_duplicate_platforms_do_not_duplicate_jobs(self):
        first = self.create(platforms=["vk", "vk"])
        second = self.create(platforms=["vk"])
        self.assertEqual([job["id"] for job in first], [job["id"] for job in second])
        self.assertEqual(len(self.store.list_jobs(77, self.project["id"])), 1)
        other = self.create(request_id="request-test-002")
        self.assertNotEqual(other[0]["id"], first[0]["id"])

    def test_reused_request_cannot_change_brief(self):
        self.create()
        with self.assertRaises(FactoryError):
            self.create(topic="Другая подробная задача для той же аудитории")
        self.assertEqual(len(self.store.list_jobs(77, self.project["id"])), 1)

    def test_all_platforms_validate_before_any_enqueue(self):
        for changes in ({"owner_id": 88}, {"topic": "  "}, {"topic": "x" * 6001},
                        {"content_format": "reel"}, {"platforms": ["vk", "threads"]},
                        {"platforms": ["instagram"]}, {"platforms": []},
                        {"request_id": "invalid request!"}):
            with self.subTest(changes=changes), self.assertRaises(FactoryError):
                self.create(**changes)
        self.assertEqual(self.store.list_jobs(77, self.project["id"]), [])

    def test_setting_save_preserves_requested_generation_but_pauses_calendar(self):
        requested = self.create()[0]
        calendar = self.store.enqueue_job(77, self.project["id"], {
            "dedupe_key": "calendar", "platform": "vk", "format": "post", "state": "queued",
            "scheduled_at": self.now, "available_at": self.now,
        })
        self.service.save_project(77, {"id": self.project["id"], "facts": "Подтверждённая информация"})
        self.assertEqual(self.store.get_job(77, requested["id"])["state"], "queued")
        self.assertEqual(self.store.get_job(77, calendar["id"])["state"], "paused")
        self.service.save_project(77, {"id": self.project["id"], "formats": ["carousel"]})
        self.assertEqual(self.store.get_job(77, requested["id"])["state"], "paused")

    def test_revision_creates_reviewed_new_version_and_preserves_original(self):
        first = self.create()[0]
        self.store.update_job(77, first["id"], {"state": "ready", "content": {
            "title": "Вечер", "format": "post", "text": "Исходный текст", "image_prompt": "Home evening",
        }})
        original = copy.deepcopy(self.store.get_job(77, first["id"]))
        revised = self.service.revise_material(77, first["id"], "Убери общие слова и добавь конкретный пример", request_id="revision-test-001")[0]
        again = self.service.revise_material(77, first["id"], "Убери общие слова и добавь конкретный пример", request_id="revision-test-001")[0]
        self.assertEqual(again["id"], revised["id"])
        self.assertNotEqual(revised["id"], first["id"])
        self.assertEqual(revised["source_job_id"], first["id"])
        self.assertEqual(revised["previous_content"], original["content"])
        self.assertTrue(revised["review_required"])
        self.assertEqual(revised["state"], "queued")
        revised["previous_content"]["text"] = "Local mutation"
        self.assertEqual(self.store.get_job(77, first["id"]), original)

    def test_failed_editorial_content_can_be_revised_but_sent_content_cannot(self):
        first = self.create()[0]
        self.store.update_job(77, first["id"], {"state": "failed", "content": {
            "title": "Вечер", "text": "Требует исправления", "quality_review": {"approved": False, "issues": ["Нет примера"]},
        }})
        self.assertTrue(self.service.revise_material(77, first["id"], "Добавь реальный пример", request_id="revision-test-002"))
        with self.assertRaises(FactoryError):
            self.service.revise_material(88, first["id"], "Измени этот материал", request_id="revision-test-003")
        self.store.update_job(77, first["id"], {"checkpoint": {"publish_started": True}})
        with self.assertRaises(FactoryError):
            self.service.revise_material(77, first["id"], "Измени этот материал", request_id="revision-test-004")

    def test_retry_incomplete_or_rejected_copy_cannot_skip_generation(self):
        for index, checkpoint, review in (
            (1, {"pipeline_version": 2, "generation_started": self.now.isoformat()}, {"approved": True, "issues": []}),
            (2, {}, {"approved": False, "issues": ["Неподтверждённое обещание"]}),
        ):
            job = self.create(request_id=f"request-partial-{index}")[0]
            self.store.update_job(77, job["id"], {"state": "failed", "checkpoint": checkpoint,
                "content": {"title": "Черновик", "text": "Незавершённый текст", "quality_review": review}})
            result = self.service.retry(77, job["id"])
            self.assertEqual(result["state"], "queued")
            self.assertNotIn("generation_started", result["checkpoint"])
            self.assertFalse(result["checkpoint"].get("owner_approved", False))
            self.store.update_job(77, job["id"], {"state": "ready"})
            with self.assertRaises(FactoryError):
                self.service.approve(77, job["id"])

    def test_completed_and_legacy_media_survive_connection_retry(self):
        for index, checkpoint in enumerate((
            {"pipeline_version": 2, "generation_complete": True, "generation_started": self.now.isoformat()},
            {"generation_started": self.now.isoformat()},
        )):
            job = self.create(request_id=f"request-completed-{index}")[0]
            content = {"title": "Готовый материал", "text": "Проверенный текст", "quality_review": {"approved": True, "issues": []}}
            assets = [{"storage_path": "77/project/job/cover.jpg", "mime_type": "image/jpeg"}]
            self.store.update_job(77, job["id"], {"state": "needs_connection", "checkpoint": checkpoint,
                                                  "content": content, "assets": assets})
            result = self.service.retry(77, job["id"])
            self.assertEqual(result["state"], "paused")
            self.assertEqual(result["content"], content)
            self.assertEqual(result["assets"], assets)
            if checkpoint.get("pipeline_version") == 2:
                self.assertTrue(result["checkpoint"]["generation_complete"])

    def test_revision_review_remains_required_when_project_review_is_off(self):
        original = self.create()[0]
        self.store.update_job(77, original["id"], {"state": "ready", "content": {"title": "Тема", "text": "Текст"}})
        revised = self.service.revise_material(77, original["id"], "Сделай текст конкретнее", request_id="revision-review-001")[0]
        self.service.save_project(77, {"id": self.project["id"], "automation_enabled": True, "review_required": False})
        self.store.update_job(77, revised["id"], {"state": "needs_connection", "content": {
            "title": "Тема", "text": "Новая версия", "quality_review": {"approved": True, "issues": []}},
            "checkpoint": {"pipeline_version": 2, "generation_complete": True}})
        self.assertEqual(self.service.retry(77, revised["id"])["state"], "ready")
        self.store.update_job(77, revised["id"], {"state": "paused"})
        self.service.save_project(77, {"id": self.project["id"], "automation_enabled": True})
        self.assertEqual(self.store.get_job(77, revised["id"])["state"], "ready")

    def test_live_video_mode_is_explicit_and_queued_intent_does_not_change(self):
        self.assertEqual(validate_project({"name": "W", "product": "Команда", "audience": "Эксперты"})["video_mode"], "illustrated")
        for invalid in ("slides", None, 1):
            with self.subTest(invalid=invalid), self.assertRaises(FactoryError):
                self.service.save_project(77, {"id": self.project["id"], "video_mode": invalid})
        self.service.save_project(77, {"id": self.project["id"], "formats": ["post", "reel"], "video_mode": "live"})
        live = self.create(content_format="reel", request_id="live-video-request-001")[0]
        self.assertEqual(live["video_mode"], "live")
        self.service.save_project(77, {"id": self.project["id"], "video_mode": "illustrated"})
        self.assertEqual(self.store.get_job(77, live["id"])["video_mode"], "live")
        with self.assertRaises(FactoryError):
            self.create(content_format="reel", request_id="live-video-request-001")
        illustrated = self.create(content_format="reel", request_id="live-video-request-002")[0]
        self.assertEqual(illustrated["video_mode"], "illustrated")

    def test_uncertain_paid_video_creation_cannot_be_retried_or_revised(self):
        for number, production in enumerate((
            {"version": 1, "starting_index": 0, "tasks": [None], "clips": [None]},
            {"version": 1, "creation_uncertain": True, "tasks": ["known-task"], "clips": [None]},
        )):
            job = self.create(request_id=f"uncertain-video-{number}")[0]
            self.store.update_job(77, job["id"], {"state": "failed", "content": {"text": "Сохранённый текст", "title": "Видео"}, "production": production})
            with self.assertRaises(FactoryError):
                self.service.retry(77, job["id"])
            with self.assertRaises(FactoryError):
                self.service.revise_material(77, job["id"], "Переделай движение героя", request_id=f"uncertain-revision-{number}")
            self.assertEqual(self.store.get_job(77, job["id"])["production"], production)

    def test_explicit_video_retry_only_restarts_known_failed_scenes(self):
        job = self.create(request_id="failed-scene-request")[0]
        production = {"version": 1, "phase": "clips", "raw_sources": [{"source": "preserved"}],
                      "speech_source": {"speech": "preserved"}, "starting_index": None,
                      "tasks": ["completed-task", "pending-task", "failed-task"],
                      "clips": [{"clip": "already-paid"}, None, None], "failed_indices": [2]}
        self.store.update_job(77, job["id"], {"state": "failed", "production": production,
            "checkpoint": {"pipeline_version": 2, "generation_started": self.now.isoformat()},
            "content": {"text": "Проверенный текст", "title": "Видео"}})
        retried = self.service.retry(77, job["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertNotIn("generation_started", retried["checkpoint"])
        saved = retried["production"]
        self.assertEqual(saved["tasks"], ["completed-task", "pending-task", None])
        self.assertEqual(saved["clips"], [{"clip": "already-paid"}, None, None])
        self.assertEqual(saved["raw_sources"], production["raw_sources"])
        self.assertEqual(saved["speech_source"], production["speech_source"])
        self.assertEqual(saved["failed_indices"], [])
        self.assertEqual(saved["phase"], "inputs_ready")

    def test_resuming_calendar_does_not_send_incomplete_live_video_to_publishing(self):
        job = self.create(request_id="paused-video-request")[0]
        production = {"version": 1, "phase": "inputs_ready", "tasks": ["paid-task"], "clips": [None]}
        self.store.update_job(77, job["id"], {"state": "paused", "production": production,
            "checkpoint": {"pipeline_version": 2, "generation_started": self.now.isoformat()},
            "content": {"text": "Проверенный текст", "title": "Видео"}})
        self.service.save_project(77, {"id": self.project["id"], "automation_enabled": True})
        saved = self.store.get_job(77, job["id"])
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["production"], production)


if __name__ == "__main__":
    unittest.main()
