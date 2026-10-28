"""Manual video routes must fail before rendering without the official mark."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

from agency_video_branding import VideoBrandingError
from agency_dialogue_video import assemble_dialogue_mp4
from content_factory_ui import _assemble_ready_reel, _assemble_animated_reel, _render_vertical_frame


class ManualVideoBrandingTests(unittest.TestCase):
    def test_manual_page_still_opens_all_formats_without_generation(self):
        script = '''
from unittest.mock import patch
import content_factory_ui as ui
import content_factory_autopilot_ui as auto
def paid(*args, **kwargs):
    raise AssertionError("A page view must not generate media")
with patch.object(auto, "render_content_autopilot"), patch.object(ui, "_load_latest_package", return_value=None):
    ui._render_single_material(77, "Владелец", paid, paid, paid, paid)
'''
        app = AppTest.from_string(script, default_timeout=15).run()
        self.assertFalse(app.exception)
        app.button(key="cf2_start_77").click().run()
        self.assertFalse(app.exception)
        for format_ in ("post", "carousel", "reel", "dialogue"):
            app.radio(key="cf2_format_77").set_value(format_).run()
            self.assertFalse(app.exception)
            self.assertTrue(any(item.label == "Или напишите задачу" for item in app.text_area))
            self.assertTrue(any(item.label == "✨ Создать сценарий из моей идеи" for item in app.button))
            self.assertTrue(any(item.label == "Отмена" for item in app.button))

    def test_manual_image_and_motion_assembly_stop_before_render_when_mark_is_missing(self):
        for assemble in (_assemble_ready_reel, _assemble_animated_reel):
            with self.subTest(assemble=assemble.__name__), \
                 patch("content_factory_ui.shutil.which", return_value="ffmpeg"), \
                 patch("content_factory_ui.check_video_logo", side_effect=VideoBrandingError("Нет фирменного знака")), \
                 patch("content_factory_ui.subprocess.run") as render:
                with self.assertRaises(VideoBrandingError):
                    assemble([b"owned-source"], b"owned-voice", "Речь", "Действие")
                render.assert_not_called()

    def test_dialogue_default_requires_official_mark_before_render(self):
        with patch("agency_dialogue_video.check_video_logo", side_effect=VideoBrandingError("Нет фирменного знака")), \
             patch("agency_dialogue_video.subprocess.run") as render:
            with self.assertRaises(VideoBrandingError):
                assemble_dialogue_mp4(b"owned-talking-video")
            render.assert_not_called()

    def test_manual_frame_does_not_silently_export_without_requested_mark(self):
        source = io.BytesIO()
        Image.new("RGB", (108, 192), "navy").save(source, "PNG")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "оригинальный логотип"):
                _render_vertical_frame(source.getvalue(), root / "frame.jpg", root / "missing-logo.png")
            self.assertFalse((root / "frame.jpg").exists())


if __name__ == "__main__":
    unittest.main()
