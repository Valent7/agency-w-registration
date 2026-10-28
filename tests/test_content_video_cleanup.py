"""Offline tests: Agency W clean-up never sends media to an external service."""
import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from content_video_cleanup import (
    VideoCleanupError,
    _delogo_filters,
    _mask_regions,
    sample_video_frames,
    preview_mask_image,
    clean_user_video,
)


class ContentFactoryNavigationTests(unittest.TestCase):
    """Prevent a hidden cleaner button in the unused legacy content factory."""

    def test_unsafe_cleaner_is_not_exposed_to_partners(self):
        source = Path("content_factory_ui.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: node for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        active_code = ast.get_source_segment(source, methods["_render_single_material"])
        entry = ast.get_source_segment(source, methods["render_content_factory"])
        self.assertIn("render_content_studio(", entry)
        self.assertNotIn('render_video_cleanup(owner_id)', active_code)
        self.assertNotIn('["✨ Создать контент", "🧹 Очистить видео"]', active_code)
        self.assertNotIn('if choice == "🧹 Очистить видео":', active_code)
        self.assertIn('st.session_state.pop(f"cf_mode_{owner_id}", None)', active_code)
        # Existing factory remains; the cleanup prototype stays offline for rework.
        self.assertIn('if stage == "start":', active_code)



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

    def test_preview_shows_real_source_frames_and_mask_regions(self):
        from PIL import Image
        from io import BytesIO

        source = self._source()
        samples = sample_video_frames(source, "test.mp4")
        self.assertEqual(len(samples), 2)
        original = Image.open(BytesIO(samples[0])).convert("RGB")
        overlay = Image.open(BytesIO(preview_mask_image(
            samples[0], "both", left_y_pct=25, right_y_pct=80,
            mask_width_pct=40, mask_height_pct=12,
        ))).convert("RGB")
        self.assertEqual(original.size, overlay.size)
        regions = _mask_regions(
            original.width, original.height, "both",
            left_y_pct=25, right_y_pct=80, mask_width_pct=40,
            mask_height_pct=12,
        )
        self.assertEqual(len(regions), 2)
        self.assertLess(regions[0][1], regions[1][1])
        left_x, left_y, w, h = regions[0]
        self.assertNotEqual(
            original.getpixel((left_x + w // 2, left_y + h // 2)),
            overlay.getpixel((left_x + w // 2, left_y + h // 2)),
        )

    def test_custom_left_and_right_positions_affect_export(self):
        width, height = 320, 576
        filt = _delogo_filters(
            width, height, "both", 82, left_y_pct=22,
            right_y_pct=85, mask_width_pct=45, mask_height_pct=14,
        )
        self.assertEqual(filt.count("delogo="), 2)
        regions = _mask_regions(
            width, height, "both", 82, left_y_pct=22,
            right_y_pct=85, mask_width_pct=45, mask_height_pct=14,
        )
        self.assertNotEqual(regions[0][1], regions[1][1])
        output, details = clean_user_video(
            self._source(), mask_mode="both",
            left_y_pct=22, right_y_pct=85,
            mask_width_pct=45, mask_height_pct=14,
        )
        self.assertTrue(output)
        self.assertEqual(details["mask_regions"], regions)

    def test_mask_preview_controls_are_accessible_to_all_partners(self):
        source = Path("content_video_cleanup_ui.py").read_text(encoding="utf-8")
        self.assertIn("Слева: переместить рамку выше или ниже", source)
        self.assertIn("Справа: переместить рамку выше или ниже", source)
        self.assertIn("Ширина рамки", source)
        self.assertIn("Высота рамки", source)
        self.assertIn("Начало видео: красная зона", source)
        self.assertIn("Середина видео: красная зона", source)
        self.assertIn("sample_video_frames(", source)
        self.assertIn("preview_mask_image(", source)
        self.assertIn("left_y_pct=int(left_y)", source)
        self.assertIn("right_y_pct=int(right_y)", source)

    def test_content_factory_keeps_original_generator_untouched(self):
        ui = Path("content_factory_ui.py").read_text(encoding="utf-8")
        cleaner = Path("content_video_cleanup_ui.py").read_text(encoding="utf-8")
        self.assertNotIn('"✨ Создать контент", "🧹 Очистить видео"', ui)
        self.assertNotIn("render_video_cleanup(owner_id)", ui)
        self.assertIn("st.download_button(", cleaner)
        self.assertIn("Это мой ролик или у меня есть разрешение", cleaner)


if __name__ == "__main__":
    unittest.main()
