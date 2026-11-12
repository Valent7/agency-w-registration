"""Regression tests for Agency W Nov 2026 calendar and VK replies."""
import unittest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch
import agency_team_schedule
import theo_consultant as theo

MSK = theo.MSK
UTC = timezone.utc

class TeamScheduleTests(unittest.TestCase):
    def test_new_class_blocks_only_on_monday_wednesday_friday(self):
        for weekday, expected in [
            (date(2026, 11, 12), False),
            (date(2026, 11, 13), True),
            (date(2026, 11, 16), True),
            (date(2026, 11, 17), False),
            (date(2026, 11, 18), True),
        ]:
            with self.subTest(day=weekday):
                blocks = agency_team_schedule.effective_blocks([], weekday)
                self.assertEqual(any(b.get("_agency_team_schedule") for b in blocks), expected)

    def test_legacy_group_meetings_retired_but_private_blocks_kept(self):
        old = {"weekday": 3, "title": "Командная встреча",
               "start_time": "20:00:00", "end_time": "21:00:00", "active": True}
        personal = {"weekday": 3, "title": "Личная встреча",
                    "start_time": "20:00:00", "end_time": "21:00:00", "active": True}
        result = agency_team_schedule.effective_blocks([old, personal], date(2026, 11, 12))
        self.assertNotIn(old, result)
        self.assertIn(personal, result)
        self.assertEqual(len(result), 1)
        past = agency_team_schedule.effective_blocks([old], date(2026, 11, 11))
        self.assertIn(old, past)

    def test_avoid_duplicate_new_class_block(self):
        b = {"weekday": 4, "title": agency_team_schedule.TITLE,
             "start_time": "19:00:00", "end_time": "19:30:00", "active": True}
        self.assertEqual(len(agency_team_schedule.effective_blocks([b], date(2026, 11, 13))), 1)

class VKMeetingRepliesTests(unittest.TestCase):
    def context(self):
        return {
            "requested_date": "2026-12-15",
            "requested_time": "19:00",
            "contact_timezone": "Europe/Moscow",
            "meeting_format": "WhatsApp",
            "offered_slots": [
                datetime(2026, 12, 15, hour, 0, tzinfo=MSK).astimezone(UTC).isoformat()
                for hour in (14, 15, 16)
            ],
        }

    def answer(self, text, state="awaiting_slot_choice", context=None):
        return theo._schedule_reply(
            owner_id=1,
            owner_name="Валентина",
            vk_user_id=42,
            visitor_first_name="Татьяна",
            incoming_text=text,
            meeting_state=state,
            meeting_context=self.context() if context is None else context,
            first_contact=False,
        )

    def test_new_1800_beats_old_numbered_options(self):
        with patch.object(theo, "_slot_free_with_buffer", return_value=True):
            reply, state, context, booking = self.answer(
                "Ни один мне не подходит, но могу перенести на 18:00 МСК")
        self.assertEqual(state, "awaiting_confirmation")
        self.assertIn("18:00", reply)
        self.assertEqual(datetime.fromisoformat(context["proposed_start_at"]).astimezone(MSK).hour, 18)
        self.assertNotIn("offered_slots", context)
        self.assertIsNone(booking)

    def test_time_change_in_confirmation_stage(self):
        context = self.context()
        context["proposed_start_at"] = datetime(2026, 12, 15, 19, 0, tzinfo=MSK).astimezone(UTC).isoformat()
        with patch.object(theo, "_slot_free_with_buffer", return_value=True):
            reply, state, ctx, booking = self.answer(
                "Лучше в 18:00 МСК, WhatsApp", "awaiting_confirmation", context)
        self.assertIn("18:00", reply)
        self.assertEqual(state, "awaiting_confirmation")
        self.assertIsNone(booking)
        self.assertEqual(datetime.fromisoformat(ctx["proposed_start_at"]).astimezone(MSK).hour, 18)

    def test_missing_day_asks_once_for_day(self):
        context = {"contact_timezone": "Europe/Moscow",
                   "meeting_format": "WhatsApp", "offered_slots": self.context()["offered_slots"]}
        reply, state, ctx, booking = self.answer("18:00 МСК", context=context)
        self.assertEqual(state, "collecting_meeting_details")
        self.assertIn("день", reply.lower())
        self.assertNotIn("номер", reply.lower())
        self.assertIsNone(booking)

    def test_declined_offers_generate_new_choices(self):
        other = [datetime(2026, 12, 16, hour, 0, tzinfo=MSK).astimezone(UTC)
                 for hour in (12, 13, 14)]
        with patch.object(theo, "_find_three_slots", return_value=other):
            reply, state, ctx, booking = self.answer("Ни один не подходит")
        self.assertEqual(state, "awaiting_slot_choice")
        self.assertIn("другие варианты", reply.lower())
        self.assertIn("16.12.2026", reply)
        self.assertIsNone(booking)

if __name__ == "__main__":
    unittest.main()
