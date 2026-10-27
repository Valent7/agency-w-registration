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

from PIL import Image

from content_factory_media import (
    MediaError, _caption_events, _write_scene_captions, render_slide, render_story_reel,
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


if __name__ == "__main__":
    unittest.main()
