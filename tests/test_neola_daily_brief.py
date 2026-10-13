"""Offline checks for Neola's spoken daily mood and calendar reminders."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from neola_daily_brief import (
    DAILY_MOODS,
    build_daily_brief,
    daily_mood_for_date,
    next_hour_reminder,
    upcoming_meetings,
)

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")


class NeolaDailyBriefTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 5, 0, tzinfo=UTC)

    def test_mood_is_deterministic_and_matches_home_cycle(self):
        self.assertEqual(len(DAILY_MOODS), 14)
        self.assertEqual(
            daily_mood_for_date(self.now.date()),
            DAILY_MOODS[self.now.date().toordinal() % 14],
        )

    def test_upcoming_filters_inactive_and_past_and_other_days(self):
        rows = [
            {"id": "past", "start_at": "2026-10-13T04:00:00Z", "status": "Подтверждена"},
            {"id": "cancel", "start_at": "2026-10-13T14:00:00Z", "status": "Отменена"},
            {"id": "moved", "start_at": "2026-10-13T14:00:00Z", "status": "Перенесена"},
            {"id": "done", "start_at": "2026-10-13T14:00:00Z", "status": "Состоялась"},
            {"id": "tomorrow_msk", "start_at": "2026-10-13T22:30:00Z", "status": "Подтверждена"},
            {"id": "later", "start_at": "2026-10-13T14:00:00+00:00", "contact_name": "Людмила", "meeting_format": "Zoom", "status": "Подтверждена"},
            {"id": "earlier", "start_at": "2026-10-13T12:00:00Z", "contact_name": "Светлана", "meeting_format": "Telegram"},
            {"id": "malformed", "start_at": "not-a-date"},
        ]
        actual = upcoming_meetings(rows, self.now)
        self.assertEqual([r["id"] for r in actual], ["earlier", "later"])
        self.assertEqual(actual[0]["start_msk"].strftime("%H:%M"), "15:00")

    def test_daily_brief_includes_motto_and_real_meeting_times(self):
        rows = upcoming_meetings([
            {"id": "a", "start_at": "2026-10-13T14:00:00Z", "contact_name": "Людмила", "meeting_format": "Zoom"}
        ], self.now)
        speech = build_daily_brief("Валентина", motto="Сохраняем время.", meetings=rows, now=self.now)
        self.assertIn("Доброе утро, Валентина", speech)
        self.assertIn("Настрой дня: Сохраняем время.", speech)
        self.assertIn("17 часов 00 минут по московскому времени", speech)
        self.assertIn("встреча с Людмила", speech)
        self.assertIn("пять новых кандидатов", speech)

    def test_calendar_unavailable_does_not_invent_meetings(self):
        speech = build_daily_brief("Надежда", motto="Действуем.", calendar_available=False, now=self.now)
        self.assertIn("Не удалось проверить календарь", speech)
        self.assertNotIn("На сегодня предстоящих встреч", speech)

    def test_reminder_within_hour_and_not_after_start(self):
        rows = upcoming_meetings([
            {"id": "meeting", "start_at": "2026-10-13T05:55:00Z", "contact_name": "Эльза", "meeting_format": "Zoom"}
        ], self.now)
        meeting_id, text = next_hour_reminder(rows, now=self.now)
        self.assertEqual(meeting_id, "meeting")
        self.assertIn("Через 55 минут", text)
        self.assertIn("8 часов 55 минут", text)
        self.assertIsNone(next_hour_reminder(rows, now=datetime(2026, 10, 13, 6, 0, tzinfo=UTC)))

    def test_no_reminder_more_than_one_hour_before(self):
        rows = upcoming_meetings([
            {"id": "meeting", "start_at": "2026-10-13T07:00:00Z", "contact_name": "Эльза"}
        ], self.now)
        self.assertIsNone(next_hour_reminder(rows, now=self.now))


if __name__ == "__main__":
    unittest.main()
