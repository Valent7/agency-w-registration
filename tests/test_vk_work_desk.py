from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import vk_scout
import vk_scout_worker
from vk_desk_fixture import OWNER, VKDeskFixture


class VKWorkDeskTests(unittest.TestCase):
    def test_partitions_and_owner_scoped_reads(self):
        from collections import Counter

        db = VKDeskFixture()
        with db.connected():
            rows = vk_scout.load_vk_private_desk(OWNER)
            self.assertEqual(Counter(row["group"] for row in rows),
                             {"waiting": 28, "active": 4, "meeting": 1, "refusal": 3, "unsent": 6})
            self.assertEqual(len(vk_scout.load_today_vk_assignments(OWNER)), 5)
            self.assertEqual(vk_scout.load_vk_private_desk(OWNER + 1), [])
            with self.assertRaises(vk_scout.VKScoutError):
                vk_scout.mark_vk_desk_event(OWNER + 1, 6, refusal=True)
        self.assertLess(db.reads, 14)

    def test_review_then_new_reply_and_refusal_are_durable(self):
        db = VKDeskFixture()
        def contact():
            return next(row for row in vk_scout.load_vk_private_desk(OWNER) if row["vk_user_id"] == 6)

        with db.connected():
            self.assertEqual(contact()["group"], "waiting")
            db.manual_reply(6, OWNER, "Первый ответ")
            first = contact()
            self.assertEqual(first["group"], "active")
            vk_scout.mark_vk_desk_event(OWNER, 6, event_key="personal", viewed_at=first["inbound_at"])
        with db.connected():
            self.assertEqual(contact()["group"], "history")
            db.manual_reply(6, OWNER, "Новый ответ")
            self.assertEqual(contact()["group"], "active")
            vk_scout.mark_vk_desk_event(OWNER, 6, refusal=True)
        with db.connected():
            row = contact()
            self.assertEqual((row["group"], row["inbound"]), ("refusal", "Новый ответ"))
            self.assertTrue(row["refused_at"])

    def test_date_rollover_and_confirmation_move_to_waiting(self):
        db = VKDeskFixture()
        with db.connected():
            db.assignments[0]["assignment_date"] = db.yesterday
            self.assertNotIn(1, {row["id"] for row in vk_scout.load_today_vk_assignments(OWNER)})
            lookup = lambda: next(row for row in vk_scout.load_vk_private_desk(OWNER) if row["vk_user_id"] == 1)
            self.assertEqual(lookup()["group"], "unsent")
            vk_scout.mark_vk_invited(1, owner_id=OWNER + 1)
            self.assertEqual(lookup()["group"], "unsent")
            vk_scout.mark_vk_invited(1, owner_id=OWNER)
            self.assertEqual(lookup()["group"], "waiting")

    def test_actual_outbound_confirmation_clears_notification(self):
        db = VKDeskFixture()
        with db.connected():
            db.manual_reply(6, OWNER, "Первая реплика")
            db.sent(6, OWNER, "Уже ответили")
            row = next(row for row in vk_scout.load_vk_private_desk(OWNER) if row["vk_user_id"] == 6)
            self.assertEqual(row["group"], "history")
            self.assertEqual(row["inbound"], "Первая реплика")
            self.assertEqual(row["last_outbound"], "Уже ответили")

    def test_same_vk_id_keeps_personal_and_community_events_separate(self):
        db = VKDeskFixture()
        db.manual_reply(35, OWNER, "Личный ответ")
        with db.connected():
            matching = [row for row in vk_scout.load_vk_private_desk(OWNER) if row["vk_user_id"] == 35]
            self.assertEqual(len(matching), 1)
            row = matching[0]
            self.assertEqual({e["source"]: e["text"] for e in row["events"]},
                             {"personal": "Личный ответ", "community": "Могу созвониться завтра"})
            self.assertEqual(row["dialog"]["pending_inbound"], "Личный ответ")
            vk_scout.mark_vk_desk_event(OWNER, 35, event_key="personal", viewed_at=row["inbound_at"])
            row = next(row for row in vk_scout.load_vk_private_desk(OWNER) if row["vk_user_id"] == 35)
            self.assertEqual((row["source"], row["group"]), ("community", "active"))
            self.assertIsNone(row["draft"])
            self.assertNotEqual(vk_scout._vk_dialog_contact_id(35), vk_scout._vk_dialog_contact_id(35, personal=True))

    def test_comment_link_and_confirmed_calendar_belong_to_owner(self):
        db = VKDeskFixture()
        with db.connected():
            rows = {row["vk_user_id"]: row for row in vk_scout.load_vk_private_desk(OWNER)}
            self.assertEqual(rows[36]["reply_url"], "https://vk.com/wall36_13?reply=7")
            self.assertEqual(rows[38]["meeting"]["id"], 501)
            db.meetings[0]["owner_telegram_id"] = OWNER + 1
            rows = {row["vk_user_id"]: row for row in vk_scout.load_vk_private_desk(OWNER)}
            self.assertIsNone(rows[38]["meeting"])

    def test_owner_pages_continue_past_api_page(self):
        full_page = [{"id": i} for i in range(200)]
        with patch.object(vk_scout, "_sb_get", side_effect=[full_page, [{"id": 200}]]) as storage:
            self.assertEqual(len(vk_scout._vk_owner_pages("test", 71, {"order": "id"})), 201)
        self.assertEqual(
            [(call.args[1]["owner_telegram_id"], call.args[1]["offset"]) for call in storage.call_args_list],
            [("eq.71", 0), ("eq.71", 200)],
        )

    def test_fresh_profile_after_old_5000_row_cutoff_is_evaluated(self):
        target_id = 5001
        active_timestamp = int(datetime.now(timezone.utc).timestamp())

        def database(table, filters):
            self.assertEqual(table, "agency_vk_candidates")
            i = filters["offset"]
            return [
                {
                    "vk_user_id": uid,
                    "source_community_id": 22,
                    "raw_profile": {"last_seen": {"time": active_timestamp}},
                }
                for uid in range(i + 1, min(i + 201, target_id + 1))
            ]

        with patch.multiple(
            vk_scout,
            load_vk_sources=lambda owner: [{"community_id": 22}],
            _vk_owner_pages=lambda *args: [
                {"vk_user_id": uid, "analyzed_at": datetime.now(timezone.utc).isoformat()}
                for uid in range(1, target_id)
            ],
            _owner_processed_vk_ids=lambda owner: set(),
            _used_vk_ids=lambda: set(),
            _partner_vk_ids=lambda: set(),
            enrich_vk_profiles=lambda ids: [],
            save_vk_candidates=lambda rows: None,
            _sb_get=database,
        ), patch.object(vk_scout, "_sb_post") as save:
            result = vk_scout.score_vk_candidates(
                71, {}, ask_openai_fn=lambda *_: '[{"vk_user_id": 5001, "score": 20}]',
                limit=5, reanalyze_exhausted=False,
            )
        self.assertEqual(result["analyzed"], 1)
        self.assertEqual(save.call_args.args[1][0]["vk_user_id"], target_id)

    def test_worker_stops_after_fresh_pool_exhausted_without_rescoring(self):
        empty = {"complete": False, "count": 0}
        with patch.object(vk_scout_worker, "ensure_quality_vk_assignments", return_value=empty), patch.object(
            vk_scout_worker, "score_vk_candidates", return_value={"analyzed": 0},
        ) as score:
            result = vk_scout_worker._score_until_daily_five(
                71, "test", {}, daily_limit=5, min_score=50, analysis_limit=30,
            )
        self.assertEqual(result[1], 0)
        score.assert_called_once()
        self.assertFalse(score.call_args.kwargs["reanalyze_exhausted"])

    def test_read_only_app_order_and_hidden_archive(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string("""
from vk_desk_fixture import OWNER, VKDeskFixture
from vk_scout_ui import render_vk_sources
with VKDeskFixture().connected():
    render_vk_sources(OWNER, "test")
""").run()
        self.assertFalse(app.exception)
        headings = [row.value for row in app.markdown]
        self.assertIn("Новые кандидаты", headings[0])
        self.assertIn("VK-диалоги · 4", " ".join(headings))
        self.assertIn("Встречи · 1", " ".join(headings))
        for folder in ("Отправленные · 28", "Неотправленные · 6", "Отказ · 3"):
            self.assertTrue(any(folder in panel.label for panel in app.expander))
        self.assertNotIn("Кандидат 06", " ".join(headings))
        self.assertNotIn("Кандидат 42", " ".join(headings))


if __name__ == "__main__":
    unittest.main()
