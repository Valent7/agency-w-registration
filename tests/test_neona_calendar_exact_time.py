"""Offline regression for the Oct 18 incorrect Neona Zoom calendar confirmation."""
import ast
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
DATE_OF_MESSAGE = datetime(2026, 10, 18, 8, 31, tzinfo=UTC)
MONDAY_AT_NOON = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
STALE_SUNDAY_AT_SEVEN = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def _extract(path, names, namespace):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    funcs = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    assert {node.name for node in funcs} == set(names), (path, names)
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"), namespace)


class CalendarRegressionTests(unittest.TestCase):
    def setUp(self):
        self.db_calls = []
        core_ns = {
            "re": re, "date": date, "timedelta": timedelta,
            "ZoneInfo": ZoneInfo, "MSK": MSK, "datetime": FrozenDatetime,
            "WEEKDAYS_RU": {
                "понедельник": 0, "понедельника": 0,
                "вторник": 1, "вторника": 1,
                "среда": 2, "среду": 2, "среды": 2,
                "четверг": 3, "четверга": 3,
                "пятница": 4, "пятницу": 4,
                "суббота": 5, "субботу": 5,
                "воскресенье": 6,
            },
        }
        _extract(
            "neona_telegram_dialogs.py",
            ["_detect_times", "_detect_time", "_detect_date", "_detect_format"],
            core_ns,
        )
        core = SimpleNamespace(
            MSK=MSK, UTC=UTC, DURATION_MINUTES=30,
            _detect_date=core_ns["_detect_date"],
            _detect_time=core_ns["_detect_time"],
            _detect_format=core_ns["_detect_format"],
            _greeting=lambda name: "Здравствуйте",
        )
        def make_event(config, owner_id, owner_name, contact_id, first_name, username, context, fmt):
            self.db_calls.append((context["proposed_start_at"], fmt))
            start = datetime.fromisoformat(context["proposed_start_at"]).astimezone(UTC)
            return {"id": "meeting-1"}, start, "", ""
        def offer(config, owner_id, owner_name, context, reason="", after_utc=None, prefix=""):
            ctx = dict(context)
            ctx["offered_slots"] = [MONDAY_AT_NOON.isoformat()]
            ctx.pop("proposed_start_at", None)
            return "Вот варианты", "awaiting_slot_choice", ctx
        ns = {
            "datetime": FrozenDatetime, "timedelta": timedelta, "re": re,
            "core": core,
            "legacy": SimpleNamespace(_slot_free_with_buffer=lambda *args: True),
            "_create_meeting": make_event,
            "_offer_slots": offer,
            "_owner_forms": lambda name: {
                "nominative": "Валентина",
                "instrumental": "Валентиной",
            },
            "_decline": lambda text: text.casefold().strip() in ("нет", "не подходит"),
        }
        _extract(
            "neona_dialog_policy_v2.py",
            ["_norm", "_fmt_msk", "_slot_choice", "_explicit_meeting_slot",
             "_slot_within_policy", "_explicit_confirmation", "_schedule_reply_v2"],
            ns,
        )
        self.ns = ns

    def reply(self, text, stage="collecting_meeting_details", context=None):
        return self.ns["_schedule_reply_v2"](
            None, 100, "Валентина", 200, "Кандидат", "candidate",
            text, DATE_OF_MESSAGE, stage, context or {}, False,
        )

    def test_new_monday_noon_overrides_sunday_19_and_never_books_immediately(self):
        old = {
            "proposed_start_at": STALE_SUNDAY_AT_SEVEN.isoformat(),
            "meeting_format": "Zoom",
        }
        message, stage, state = self.reply("Zoom в понедельник в 12.00 мск", context=old)
        self.assertEqual(stage, "awaiting_confirmation")
        self.assertIn("19.10.2026 в 12:00 МСК", message)
        self.assertNotIn("18.10.2026", message)
        self.assertEqual(
            datetime.fromisoformat(state["proposed_start_at"]).astimezone(UTC),
            MONDAY_AT_NOON,
        )
        self.assertEqual(self.db_calls, [])

        # Exactly a separate confirmation may persist the meeting.
        confirmed, stage, ctx = self.reply("Подтверждаю", stage, state)
        self.assertEqual(stage, "scheduled")
        self.assertIn("19.10.2026 в 12:00", confirmed)
        self.assertEqual(len(self.db_calls), 1)

    def test_format_alone_must_not_confirm_previous_slot(self):
        text, stage, ctx = self.reply(
            "Zoom", context={"proposed_start_at": MONDAY_AT_NOON.isoformat()}
        )
        self.assertEqual(stage, "awaiting_confirmation")
        self.assertIn("Подтверждаю", text)
        self.assertFalse(self.db_calls)

    def test_partial_correction_discards_stale_date(self):
        for user_text in ("Zoom в понедельник", "Zoom в 12.00 МСК"):
            text, stage, ctx = self.reply(
                user_text,
                context={"proposed_start_at": STALE_SUNDAY_AT_SEVEN.isoformat()},
            )
            self.assertEqual(stage, "collecting_meeting_details")
            self.assertNotIn("proposed_start_at", ctx)
            self.assertFalse(self.db_calls)

    def test_new_requested_time_overwrites_pending_confirmation(self):
        ctx = {
            "proposed_start_at": MONDAY_AT_NOON.isoformat(),
            "meeting_format": "Zoom", "meeting_confirmation_pending": True,
        }
        msg, stage, new = self.reply(
            "Нет, во вторник в 14.00 мск Zoom", "awaiting_confirmation", ctx
        )
        self.assertEqual(stage, "awaiting_confirmation")
        self.assertIn("20.10.2026 в 14:00", msg)
        self.assertFalse(self.db_calls)
        self.assertNotEqual(ctx["proposed_start_at"], new["proposed_start_at"])

    def test_no_booking_on_sunday_or_premature_confirmation(self):
        self.assertFalse(self.ns["_slot_within_policy"](STALE_SUNDAY_AT_SEVEN))
        msg, stage, ctx = self.reply(
            "Zoom", "awaiting_confirmation",
            {"proposed_start_at": STALE_SUNDAY_AT_SEVEN.isoformat(),
             "meeting_format": "Zoom"},
        )
        self.assertNotEqual(stage, "scheduled")
        self.assertEqual(self.db_calls, [])

    def test_zoom_or_interest_is_not_explicit_confirmation(self):
        for text in ("Zoom", "интересно", "хочу", "понедельник"):
            self.assertFalse(self.ns["_explicit_confirmation"](text), text)
        self.assertTrue(self.ns["_explicit_confirmation"]("Подтверждаю"))


if __name__ == "__main__":
    unittest.main()
