"""Regression tests for the Brag-inspired Website -> Video tool."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from content_factory_core import ContentFactoryService, FactoryError
from content_factory_store import SQLiteStore
from content_studio_launch_video_ui import (
    LaunchVideoError,
    _normalize_plan,
    _safe_website,
)


BRIEF = {
    "title": "Агентство W",
    "website": "https://example.com",
    "tone": "polished",
    "hook": "Верните себе время",
    "duration_seconds": 20,
    "music": "Спокойный современный ритм",
    "cta": "Посмотреть, что можно передать ИИ",
    "share_copy": "Рутина не обязана забирать Ваш день.",
    "scenes": [
        {"seconds": 3, "visual": "Часы и интерфейс проекта", "text": "Время уходит", "motion": "мягкий наезд"},
        {"seconds": 5, "visual": "Экран с задачами", "text": "Рутину берёт ИИ", "motion": "задачи исчезают"},
        {"seconds": 6, "visual": "Человек выходит на прогулку", "text": "Время возвращается", "motion": "живое движение"},
        {"seconds": 6, "visual": "Логотип и CTA", "text": "Время жить", "motion": "спокойный финал"},
    ],
}


class LaunchVideoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        access = patch("content_factory_core.is_agency_access_revoked", return_value=False)
        access.start()
        self.addCleanup(access.stop)
        self.store = SQLiteStore(
            Path(self.directory.name) / "launch.sqlite3",
            Fernet.generate_key(),
        )
        self.now = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        self.service = ContentFactoryService(self.store, lambda: self.now)
        self.project = self.service.save_project(77, {
            "name": "Агентство W",
            "product": "ИИ-сотрудники, которые возвращают человеку время",
            "audience": "Люди, готовые передать рутину ИИ",
            "website": "https://example.com",
            "platforms": ["vk"],
            "formats": ["post"],
            "automation_enabled": False,
        })

    def test_public_website_normalization(self):
        self.assertEqual(_safe_website("example.com"), "https://example.com")
        self.assertEqual(_safe_website("https://example.com/a"), "https://example.com/a")
        for value in ("", "http://localhost:8501", "http://127.0.0.1/test", "file:///tmp/x"):
            with self.subTest(value=value), self.assertRaises(LaunchVideoError):
                _safe_website(value)

    def test_plan_rebalances_bad_timing_without_losing_story(self):
        data = {
            "title": "W",
            "hook": "Верните себе вечер",
            "share_copy": "Короткий пост",
            "scenes": [
                {"seconds": 1, "visual": "A"},
                {"seconds": 1, "visual": "B"},
                {"seconds": 1, "visual": "C"},
                {"seconds": 1, "visual": "D"},
            ],
        }
        plan = _normalize_plan(
            data,
            "https://example.com",
            "polished",
            self.project,
        )
        self.assertEqual(len(plan["scenes"]), 4)
        self.assertTrue(15 <= plan["duration_seconds"] <= 25)
        self.assertTrue(all(2 <= item["seconds"] <= 8 for item in plan["scenes"]))

    def test_launch_master_does_not_require_social_connection(self):
        job = self.service.create_master_from_launch_brief(
            77,
            self.project["id"],
            BRIEF,
            request_id="launch-test-001",
        )
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["format"], "reel")
        self.assertEqual(job["platform"], "instagram")
        self.assertTrue(job["studio_master"])
        self.assertTrue(job["requested_by_owner"])
        self.assertTrue(job["review_required"])
        self.assertEqual(job["video_mode"], "live")
        self.assertGreaterEqual(len(job["production_plan"]), 4)
        self.assertEqual(job["approved_intent"]["main_idea"], BRIEF["hook"])

    def test_same_request_is_idempotent_and_changed_brief_is_rejected(self):
        first = self.service.create_master_from_launch_brief(
            77, self.project["id"], BRIEF, request_id="launch-test-002"
        )
        second = self.service.create_master_from_launch_brief(
            77, self.project["id"], BRIEF, request_id="launch-test-002"
        )
        self.assertEqual(first["id"], second["id"])
        changed = dict(BRIEF)
        changed["hook"] = "Другая идея"
        with self.assertRaises(FactoryError):
            self.service.create_master_from_launch_brief(
                77, self.project["id"], changed, request_id="launch-test-002"
            )


if __name__ == "__main__":
    unittest.main()
