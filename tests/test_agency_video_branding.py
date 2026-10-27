"""Official PNG pixels and audio-preserving local video branding, without APIs."""
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

from agency_video_branding import (
    LOGO_PATH, LOGO_VERSION, VideoBrandingError, apply_agency_w_logo, branding_proof_matches, check_video_logo,
    is_agency_w_brand, logo_layout, prepare_video_logo, video_branding_proof, video_logo_filter,
)


class LogoPolicyTests(unittest.TestCase):
    def test_only_exact_agency_names_select_the_official_mark(self):
        for name in ("Агентство W", " Agency  W ", "АГЕНТСТВО W", "AgencyW"):
            self.assertTrue(is_agency_w_brand(name))
        for name in ("Partner W", "Agency W client", "W", "Агентство В", None):
            self.assertFalse(is_agency_w_brand(name))
        original = b"partner video remains identical"
        with patch("agency_video_branding.subprocess.run") as run:
            self.assertIs(apply_agency_w_logo(original, brand_name="Partner W"), original)
            self.assertIsNone(video_branding_proof(original, "Partner W"))
            run.assert_not_called()

    def test_prepared_mark_uses_exact_official_png_proportions_and_transparency(self):
        with tempfile.TemporaryDirectory() as directory:
            logo = prepare_video_logo(directory, (1080, 1920))
            prepared = Image.open(logo).convert("RGBA")
            expected = Image.open(LOGO_PATH).convert("RGBA")
            expected.thumbnail((144, 144), Image.Resampling.LANCZOS)
            self.assertEqual(prepared.size, (144, 144))
            self.assertEqual(prepared.tobytes(), expected.tobytes())
            self.assertEqual(prepared.getchannel("A").getextrema(), (0, 255))
            self.assertEqual(logo_layout((1080, 1920)), (54, 54, 144, 144))
            self.assertEqual(logo_layout((1920, 1080)), (54, 54, 144, 144))
            self.assertIsNone(prepare_video_logo(directory, (1080, 1920), "Partner"))

    def test_missing_official_asset_stops_instead_of_substituting_a_letter(self):
        with tempfile.TemporaryDirectory() as directory, patch("agency_video_branding.LOGO_PATH", Path(directory) / "missing.png"):
            with self.assertRaises(VideoBrandingError):
                prepare_video_logo(directory, (1080, 1920))
            with patch("agency_video_branding.subprocess.run") as run:
                with self.assertRaises(VideoBrandingError):
                    check_video_logo("Агентство W")
                self.assertIsNone(check_video_logo("Partner Brand"))
                run.assert_not_called()

    def test_proof_binds_final_bytes_and_official_asset_version(self):
        result = video_branding_proof(b"known branded export")
        self.assertEqual(result["version"], LOGO_VERSION)
        self.assertEqual(result["logo_sha256"], hashlib.sha256(LOGO_PATH.read_bytes()).hexdigest())
        self.assertEqual(result["asset_sha256"], hashlib.sha256(b"known branded export").hexdigest())
        self.assertNotEqual(result["asset_sha256"], video_branding_proof(b"replaced video")["asset_sha256"])
        self.assertTrue(branding_proof_matches(result, result["asset_sha256"]))
        for invalid in (None, {}, {**result, "unexpected": "field"}, {**result, "asset_sha256": True},
                        {**result, "logo_filename": "agency_w_logo.png"}, {**result, "version": "old"},
                        {**result, "logo_sha256": "0" * 64}, {**result, "asset_sha256": "0" * 64}):
            self.assertFalse(branding_proof_matches(invalid, result["asset_sha256"]))
        self.assertFalse(branding_proof_matches(result, "not a digest"))

    def test_filter_does_not_move_captions_or_crop_resize_underlying_footage(self):
        result = video_logo_filter("fps=24", "subtitles=scene.ass,format=yuv420p", (1080, 1920))
        self.assertIn("overlay=x=54:y=54", result)
        self.assertIn("subtitles=scene.ass", result)
        self.assertNotIn("scale=", result)
        self.assertNotIn("crop=", result)
        self.assertNotIn("setpts", result)
        with self.assertRaises(VideoBrandingError):
            video_logo_filter("null", "", (1080, 1920), video_label="0:v];movie=http://bad[")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ExistingVideoBrandingTests(unittest.TestCase):
    def test_silent_landscape_video_keeps_fractional_frame_rate_and_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, final = root / "silent.mp4", root / "branded.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=24000/1001:duration=0.5", "-an",
                "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", str(source),
            ], capture_output=True, check=True, timeout=15)
            final.write_bytes(apply_agency_w_logo(source.read_bytes()))
            streams = []
            for path in (source, final):
                result = subprocess.run([
                    "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path),
                ], capture_output=True, check=True, timeout=15)
                data = json.loads(result.stdout)["streams"]
                self.assertEqual(len(data), 1)
                streams.append(data[0])
            self.assertEqual((streams[1]["width"], streams[1]["height"]), (320, 180))
            self.assertEqual(streams[0]["nb_frames"], streams[1]["nb_frames"])
            self.assertEqual(streams[0]["avg_frame_rate"], streams[1]["avg_frame_rate"])

    def test_real_video_has_official_logo_and_identical_audio_duration_and_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "color=c=0x234567:size=1080x1920:rate=24:duration=0.75",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.75",
                "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "64k", str(original),
            ], capture_output=True, check=True, timeout=30)
            final = root / "final.mp4"
            final.write_bytes(apply_agency_w_logo(original.read_bytes()))

            def metadata(path):
                return json.loads(subprocess.run([
                    "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
                ], capture_output=True, check=True, timeout=15).stdout)

            before, after = metadata(original), metadata(final)
            self.assertAlmostEqual(float(before["format"]["duration"]), float(after["format"]["duration"]), places=6)
            old_video, new_video = (next(item for item in info["streams"] if item["codec_type"] == "video") for info in (before, after))
            self.assertEqual((new_video["width"], new_video["height"], new_video["nb_frames"]),
                             (old_video["width"], old_video["height"], old_video["nb_frames"]))

            def audio_packets(path):
                data = subprocess.run([
                    "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
                    "-show_data_hash", "sha256", "-show_entries", "packet=data_hash,size", "-of", "json", str(path),
                ], capture_output=True, check=True, timeout=15)
                return [(packet["data_hash"], packet["size"]) for packet in json.loads(data.stdout)["packets"]]

            self.assertEqual(audio_packets(original), audio_packets(final))
            frame = subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(final), "-frames:v", "1", "-f", "image2pipe",
                "-vcodec", "png", "pipe:1",
            ], capture_output=True, check=True, timeout=15).stdout
            image = Image.open(io.BytesIO(frame)).convert("RGB")
            expected = Image.new("RGBA", (144, 144), (*image.getpixel((300, 300)), 255))
            logo = Image.open(LOGO_PATH).convert("RGBA").resize((144, 144), Image.Resampling.LANCZOS)
            expected.alpha_composite(logo)
            observed = image.crop((54, 54, 198, 198))
            # Comparing decoded pixels to the real PNG rejects a textual W,
            # generic badge or changed mark while allowing H.264 chroma loss.
            error = ImageStat.Stat(ImageChops.difference(observed, expected.convert("RGB"))).mean
            self.assertLess(max(error), 12)
            self.assertGreater(sum(1 for red, green, blue in observed.getdata() if blue > 120 and green > 120 and red < 130), 10)
            # The transparent corner and the central face/artwork zone survive.
            self.assertLess(max(abs(a - b) for a, b in zip(image.getpixel((54, 54)), image.getpixel((300, 300)))), 4)

    def test_oversize_and_remote_playlist_inputs_are_rejected(self):
        with patch("agency_video_branding.MAX_VIDEO_BYTES", 8), patch("agency_video_branding.subprocess.run") as run:
            with self.assertRaises(VideoBrandingError):
                apply_agency_w_logo(b"a" * 9)
            run.assert_not_called()
        with self.assertRaises(VideoBrandingError):
            apply_agency_w_logo(b"#EXTM3U\nhttps://example.invalid/private\n")


if __name__ == "__main__":
    unittest.main()
