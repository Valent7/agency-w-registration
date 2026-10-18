"""Offline tests: Agency W clean-up never sends media to an external service."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from content_video_cleanup import (
    VideoCleanupError,
    _delogo_filters,
    clean_user_video,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class VideoCleanupTests(unittest.TestCase):
    def _source(self):
        source = Path(self.folder) / "input.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x576:rate=12:duration=2.8",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2.8",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
            ], check=True, timeout=45,
        )
        return source.read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def _streams(self, video):
        path = Path(self.folder) / "test_output.mp4"
        path.write_bytes(video)
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=12,
        )
        return json.loads(p.stdout)

    def test_two_side_retouch_preserves_audio_and_vertical_frame(self):
        output, details = clean_user_video(
            self._source(), mask_mode="both", position_pct=82,
        )
        data = self._streams(output)
        self.assertEqual((details["width"], details["height"]), (320, 576))
        self.assertTrue(any(s.get("codec_type") == "audio" for s in data["streams"]))
        self.assertTrue(any(s.get("codec_type") == "video"
                            and s.get("width") == 320
                            and s.get("height") == 576 for s in data["streams"]))
        self.assertAlmostEqual(float(data["format"]["duration"]), 2.8, delta=0.3)

    def test_end_trim_preserves_audio_and_no_unrequested_brand(self):
        output, details = clean_user_video(
            self._source(), mask_mode="none", trim_end_seconds=1,
        )
        self.assertLess(abs(float(self._streams(output)["format"]["duration"]) - 1.8), 0.3)
        self.assertAlmostEqual(details["estimated_duration"], 1.8, delta=0.15)

    def test_invalid_file_and_unsafe_parameters(self):
        source = self._source()
        with self.assertRaises(VideoCleanupError):
            clean_user_video(b"not a video")
        with self.assertRaises(VideoCleanupError):
            clean_user_video(source, mask_mode="none")
        with self.assertRaises(VideoCleanupError):
            clean_user_video(source, filename="file.exe")
        with self.assertRaises(VideoCleanupError):
            clean_user_video(source, position_pct=99)
        with self.assertRaises(VideoCleanupError):
            clean_user_video(source, trim_end_seconds=20)

    def test_selected_masks_are_local_and_not_cropping(self):
        both = _delogo_filters(720, 1280, "both", 82)
        self.assertEqual(both.count("delogo="), 2)
        self.assertEqual(_delogo_filters(720, 1280, "none", 82), "")
        self.assertEqual(_delogo_filters(720, 1280, "left", 82).count("delogo="), 1)

    def test_content_factory_keeps_original_generator_untouched(self):
        ui = Path("content_factory_ui.py").read_text(encoding="utf-8")
        cleaner = Path("content_video_cleanup_ui.py").read_text(encoding="utf-8")
        self.assertIn('"✨ Создать контент", "🧹 Очистить видео"', ui)
        self.assertIn("render_video_cleanup(owner_id)", ui)
        self.assertIn("if choice == \"🧹 Очистить видео\":", ui)
        self.assertIn("st.download_button(", cleaner)
        self.assertIn("Это мой ролик или у меня есть разрешение", cleaner)


if __name__ == "__main__":
    unittest.main()
