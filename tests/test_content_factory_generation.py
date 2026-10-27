"""Offline AI-provider contracts plus a real audio/video rendering check."""
import base64
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from content_factory_generation import (
    Generator, GenerationError, render_branded_image, render_reel, validate_content,
)


PROJECT = {
    "name": "Студия керамики", "product": "Мастер-классы по керамике",
    "audience": "Взрослые начинающие", "language": "Русский",
    "tone": "Тёплый, понятный", "goal": "Запись на занятия", "brand_color": "#336699",
    "facts": "Занятия для начинающих, готовые работы проходят обжиг.",
    "examples": "Проведите вечер за гончарным кругом.",
    "visual_style": "Редакционная фотография в тёплом естественном свете.",
    "avoid": "Не обещать изготовление готовой чашки за один вечер.",
}
POST = {"title": "Первый вечер за гончарным кругом", "text": "Слепите свою первую чашку. Выберите удобное занятие на сайте студии."}


def artwork():
    output = io.BytesIO()
    Image.new("RGB", (256, 384), "#97B5D3").save(output, "PNG")
    return output.getvalue()


def response(payload=None, *, status=200, content=b""):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.content = content
    return result


def copy_response(content):
    return response({"status": "completed", "output": [{"type": "message", "content": [{
        "type": "output_text", "text": json.dumps(content, ensure_ascii=False),
    }]}]})


def image_response():
    return response({"data": [{"b64_json": base64.b64encode(artwork()).decode()}]})


def review_response(approved=True, *, issue="Изображение не соответствует сцене."):
    return copy_response({
        "approved": approved, "issues": [] if approved else [issue],
        "brief_feedback": "Готово." if approved else "Исправь конкретную указанную проблему.",
    })


def visual_plan(count=1):
    return {"art_direction": "Одна мастерская, натуральные материалы и тёплый свет.", "frames": [
        {"subject": f"Этап работы с глиной {index + 1}: человек создаёт чашку.",
         "composition": "Руки и глина в центре; свободная область под короткую подпись.",
         "lighting": "Мягкий свет из окна.", "palette": "Тёплая глина и синий акцент."}
        for index in range(count)
    ]}


def production_prefix(content=POST, count=1, *, proofread=None):
    return [copy_response(content), copy_response(content if proofread is None else proofread),
            review_response(), copy_response(visual_plan(count))]


def requested_paths(session):
    return [call.args[0].removeprefix("https://api.openai.com/v1/") for call in session.post.call_args_list]


class GenerationTests(unittest.TestCase):
    def test_provider_diagnostics_never_persist_raw_error_or_credentials(self):
        generator = Generator("private-key", session=Mock())
        generator.session.post.return_value = response({"error": {
            "code": "unsupported_parameter", "param": "reasoning.effort",
            "message": "Unsupported reasoning option; private-key",
        }}, status=400)
        with self.assertRaises(GenerationError) as caught:
            generator._request("responses", {"model": "gpt-5"})
        details = caught.exception.provider_details
        self.assertEqual(details["parameter"], "reasoning.effort")
        self.assertIn("reasoning_option", details["categories"])
        self.assertNotIn("private-key", json.dumps(details))

    def generator(self, replies, **kwargs):
        session = Mock()
        session.post.side_effect = replies
        return Generator("test-provider-key", session=session, **kwargs), session

    def test_post_is_platform_copy_and_a_real_branded_image(self):
        generator, session = self.generator(production_prefix() + [image_response(), review_response()])
        content, assets = generator.generate(PROJECT, {"id": "one", "platform": "threads", "format": "post"})
        self.assertEqual(content["format"], "post")
        self.assertEqual(content["text"], POST["text"])
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["mime_type"], "image/jpeg")
        with Image.open(io.BytesIO(assets[0]["data"])) as generated:
            self.assertEqual(generated.size, (1080, 1350))
        self.assertEqual(requested_paths(session), ["responses"] * 4 + ["images/generations", "responses"])
        self.assertTrue(content["quality_review"]["approved"])
        self.assertTrue(content["visual_reviews"][0]["approved"])
        sent_project = json.loads(session.post.call_args_list[0].kwargs["json"]["input"])["project"]
        self.assertIn("JSON", session.post.call_args_list[0].kwargs["json"]["input"])
        self.assertEqual(sent_project["name"], PROJECT["name"])
        self.assertNotIn("Агентство W", sent_project["name"])
        self.assertEqual(session.post.call_args_list[4].kwargs["json"]["n"], 1)
        self.assertEqual(session.post.call_args_list[4].kwargs["json"]["quality"], "high")

    def test_image_quality_is_configurable_without_changing_the_model(self):
        for quality in ("low", "medium", "high", "auto"):
            with self.subTest(quality=quality):
                session = Mock()
                session.post.side_effect = production_prefix() + [image_response(), review_response()]
                generator = Generator("test-provider-key", session=session, image_quality=quality)
                generator.generate(PROJECT, {"platform": "vk", "format": "post"})
                image_payload = session.post.call_args_list[4].kwargs["json"]
                self.assertEqual(image_payload["quality"], quality)
                self.assertEqual(image_payload["model"], "gpt-image-2.5-sunburst")

    def test_invalid_image_quality_is_rejected_before_any_paid_calls(self):
        session = Mock()
        with self.assertRaises(GenerationError):
            Generator("test-provider-key", session=session, image_quality="ultra")
        session.post.assert_not_called()

    def test_carousel_order_and_paid_image_count_are_bounded(self):
        copy = dict(POST, slides=["Один: подготовьте глину.", "Два: найдите центр.", "Три: сформируйте чашку."])
        generator, session = self.generator(production_prefix(copy, 3) + [
            reply for _ in range(3) for reply in (image_response(), review_response())
        ])
        content, assets = generator.generate(PROJECT, {"platform": "instagram", "format": "carousel"})
        self.assertEqual(content["slides"], copy["slides"])
        self.assertEqual([a["filename"] for a in assets], ["slide_01.jpg", "slide_02.jpg", "slide_03.jpg"])
        self.assertEqual(session.post.call_count, 10)
        self.assertEqual(requested_paths(session)[4:], ["images/generations", "responses"] * 3)
        self.assertEqual(sum(c.kwargs["json"].get("n", 0) for c in session.post.call_args_list), 3)

    def test_semantic_rejection_gets_one_rewrite_and_an_independent_recheck(self):
        revised = dict(POST, text="Создайте чашку своими руками. После занятия она пройдёт обжиг. Выберите занятие на сайте.")
        issue = "Утверждение о готовой чашке в тот же вечер не подтверждено."
        generator, session = self.generator([
            copy_response(POST), copy_response(POST), review_response(False, issue=issue),
            copy_response(revised), copy_response(revised), review_response(),
            copy_response(visual_plan()), image_response(), review_response(),
        ], review_model="independent-review-model")
        content, _ = generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(content["text"], revised["text"])
        self.assertTrue(content["quality_review"]["approved"])
        rewrite_payload = json.loads(session.post.call_args_list[3].kwargs["json"]["input"])
        self.assertEqual(rewrite_payload["previous"]["text"], POST["text"])
        self.assertEqual(rewrite_payload["review"]["issues"], [issue])
        self.assertEqual([session.post.call_args_list[i].kwargs["json"]["model"] for i in (1, 2, 4, 5, 8)],
                         ["independent-review-model"] * 5)
        self.assertEqual(requested_paths(session), ["responses"] * 7 + ["images/generations", "responses"])

    def test_repeated_semantic_failure_preserves_draft_and_never_starts_media(self):
        revised = dict(POST, text="Исправленный, но всё ещё неподтверждённый текст.")
        generator, session = self.generator([
            copy_response(POST), copy_response(POST), review_response(False),
            copy_response(revised), copy_response(revised), review_response(False),
        ])
        with self.assertRaises(GenerationError) as caught:
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(requested_paths(session), ["responses"] * 6)
        self.assertEqual(caught.exception.draft["text"], revised["text"])
        self.assertFalse(caught.exception.draft["quality_review"]["approved"])

    def test_contradictory_editorial_approval_cannot_unlock_media(self):
        generator, session = self.generator([
            copy_response(POST), copy_response(POST),
            copy_response({"approved": True, "issues": ["Выдумана цена"], "brief_feedback": "Готово"}),
        ])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(requested_paths(session), ["responses"] * 3)

    def test_proofreading_fixes_title_and_slide_before_review_art_direction_and_overlay(self):
        draft = {
            "title": "Как передать повторяющиеся контакты ИИ", "text": "Мелочи перестают забирать вечер. Узнайте подробнее.",
            "slides": ["Вечер снова ушёл на сообщения?", "На важное дела остаются крохи.", "Узнайте подробнее."],
        }
        corrected = {
            "title": "Как передать повторяющиеся задачи ИИ", "text": "ИИ-помощники помогают сократить время на подготовку. Узнайте подробнее.",
            "slides": ["Вечер снова ушёл на сообщения?", "На важные дела остаются крохи.", "Узнайте подробнее."],
        }
        generator, session = self.generator(production_prefix(draft, 3, proofread=corrected) + [
            reply for _ in range(3) for reply in (image_response(), review_response())
        ])
        with patch("content_factory_media.render_slide", return_value=b"rendered-slide") as render:
            content, assets = generator.generate(PROJECT, {"platform": "vk", "format": "carousel"})
        self.assertEqual(content["title"], corrected["title"])
        self.assertEqual(content["text"], corrected["text"])
        self.assertEqual(content["slides"], corrected["slides"])
        for index in (2, 3):
            reviewed = json.loads(session.post.call_args_list[index].kwargs["json"]["input"])["content"]
            self.assertEqual(reviewed["title"], corrected["title"])
            self.assertEqual(reviewed["slides"], corrected["slides"])
        self.assertEqual([call.args[1] for call in render.call_args_list], corrected["slides"])
        self.assertEqual(len(assets), 3)

    def test_corrupt_proofreading_stops_before_semantic_review_and_media(self):
        corrupt = response({"output": [{"content": [{"type": "output_text", "text": "{broken json"}]}]})
        generator, session = self.generator([copy_response(POST), corrupt])
        with self.assertRaises(GenerationError) as caught:
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(requested_paths(session), ["responses"] * 2)
        self.assertEqual(caught.exception.draft["text"], POST["text"])

    def test_proofreader_cannot_change_carousel_length_order_or_format(self):
        draft = {**POST, "slides": ["Подготовьте глину.", "Найдите центр.", "Сформируйте чашку."]}
        for corrected in (
            {**draft, "slides": draft["slides"] + ["Дополнительный слайд."]},
            {**draft, "slides": list(reversed(draft["slides"]))},
            {**draft, "format": "post"},
        ):
            with self.subTest(corrected=corrected):
                generator, session = self.generator([copy_response(draft), copy_response(corrected)])
                with self.assertRaises(GenerationError):
                    generator.generate(PROJECT, {"platform": "vk", "format": "carousel"})
                self.assertEqual(requested_paths(session), ["responses"] * 2)

    def test_project_memory_reaches_author_art_director_and_image_brief(self):
        generator, session = self.generator(production_prefix() + [image_response(), review_response()])
        generator.generate({**PROJECT, "token": "must-not-leave-project-boundary"},
                           {"platform": "vk", "format": "post", "topic": "Тихий вечер с глиной"})
        for index in (0, 1, 2, 3):
            project = json.loads(session.post.call_args_list[index].kwargs["json"]["input"])["project"]
            for field in ("facts", "examples", "visual_style", "avoid"):
                self.assertEqual(project[field], PROJECT[field])
            self.assertNotIn("token", project)
        image_prompt = session.post.call_args_list[4].kwargs["json"]["prompt"]
        self.assertIn(PROJECT["visual_style"], image_prompt)
        self.assertIn(visual_plan()["frames"][0]["subject"], image_prompt)
        self.assertNotIn("must-not-leave-project-boundary", image_prompt)

    def test_vision_review_receives_the_generated_image_and_scene_brief(self):
        generator, session = self.generator(production_prefix() + [image_response(), review_response()])
        generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        vision = session.post.call_args_list[5].kwargs["json"]["input"][0]
        self.assertEqual(vision["role"], "user")
        text, image = vision["content"]
        self.assertEqual(text["type"], "input_text")
        self.assertEqual(json.loads(text["text"])["frames"], visual_plan()["frames"])
        self.assertEqual(image["type"], "input_image")
        self.assertEqual(image["detail"], "high")
        prefix, encoded = image["image_url"].split(",", 1)
        self.assertEqual(prefix, "data:image/jpeg;base64")
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as preview:
            self.assertEqual(preview.size, (256, 384))

    def test_failed_visual_gets_one_concrete_correction_then_recheck(self):
        issue = "На руках заметно сросшиеся пальцы."
        generator, session = self.generator(production_prefix() + [
            image_response(), review_response(False, issue=issue), image_response(), review_response(),
        ])
        content, assets = generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(len(assets), 1)
        self.assertTrue(content["visual_reviews"][0]["approved"])
        self.assertEqual(requested_paths(session), ["responses"] * 4 + ["images/generations", "responses"] * 2)
        correction_prompt = session.post.call_args_list[6].kwargs["json"]["prompt"]
        self.assertIn(issue, correction_prompt)

    def test_second_visual_failure_stops_and_preserves_explanation(self):
        generator, session = self.generator(production_prefix() + [
            image_response(), review_response(False), image_response(), review_response(False),
        ])
        with patch("content_factory_media.render_slide") as render:
            with self.assertRaises(GenerationError) as caught:
                generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        render.assert_not_called()
        self.assertEqual(session.post.call_count, 8)
        self.assertFalse(caught.exception.draft["visual_reviews"][0]["approved"])
        self.assertIn("Изображение не соответствует", caught.exception.draft["quality_review"]["issues"][0])

    def test_visual_correction_budget_is_shared_by_the_whole_carousel(self):
        content = dict(POST, slides=["Подготовьте глину.", "Найдите центр.", "Сформируйте чашку."])
        generator, session = self.generator(production_prefix(content, 3) + [
            image_response(), review_response(False), image_response(), review_response(),
            image_response(), review_response(False),
        ])
        with patch("content_factory_media.render_slide") as render:
            with self.assertRaises(GenerationError):
                generator.generate(PROJECT, {"platform": "vk", "format": "carousel"})
        render.assert_not_called()
        self.assertEqual(sum(path == "images/generations" for path in requested_paths(session)), 3)
        self.assertEqual(session.post.call_count, 10)

    def test_malformed_visual_plan_stops_before_the_first_image(self):
        generator, session = self.generator([
            copy_response(POST), copy_response(POST), review_response(),
            copy_response({"art_direction": "Стиль", "frames": []}),
        ])
        with self.assertRaises(GenerationError) as caught:
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(requested_paths(session), ["responses"] * 4)
        self.assertTrue(caught.exception.draft["quality_review"]["approved"])

    def test_broken_vision_response_does_not_repeat_a_paid_image_request(self):
        corrupt = response({"output": [{"content": [{"type": "output_text", "text": "not json"}]}]})
        generator, session = self.generator(production_prefix() + [image_response(), corrupt])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(session.post.call_count, 6)
        self.assertEqual(requested_paths(session).count("images/generations"), 1)

    def test_provider_error_during_visual_review_is_sanitized_without_regeneration(self):
        generator, session = self.generator(production_prefix() + [
            image_response(), response({"error": "test-provider-key"}, status=401),
        ])
        with self.assertRaises(GenerationError) as caught:
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertNotIn("test-provider-key", str(caught.exception))
        self.assertEqual(session.post.call_count, 6)
        self.assertEqual(requested_paths(session).count("images/generations"), 1)

    def test_progress_describes_real_stages_without_claiming_publication(self):
        generator, _ = self.generator(production_prefix() + [image_response(), review_response()])
        progress = Mock()
        generator.generate(PROJECT, {"platform": "vk", "format": "post"}, progress=progress)
        stages = [call.args[0]["stage"] for call in progress.call_args_list]
        self.assertEqual(stages, ["copy", "proofread", "review", "visual", "image", "saving"])
        self.assertNotIn("published", stages)

    def test_invalid_copy_never_starts_paid_media_generation(self):
        invalid = (
            dict(POST, text="А" * 501),
            dict(POST, title=""),
            dict(POST, text={"not": "text"}),
        )
        for copy in invalid:
            with self.subTest(copy=copy):
                generator, session = self.generator([copy_response(copy)])
                with self.assertRaises(GenerationError):
                    generator.generate(PROJECT, {"platform": "threads", "format": "post"})
                self.assertEqual(session.post.call_count, 1)

    def test_invalid_carousel_stops_before_images(self):
        for slides in (["one", "two"], [str(i) for i in range(7)], ["same"] * 3, ["A" * 401, "b", "c"]):
            with self.subTest(slides=slides):
                generator, session = self.generator([copy_response(dict(POST, slides=slides))])
                with self.assertRaises(GenerationError):
                    generator.generate(PROJECT, {"platform": "vk", "format": "carousel"})
                self.assertEqual(session.post.call_count, 1)

    def test_missing_config_or_unknown_target_never_calls_provider(self):
        generator, session = self.generator([])
        for project, job in (
            (dict(PROJECT, product=""), {"platform": "vk", "format": "post"}),
            (dict(PROJECT, brand_color="red"), {"platform": "vk", "format": "post"}),
            (PROJECT, {"platform": "unknown", "format": "post"}),
            (PROJECT, {"platform": "vk", "format": "unknown"}),
        ):
            with self.subTest(project=project, job=job), self.assertRaises(GenerationError):
                generator.generate(project, job)
        session.post.assert_not_called()
        with self.assertRaises(GenerationError):
            Generator("", session=session).generate(PROJECT, {"platform": "vk", "format": "post"})
        session.post.assert_not_called()

    def test_corrupt_or_incomplete_text_response_does_not_bill_for_images(self):
        bad_responses = (
            response({"output": [{"content": [{"type": "output_text", "text": "{broken json"}]}]}),
            response({"status": "incomplete", "output": []}),
        )
        for reply in bad_responses:
            generator, session = self.generator([reply])
            with self.assertRaises(GenerationError):
                generator.generate(PROJECT, {"platform": "instagram", "format": "post"})
            self.assertEqual(session.post.call_count, 1)

    def test_provider_error_hides_credentials_and_is_not_retried(self):
        for reply in (response({"error": "test-provider-key"}, status=401), requests.Timeout("test-provider-key")):
            generator, session = self.generator([reply])
            with self.assertRaises(GenerationError) as caught:
                generator.generate(PROJECT, {"platform": "vk", "format": "post"})
            self.assertNotIn("test-provider-key", str(caught.exception))
            self.assertEqual(session.post.call_count, 1)

    def test_invalid_image_is_rejected_without_url_fallback(self):
        generator, session = self.generator(production_prefix() + [response({"data": [{"url": "https://example.invalid/image"}]})])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(session.post.call_count, 5)
        generator, session = self.generator(production_prefix() + [response({"data": [{"b64_json": "not base64"}]})])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})

    def test_reel_reviews_four_distinct_scenes_before_speech_and_story_renderer(self):
        narration = " ".join(["керамика"] * 50)
        generator, session = self.generator(production_prefix(dict(POST, narration=narration), 4) + [
            reply for _ in range(4) for reply in (image_response(), review_response())
        ] + [response(content=b"speech")])
        with patch("content_factory_media.render_story_reel", return_value=b"real-renderer-result") as render:
            content, assets = generator.generate(PROJECT, {"platform": "instagram", "format": "reel"})
        self.assertEqual(content["narration"], narration)
        self.assertEqual(assets, [{"data": b"real-renderer-result", "mime_type": "video/mp4", "filename": "reel.mp4"}])
        self.assertEqual(session.post.call_count, 13)
        self.assertEqual(requested_paths(session), ["responses"] * 4 + ["images/generations", "responses"] * 4 + ["audio/speech"])
        self.assertEqual(session.post.call_args_list[-1].kwargs["json"]["input"], narration)
        self.assertEqual(render.call_args.args[1], b"speech")
        self.assertEqual(len(render.call_args.args[0]), 4)
        self.assertEqual(render.call_args.args[2], narration)

    def test_reel_rejects_short_speech_and_camera_directions_before_images(self):
        for narration in ("Too short.", "Сцена 1: " + " ".join(["текст"] * 50)):
            generator, session = self.generator([copy_response(dict(POST, narration=narration))])
            with self.assertRaises(GenerationError):
                generator.generate(PROJECT, {"platform": "instagram", "format": "reel"})
            self.assertEqual(session.post.call_count, 1)

    def test_missing_ffmpeg_is_detected_before_any_remote_request(self):
        generator, session = self.generator([])
        with patch("content_factory_generation.shutil.which", return_value=None):
            with self.assertRaises(GenerationError):
                generator.generate(PROJECT, {"platform": "vk", "format": "reel"})
        session.post.assert_not_called()

    def test_font_overlay_rejects_unreadable_excessive_text(self):
        with self.assertRaises(GenerationError):
            render_branded_image(artwork(), "длинный " * 1000, PROJECT["name"], "#336699")

    def test_platform_caption_limits_are_enforced(self):
        self.assertEqual(validate_content(dict(POST, text="a" * 2200), "post", "instagram")["text"], "a" * 2200)
        with self.assertRaises(GenerationError):
            validate_content(dict(POST, text="a" * 1025), "post", "telegram")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_reel_contains_vertical_video_and_audio(self):
        pcm = io.BytesIO()
        with wave.open(pcm, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000)
        video = render_reel(artwork(), pcm.getvalue(), "Создайте первую чашку своими руками.", POST["title"], PROJECT["name"], "#336699")
        self.assertGreater(len(video), 5000)
        with tempfile.TemporaryDirectory(prefix="factory_generation_test_") as directory:
            output = Path(directory) / "output.mp4"
            output.write_bytes(video)
            probe = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                capture_output=True, check=True, timeout=15,
            )
        media = json.loads(probe.stdout)
        self.assertTrue(any(stream["codec_type"] == "audio" for stream in media["streams"]))
        video_stream = next(stream for stream in media["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video_stream["width"], video_stream["height"]), (1080, 1920))
        self.assertEqual(video_stream["codec_name"], "h264")
        self.assertGreaterEqual(float(media["format"]["duration"]), 1.4)


class LiveGenerationTests(unittest.TestCase):
    def setUp(self):
        self.sources = {}
        self.snapshots = []
        self.session = Mock()
        self.client = Mock()
        self.client.model = "gen4.5"
        self.client.preflight.return_value = {"key_valid": True, "credit_balance": 1000}
        self.client.create_scene.side_effect = [f"task-{i}" for i in range(4)]
        self.client.task_status.return_value = {"status": "pending"}
        self.client.download.side_effect = lambda url: url.rsplit("/", 1)[-1].encode()
        self.generator = Generator("test-provider-key", session=self.session,
                                   live_video_client_resolver=lambda project: self.client)
        self.job = {"platform": "vk", "format": "reel", "video_mode": "live", "topic": "Вечер в мастерской"}
        self.content = dict(POST, format="reel", narration=" ".join(["керамика"] * 50),
                            quality_review={"approved": True, "issues": [], "brief_feedback": "Готово"},
                            visual_plan=visual_plan(4))

    def save_source(self, name, data, mime):
        self.sources[name] = data
        return {"storage_path": name, "mime_type": mime}

    def load_source(self, ref):
        return self.sources[ref["storage_path"]]

    def save_production(self, value):
        self.snapshots.append(copy.deepcopy(value))

    def state(self, *, tasks=None):
        images = [self.save_source(f"image-{i}", artwork(), "image/png") for i in range(4)]
        speech = self.save_source("speech", b"speech", "audio/mpeg")
        return {"version": 1, "phase": "inputs_ready", "content": copy.deepcopy(self.content),
                "raw_sources": images, "speech_source": speech, "duration": 37.0,
                "scene_seconds": [10] * 4, "tasks": tasks or [None] * 4,
                "clips": [None] * 4, "failed_indices": [], "starting_index": None}

    def run_generation(self, **kwargs):
        return self.generator.generate(PROJECT, self.job, save_production=self.save_production,
                                       save_source=self.save_source, load_source=self.load_source, **kwargs)

    def test_live_job_is_created_once_then_resumes_without_copy_images_or_speech(self):
        from content_factory_live_video import LiveVideoPending
        reel = {k: self.content[k] for k in ("title", "text", "narration")}
        self.session.post.side_effect = [copy_response(reel), copy_response(reel), review_response(),
                                        response(content=b"speech"), copy_response(visual_plan(4))] + [
            reply for _ in range(4) for reply in (image_response(), review_response())
        ]
        def create(image, prompt, duration):
            saved = self.snapshots[-1]
            index = saved["starting_index"]
            self.assertIsNone(saved["tasks"][index])
            self.assertTrue(all(saved["tasks"][j] for j in range(index)))
            self.assertEqual(duration, 10)
            self.assertIn("physical action", prompt)
            return f"task-{index}"
        self.client.create_scene.side_effect = create
        with patch("content_factory_live_media.speech_duration", return_value=37.0):
            with self.assertRaises(LiveVideoPending):
                self.run_generation()
        state = self.snapshots[-1]
        self.assertEqual(state["tasks"], [f"task-{i}" for i in range(4)])
        self.assertIsNone(state["starting_index"])
        self.assertEqual(len(self.sources), 5)
        self.assertEqual(self.session.post.call_count, 13)
        self.assertEqual(requested_paths(self.session)[3], "audio/speech")
        self.assertEqual(self.client.create_scene.call_count, 4)
        self.client.task_status.side_effect = lambda task: {"status": "succeeded", "url": "https://public.invalid/" + task}
        with patch("content_factory_live_media.render_live_reel", return_value=b"moving-video") as render:
            content, assets = self.run_generation(production=state)
        self.assertEqual(content["video_kind"], "live")
        self.assertEqual(assets[0]["data"], b"moving-video")
        self.assertEqual(render.call_args.args[0], [f"task-{i}".encode() for i in range(4)])
        self.assertEqual(render.call_args.args[1], b"speech")
        self.assertEqual(self.session.post.call_count, 13)
        self.assertEqual(self.client.create_scene.call_count, 4)
        self.assertEqual(self.client.preflight.call_count, 1)
        self.assertEqual(self.snapshots[-1]["phase"], "complete")

    def test_resume_known_task_does_not_recreate_it_after_uncertain_ack_marker(self):
        from content_factory_live_video import LiveVideoUncertain
        state = self.state()
        state["starting_index"] = 0
        with self.assertRaises(LiveVideoUncertain):
            self.run_generation(production=state)
        self.client.create_scene.assert_not_called()
        self.session.post.assert_not_called()
        self.client.preflight.assert_not_called()

    def test_creation_timeout_persists_uncertainty_and_cannot_auto_resend(self):
        from content_factory_live_video import LiveVideoUncertain
        self.client.create_scene.side_effect = LiveVideoUncertain("Ответ создания не подтверждён")
        with self.assertRaises(LiveVideoUncertain):
            self.run_generation(production=self.state())
        state = self.snapshots[-1]
        self.assertTrue(state["creation_uncertain"])
        self.assertEqual(state["starting_index"], 0)
        self.assertEqual(self.client.create_scene.call_count, 1)
        with self.assertRaises(LiveVideoUncertain):
            self.run_generation(production=state)
        self.assertEqual(self.client.create_scene.call_count, 1)
        self.session.post.assert_not_called()

    def test_failed_scenes_are_retained_and_explicit_retry_recreates_only_failed_index(self):
        from content_factory_live_video import LiveVideoError
        state = self.state(tasks=[f"task-{i}" for i in range(4)])
        self.client.task_status.side_effect = lambda task: ({"status": "failed"} if task == "task-1" else
            {"status": "succeeded", "url": "https://public.invalid/" + task})
        with self.assertRaises(LiveVideoError):
            self.run_generation(production=state)
        saved = self.snapshots[-1]
        self.assertEqual(saved["failed_indices"], [1])
        self.assertEqual([v is not None for v in saved["clips"]], [True, False, True, True])
        self.client.create_scene.assert_not_called()
        saved["tasks"][1], saved["failed_indices"] = None, []
        self.client.create_scene.side_effect = ["replacement-1"]
        self.client.task_status.side_effect = lambda task: {"status": "succeeded", "url": "https://public.invalid/" + task}
        with patch("content_factory_live_media.render_live_reel", return_value=b"moving-video"):
            self.run_generation(production=saved)
        self.client.create_scene.assert_called_once()
        self.session.post.assert_not_called()
        self.assertEqual(self.snapshots[-1]["tasks"], ["task-0", "replacement-1", "task-2", "task-3"])

    def test_read_status_failures_defer_without_another_paid_order(self):
        from content_factory_live_video import LiveVideoError, LiveVideoPending
        self.client.task_status.side_effect = LiveVideoError("Временно недоступно", retryable=True)
        with self.assertRaises(LiveVideoPending):
            self.run_generation(production=self.state(tasks=[f"task-{i}" for i in range(4)]))
        self.client.create_scene.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(self.snapshots[-1]["tasks"], [f"task-{i}" for i in range(4)])

    def test_live_intent_never_falls_back_to_stills_when_provider_or_state_is_missing(self):
        self.generator.live_video_client_resolver = None
        with self.assertRaisesRegex(GenerationError, "не подключено"):
            self.run_generation()
        self.session.post.assert_not_called()
        self.generator.live_video_client_resolver = lambda project: self.client
        with self.assertRaisesRegex(GenerationError, "требует проверки"):
            self.run_generation(production={"version": 1})
        self.session.post.assert_not_called()
        self.client.create_scene.assert_not_called()

    def test_source_movie_audio_and_specific_actions_are_preserved_on_seeded_resume(self):
        state = self.state(tasks=[f"task-{i}" for i in range(4)])
        state["motion_prompts"] = ["Person closes laptop" + str(i) for i in range(4)]
        state["tasks"] = [None] * 4
        self.client.task_status.side_effect = lambda task: {"status": "succeeded", "url": "https://public.invalid/" + task}
        with patch("content_factory_live_media.render_live_reel", return_value=b"moving-video") as render:
            self.run_generation(production=state)
        self.assertEqual([call.args[1] for call in self.client.create_scene.call_args_list], state["motion_prompts"])
        self.assertEqual(render.call_args.args[1:4], (b"speech", self.content["narration"], self.content["title"]))
        self.session.post.assert_not_called()

    def test_malformed_resume_contract_is_rejected_before_paid_scene_creation(self):
        from content_factory_generation import can_resume_video
        state = self.state()
        self.assertTrue(can_resume_video(state))
        for changes in ({"content": {"quality_review": "wrong"}}, {"tasks": []},
                        {"version": True}, {"phase": []}, {"scene_seconds": [True] * 4}):
            with self.subTest(changes=changes):
                self.assertFalse(can_resume_video({**state, **changes}))


if __name__ == "__main__":
    unittest.main()
