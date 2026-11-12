"""Guard the actual VK referral route against regression to legacy Neona."""
import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import neona_dialog_policy_v3 as v3

ROOT = Path(__file__).resolve().parents[1]

class NeonaVKRoutingTests(unittest.TestCase):
    def test_referral_vk_uses_v3_not_old_policy(self):
        tree = ast.parse((ROOT / "instagram_webhook.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_process_vk_message_unlocked")
        imports = [n for n in ast.walk(fn) if isinstance(n, ast.Import)]
        self.assertTrue(any(alias.name == "neona_dialog_policy_v3"
                            for item in imports for alias in item.names))
        self.assertFalse(any(alias.name == "neona_dialog_policy"
                             for item in imports for alias in item.names))

    def test_live_policy_binds_v3_brain_and_v2_calendar(self):
        v3.apply_policy()
        self.assertIs(v3.core._schedule_reply, v3.v2._schedule_reply_v2)
        self.assertIs(v3.legacy._process_message_without_memory, v3._process_v3)

    def test_exact_1800_from_vk_overrides_three_stale_options(self):
        day = datetime(2026, 11, 12, 10, 24, tzinfo=timezone.utc)
        context = {
            "contact_timezone": "Europe/Moscow",
            "offered_slots": [
                "2026-11-12T11:00:00+00:00",
                "2026-11-12T11:30:00+00:00",
                "2026-11-12T12:00:00+00:00",
            ],
        }
        with patch.object(v3.v2, "_slot_within_policy", return_value=True), \
             patch.object(v3.v2.legacy, "_slot_free_with_buffer", return_value=True):
            reply, stage, context = v3.v2._schedule_reply_v2(
                None, 235805138, "Валентина", -999, "Татьяна", "",
                "Повторяю:Сегодня в 18:00 МСК, формат WhatsApp.",
                day, "awaiting_slot_choice", context, False)
        self.assertEqual(stage, "awaiting_confirmation")
        self.assertIn("18:00 МСК", reply)
        self.assertIn("WhatsApp", reply)
        self.assertNotIn("offered_slots", context)
        self.assertNotIn("1, 2 или 3", reply)
        self.assertIn("proposed_start_at", context)

    def test_unavailable_1800_does_not_offer_earlier_numbers(self):
        day = datetime(2026, 11, 12, 10, 24, tzinfo=timezone.utc)
        context = {"offered_slots": ["2026-11-12T11:00:00+00:00"]}
        with patch.object(v3.v2, "_slot_within_policy", return_value=True), \
             patch.object(v3.v2.legacy, "_slot_free_with_buffer", return_value=False):
            reply, stage, context = v3.v2._schedule_reply_v2(
                None, 235805138, "Валентина", -999, "Татьяна", "",
                "Повторяю:Сегодня в 18:00 МСК, формат WhatsApp.",
                day, "awaiting_slot_choice", context, False)
        self.assertIn("18:00 МСК", reply)
        self.assertEqual(stage, "collecting_meeting_details")
        self.assertNotIn("offered_slots", context)
        self.assertNotIn("1, 2 или 3", reply)


if __name__ == "__main__":
    unittest.main()
