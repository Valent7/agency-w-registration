"""Check readable layouts and the actual three-scene video, without services."""
import io
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

from agency_video_branding import LOGO_PATH
from content_factory_media import (
    MediaError, _caption_events, _write_scene_captions, reletter_slide, render_slide, render_story_reel,
)


def artwork(color=(204, 80, 60)):
    output = io.BytesIO()
    Image.new("RGB", (1080, 1920), color).save(output, format="PNG")
    return output.getvalue()


def speech(seconds=3.2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"".join(struct.pack("<h", round(5000 * math.sin(i * 2 * math.pi * 220 / 16000)))
                                   for i in range(round(16000 * seconds))))
    return buffer.getvalue()


class EditorialSlideTests(unittest.TestCase):
    def test_layouts_keep_artwork_and_use_distinct_readable_text_treatments(self):
        images = {}
        for layout in ("cover", "editorial", "clean"):
            result = render_slide(artwork(), "Верните себе вечер", "Агентство W", "#D4AF37",
                                  slide_number="1 / 4", layout=layout)
            image = Image.open(io.BytesIO(result))
            self.assertEqual(image.size, (1080, 1350))
            center = image.getpixel((540, 500))
            self.assertTrue(all(abs(left - right) < 5 for left, right in zip(center, (204, 80, 60))))
            images[layout] = image.getpixel((25, 1325))
        self.assertLess(sum(images["cover"]), 140)
        self.assertGreater(sum(images["clean"]), 700)
        self.assertGreater(images["editorial"][0], images["cover"][0])

    def test_unfittable_text_and_invalid_inputs_fail_before_video_processes(self):
        with self.assertRaises(MediaError):
            render_slide(artwork(), "длинно " * 100, "Агентство W", "#D4AF37", size=(540, 540))
        for kwargs in ({"layout": "<script>"}, {"size": (10000, 10000)}, {"brand_color": "red"}):
            values = {"brand_color": "#D4AF37", **kwargs}
            with self.assertRaises(MediaError):
                render_slide(artwork(), "Заголовок", "Агентство W", **values)
        with self.assertRaises(MediaError):
            render_slide(b"not an image", "Заголовок", "Агентство W", "#D4AF37")

    def test_long_unbroken_words_are_wrapped_without_cropping(self):
        result = render_slide(artwork(), "ОченьДлинноеСловоБезПробелов" * 4,
                              "Агентство W", "#D4AF37")
        self.assertEqual(Image.open(io.BytesIO(result)).size, (1080, 1350))

    def test_caption_boundaries_are_relative_to_each_scene_and_cannot_inject_ass(self):
        events = _caption_events("Начало {\\pos(1,2)} этой сцены и продолжение следующей сцены", 4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            _write_scene_captions(path, events, 2, 4)
            contents = path.read_text()
        self.assertNotIn("{\\pos", contents)
        self.assertIn("Dialogue: 0,0:00:00.00", contents)
        self.assertNotIn("0:00:04.00", contents)


class MultiSceneVideoTests(unittest.TestCase):
    def test_missing_tools_and_bad_scene_counts_fail_without_subprocesses(self):
        for images in ([artwork()], [artwork()] * 6, "not a list"):
            with self.assertRaises(MediaError):
                render_story_reel(images, speech(), "Текст", "Название", "Агентство W", "#D4AF37")
        with patch("content_factory_media.shutil.which", return_value=None), patch(
                "content_factory_media.subprocess.run") as run:
            with self.assertRaises(MediaError):
                render_story_reel([artwork()] * 3, speech(), "Текст", "Название", "Агентство W", "#D4AF37")
            run.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_video_has_three_distinct_scenes_audio_and_vertical_resolution(self):
        colors = [(210, 35, 30), (25, 190, 55), (35, 50, 210)]
        result = render_story_reel([artwork(c) for c in colors], speech(),
                                   "Одна задача. Несколько понятных шагов. Время для себя.",
                                   "Верните себе вечер", "Агентство W", "#D4AF37")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.mp4").write_bytes(result)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                    "-of", "json", "result.mp4"], cwd=root,
                                   capture_output=True, check=True, timeout=15)
            details = json.loads(probe.stdout)
            video = next(stream for stream in details["streams"] if stream["codec_type"] == "video")
            audio = next(stream for stream in details["streams"] if stream["codec_type"] == "audio")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertEqual((video["codec_name"], audio["codec_name"]), ("h264", "aac"))
            self.assertTrue(3.5 <= float(details["format"]["duration"]) <= 3.7)
            for moment, expected_channel in zip((0.5, 1.7, 2.9), (0, 1, 2)):
                frame = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(moment),
                                        "-i", "result.mp4", "-frames:v", "1", "-f", "image2pipe",
                                        "-vcodec", "png", "pipe:1"], cwd=root,
                                       capture_output=True, check=True, timeout=15)
                image = Image.open(io.BytesIO(frame.stdout))
                pixel = image.getpixel((540, 960))
                self.assertEqual(max(range(3), key=lambda channel: pixel[channel]), expected_channel)
                logo = Image.open(LOGO_PATH).convert("RGBA").resize((144, 144), Image.Resampling.LANCZOS)
                observed = image.crop((54, 54, 198, 198)).convert("RGB")
                opaque = [(source[:3], actual) for source, actual in zip(logo.getdata(), observed.getdata()) if source[3] > 240]
                self.assertGreater(len(opaque), 400)
                self.assertLess(sum(sum(abs(a-b) for a,b in zip(source, actual)) / 3
                                    for source, actual in opaque) / len(opaque), 35)


class LetteringRepairTests(unittest.TestCase):
    @staticmethod
    def decoded(data):
        return Image.open(io.BytesIO(data)).convert("RGB")

    def test_wrong_glyphs_are_removed_and_correct_glyphs_replace_them_in_both_light_layouts(self):
        previous, corrected = "На важное дела", "На важные дела"
        for layout in ("editorial", "clean"):
            with self.subTest(layout=layout):
                source = artwork()
                original = render_slide(source, previous, "Агентство W", "#D4AF37",
                                        slide_number="3 / 4", layout=layout)
                repaired = reletter_slide(original, previous, corrected,
                                          slide_number="3 / 4", layout=layout)
                expected = render_slide(source, corrected, "Агентство W", "#D4AF37",
                                        slide_number="3 / 4", layout=layout)
                # Both short phrases use one line at the same size. The freshly
                # composed corrected reference gives independent glyph positions.
                region = (90, 1100, 985, 1235)
                before = self.decoded(original).crop(region).convert("L").tobytes()
                after = self.decoded(repaired).crop(region).convert("L").tobytes()
                target = self.decoded(expected).crop(region).convert("L").tobytes()
                old_only = [i for i, (a, b) in enumerate(zip(before, target)) if a < 100 and b > 210]
                new_only = [i for i, (a, b) in enumerate(zip(before, target)) if a > 210 and b < 100]
                self.assertGreater(len(old_only), 40)
                self.assertGreater(len(new_only), 40)
                self.assertGreater(sum(after[i] > 200 for i in old_only) / len(old_only), 0.97)
                self.assertGreater(sum(after[i] < 115 for i in new_only) / len(new_only), 0.97)

    def test_shorter_text_keeps_the_original_card_bounds_and_artwork_outside_it(self):
        previous = "Одна повторяющаяся задача забирает ваш вечер. Передайте часть подготовки помощнику."
        for layout in ("editorial", "clean"):
            with self.subTest(layout=layout):
                original = self.decoded(render_slide(artwork(), previous, "Агентство W", "#D4AF37",
                                                     slide_number="2 / 4", layout=layout))
                buffer = io.BytesIO()
                original.save(buffer, format="JPEG", quality=95, subsampling=0)
                original = self.decoded(buffer.getvalue())
                repaired = self.decoded(reletter_slide(buffer.getvalue(), previous, "Верните себе вечер",
                                                       slide_number="2 / 4", layout=layout))
                old_top = next(y for y in range(500, 1350) if min(original.getpixel((540, y))) > 220)
                new_top = next(y for y in range(500, 1350) if min(repaired.getpixel((540, y))) > 220)
                self.assertEqual(old_top, new_top)
                outside = (0, 0, 1080, old_top - 10)
                difference = ImageChops.difference(original.crop(outside), repaired.crop(outside))
                self.assertLess(max(ImageStat.Stat(difference).mean), 1.0)
                # The existing slide number stays at its original lower position.
                number_region = (850, 1220, 985, 1292)
                difference = ImageChops.difference(original.crop(number_region), repaired.crop(number_region))
                self.assertLess(max(ImageStat.Stat(difference).mean), 10)

    def test_clean_footer_preserves_existing_accent_color_without_repainting_brand(self):
        previous = "На важное дела"
        original_data = render_slide(artwork(), previous, "Агентство W", "#7B4FD2",
                                     slide_number="4 / 4", layout="clean")
        original = self.decoded(original_data)
        repaired = self.decoded(reletter_slide(original_data, previous, "На важные дела",
                                               slide_number="4 / 4", layout="clean"))
        # The narrow violet accent above the lettering must survive the repair.
        pixel = original.getpixel((130, 1119))
        self.assertGreater(pixel[2], pixel[1] + 60)
        self.assertTrue(all(abs(a - b) < 8 for a, b in zip(pixel, repaired.getpixel((130, 1119)))))
        difference = ImageChops.difference(original.crop((45, 45, 300, 115)), repaired.crop((45, 45, 300, 115)))
        self.assertLess(max(ImageStat.Stat(difference).mean), 1)

    def test_cover_wrong_size_and_text_overflow_fail_instead_of_destroying_artwork(self):
        original = render_slide(artwork(), "Вечер", "Агентство W", "#D4AF37")
        with self.assertRaises(MediaError):
            reletter_slide(original, "Вечер", "Новый текст", layout="cover")
        with self.assertRaises(MediaError):
            reletter_slide(artwork(), "Вечер", "Новый текст")
        with self.assertRaises(MediaError):
            reletter_slide(original, "Вечер", "Ш" * 600)


if __name__ == "__main__":
    unittest.main()
