"""Opt-in two-minute films preserve the short path and original audio."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from content_factory_live_media import (
    LiveMediaError, render_live_reel, scene_durations, speech_duration,
)
from content_factory_live_video import RunwayVideoClient, LiveVideoError


def voice(seconds):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * round(seconds * 16000))
    return output.getvalue()


class TwoMinuteDurationTests(unittest.TestCase):
    def test_long_plan_is_explicit_and_every_scene_remains_provider_compatible(self):
        for duration, count in ((60.0, 8), (110.0, 16), (119.5, 12), (119.5, 16)):
            with self.subTest(duration=duration, count=count):
                seconds = scene_durations(duration, count, target_seconds=120)
                self.assertEqual(len(seconds), count)
                self.assertTrue(all(type(value) is int and 2 <= value <= 10 for value in seconds))
                self.assertGreaterEqual(sum(seconds), duration + 0.35)
        for duration, count in ((110, 16), (20, 7), (60, 6)):
            with self.subTest(duration=duration, count=count), self.assertRaises(LiveMediaError):
                scene_durations(duration, count)

    def test_invalid_opt_in_duration_and_scene_count_remain_bounded(self):
        for target in (True, 119, 121, "120", None, 120.0):
            with self.subTest(target=target), self.assertRaises(LiveMediaError):
                scene_durations(110, 16, target_seconds=target)
        for duration in (True, 119.501, 120, float("nan"), float("inf"), "110"):
            with self.subTest(duration=duration), self.assertRaises(LiveMediaError):
                scene_durations(duration, 16, target_seconds=120)
        for count in (True, 2, 17, 16.0):
            with self.subTest(count=count), self.assertRaises(LiveMediaError):
                scene_durations(110, count, target_seconds=120)
        with self.assertRaisesRegex(LiveMediaError, "больше"):
            scene_durations(110, 10, target_seconds=120)

    def test_long_credit_estimate_does_not_make_a_provider_request(self):
        client = RunwayVideoClient("local-test-key")
        with patch.object(client.session, "post") as post, patch.object(client.session, "get") as get:
            self.assertEqual(client.estimate_credits([10] * 16), 1920)
            self.assertEqual(client.estimate_credits(scene_durations(110, 16, target_seconds=120)), 1344)
            post.assert_not_called()
            get.assert_not_called()
        for plan in ([10] * 17, [11] * 16, [True] * 16):
            with self.subTest(plan=plan), self.assertRaises(LiveVideoError):
                client.estimate_credits(plan)
        self.assertIsNone(RunwayVideoClient("local-test-key", model="gen4_turbo").estimate_credits([10] * 16))

    def test_extended_media_count_does_not_relax_bytes_or_speech_limits(self):
        with patch("content_factory_live_media.subprocess.run") as run:
            for clips in ([b"clip"] * 17, [b""] * 16,
                          [b"x" * (25 * 1024 * 1024 + 1)] * 3,
                          [b"x" * (7 * 1024 * 1024)] * 16):
                with self.subTest(count=len(clips)), self.assertRaises(LiveMediaError):
                    render_live_reel(clips, voice(1), "Речь.", "Тема", "Партнёр", "#336699", target_seconds=120)
            run.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class TwoMinuteRealMediaTests(unittest.TestCase):
    def test_speech_probe_accepts_two_minutes_only_when_requested(self):
        speech = voice(110)
        self.assertAlmostEqual(speech_duration(speech, target_seconds=120), 110, delta=0.01)
        with self.assertRaises(LiveMediaError):
            speech_duration(speech)
        with self.assertRaises(LiveMediaError):
            speech_duration(voice(120), target_seconds=120)

    def test_real_sixteen_scene_film_reaches_the_end_without_losing_aac_packets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            original_audio = root / "voice.m4a"
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-filter_threads", "1",
                "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=24:duration=8",
                "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source),
            ], capture_output=True, check=True, timeout=30)
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=24000:duration=110", "-c:a", "aac", "-b:a", "48k",
                "-threads", "1", str(original_audio),
            ], capture_output=True, check=True, timeout=30)
            narration = " ".join(["Действие развивается и приводит к результату."] * 35) + " Завершение истории."
            output = render_live_reel([source.read_bytes()] * 16, original_audio.read_bytes(), narration,
                                      "Новая история", "Агентство W", "#D4AF37", target_seconds=120)
            final = root / "final.mp4"
            final.write_bytes(output)
            result = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(final),
            ], capture_output=True, check=True, timeout=20)
            info = json.loads(result.stdout)
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            audio = next(stream for stream in info["streams"] if stream["codec_type"] == "audio")
            self.assertEqual((video["width"], video["height"], video["codec_name"]), (1080, 1920, "h264"))
            self.assertGreater(float(info["format"]["duration"]), 110.30)
            self.assertLessEqual(float(info["format"]["duration"]), 120)
            self.assertEqual(int(video["nb_frames"]), 2649)
            self.assertAlmostEqual(float(audio["duration"]), 110, delta=0.05)

            def audio_packets(path):
                packets = subprocess.run([
                    shutil.which("ffprobe"), "-v", "error", "-select_streams", "a:0",
                    "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=size,data_hash",
                    "-of", "json", str(path),
                ], capture_output=True, check=True, timeout=20)
                return [(packet["size"], packet["data_hash"]) for packet in json.loads(packets.stdout)["packets"]]

            self.assertEqual(audio_packets(original_audio), audio_packets(final))
            # Decoding the last scene verifies the concat did not stop after
            # the old sixth scene or replace the remaining film with padding.
            last = subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-ss", "109.5", "-i", str(final),
                "-vf", "scale=54:96", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=20)
            self.assertEqual(len(last.stdout), 54 * 96 * 3)


if __name__ == "__main__":
    unittest.main()
