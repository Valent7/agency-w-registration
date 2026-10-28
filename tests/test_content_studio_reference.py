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
    FRAME_COUNT, MAX_VIDEO_BYTES, MAX_VIDEO_SECONDS, MAX_AUDIO_BYTES, ReferenceError, analyze_reference_video,
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


def long_inspected():
    source = inspected()
    source.update(duration=540.0, audio=b"ID3-full-nine-minute-audio",
                  audio_filename="reference.mp3", audio_mime="audio/mpeg")
    source["frames"] = [{"seconds": moment, "jpeg": jpeg()} for moment in
                        (0, 1.5, 4.0, 108.0, 216.0, 324.0, 432.0, 523.8)]
    source["audio_segments"] = [
        {"audio": b"ID3" + bytes([index]) * 80, "filename": f"reference-part-{index + 1:02d}.mp3",
         "mime_type": "audio/mpeg", "start": float(index * 180), "end": float((index + 1) * 180)}
        for index in range(3)]
    return source


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
        for field, value in (("sha256", "../not-a-hash"), ("duration", float("nan")), ("duration", True), ("duration", 901)):
            with self.subTest(field=field, value=value):
                broken = report()
                broken["source"][field] = value
                with self.assertRaises(ReferenceError):
                    validate_reference_report(broken)
        broken = report()
        broken["reference_plan"]["text"] = "x" * 501
        with self.assertRaises(ReferenceError):
            validate_reference_report(broken)

    def test_long_source_full_transcript_is_used_for_an_independent_two_minute_output(self):
        generator = self.generator()
        full_speech = "Вступление. " + "Важная мысль о проекте. " * 900 + "Финальный аргумент в девятой минуте."
        texts = [full_speech[:8000], full_speech[8000:16000], full_speech[16000:]]
        responses = []
        for text in texts:
            response = Mock(status_code=200)
            response.json.return_value = {"text": text}
            responses.append(response)
        generator.session.post.side_effect = responses
        source = long_inspected()
        long_plan = copy.deepcopy(PLAN)
        long_plan.update(target_seconds=120, narration=" ".join(["действие"] * 210))
        long_plan["visual_plan"]["frames"] = [
            {**PLAN["visual_plan"]["frames"][0], "subject": f"Новое самостоятельное действие {i}."}
            for i in range(16)]
        generator._structured.return_value["reference_plan"] = long_plan
        with patch("content_studio_reference.inspect_reference_video", return_value=source):
            output = analyze_reference_video(generator, PROJECT, b"upload", target_seconds=120,
                                             goal="Покажи актуальность нашего Агентства")
        self.assertEqual(output["source"]["duration"], 540.0)
        self.assertEqual(output["reference_plan"]["target_seconds"], 120)
        self.assertEqual(len(output["reference_plan"]["visual_plan"]["frames"]), 16)
        self.assertEqual(output["transcript"], "\n\n".join(text.strip() for text in texts))
        self.assertEqual(generator.session.post.call_args.kwargs["files"]["file"],
                         ("reference-part-03.mp3", source["audio_segments"][2]["audio"], "audio/mpeg"))
        self.assertEqual([call.kwargs["files"]["file"][0] for call in generator.session.post.call_args_list],
                         [f"reference-part-{index:02d}.mp3" for index in (1, 2, 3)])
        payload = json.loads(generator._structured.call_args.args[1][0]["content"][0]["text"])
        self.assertEqual(payload["target_seconds"], 120)
        self.assertIn("Финальный аргумент в девятой минуте.", payload["source_transcript"])
        self.assertIn("ВСЕЙ распознанной речи", generator._structured.call_args.args[0])
        self.assertEqual(generator.session.post.call_count, 3)
        self.assertEqual(generator._structured.call_count, 1)
        self.assertEqual(output["source"]["audio_ranges"], [{"start": start, "end": start + 180}
                                                          for start in (0, 180, 360)])
        for response in responses:
            response.close.assert_called_once()
        brief = build_reference_brief(PROJECT, output, goal="Покажи актуальность Агентства")
        self.assertLessEqual(len(brief), 6000)
        self.assertIn(long_plan["narration"], brief)
        self.assertIn('"target_seconds": 120', brief)

    def test_incomplete_audio_coverage_is_rejected_before_any_paid_recognition(self):
        for defect in ("missing", "gap", "ending", "invalid-audio"):
            source = long_inspected()
            if defect == "missing":
                source["audio_segments"].pop()
            elif defect == "gap":
                source["audio_segments"][1]["start"] = 181
            elif defect == "ending":
                source["audio_segments"][2]["end"] = 539
            else:
                source["audio_segments"][2]["audio"] = b""
            with self.subTest(defect=defect), patch("content_studio_reference.inspect_reference_video", return_value=source):
                generator = self.generator()
                with self.assertRaises(ReferenceError):
                    analyze_reference_video(generator, PROJECT, b"upload")
                generator.session.post.assert_not_called()
                generator._structured.assert_not_called()

    def test_failed_segment_stops_without_repeating_paid_calls_or_analyzing_partial_source(self):
        generator = self.generator()
        first = Mock(status_code=200)
        first.json.return_value = {"text": "Вступление, первая часть."}
        generator.session.post.side_effect = [first, requests.ConnectionError("private-api-key")]
        with patch("content_studio_reference.inspect_reference_video", return_value=long_inspected()):
            with self.assertRaises(ReferenceError) as error:
                analyze_reference_video(generator, PROJECT, b"upload")
        self.assertNotIn("private-api-key", str(error.exception))
        self.assertEqual(generator.session.post.call_count, 2)
        generator._structured.assert_not_called()

    def test_source_duration_never_forces_same_output_duration_and_invalid_choice_is_free(self):
        generator = self.generator()
        source = inspected(audio=False)
        source["duration"] = 540.0
        with patch("content_studio_reference.inspect_reference_video", return_value=source):
            output = analyze_reference_video(generator, PROJECT, b"upload")
        self.assertNotIn("target_seconds", output["reference_plan"])
        self.assertEqual(output["reference_plan"]["narration"], NARRATION)
        for target in (None, True, 60, 900, "120"):
            with self.subTest(target=target), patch("content_studio_reference.inspect_reference_video") as inspect:
                generator = self.generator()
                with self.assertRaises(ReferenceError):
                    analyze_reference_video(generator, PROJECT, b"upload", target_seconds=target)
                inspect.assert_not_called()
                generator.session.post.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class LocalReferenceInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.audio_video = root / "audio.mp4"
        cls.silent_video = root / "silent.webm"
        cls.long_video = root / "nine-minute.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-f", "lavfi", "-i",
                        "testsrc2=size=320x240:rate=12:duration=1.2", "-f", "lavfi", "-i",
                        "sine=frequency=550:duration=1.2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(cls.audio_video)], check=True, timeout=30, capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-f", "lavfi", "-i",
                        "testsrc2=size=240x320:rate=8:duration=0.8", "-an", "-c:v", "libvpx-vp9",
                        "-cpu-used", "8", "-threads", "1", str(cls.silent_video)], check=True, timeout=30, capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:size=64x96:rate=1:duration=540", "-f", "lavfi", "-i",
                        "sine=frequency=550:duration=540", "-shortest", "-c:v", "libx264", "-threads", "1",
                        "-preset", "ultrafast", "-c:a", "aac", "-b:a", "24k", str(cls.long_video)],
                       check=True, timeout=60, capture_output=True)

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

    def test_actual_nine_minute_source_keeps_ending_audio_and_samples_whole_video(self):
        value = inspect_reference_video(self.long_video.read_bytes(), "nine-minutes.mp4")
        self.assertGreater(value["duration"], 539)
        self.assertEqual(len(value["frames"]), FRAME_COUNT)
        self.assertGreater(value["frames"][-1]["seconds"], 520)
        self.assertEqual(value["audio_filename"], "reference.mp3")
        self.assertEqual(value["audio_mime"], "audio/mpeg")
        self.assertLessEqual(len(value["audio"]), MAX_AUDIO_BYTES)
        audio_path = Path(self.directory.name) / "extracted-nine-minutes.mp3"
        audio_path.write_bytes(value["audio"])
        probe = json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=sample_rate,channels",
            "-of", "json", str(audio_path)], check=True, capture_output=True, timeout=15).stdout)
        self.assertGreater(float(probe["format"]["duration"]), 539)
        self.assertEqual(probe["streams"][0]["channels"], 1)
        self.assertEqual(probe["streams"][0]["sample_rate"], "16000")
        self.assertEqual(len(value["audio_segments"]), 3)
        self.assertEqual([(part["start"], part["end"]) for part in value["audio_segments"]],
                         [(0.0, 180.0), (180.0, 360.0), (360.0, 540.0)])
        for index, part in enumerate(value["audio_segments"]):
            part_path = Path(self.directory.name) / f"extracted-part-{index}.mp3"
            part_path.write_bytes(part["audio"])
            duration = float(subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                str(part_path)], check=True, capture_output=True, timeout=15).stdout)
            self.assertAlmostEqual(duration, 180, delta=.2)

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
        valid = {"format": {"duration": str(MAX_VIDEO_SECONDS + .001)}, "streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080}]}
        for value in (valid, {**valid, "format": {"duration": "nan"}},
                      {**valid, "format": {"duration": "10"}, "streams": valid["streams"] * 9},
                      {"format": {"duration": "10"}, "streams": [{"index": 0, "codec_type": "video", "width": 8192, "height": 8192}]}):
            with self.subTest(metadata=value), patch("content_studio_reference._run", return_value=json.dumps(value).encode()) as run, self.assertRaises(ReferenceError):
                inspect_reference_video(b"fixture", "clip.mp4")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
