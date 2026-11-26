import os
import unittest
from unittest.mock import patch

import neonia_public_scout_ui
import telegram_scout_worker
import vk_scout_worker


class PilotSearchGuardTests(unittest.TestCase):
    @patch.dict(os.environ, {"NEONIA_NETWORK_ONLY_OWNERS": "77"})
    def test_vk_keeps_saved_assignments_without_model_request(self):
        with (
            patch.object(vk_scout_worker, "ensure_quality_vk_assignments",
                         return_value={"complete": False, "count": 3}),
            patch.object(vk_scout_worker, "score_vk_candidates") as score,
        ):
            batch, analyzed, _ = vk_scout_worker._score_until_daily_five(
                77, "pilot", {}, daily_limit=5, min_score=50, analysis_limit=30,
            )
        self.assertEqual(batch["count"], 3)
        self.assertTrue(batch["paid_search_blocked"])
        self.assertEqual(analyzed, 0)
        score.assert_not_called()

    @patch.dict(os.environ, {"NEONIA_NETWORK_ONLY_OWNERS": "77"})
    @patch("vk_scout_worker._load_target_profiles", new=lambda _: {})
    def test_telegram_worker_skips_only_strict_pilot(self):
        with patch.object(telegram_scout_worker, "_workspace_state",
                          side_effect=RuntimeError("Legacy path")) as workspace:
            result = telegram_scout_worker.process_owner_once(77, "unused")
            self.assertEqual(result["status"], "skip")
            workspace.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "Legacy path"):
                telegram_scout_worker.process_owner_once(78, "unused")

    @patch.dict(os.environ, {"NEONIA_NETWORK_ONLY_OWNERS": "77"})
    def test_web_ui_cannot_start_unmetered_pilot_search(self):
        with (
            patch.object(neonia_public_scout_ui, "st") as ui,
            patch.object(neonia_public_scout_ui, "discover_public_candidates") as discover,
        ):
            neonia_public_scout_ui.render_neonia_public_scout({}, 77)
        ui.button.assert_not_called()
        discover.assert_not_called()
