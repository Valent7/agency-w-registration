"""No paid APIs, external uploads, or unsafe generic delogo in owner cleanup test."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_cleanup_pilot import PilotError, recognized_reference, make_test_preview


class OwnerOnlyPilotTests(unittest.TestCase):
    def test_unknown_video_rejected_before_processing(self):
        with self.assertRaises(PilotError):
            make_test_preview(b"not the example")
        self.assertFalse(recognized_reference(b"wrong file"))

    def test_active_factory_shows_owner_pilot_but_not_old_cleaner(self):
        factory = Path("content_factory_ui.py").read_text(encoding="utf-8")
        source = factory[factory.index("def render_content_factory("):]
        self.assertIn("render_owner_video_cleanup_pilot(owner_id)", source)
        self.assertNotIn("render_video_cleanup(owner_id)", source)
        self.assertNotIn('["✨ Создать контент", "🧹 Очистить видео"]', source)
        ui = Path("video_cleanup_pilot_ui.py").read_text(encoding="utf-8")
        self.assertIn("if not _is_agency_owner(int(owner_id)):", ui)
        self.assertIn("recognized_reference(raw)", ui)
        self.assertIn("Скачать тестовый MP4", ui)

    def test_video_is_private_and_preview_bounded(self):
        src = Path("video_cleanup_pilot.py").read_text(encoding="utf-8")
        self.assertNotIn("requests.post(", src)
        self.assertNotIn("http://", src)
        self.assertNotIn("https://", src)
        self.assertIn("MAX_PREVIEW_SECONDS = 12.0", src)
        self.assertIn("Это мой ролик", Path("video_cleanup_pilot_ui.py").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_mp4_with_sound_short_preview(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("opencv unavailable")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "input.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc2=size=464x848:rate=30:duration=9.2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=9.2",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
            ], check=True, timeout=70)
            # Synthetic fixture contains no actual user data. We mock the
            # approved-file hash and total-length check to exercise the codec
            # pipeline in isolation, not to certify image quality.
            with patch("video_cleanup_pilot.recognized_reference", return_value=True), \
                 patch("video_cleanup_pilot._probe", return_value=(464,848,30.0,309.166)):
                preview, details = make_test_preview(
                    path.read_bytes(), start_second=0, duration_seconds=2,
                )
            dst = Path(work) / "out.mp4"
            dst.write_bytes(preview)
            p = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,width,height:format=duration",
                "-of", "json", str(dst),
            ], capture_output=True, text=True, check=True, timeout=15)
            data = json.loads(p.stdout)
            self.assertTrue(any(s.get("codec_type")=="audio" for s in data["streams"]))
            self.assertTrue(any(
                s.get("codec_type")=="video" and s.get("width")==464 and
                s.get("height")==848 for s in data["streams"]
            ))
            self.assertAlmostEqual(float(data["format"]["duration"]), 2.0, delta=.2)
            self.assertEqual(details["frames"],60)


if __name__ == "__main__":
    unittest.main()
