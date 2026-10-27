"""Real moving-footage composition plus bounded media validation."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from agency_video_branding import LOGO_PATH
from content_factory_live_media import LiveMediaError, render_live_reel, scene_durations, speech_duration


def voice(seconds):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * round(seconds * 16000))
    return output.getvalue()


class DurationTests(unittest.TestCase):
    def test_paid_scene_lengths_cover_voice_and_are_provider_compatible(self):
        for duration, count in ((0.3, 3), (19.7, 4), (37.71, 4), (49.5, 5), (59.5, 6)):
            result = scene_durations(duration, count)
            self.assertEqual(len(result), count)
            self.assertTrue(all(type(v) is int and 2 <= v <= 10 for v in result))
            self.assertGreaterEqual(sum(result), duration + 0.35)
        with self.assertRaises(LiveMediaError):
            scene_durations(37.71, 3)

    def test_invalid_lengths_and_counts_are_rejected(self):
        for duration in (True, 0.29, 60, float("nan"), float("inf"), "38"):
            with self.subTest(duration=duration), self.assertRaises(LiveMediaError):
                scene_durations(duration, 4)
        for count in (True, 2, 7, 4.0):
            with self.subTest(count=count), self.assertRaises(LiveMediaError):
                scene_durations(20, count)

    def test_source_limits_stop_before_a_media_process(self):
        with patch("content_factory_live_media.subprocess.run") as run:
            for clips in ([b"clip"] * 2, [b"clip"] * 7, [b""] * 3,
                          [b"a" * (25 * 1024 * 1024 + 1)] * 3,
                          [b"a" * (21 * 1024 * 1024)] * 5):
                with self.assertRaises(LiveMediaError):
                    render_live_reel(clips, voice(1), "Речь.", "Тема", "Агентство W", "#D4AF37")
            run.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.clips = []
        for index, hue in enumerate((0, 90, 180)):
            path = cls.root / f"input-{index}.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-filter_threads", "1",
                "-f", "lavfi", "-i", "testsrc2=size=144x256:rate=24:duration=1.5",
                "-vf", f"hue=h={hue}", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", str(path),
            ], capture_output=True, timeout=30, check=True)
            cls.clips.append(path.read_bytes())
        cls.aac_path = cls.root / "original-voice.m4a"
        subprocess.run([
            shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.65", "-c:a", "aac",
            "-b:a", "64k", "-threads", "1", str(cls.aac_path),
        ], capture_output=True, timeout=30, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def frame(self, path, seconds):
        result = subprocess.run([
            shutil.which("ffmpeg"), "-v", "error", "-ss", str(seconds), "-i", str(path),
            "-vf", "scale=64:112", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], capture_output=True, timeout=20, check=True)
        self.assertEqual(len(result.stdout), 64 * 112 * 3)
        return result.stdout

    def test_real_moving_sources_remain_moving_with_audio_and_subtitles(self):
        output = render_live_reel(self.clips, voice(2.65),
                                  "Человек закрывает ноутбук и идёт к семье.",
                                  "Верните себе вечер", "Агентство W", "#D4AF37")
        path = self.root / "result.mp4"
        path.write_bytes(output)
        probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                                "-of", "json", str(path)], capture_output=True, timeout=20, check=True)
        data = json.loads(probe.stdout)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["codec_name"]), (1080, 1920, "h264"))
        self.assertTrue(any(s["codec_type"] == "audio" for s in data["streams"]))
        self.assertAlmostEqual(float(data["format"]["duration"]), 3.0, delta=0.09)
        first, moving, next_scene = (self.frame(path, t) for t in (0.15, 0.75, 1.4))
        # The first scene is the same moving source, not a generated pan over a
        # still. Its inner pattern changes over time; the next real clip differs.
        self.assertGreater(sum(abs(a - b) for a, b in zip(first, moving)) / len(first), 1)
        self.assertGreater(sum(abs(a - b) for a, b in zip(moving, next_scene)) / len(first), 8)

    def test_short_sources_are_not_looped_or_slowed_to_fill_narration(self):
        with self.assertRaisesRegex(LiveMediaError, "короче"):
            render_live_reel(self.clips, voice(5), "Длинная речь для слишком коротких сцен.",
                             "Вечер", "Агентство W", "#D4AF37")

    def test_m4a_aac_soundtrack_is_copied_without_reencoding_or_audio_padding(self):
        original = self.aac_path.read_bytes()
        self.assertAlmostEqual(speech_duration(original), 2.65, delta=0.01)
        output = render_live_reel(self.clips, original, "Сохраняем исходный голос без потери качества.",
                                  "Живые сцены", "Агентство W", "#D4AF37")
        final = self.root / "aac-preserved.mp4"
        final.write_bytes(output)

        def packets(path):
            result = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-select_streams", "a:0",
                "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=size,data_hash",
                "-of", "json", str(path),
            ], capture_output=True, timeout=20, check=True)
            return [(packet["size"], packet["data_hash"]) for packet in json.loads(result.stdout)["packets"]]

        source_packets, final_packets = packets(self.aac_path), packets(final)
        self.assertGreater(len(source_packets), 10)
        self.assertEqual(source_packets, final_packets)

        probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                                "-of", "json", str(final)], capture_output=True, timeout=20, check=True)
        metadata = json.loads(probe.stdout)
        audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertAlmostEqual(float(audio["duration"]), 2.65, delta=0.01)
        self.assertGreater(float(metadata["format"]["duration"]), float(audio["duration"]) + 0.30)

        def pcm(path):
            return subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0",
                "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
            ], capture_output=True, timeout=20, check=True).stdout

        self.assertEqual(pcm(self.aac_path), pcm(final))
        frame = subprocess.run([
            shutil.which("ffmpeg"), "-v", "error", "-ss", "1.3", "-i", str(final),
            "-vf", "crop=144:144:54:54", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ], capture_output=True, timeout=20, check=True)
        observed = Image.open(io.BytesIO(frame.stdout)).convert("RGB")
        official = Image.open(LOGO_PATH).convert("RGBA").resize((144, 144), Image.Resampling.LANCZOS)
        opaque = [(source[:3], actual) for source, actual in zip(official.getdata(), observed.getdata()) if source[3] > 240]
        self.assertGreater(len(opaque), 400)
        self.assertLess(sum(sum(abs(a-b) for a,b in zip(source, actual)) / 3
                            for source, actual in opaque) / len(opaque), 35)

    def test_still_image_and_remote_playlist_are_not_video_sources(self):
        for bad in (b"\x89PNG\r\n\x1a\n", b"#EXTM3U\nhttp://127.0.0.1/private\n"):
            with self.subTest(bad=bad), self.assertRaises(LiveMediaError):
                render_live_reel([bad] * 3, voice(1), "Речь.", "Вечер", "Агентство W", "#D4AF37")

    def test_speech_duration_and_invalid_audio(self):
        self.assertAlmostEqual(speech_duration(voice(1.25)), 1.25, delta=0.01)
        for bad in (b"", b"not audio", voice(0.2)):
            with self.subTest(size=len(bad)), self.assertRaises(LiveMediaError):
                speech_duration(bad)

    def test_owner_text_cannot_inject_subtitle_events(self):
        from content_factory_live_media import _scene_captions
        path = self.root / "unsafe.ass"
        _scene_captions(path, [(0, 1, "Текст")], 0, 1, "{\\pos(0,0)}\nDialogue: evil", "W{\\an1}", "#D4AF37")
        text = path.read_text()
        self.assertNotIn("{\\pos(0,0)}", text)
        self.assertNotIn("W{\\an1}", text)
        self.assertEqual(sum(line.startswith("Dialogue:") for line in text.splitlines()), 3)

    def test_official_agency_mark_replaces_text_badge_but_partner_brand_remains(self):
        from content_factory_live_media import _scene_captions
        path = self.root / "brands.ass"
        for brand, expected_count in (("Агентство W", 0), ("Partner Brand", 1)):
            _scene_captions(path, [(0, 1, "Текст")], 0, 1, "Название", brand, "#D4AF37")
            self.assertEqual(sum(",Brand," in line for line in path.read_text().splitlines()
                                 if line.startswith("Dialogue:")), expected_count)
            self.assertIn("90,90,240,1", path.read_text())
            if brand == "Агентство W":
                self.assertIn("8,90,90,218,1", path.read_text())


if __name__ == "__main__":
    unittest.main()
