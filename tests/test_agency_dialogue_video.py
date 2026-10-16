"""Offline tests for Agency W two-speaker AI-dialogue mode.

All network and paid generation endpoints are mocked.
"""
import io
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from agency_outro import OUTRO_SECONDS, render_agency_outro, append_agency_outro

from agency_dialogue_video import (
    _call_overlay, assemble_dialogue_mp4, build_heygen_dialogue_payload,
    create_dialogue_video, dialogue_errors, dialogue_text, normalise_dialogue_turns,
    parse_dialogue_editor, validate_cast, list_heygen_catalog,
    suggest_dialogue_cast,
)

TURNS = [
    {"speaker": "A", "text": "Скажи, а почему ты больше не сидишь вечерами с телефоном и не ищешь новых клиентов?"},
    {"speaker": "B", "text": "Потому что часть рутины теперь помогает выполнять моя команда искусственного интеллекта в Агентстве W."},
    {"speaker": "A", "text": "То есть тебе больше не приходится отвечать на сотни сообщений и напоминать о встречах самой?"},
    {"speaker": "B", "text": "Агенты помогают находить людей, поддерживать диалоги и организовывать встречи. А у меня появляется больше времени для жизни."},
]
CAST = {
    "A": {"avatar_id": "male_actor_1", "voice_id": "ru_voice_male_1"},
    "B": {"avatar_id": "female_actor_2", "voice_id": "ru_voice_female_2"},
}


class DialogueVideoTests(unittest.TestCase):
    def test_realistic_alternating_script_passes(self):
        self.assertEqual(dialogue_errors(TURNS), [])
        self.assertEqual(normalise_dialogue_turns(TURNS), TURNS)
        self.assertEqual(parse_dialogue_editor(dialogue_text(TURNS)), TURNS)

    def test_missing_role_and_non_alternation_blocked(self):
        bad = [dict(x) for x in TURNS]
        bad[1]["speaker"] = "A"
        self.assertTrue(dialogue_errors(bad))
        self.assertEqual(normalise_dialogue_turns([{"speaker": "C", "text": "Да"}]), [])

    def test_cast_two_distinct_voices_and_actors(self):
        self.assertEqual(validate_cast(CAST), [])
        self.assertTrue(validate_cast({"A": CAST["A"], "B": CAST["A"]}))

    def test_payload_single_job_with_four_alternating_scenes(self):
        request = build_heygen_dialogue_payload(TURNS, CAST, is_test=True)
        self.assertEqual(request["dimension"], {"width": 720, "height": 1280})
        self.assertTrue(request["test"])
        self.assertEqual(len(request["video_inputs"]), 4)
        avatar_sequence = [
            x["character"]["avatar_id"] for x in request["video_inputs"]
        ]
        self.assertEqual(avatar_sequence, ["male_actor_1", "female_actor_2"] * 2)
        self.assertEqual(
            request["video_inputs"][1]["voice"]["voice_id"], "ru_voice_female_2"
        )
        self.assertNotIn("партнёров", str(request))

    def test_no_paid_api_calls_when_cast_missing_or_script_bad(self):
        with patch("agency_dialogue_video.requests.post") as post:
            result = create_dialogue_video("key", TURNS, {"A": CAST["A"]}, is_test=False)
            self.assertFalse(result["ok"])
            result = create_dialogue_video("key", [], CAST, is_test=False)
            self.assertFalse(result["ok"])
            result = create_dialogue_video("", TURNS, CAST, is_test=False)
            self.assertFalse(result["ok"])
            post.assert_not_called()

    def test_create_sends_one_job_not_one_request_per_turn(self):
        response = Mock()
        response.ok = True
        response.content = b'{"data":{"video_id":"job123"}}'
        response.json.return_value = {"data": {"video_id": "job123"}}
        with patch("agency_dialogue_video.requests.post", return_value=response) as post:
            result = create_dialogue_video("private-key", TURNS, CAST, is_test=True)
        self.assertEqual(result["video_id"], "job123")
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.kwargs["json"]["test"])
        self.assertEqual(len(post.call_args.kwargs["json"]["video_inputs"]), 4)

    def test_unknown_api_response_never_auto_retries_paid_job(self):
        response = Mock()
        response.ok = True
        response.content = b'{}'
        response.json.return_value = {"data": {}}
        with patch("agency_dialogue_video.requests.post", return_value=response) as post:
            result = create_dialogue_video("private-key", TURNS, CAST, is_test=False)
        self.assertFalse(result["ok"])
        self.assertIn("Повторять", result["error"])
        post.assert_called_once()

    def test_catalog_is_read_only(self):
        avatars = Mock(ok=True)
        avatars.json.return_value = {
            "data": {"avatars": [
                {"avatar_id": "female_actor_2", "avatar_name": "Неона"},
                {"avatar_id": "male_actor_1", "avatar_name": "Мужчина"},
            ]}
        }
        voices = Mock(ok=True)
        voices.json.return_value = {
            "data": {"voices": [
                {"voice_id": "ru_voice_male_1", "name": "Мужской", "language": "Russian"},
                {"voice_id": "ru_voice_female_2", "name": "Женский", "language": "Russian"},
            ]}
        }
        with (
            patch("agency_dialogue_video.requests.get", side_effect=[avatars, voices]) as get,
            patch("agency_dialogue_video.requests.post") as post,
        ):
            result = list_heygen_catalog("key")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["avatars"]), 2)
        self.assertEqual(get.call_count, 2)
        post.assert_not_called()

    def test_automatic_cast_selects_two_russian_gender_matched_voices(self):
        catalog = {
            "avatars": [
                {"id": "female_person", "name": "Анна", "gender": "female"},
                {"id": "male_person", "name": "Андрей", "gender": "male"},
                {"id": "unknown", "name": "Кто-то", "gender": ""},
            ],
            "voices": [
                {"id": "female_ru", "name": "Русская женщина", "gender": "female", "language": "Russian"},
                {"id": "male_en", "name": "English male", "gender": "male", "language": "English"},
                {"id": "male_ru", "name": "Русский мужчина", "gender": "male", "language": "ru-RU"},
            ],
        }
        cast = suggest_dialogue_cast(catalog)
        self.assertEqual(
            cast, {"A": {"avatar_id": "male_person", "voice_id": "male_ru"},
                   "B": {"avatar_id": "female_person", "voice_id": "female_ru"}}
        )
        self.assertEqual(validate_cast(cast), [])

    def test_autocast_refuses_to_guess_gender_or_language(self):
        catalog = {
            "avatars": [{"id": "mystery", "name": "Model", "gender": ""}],
            "voices": [{"id": "english", "name": "English", "gender": "male", "language": "English"}],
        }
        cast = suggest_dialogue_cast(catalog)
        self.assertTrue(validate_cast(cast))
        self.assertFalse(cast["A"]["avatar_id"])
        self.assertFalse(cast["B"]["voice_id"])

    def test_automatic_cast_respects_saved_valid_characters(self):
        catalog = {
            "avatars": [
                {"id": "m1", "name": "Первый", "gender": "male"},
                {"id": "m2", "name": "Второй", "gender": "male"},
                {"id": "f1", "name": "Первая", "gender": "female"},
            ],
            "voices": [
                {"id": "mv", "name": "Рус", "gender": "male", "language": "Russian"},
                {"id": "fv", "name": "Рус", "gender": "female", "language": "Russian"},
            ],
        }
        cast = suggest_dialogue_cast(
            catalog, {"A": {"avatar_id": "m2", "voice_id": "mv"}}
        )
        self.assertEqual(cast["A"]["avatar_id"], "m2")
        self.assertEqual(cast["B"]["voice_id"], "fv")

    def test_call_overlay_is_png(self):
        img = Image.open(io.BytesIO(_call_overlay()))
        self.assertEqual(img.size, (720, 1280))
        self.assertEqual(img.mode, "RGBA")

    def test_end_card_uses_real_supplied_crest_and_cyrillic_text(self):
        with tempfile.TemporaryDirectory() as folder:
            logo = Path(folder) / "original.png"
            # No AI-generated substitute logo. Renderer must use this exact input.
            original = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            original.paste((17, 234, 200, 255), (22, 22, 42, 42))
            original.save(logo)
            frame = Image.open(io.BytesIO(render_agency_outro(logo)))
            self.assertEqual(frame.size, (720, 1280))
            # Exact arbitrary color from the supplied logo is present at its center.
            self.assertEqual(frame.convert("RGB").getpixel((360, 257)), (17, 234, 200))
            self.assertEqual(OUTRO_SECONDS, 3.0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_brand_is_after_complete_video_and_preserves_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "original.mp4"
            logo = folder / "logo.png"
            Image.new("RGBA", (64, 64), (233, 180, 25, 255)).save(logo)
            subprocess.run(
                [
                    shutil.which("ffmpeg"), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=30:d=1.1",
                    "-f", "lavfi", "-i", "sine=frequency=600:duration=1.1",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ],
                check=True, timeout=35,
            )
            branded = folder / "branded.mp4"
            branded.write_bytes(append_agency_outro(
                source.read_bytes(), logo_path=logo, size=(720, 1280)
            ))
            def duration(path):
                result = subprocess.run(
                    [shutil.which("ffprobe"), "-v", "quiet",
                     "-show_entries", "format=duration",
                     "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                    capture_output=True, text=True, check=True, timeout=10,
                )
                return float(result.stdout.strip())
            self.assertAlmostEqual(duration(branded) - duration(source), 3.0, delta=0.25)
            media = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error",
                 "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", str(branded)],
                capture_output=True, text=True, check=True, timeout=10,
            )
            self.assertIn("audio", media.stdout)
            self.assertIn("video", media.stdout)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_export_preserves_audio_and_expected_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "source.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg"), "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=720x1280:r=12:d=0.75",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.75",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(raw)
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30,
            )
            rendered = assemble_dialogue_mp4(raw.read_bytes())
            self.assertGreater(len(rendered), 2048)
            result = Path(folder) / "rendered.mp4"
            result.write_bytes(rendered)
            probe = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error",
                 "-show_entries", "stream=codec_type,width,height",
                 "-of", "csv=p=0", str(result)],
                capture_output=True, text=True, timeout=10, check=True,
            )
            self.assertIn("720,1280", probe.stdout)
            self.assertIn("audio", probe.stdout)


if __name__ == "__main__":
    unittest.main()
