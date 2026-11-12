"""Offline checks for TikTok overlay detection; no user videos in GitHub."""
import unittest
import numpy as np
from content_tiktok_detection import _detect_from_frames


class TikTokDetectionTests(unittest.TestCase):
    def _frames(self, total=14):
        frames = np.zeros((total, 100, 60, 3), dtype=np.uint8)
        frames[:11] = (85, 80, 75)
        for i in range(11):
            if i < 5:
                frames[i, 40:49, 3:11] = (30, 222, 240)
            else:
                frames[i, 72:81, 50:58] = (30, 222, 240)
        return frames

    def test_two_sides_are_time_scoped_not_simultaneous(self):
        frames = self._frames()
        o = _detect_from_frames(frames, {"width": 480, "height": 800, "duration": 14})
        self.assertTrue(o["found"])
        self.assertEqual(o["segments"][0]["side"], "слева")
        self.assertEqual(o["segments"][0]["from"], 0)
        self.assertEqual(o["segments"][0]["to"], 5)
        self.assertEqual(o["segments"][1]["side"], "справа")
        self.assertEqual(o["segments"][1]["from"], 5)
        self.assertEqual(o["segments"][1]["to"], 11)
        self.assertEqual(o["outro_start"], 11)

    def test_a_bright_colored_center_is_not_mistaken_for_watermark(self):
        frames = np.full((12, 100, 60, 3), 70, dtype=np.uint8)
        frames[:, 40:55, 25:36] = (30, 220, 230)
        o = _detect_from_frames(frames, {"width": 480, "height": 800, "duration": 12})
        self.assertFalse(o["found"])

    def test_one_off_false_side_change_is_ignored(self):
        frames = np.full((18, 100, 60, 3), 70, dtype=np.uint8)
        frames[:, 72:81, 50:58] = (30, 222, 240)
        frames[9:12, 72:81, 50:58] = (70, 70, 70)
        frames[9:12, 40:49, 3:11] = (30, 222, 240)
        o = _detect_from_frames(frames, {"width": 480, "height": 800, "duration": 18})
        self.assertTrue(o["found"])
        self.assertEqual(len(o["segments"]), 1)
        self.assertEqual(o["segments"][0]["side"], "справа")


if __name__ == "__main__":
    unittest.main()
