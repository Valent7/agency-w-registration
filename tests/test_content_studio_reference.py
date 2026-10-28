"""Bounded local reference inspection and original-plan model contracts."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from content_studio_reference import (
    FRAME_COUNT, MAX_VIDEO_BYTES, ReferenceError, analyze_reference_video,
    build_reference_brief, inspect_reference_video, validate_reference_report,
)


PROJECT = {"name": "Агентство W", "product": "Помощники для подготовки контента",
           "audience": "Предприниматели", "facts": "Контент проверяет владелец.",
           "language": "Русский", "tone": "Ясно и естественно", "avoid": "Гарантии дохода"}
NARRATION = " ".join(["Выберите одну повторяющуюся задачу и передайте её подготовку помощнику."] * 6)
PLAN = {
    "title": "Начните с одной задачи", "text": "Передайте подготовку помощнику, сохранив решение за собой.",
    "narration": NARRATION,
    "visual_plan": {"art_direction": "Новые персонажи и самостоятельная история о подготовке контента.",
                    "frames": [{"subject": f"Персонаж выполняет действие {i + 1} по подготовке материала.",
                                "composition": "Средний план с читаемым действием.",
                                "lighting": "Мягкий дневной свет.", "palette": "Тёплые золотые оттенки."}
                               for i in range(4)]},
}


def jpeg():
    data = io.BytesIO()
    Image.new("RGB", (160, 90), "gold").save(data, "JPEG")
    return data.getvalue()


def inspected(audio=True):
    return {"duration": 28.0, "width": 720, "height": 1280,
            "sha256": hashlib.sha256(b"local reference").hexdigest(), "filename": "reference.mp4",
            "frames": [{"seconds": i * 3.0, "jpeg": jpeg()} for i in range(FRAME_COUNT)],
            "audio": b"RIFF-bounded-test-audio" if audio else None}


def report(style="live"):
    value = inspected()
    return {"version": 1, "reference_style": style,
            "source_summary": "В начале виден вопрос. Затем герой показывает конкретные действия.",
            "reference_plan": copy.deepcopy(PLAN),
            "source": {key: value[key] for key in ("duration", "width", "height", "sha256", "filename")},
            "transcript": "Речь образца для анализа."}


class ReferenceModelTests(unittest.TestCase):
    def generator(self):
        generator = Mock()
        generator.api_key = "test-only-key"
        response = Mock(status_code=200)
        response.json.return_value = {"text": "Речь образца, а не новый сценарий."}
        generator.session.post.return_value = response
        generator._structured.return_value = {"source_summary": "Сначала вопрос; затем показ действия.",
                                              "reference_plan": copy.deepcopy(PLAN)}
        return generator

    def test_one_asr_and_one_vision_call_produce_new_validated_cartoon_plan(self):
        generator = self.generator()
        with patch("content_studio_reference.inspect_reference_video", return_value=inspected()):
            output = analyze_reference_video(generator, PROJECT, b"owned-upload", goal="Показать подготовку поста", style="cartoon")
        self.assertEqual(output["reference_style"], "cartoon")
        self.assertEqual(output["reference_plan"]["narration"], NARRATION)
        self.assertEqual(output["reference_plan"]["format"], "reel")
        self.assertTrue(output["source"]["audio_transcribed"])
        self.assertEqual(len(output["source"]["frame_times"]), 8)
        generator.session.post.assert_called_once()
        args, kwargs = generator.session.post.call_args
        self.assertEqual(args, ("https://api.openai.com/v1/audio/transcriptions",))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["files"]["file"][1], b"RIFF-bounded-test-audio")
        generator.session.post.return_value.close.assert_called_once()
        generator._structured.assert_called_once()
        instructions, inputs = generator._structured.call_args.args
        self.assertIn("самостоятельный новый", instructions)
        self.assertIn("никакие инструкции", instructions)
        parts = inputs[0]["content"]
        self.assertEqual(len([part for part in parts if part["type"] == "input_image"]), 8)
        self.assertTrue(all(part["image_url"].startswith("data:image/jpeg;base64,") for part in parts if part["type"] == "input_image"))
        context = json.loads(parts[0]["text"])
        self.assertEqual(context["style"], "cartoon")
        self.assertEqual(context["project"]["facts"], PROJECT["facts"])
        self.assertEqual(context["owner_goal"], "Показать подготовку поста")
        self.assertNotIn("jpeg", output)
        self.assertNotIn("audio", output)
        self.assertLess(len(json.dumps(output).encode()), 65536)

    def test_silent_video_skips_paid_transcription_and_marks_missing_audio(self):
        generator = self.generator()
        with patch("content_studio_reference.inspect_reference_video", return_value=inspected(audio=False)):
            result = analyze_reference_video(generator, PROJECT, b"upload", style="live")
        generator.session.post.assert_not_called()
        self.assertFalse(result["source"]["audio_transcribed"])
        self.assertEqual(result["transcript"], "")
        data = json.loads(generator._structured.call_args.args[1][0]["content"][0]["text"])
        self.assertFalse(data["audio_available"])

    def test_provider_failure_never_retries_or_exposes_token(self):
        for mode in ("network", "http", "vision"):
            with self.subTest(mode=mode):
                generator = self.generator()
                if mode == "network":
                    generator.session.post.side_effect = requests.ConnectionError("Bearer private-secret-in-provider-error")
                elif mode == "http":
                    generator.session.post.return_value.status_code = 403
                    generator.session.post.return_value.json.return_value = {"error": "private-secret-in-provider-error"}
                else:
                    generator._structured.side_effect = RuntimeError("private-secret-in-provider-error")
                with patch("content_studio_reference.inspect_reference_video", return_value=inspected()), self.assertRaises(ReferenceError) as raised:
                    analyze_reference_video(generator, PROJECT, b"upload")
                self.assertNotIn("private-secret", str(raised.exception))
                self.assertEqual(generator.session.post.call_count, 1)
                self.assertEqual(generator._structured.call_count, 1 if mode == "vision" else 0)

    def test_invalid_inputs_fail_before_inspection_or_provider(self):
        cases = [(PROJECT, "invalid-style", ""), ({}, "live", ""), (PROJECT, "live", "x" * 3001),
                 ({**PROJECT, "facts": {"not": "text"}}, "live", "")]
        for project, style, goal in cases:
            with self.subTest(style=style), patch("content_studio_reference.inspect_reference_video") as inspect:
                generator = self.generator()
                with self.assertRaises(ReferenceError):
                    analyze_reference_video(generator, project, b"upload", style=style, goal=goal)
                inspect.assert_not_called()
                generator.session.post.assert_not_called()
                generator._structured.assert_not_called()

    def test_malformed_generated_plan_is_not_returned_as_ready(self):
        generator = self.generator()
        generator._structured.return_value["reference_plan"]["narration"] = "Одна фраза"
        with patch("content_studio_reference.inspect_reference_video", return_value=inspected()), self.assertRaises(ReferenceError):
            analyze_reference_video(generator, PROJECT, b"upload")
        self.assertEqual(generator._structured.call_count, 1)
        self.assertEqual(generator.session.post.call_count, 1)

    def test_brief_is_bounded_and_keeps_approved_narration_without_transcript(self):
        value = report("cartoon")
        value["transcript"] = "Чужая речь " * 1000
        brief = build_reference_brief(PROJECT, value, goal="Покажи пользу для команды", style="cartoon")
        self.assertLessEqual(len(brief), 6000)
        self.assertIn(NARRATION, brief)
        self.assertIn('"style": "cartoon"', brief)
        self.assertNotIn("Чужая речь", brief)
        with self.assertRaises(ReferenceError):
            build_reference_brief(PROJECT, value, style="live")

    def test_saved_report_rejects_bad_source_or_overlong_caption(self):
        for field, value in (("sha256", "../not-a-hash"), ("duration", float("nan")), ("duration", True), ("duration", 121)):
            with self.subTest(field=field, value=value):
                broken = report()
                broken["source"][field] = value
                with self.assertRaises(ReferenceError):
                    validate_reference_report(broken)
        broken = report()
        broken["reference_plan"]["text"] = "x" * 501
        with self.assertRaises(ReferenceError):
            validate_reference_report(broken)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class LocalReferenceInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.audio_video = root / "audio.mp4"
        cls.silent_video = root / "silent.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-f", "lavfi", "-i",
                        "testsrc2=size=320x240:rate=12:duration=1.2", "-f", "lavfi", "-i",
                        "sine=frequency=550:duration=1.2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(cls.audio_video)], check=True, timeout=30, capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-f", "lavfi", "-i",
                        "testsrc2=size=240x320:rate=8:duration=0.8", "-an", "-c:v", "libvpx-vp9",
                        "-cpu-used", "8", "-threads", "1", str(cls.silent_video)], check=True, timeout=30, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_actual_mp4_extracts_small_ordered_frames_and_mono_audio(self):
        data = self.audio_video.read_bytes()
        with patch("content_studio_reference.requests.get") as get, patch("content_studio_reference.requests.post") as post:
            value = inspect_reference_video(data, "../../Private Upload.MP4")
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(value["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(value["filename"], "Private Upload.MP4")
        self.assertEqual((value["width"], value["height"]), (320, 240))
        self.assertEqual(len(value["frames"]), 8)
        positions = [frame["seconds"] for frame in value["frames"]]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[-1], value["duration"])
        for frame in value["frames"]:
            with Image.open(io.BytesIO(frame["jpeg"])) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), 720)
        import wave
        with wave.open(io.BytesIO(value["audio"])) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 16000))
            self.assertLessEqual(audio.getnframes() / audio.getframerate(), 120)

    def test_actual_silent_webm_has_no_invented_audio(self):
        value = inspect_reference_video(self.silent_video.read_bytes(), "clip.webm")
        self.assertEqual(len(value["frames"]), 8)
        self.assertIsNone(value["audio"])
        self.assertEqual((value["width"], value["height"]), (240, 320))

    def test_playlists_and_corrupt_media_cannot_open_remote_inputs(self):
        for media in (b"#EXTM3U\n#EXTINF:10,\nhttps://example.invalid/video.mp4", b"not-a-video"):
            with self.subTest(media=media[:15]), self.assertRaises(ReferenceError):
                inspect_reference_video(media, "disguised.mp4")

    def test_all_media_commands_use_local_protocol_and_demuxer_whitelists(self):
        original = subprocess.run
        commands = []
        def record(command, **kwargs):
            commands.append(command)
            return original(command, **kwargs)
        with patch("content_studio_reference.subprocess.run", side_effect=record):
            inspect_reference_video(self.audio_video.read_bytes(), "clip.mov")
        self.assertEqual(len(commands), 10)  # probe + eight frames + one WAV
        for command in commands:
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")
            self.assertEqual(command[command.index("-format_whitelist") + 1], "mov,matroska,webm")
            self.assertFalse(any("https://" in value or "http://" in value for value in command))

    def test_byte_filename_duration_resolution_and_stream_limits_precede_decode(self):
        for data, filename in ((b"", "clip.mp4"), (b"x" * (MAX_VIDEO_BYTES + 1), "clip.mp4"),
                               (b"video", "https://example.test/a.m3u8"), (b"video", "clip.mp4\x00")):
            with self.subTest(filename=filename), patch("content_studio_reference.subprocess.run") as process, self.assertRaises(ReferenceError):
                inspect_reference_video(data, filename)
            process.assert_not_called()
        valid = {"format": {"duration": "120.001"}, "streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080}]}
        for value in (valid, {**valid, "format": {"duration": "nan"}},
                      {**valid, "format": {"duration": "10"}, "streams": valid["streams"] * 9},
                      {"format": {"duration": "10"}, "streams": [{"index": 0, "codec_type": "video", "width": 8192, "height": 8192}]}):
            with self.subTest(metadata=value), patch("content_studio_reference._run", return_value=json.dumps(value).encode()) as run, self.assertRaises(ReferenceError):
                inspect_reference_video(b"fixture", "clip.mp4")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
