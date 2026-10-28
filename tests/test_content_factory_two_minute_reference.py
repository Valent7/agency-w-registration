"""Opted-in two-minute films retain their script through production and resume."""
import copy
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch
import uuid

from content_factory_editorial import (
    EditorialError, build_copy_prompt, build_proofread_prompt, build_reference_seed,
    validate_reference_plan, validate_reference_seed,
)
from content_factory_generation import Generator, GenerationError, can_resume_video, validate_content
from content_factory_live_video import LiveVideoPending, RunwayVideoClient
from test_content_factory_reference_pipeline import PROJECT, PLAN, image_bytes


def long_plan():
    plan = copy.deepcopy(PLAN)
    plan["target_seconds"] = 120
    plan["narration"] = " ".join(["Одна конкретная история развивается последовательно."] * 40)
    plan["visual_plan"]["frames"] = [
        {**PLAN["visual_plan"]["frames"][0], "subject": f"Сцена {index + 1}: герой делает следующий шаг и изменяет свою задачу."}
        for index in range(16)
    ]
    return plan


def long_job():
    seed = build_reference_seed(77, PROJECT["id"], str(uuid.uuid4()), "cartoon", long_plan(), PROJECT)
    return {"id": str(uuid.uuid4()), "owner_id": 77, "project_id": PROJECT["id"], "platform": "vk",
            "format": "reel", "video_mode": "live", "video_style": "cartoon", "approved_reference": seed,
            "topic": "Создать собственный двухминутный фильм по принятому сценарию"}


class TwoMinuteReferenceTests(unittest.TestCase):
    def test_long_plan_is_explicit_and_old_short_plan_contract_remains(self):
        plan = validate_reference_plan(long_plan())
        self.assertEqual(plan["target_seconds"], 120)
        self.assertEqual(len(plan["narration"].split()), 200)
        self.assertEqual(len(plan["visual_plan"]["frames"]), 16)
        self.assertNotIn("target_seconds", validate_reference_plan(PLAN))
        for changes in ({"target_seconds": True}, {"target_seconds": 45}, {"target_seconds": 119},
                        {"narration": PLAN["narration"]},
                        {"visual_plan": PLAN["visual_plan"]}):
            with self.subTest(changes=changes), self.assertRaises(EditorialError):
                validate_reference_plan({**long_plan(), **changes})
        no_marker = long_plan()
        no_marker.pop("target_seconds")
        with self.assertRaises(EditorialError):
            validate_reference_plan(no_marker)

    def test_seed_hash_binds_the_accepted_output_duration(self):
        job = long_job()
        self.assertEqual(validate_reference_seed(job["approved_reference"])["plan"]["target_seconds"], 120)
        job["approved_reference"]["plan"].pop("target_seconds")
        with self.assertRaises(EditorialError):
            validate_reference_seed(job["approved_reference"])

    def test_copy_and_proofread_keep_long_instruction_instead_of_short_limit(self):
        job, content = long_job(), validate_content(long_plan(), "reel", "vk")
        copy_prompt, copy_input = build_copy_prompt(PROJECT, job)
        proof_prompt, proof_input = build_proofread_prompt(PROJECT, job, content)
        for prompt in (copy_prompt, proof_prompt):
            self.assertIn("180–250", prompt)
            self.assertIn("4000", prompt)
            self.assertNotIn("45–85", prompt)
            self.assertIn('"target_seconds":120', prompt)
        self.assertEqual(json.loads(copy_input)["approved_storyboard"]["plan"]["target_seconds"], 120)
        self.assertEqual(json.loads(proof_input)["content"]["narration"], content["narration"])
        revision = {"platform": "vk", "format": "reel", "topic": job["topic"],
                    "revision_feedback": "Усиль начало", "previous_content": content}
        self.assertIn("180–250", build_copy_prompt(PROJECT, revision)[0])

    def test_real_proofreader_validation_binds_omitted_marker_and_rejects_shortening(self):
        job, content = long_job(), validate_content(long_plan(), "reel", "vk")
        generator = Generator("synthetic-provider-key")
        output = {key: value for key, value in content.items() if key != "target_seconds"}
        generator._structured = Mock(return_value=output)
        corrected = generator._proofread(PROJECT, job, content)
        self.assertEqual(corrected["target_seconds"], 120)
        self.assertEqual(corrected["narration"], content["narration"])
        generator._structured.return_value = {**output, "narration": PLAN["narration"]}
        with self.assertRaises(GenerationError):
            generator._proofread(PROJECT, job, content)
        generator._structured.return_value = {**output, "target_seconds": 45}
        with self.assertRaises(GenerationError):
            generator._proofread(PROJECT, job, content)

    def test_normal_copy_cannot_silently_choose_two_minutes(self):
        generator = Generator("synthetic-provider-key")
        generator._structured = Mock(return_value=long_plan())
        with self.assertRaises(GenerationError):
            generator._copy(PROJECT, {"format": "reel", "platform": "vk", "topic": "Короткий ролик"})
        generator._structured.return_value = {key: value for key, value in long_plan().items()
                                             if key not in {"target_seconds", "visual_plan"}}
        copied = generator._copy(PROJECT, long_job())
        self.assertEqual(copied["target_seconds"], 120)

    def generator(self, balance=2000):
        client = Mock(model="gen4.5")
        client.preflight.return_value = {"credit_balance": balance}
        client.estimate_credits = RunwayVideoClient("local-estimator-key").estimate_credits
        generator = Generator("synthetic-provider-key", live_video_client_resolver=Mock(return_value=client))
        generator._copy = Mock(side_effect=AssertionError("Must use the accepted original script"))
        generator._proofread = Mock(side_effect=lambda project, job, content: dict(content))
        generator._review_copy = Mock(return_value={"approved": True, "issues": [], "brief_feedback": "Готово"})
        generator._structured = Mock(side_effect=AssertionError("Must preserve accepted scenes"))
        generator._illustration = Mock(return_value=image_bytes())
        generator._review_artwork = Mock(return_value={"approved": True, "issues": [], "brief_feedback": "Готово"})
        generator._request = Mock(return_value=Mock(content=b"owned-long-narration"))
        return generator, client

    def create_inputs(self, generator, job, duration=110):
        self.states, self.sources = [], {}
        def save(filename, data, mime):
            reference = {"storage_path": f"77/{job['project_id']}/{job['id']}/{filename}",
                         "sha256": sha256(data).hexdigest(), "mime_type": mime, "size": len(data)}
            self.sources[filename] = data
            return reference
        with patch("content_factory_live_media.speech_duration", return_value=duration) as voice_duration, \
             patch.object(generator, "_continue_live_video", side_effect=LiveVideoPending()):
            try:
                generator.generate(PROJECT, job, save_production=lambda value: self.states.append(copy.deepcopy(value)),
                                   save_source=save, load_source=Mock())
            finally:
                self.voice_call = voice_duration.call_args

    def test_full_sixteen_scene_inputs_preserve_long_content_and_resume_contract(self):
        generator, client = self.generator()
        job = long_job()
        with self.assertRaises(LiveVideoPending):
            self.create_inputs(generator, job)
        state = self.states[-1]
        self.assertEqual(self.voice_call.kwargs, {"target_seconds": 120})
        self.assertEqual(state["content"]["target_seconds"], 120)
        self.assertEqual(state["content"]["narration"], long_plan()["narration"])
        self.assertEqual(len(state["raw_sources"]), 16)
        self.assertEqual(state["scene_seconds"], [7] * 16)
        self.assertEqual(generator._illustration.call_count, 16)
        self.assertTrue(can_resume_video(state))
        self.assertEqual(state["content"]["video_credit_estimate"]["credits"], 1344)
        client.create_scene.assert_not_called()

    def test_long_plan_credit_check_stops_before_images_and_scene_orders(self):
        generator, client = self.generator(balance=320)
        with self.assertRaisesRegex(GenerationError, "примерно 1344 кредитов, доступно 320") as caught:
            self.create_inputs(generator, long_job())
        self.assertEqual(caught.exception.draft["target_seconds"], 120)
        generator._illustration.assert_not_called()
        client.create_scene.assert_not_called()
        self.assertEqual(self.states, [])

    def test_resume_with_completed_clips_uses_long_renderer_without_regeneration(self):
        generator, client = self.generator()
        job = long_job()
        with self.assertRaises(LiveVideoPending):
            self.create_inputs(generator, job)
        state = self.states[-1]
        state["phase"] = "clips"
        state["tasks"] = [f"known-task-{index}" for index in range(16)]
        state["clips"] = [{"storage_path": f"known-scene-{index}", "sha256": "a" * 64} for index in range(16)]
        generator._request.reset_mock()
        generator._illustration.reset_mock()
        client.preflight.reset_mock()
        saved = []
        with patch("content_factory_live_media.render_live_reel", return_value=b"completed-own-film") as render:
            content, assets = generator.generate(PROJECT, job, production=state,
                save_production=lambda value: saved.append(copy.deepcopy(value)), save_source=Mock(),
                load_source=lambda value: b"saved-media")
        self.assertEqual(render.call_args.kwargs, {"target_seconds": 120})
        self.assertEqual(len(render.call_args.args[0]), 16)
        self.assertEqual(content["target_seconds"], 120)
        self.assertEqual(assets[0]["data"], b"completed-own-film")
        self.assertEqual(saved[-1]["phase"], "complete")
        generator._request.assert_not_called()
        generator._illustration.assert_not_called()
        client.create_scene.assert_not_called()
        client.preflight.assert_not_called()

    def test_resume_limits_remain_strict_for_legacy_or_corrupt_state(self):
        generator, _ = self.generator()
        with self.assertRaises(LiveVideoPending):
            self.create_inputs(generator, long_job())
        valid = self.states[-1]
        for corrupt in (
            {**valid, "duration": 120.1},
            {**valid, "content": {key: value for key, value in valid["content"].items() if key != "target_seconds"}},
            {**valid, "content": {**valid["content"], "target_seconds": True}},
        ):
            with self.subTest(corrupt=corrupt):
                self.assertFalse(can_resume_video(corrupt))


if __name__ == "__main__":
    unittest.main()
