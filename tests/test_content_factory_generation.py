"""Offline AI-provider contracts plus a real audio/video rendering check."""
import base64
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


class GenerationTests(unittest.TestCase):
    def generator(self, replies):
        session = Mock()
        session.post.side_effect = replies
        return Generator("test-provider-key", session=session), session

    def test_post_is_platform_copy_and_a_real_branded_image(self):
        generator, session = self.generator([copy_response(POST), image_response()])
        content, assets = generator.generate(PROJECT, {"id": "one", "platform": "threads", "format": "post"})
        self.assertEqual(content["format"], "post")
        self.assertEqual(content["text"], POST["text"])
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["mime_type"], "image/jpeg")
        with Image.open(io.BytesIO(assets[0]["data"])) as generated:
            self.assertEqual(generated.size, (1080, 1350))
        self.assertEqual(session.post.call_count, 2)
        sent_project = json.loads(session.post.call_args_list[0].kwargs["json"]["input"])["project"]
        self.assertEqual(sent_project["name"], PROJECT["name"])
        self.assertNotIn("Агентство W", sent_project["name"])
        self.assertEqual(session.post.call_args_list[1].kwargs["json"]["n"], 1)
        self.assertEqual(session.post.call_args_list[1].kwargs["json"]["quality"], "high")

    def test_image_quality_is_configurable_without_changing_the_model(self):
        for quality in ("low", "medium", "high", "auto"):
            with self.subTest(quality=quality):
                session = Mock()
                session.post.side_effect = [copy_response(POST), image_response()]
                generator = Generator("test-provider-key", session=session, image_quality=quality)
                generator.generate(PROJECT, {"platform": "vk", "format": "post"})
                image_payload = session.post.call_args_list[-1].kwargs["json"]
                self.assertEqual(image_payload["quality"], quality)
                self.assertEqual(image_payload["model"], "gpt-image-1")

    def test_invalid_image_quality_is_rejected_before_any_paid_calls(self):
        session = Mock()
        with self.assertRaises(GenerationError):
            Generator("test-provider-key", session=session, image_quality="ultra")
        session.post.assert_not_called()

    def test_carousel_order_and_paid_image_count_are_bounded(self):
        copy = dict(POST, slides=["Один: подготовьте глину.", "Два: найдите центр.", "Три: сформируйте чашку."])
        generator, session = self.generator([copy_response(copy)] + [image_response() for _ in range(3)])
        content, assets = generator.generate(PROJECT, {"platform": "instagram", "format": "carousel"})
        self.assertEqual(content["slides"], copy["slides"])
        self.assertEqual([a["filename"] for a in assets], ["slide_01.jpg", "slide_02.jpg", "slide_03.jpg"])
        self.assertEqual(session.post.call_count, 4)
        self.assertEqual(sum(c.kwargs["json"].get("n", 0) for c in session.post.call_args_list), 3)

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
        generator, session = self.generator([copy_response(POST), response({"data": [{"url": "https://example.invalid/image"}]})])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})
        self.assertEqual(session.post.call_count, 2)
        generator, session = self.generator([copy_response(POST), response({"data": [{"b64_json": "not base64"}]})])
        with self.assertRaises(GenerationError):
            generator.generate(PROJECT, {"platform": "vk", "format": "post"})

    def test_reel_uses_copy_image_speech_then_real_video_renderer(self):
        narration = " ".join(["керамика"] * 50)
        generator, session = self.generator([
            copy_response(dict(POST, narration=narration)), image_response(), response(content=b"speech"),
        ])
        with patch("content_factory_generation.render_reel", return_value=b"real-renderer-result") as render:
            content, assets = generator.generate(PROJECT, {"platform": "instagram", "format": "reel"})
        self.assertEqual(content["narration"], narration)
        self.assertEqual(assets, [{"data": b"real-renderer-result", "mime_type": "video/mp4", "filename": "reel.mp4"}])
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(session.post.call_args_list[-1].kwargs["json"]["input"], narration)
        self.assertEqual(render.call_args.args[1], b"speech")

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


if __name__ == "__main__":
    unittest.main()
