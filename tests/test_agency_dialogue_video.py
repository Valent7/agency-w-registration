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
from agency_outro import INTRO_SECONDS, OUTRO_SECONDS, render_agency_intro, render_agency_outro, append_agency_outro

from agency_dialogue_video import (
    _call_overlay, assemble_dialogue_mp4, build_heygen_dialogue_payload,
    create_dialogue_video, dialogue_errors, dialogue_text, normalise_dialogue_turns,
    parse_dialogue_editor, validate_cast, list_heygen_catalog,
    suggest_dialogue_cast, suggest_partner_dialogue_cast, partner_dialogue_catalog,
    duration_warning, estimate_dialogue_seconds,
    voice_ready_text,
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
    def test_shared_content_factory_finishing_files_compile(self):
        files = (
            "agency_outro.py", "content_factory_core.py", "content_factory_generation.py",
            "content_factory_live_media.py", "content_factory_media.py", "content_factory_worker.py",
            "content_studio_reference_ui.py", "content_studio_ui.py", "content_studio_music_ui.py",
        )
        for filename in files:
            with self.subTest(filename=filename):
                source = Path(filename).read_text(encoding="utf-8")
                compile(source, filename, "exec")

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

    def test_brand_name_is_said_dabbl_u_but_script_still_says_w(self):
        source = "Зачем мне Агентство W? Я работаю в Агентстве W."
        self.assertIn("Агентство дабл-ю", voice_ready_text(source))
        self.assertIn("Агентстве дабл-ю", voice_ready_text(source))
        self.assertIn("Агентство W", source)
        edited = [{"speaker": x["speaker"], "text": x["text"]} for x in TURNS]
        edited[0]["text"] = "Зачем мне Агентство W?"
        payload = build_heygen_dialogue_payload(edited, CAST)
        self.assertIn("дабл-ю", payload["video_inputs"][0]["voice"]["input_text"])
        self.assertEqual(edited[0]["text"], "Зачем мне Агентство W?")

    def test_under_one_minute_and_both_orientation_options(self):
        self.assertLess(estimate_dialogue_seconds(TURNS), 60)
        landscape = build_heygen_dialogue_payload(
            TURNS, CAST, is_test=True, aspect_ratio="16:9",
        )
        self.assertEqual(landscape["dimension"], {"width": 1280, "height": 720})
        with self.assertRaises(ValueError):
            build_heygen_dialogue_payload(
                TURNS, CAST, aspect_ratio="1:1",
            )
        long_script = [
            {"speaker": t["speaker"], "text": (t["text"] + " ") * 12}
            for t in TURNS
        ]
        self.assertTrue(duration_warning(long_script))
        with patch("agency_dialogue_video.requests.post") as post:
            result = create_dialogue_video(
                "key", long_script, CAST, aspect_ratio="16:9",
            )
            self.assertFalse(result["ok"])
            post.assert_not_called()

    def test_landscape_brand_cards_contain_real_logo(self):
        with tempfile.TemporaryDirectory() as folder:
            logo = Path(folder) / "crest.png"
            Image.new("RGBA", (64, 64), (17, 234, 200, 255)).save(logo)
            with self._mock_branded_template():
                opening = Image.open(io.BytesIO(render_agency_intro(
                    "Зачем мне Агентство W?", logo, size=(1280, 720),
                )))
                closing = Image.open(io.BytesIO(render_agency_outro(
                    logo, size=(1280, 720),
                )))
            self.assertEqual(opening.size, (1280, 720))
            self.assertEqual(closing.size, (1280, 720))
            self.assertNotEqual(opening.tobytes(), closing.tobytes())

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

    def test_partner_film_never_uses_theo_or_stagirit_as_customer(self):
        catalog = {
            "avatars": [
                {"id": "theo", "name": "Theo", "gender": "Man"},
                {"id": "stag", "name": "Стагирит", "gender": "Man"},
                {"id": "human", "name": "Dante Office 1", "gender": "male"},
                {"id": "neonia", "name": "Неония", "gender": "Woman"},
                {"id": "other", "name": "Office Woman", "gender": "female"},
            ],
            "voices": [
                {"id": "oleg", "name": "Олег", "gender": "male", "language": "Russian"},
                {"id": "nadia", "name": "Надя", "gender": "female", "language": "Russian"},
            ],
        }
        cast = suggest_partner_dialogue_cast(
            catalog,
            preferred={"A": {"avatar_id": "theo", "voice_id": "oleg"},
                       "B": {"avatar_id": "other", "voice_id": "nadia"}},
        )
        self.assertEqual(cast["A"]["avatar_id"], "human")
        self.assertEqual(cast["B"]["avatar_id"], "neonia")
        self.assertEqual(validate_cast(cast), [])
        self.assertEqual(len(partner_dialogue_catalog(catalog)["avatars"]), 2)

    def test_partner_film_blocks_when_no_human_or_no_w_agent(self):
        c = {
            "avatars": [{"id": "theo", "name": "Theo", "gender": "male"},
                        {"id": "other", "name": "Other Woman", "gender": "female"}],
            "voices": [
                {"id": "v1", "name": "Oleg", "gender": "male", "language": "Russian"},
                {"id": "v2", "name": "Nadia", "gender": "female", "language": "Russian"},
            ],
        }
        self.assertTrue(validate_cast(suggest_partner_dialogue_cast(c)))

    def test_dialogue_both_scenes_have_dark_background(self):
        payload = build_heygen_dialogue_payload(TURNS, CAST)
        for v in payload["video_inputs"]:
            self.assertEqual(v["background"]["value"], "#142839")

    def test_call_overlay_is_png(self):
        img = Image.open(io.BytesIO(_call_overlay()))
        self.assertEqual(img.size, (720, 1280))
        self.assertEqual(img.mode, "RGBA")

    def _mock_branded_template(self):
        # The approved image is normally loaded read-only from the owner's
        # HeyGen Assets. Tests must never access paid or live endpoints.
        bg = Image.new("RGB", (941, 1672), (12, 25, 48))
        buf = io.BytesIO()
        bg.save(buf, format="PNG")
        return patch("agency_outro._fetch_template", return_value=buf.getvalue())

    def test_approved_cards_are_9x16_and_title_changes_per_film(self):
        with tempfile.TemporaryDirectory() as folder:
            logo = Path(folder) / "original.png"
            Image.new("RGBA", (64, 64), (17, 234, 200, 255)).save(logo)
            with self._mock_branded_template():
                end = Image.open(io.BytesIO(render_agency_outro(logo)))
                first = Image.open(io.BytesIO(render_agency_intro(
                    "Зачем мне Агентство W?", logo
                )))
                second = Image.open(io.BytesIO(render_agency_intro(
                    "Не охваты, а партнёры!", logo
                )))
            self.assertEqual(end.size, (720, 1280))
            self.assertEqual(first.size, (720, 1280))
            self.assertEqual(INTRO_SECONDS, 2.0)
            self.assertEqual(OUTRO_SECONDS, 2.0)
            self.assertNotEqual(first.tobytes(), second.tobytes())
            self.assertEqual(end.getpixel((360, 900)), (12, 25, 48))
            with self.assertRaises(ValueError):
                render_agency_intro("", logo)

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
            with self._mock_branded_template():
                branded.write_bytes(append_agency_outro(
                    source.read_bytes(), logo_path=logo, size=(720, 1280), title='Где искать партнёров?'
                ))
            def duration(path):
                result = subprocess.run(
                    [shutil.which("ffprobe"), "-v", "quiet",
                     "-show_entries", "format=duration",
                     "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                    capture_output=True, text=True, check=True, timeout=10,
                )
                return float(result.stdout.strip())
            self.assertAlmostEqual(duration(branded) - duration(source), 4.0, delta=0.25)
            media = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error",
                 "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", str(branded)],
                capture_output=True, text=True, check=True, timeout=10,
            )
            self.assertIn("audio", media.stdout)
            self.assertIn("video", media.stdout)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_landscape_mp4_from_existing_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "source.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=12:d=0.65",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.65",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(raw)
            ], check=True, timeout=30)
            with self._mock_branded_template():
                output = assemble_dialogue_mp4(
                    raw.read_bytes(), title="Как найти партнёра?",
                    aspect_ratio="16:9",
                )
            dest = Path(folder) / "out.mp4"
            dest.write_bytes(output)
            probe = subprocess.run([
                shutil.which("ffprobe"), "-v", "error",
                "-show_entries", "stream=codec_type,width,height",
                "-of", "csv=p=0", str(dest),
            ], capture_output=True, text=True, check=True, timeout=10)
            self.assertIn("1280,720", probe.stdout)
            self.assertIn("audio", probe.stdout)

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
            with self._mock_branded_template():
                rendered = assemble_dialogue_mp4(raw.read_bytes(), title="Зачем мне Агентство W?")
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
