"""A checked reference becomes a new owned film, never copied source footage."""
import copy
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from cryptography.fernet import Fernet
from PIL import Image

from content_factory_core import ContentFactoryService, FactoryError
from content_factory_editorial import (
    EditorialError, build_reference_seed, validate_reference_plan,
    build_image_prompt, build_motion_prompt, build_visual_prompt,
)
from content_factory_generation import Generator, GenerationError
from content_factory_live_video import LiveVideoPending
from content_factory_store import SQLiteStore


PROJECT = {
    "id": str(uuid.uuid4()), "owner_id": 77, "name": "Студия керамики",
    "product": "Мастер-классы по керамике", "audience": "Начинающие взрослые",
    "language": "Русский", "tone": "Тёплый и понятный", "brand_color": "#336699",
    "video_mode": "illustrated", "goal": "Запись на занятие", "visual_style": "Уютная мастерская",
}
PLAN = {
    "title": "Слишком поздно для творчества?", "text": "Начните с одного вечера в мастерской.",
    "narration": (
        "Вы опять откладываете свой первый урок керамики, потому что вечер занят делами? "
        "Попробуйте выбрать одну небольшую паузу для себя. Вместо бесконечного списка задач "
        "поставьте на стол кусок глины. Мастер покажет первое движение, а вы почувствуете "
        "материал руками. За один вечер не нужно становиться художником. Достаточно начать. "
        "Выберите удобное занятие в нашей мастерской."
    ),
    "visual_plan": {"art_direction": "Оригинальная объёмная мультяшная история в одной мастерской.",
                    "frames": [
        {"subject": f"Сцена {i + 1}: персонаж выполняет движение с глиной и меняет форму чашки.",
         "composition": "Персонаж и руки по центру, свободное место внизу.",
         "lighting": "Тёплый дневной свет.", "palette": "Глина и синий акцент."}
        for i in range(4)]},
}


def image_bytes():
    result = io.BytesIO()
    Image.new("RGB", (64, 96), "#aabbcc").save(result, "PNG")
    return result.getvalue()


class ReferenceServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "ref.sqlite3", Fernet.generate_key())
        self.access = patch("content_factory_core.is_agency_access_revoked", return_value=False)
        self.access.start()
        self.addCleanup(self.access.stop)
        self.service = ContentFactoryService(self.store, lambda: datetime(2030, 1, 1, 8, tzinfo=timezone.utc))
        self.project = self.service.save_project(77, {**{key: value for key, value in PROJECT.items() if key not in {"id", "owner_id"}}, "platforms": ["vk", "threads"],
                                                      "formats": ["reel"], "automation_enabled": False})
        self.store.save_connection(77, self.project["id"], "vk", "123", "Мастерская", "synthetic-token")
        self.reference = self.store.enqueue_job(77, self.project["id"], {
            "state": "studio", "platform": "studio", "format": "reference", "dedupe_key": "reference-one",
            "studio_tool": "reference", "studio_complete": True,
            "content": {"reference_plan": copy.deepcopy(PLAN), "reference_style": "cartoon",
                        "source_summary": "Образец строится на простом противоречии и действии."},
            "assets": [],
        })

    def create(self, **changes):
        arguments = {"owner_id": 77, "project_id": self.project["id"], "reference_job_id": self.reference["id"],
                     "platforms": ["vk"], "request_id": "reference-create-one"}
        arguments.update(changes)
        return self.service.create_from_video_reference(**arguments)

    def test_accepted_reference_freezes_original_story_and_queues_reviewed_motion(self):
        job = self.create()[0]
        self.assertEqual(job["format"], "reel")
        self.assertEqual(job["video_mode"], "live")
        self.assertEqual(job["video_style"], "cartoon")
        self.assertTrue(job["review_required"])
        self.assertTrue(job["requested_by_owner"])
        self.assertEqual(job["state"], "queued")
        self.assertFalse(job["checkpoint"].get("owner_approved"))
        self.assertEqual(job["approved_reference"]["plan"]["narration"], PLAN["narration"])
        self.assertFalse(self.store.get_project(77, self.project["id"])["automation_enabled"])
        self.service.save_project(77, {"id": self.project["id"], "product": "Совсем другая услуга"})
        frozen = self.store.get_job(77, job["id"])["approved_reference"]["project"]
        self.assertEqual(frozen["product"], PROJECT["product"])
        self.assertEqual(frozen["video_style"], "cartoon")

    def test_saved_film_finishing_is_copied_into_the_new_production_job(self):
        intro = {"storage_path": f"77/{self.project['id']}/{self.reference['id']}/film-intro.png",
                 "mime_type": "image/png", "sha256": "a" * 64, "size": 10}
        music = {"storage_path": f"77/{self.project['id']}/{self.reference['id']}/film-music.mp3",
                 "mime_type": "audio/mpeg", "sha256": "b" * 64, "size": 10}
        self.store.update_job(77, self.reference["id"], {
            "checkpoint": {"video_personalization": {
                "intro_asset": intro, "music_asset": music,
                "voice_volume": 1.0, "music_volume": 0.10,
            }}
        })
        job = self.service.create_from_video_reference(
            77, self.project["id"], self.reference["id"], platforms=["vk"],
            request_id="reference-create-with-finishing",
        )[0]
        saved = job["checkpoint"]["video_personalization"]
        self.assertEqual(saved["intro_asset"]["storage_path"], intro["storage_path"])
        self.assertEqual(saved["music_asset"]["storage_path"], music["storage_path"])
        self.assertEqual(saved["music_volume"], 0.10)

    def test_duplicate_submission_reuses_job_and_changed_source_requires_new_request(self):
        first = self.create()[0]
        self.assertEqual(self.create()[0]["id"], first["id"])
        changed = {"reference_plan": {**PLAN, "title": "Другая собственная история"}, "reference_style": "cartoon"}
        self.store.update_job(77, self.reference["id"], {"content": changed})
        with self.assertRaises(FactoryError):
            self.create()

    def test_wrong_owner_project_incomplete_and_wrong_kind_never_enqueue(self):
        for change in ({"owner_id": 88}, {"project_id": str(uuid.uuid4())}):
            with self.subTest(change=change), self.assertRaises((FactoryError, ValueError)):
                self.create(**change)
        for change in ({"studio_complete": False}, {"studio_tool": "image"}, {"state": "cancelled"},
                       {"content": {"reference_plan": {}, "reference_style": "cartoon"}}):
            original = self.store.get_job(77, self.reference["id"])
            self.store.update_job(77, self.reference["id"], change)
            with self.subTest(change=change), self.assertRaises(FactoryError):
                self.create()
            self.store.update_job(77, self.reference["id"], {key: original.get(key) for key in change})
        self.assertEqual(len(self.store.list_jobs(77, self.project["id"])), 1)

    def test_all_platform_limits_and_connections_checked_before_enqueue(self):
        with self.assertRaises(FactoryError):
            self.create(platforms=["vk", "threads"])
        self.store.save_connection(77, self.project["id"], "threads", "456", "Треды", "synthetic-token")
        bad = {**PLAN, "text": "x" * 501}
        self.store.update_job(77, self.reference["id"], {"content": {"reference_plan": bad, "reference_style": "cartoon"}})
        with self.assertRaises(FactoryError):
            self.create(platforms=["vk", "threads"])
        self.assertEqual(len(self.store.list_jobs(77, self.project["id"])), 1)

    def test_master_video_is_one_job_and_needs_no_social_connection(self):
        self.store.delete_connection(77, self.project["id"], "vk")
        self.service.save_project(77, {
            "id": self.project["id"],
            "platforms": ["threads"],
            "formats": ["post"],
        })
        job = self.service.create_master_from_video_reference(
            77,
            self.project["id"],
            self.reference["id"],
            request_id="reference-master-one",
        )
        self.assertEqual(job["platform"], "instagram")
        self.assertEqual(job["format"], "reel")
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["studio_master"])
        self.assertTrue(job["requested_by_owner"])
        self.assertTrue(job["review_required"])
        self.assertEqual(job["video_style"], "cartoon")
        self.assertEqual(job["approved_reference"]["plan"]["narration"], PLAN["narration"])
        self.assertEqual(
            self.service.create_master_from_video_reference(
                77,
                self.project["id"],
                self.reference["id"],
                request_id="reference-master-one",
            )["id"],
            job["id"],
        )
        self.assertEqual(
            len([
                item for item in self.store.list_jobs(77, self.project["id"])
                if item.get("studio_master") is True
            ]),
            1,
        )

    def test_project_social_setting_change_does_not_pause_master_video(self):
        job = self.service.create_master_from_video_reference(
            77,
            self.project["id"],
            self.reference["id"],
            request_id="reference-master-survives-settings",
        )
        self.assertEqual(job["state"], "queued")
        self.service.save_project(77, {
            "id": self.project["id"],
            "platforms": ["threads"],
            "formats": ["post"],
            "automation_enabled": False,
        })
        saved = self.store.get_job(77, job["id"])
        self.assertEqual(saved["state"], "queued")
        self.assertTrue(saved["studio_master"])

    def test_normal_video_style_is_explicit_idempotent_and_revision_keeps_animation(self):
        job = self.service.create_material(77, self.project["id"], "Сделай мультфильм о первом уроке керамики", "reel", ["vk"],
                                           request_id="cartoon-new-request", video_style="cartoon")[0]
        self.assertEqual((job["video_mode"], job["video_style"]), ("live", "cartoon"))
        with self.assertRaises(FactoryError):
            self.service.create_material(77, self.project["id"], job["topic"], "reel", ["vk"],
                                         request_id="cartoon-new-request", video_style="live")
        self.store.update_job(77, job["id"], {"state": "ready", "content": {"title": "Мультфильм", "text": "Текст"}})
        revision = self.service.revise_material(77, job["id"], "Сделай героя добрее", request_id="cartoon-revision-one")[0]
        self.assertEqual((revision["video_mode"], revision["video_style"]), ("live", "cartoon"))
        self.assertNotIn("approved_reference", revision)


class ReferenceGenerationTests(unittest.TestCase):
    def job(self, style="cartoon"):
        seed = build_reference_seed(77, PROJECT["id"], str(uuid.uuid4()), style, PLAN, PROJECT)
        return {"id": str(uuid.uuid4()), "owner_id": 77, "project_id": PROJECT["id"], "platform": "vk",
                "format": "reel", "video_mode": "live", "video_style": style, "approved_reference": seed,
                "topic": "Создай собственную историю по принятому плану"}

    def generator(self):
        client = Mock(model="gen4.5")
        client.preflight.return_value = {"credit_balance": 500}
        generator = Generator("synthetic-key", live_video_client_resolver=Mock(return_value=client))
        generator._copy = Mock(side_effect=AssertionError("Accepted script must seed generation"))
        generator._proofread = Mock(side_effect=lambda project, job, content: dict(content))
        generator._review_copy = Mock(return_value={"approved": True, "issues": [], "brief_feedback": "Хорошо"})
        generator._structured = Mock(side_effect=AssertionError("Accepted scene plan must not be replaced"))
        generator._illustration = Mock(return_value=image_bytes())
        generator._review_artwork = Mock(return_value={"approved": True, "issues": [], "brief_feedback": "Готово"})
        generator._request = Mock(return_value=Mock(content=b"owned-narration"))
        return generator, client

    def generate(self, generator, job, duration=35):
        self.states = []
        self.saved = []
        def save(filename, data, mime):
            self.saved.append((filename, data, mime))
            return {"storage_path": f"77/{job['project_id']}/{job['id']}/{filename}",
                    "sha256": sha256(data).hexdigest(), "mime_type": mime, "size": len(data)}
        with patch("content_factory_live_media.speech_duration", return_value=duration), \
             patch.object(generator, "_continue_live_video", side_effect=LiveVideoPending()):
            generator.generate({**PROJECT, "product": "Changed after queue"}, job,
                               save_production=lambda value: self.states.append(copy.deepcopy(value)),
                               save_source=save, load_source=Mock())

    def test_render_uses_frozen_script_storyboard_and_new_artwork_without_source_pixels(self):
        generator, client = self.generator()
        job = self.job()
        with self.assertRaises(LiveVideoPending):
            self.generate(generator, job)
        generator._copy.assert_not_called()
        generator._structured.assert_not_called()
        self.assertEqual(generator._illustration.call_count, 4)
        self.assertEqual(generator._proofread.call_args.args[2]["narration"], PLAN["narration"])
        self.assertEqual(generator._proofread.call_args.args[0]["product"], PROJECT["product"])
        self.assertEqual(self.states[-1]["content"]["visual_plan"], PLAN["visual_plan"])
        self.assertEqual(self.states[-1]["content"]["video_style"], "cartoon")
        self.assertEqual(self.states[-1]["project"]["video_style"], "cartoon")
        self.assertTrue(all("START FRAME for an original animated film" in call.args[0]
                            for call in generator._illustration.call_args_list))
        self.assertEqual(generator._request.call_count, 1)
        self.assertEqual(generator._request.call_args.args[1]["input"], PLAN["narration"])
        self.assertEqual(len(self.saved), 5)
        self.assertFalse(any("competitor" in name for name, _, _ in self.saved))
        client.create_scene.assert_not_called()  # mocked continuation boundary owns paid orders

    def test_tampered_plan_or_owner_binding_stops_before_paid_requests(self):
        for change in ("plan", "owner", "project", "style"):
            generator, client = self.generator()
            job = self.job()
            if change == "plan":
                job["approved_reference"]["plan"]["narration"] += " Подмена."
            elif change == "owner":
                job["owner_id"] = 88
            elif change == "project":
                job["project_id"] = str(uuid.uuid4())
            else:
                job["video_style"] = "live"
            with self.subTest(change=change), self.assertRaises(GenerationError):
                self.generate(generator, job)
            generator._request.assert_not_called()
            generator._illustration.assert_not_called()
            client.preflight.assert_not_called()

    def test_voice_too_long_for_accepted_story_stops_before_paid_images(self):
        generator, _ = self.generator()
        with self.assertRaisesRegex(GenerationError, "больше живых сцен"):
            self.generate(generator, self.job(), duration=45)
        generator._illustration.assert_not_called()
        generator._structured.assert_not_called()
        generator._request.assert_called_once()

    def test_whole_video_credit_estimate_stops_before_images_when_balance_is_short(self):
        from content_factory_live_video import RunwayVideoClient
        generator, client = self.generator()
        estimator = RunwayVideoClient("synthetic-estimator-key", session=Mock())
        client.estimate_credits = estimator.estimate_credits
        client.preflight.return_value = {"credit_balance": 320}
        with self.assertRaisesRegex(GenerationError, "примерно 432 кредитов, доступно 320") as caught:
            self.generate(generator, self.job(), duration=35)
        generator._illustration.assert_not_called()
        client.create_scene.assert_not_called()
        self.assertEqual(caught.exception.draft["video_credit_estimate"]["credits"], 432)
        self.assertTrue(caught.exception.draft["video_credit_estimate"]["is_estimate"])
        self.assertEqual(self.saved, [])

    def test_whole_video_estimate_allows_known_adequate_or_unknown_balance(self):
        from content_factory_live_video import RunwayVideoClient
        for balance in (432, 500, None):
            generator, client = self.generator()
            estimator = RunwayVideoClient("synthetic-estimator-key", session=Mock())
            client.estimate_credits = estimator.estimate_credits
            client.preflight.return_value = {"credit_balance": balance}
            with self.subTest(balance=balance), self.assertRaises(LiveVideoPending):
                self.generate(generator, self.job(), duration=35)
            self.assertEqual(generator._illustration.call_count, 4)
            self.assertEqual(self.states[-1]["content"]["video_credit_estimate"]["credits"], 432)

    def test_style_prompts_preserve_actual_animation_or_live_action(self):
        job = self.job()
        cartoon = {**PROJECT, "video_mode": "live", "video_style": "cartoon"}
        visual, _ = build_visual_prompt(cartoon, job, PLAN, frame_count=4)
        image = build_image_prompt(cartoon, PLAN["visual_plan"]["frames"][0], PLAN["visual_plan"]["art_direction"], total=4)
        motion = build_motion_prompt(cartoon, PLAN["visual_plan"]["frames"][0], PLAN["visual_plan"]["art_direction"], total=4)
        self.assertIn("анимационного фильма", visual)
        self.assertNotIn("Выбери фотореалистичную живую съёмку", visual)
        self.assertIn("original animated film", image)
        self.assertNotIn("believable photographic people", image)
        self.assertIn("Animated film shot", motion)
        self.assertLessEqual(len(motion.encode("utf-16-le")) // 2, 1000)
        live = {**cartoon, "video_style": "live"}
        self.assertIn("Live-action film", build_motion_prompt(live, PLAN["visual_plan"]["frames"][0], "Warm light", total=4))

    def test_plan_rejects_missing_story_short_voice_and_corrupted_fields(self):
        self.assertEqual(validate_reference_plan(PLAN)["format"], "reel")
        for bad in ({}, {**PLAN, "narration": "Короткий текст"}, {**PLAN, "text": {}},
                    {**PLAN, "visual_plan": {"art_direction": "Стиль", "frames": []}}):
            with self.subTest(bad=bad), self.assertRaises(EditorialError):
                validate_reference_plan(bad)


if __name__ == "__main__":
    unittest.main()
