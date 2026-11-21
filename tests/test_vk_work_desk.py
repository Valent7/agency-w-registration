from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import vk_scout
import vk_scout_worker


class VKWorkDeskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).isoformat()
        self.assignments = {
            71: [
                {"id": 12, "vk_user_id": 4, "status": "invited", "invited_at": self.now},
                {"id": 11, "vk_user_id": 4, "status": "invited", "invited_at": self.now},
                {"id": 10, "vk_user_id": 5, "status": "skipped", "invited_at": self.now},
            ],
            72: [{"id": 20, "vk_user_id": 4, "status": "invited", "invited_at": self.now}],
        }
        self.states = {71: [], 72: []}
        self.meetings = {71: [], 72: []}
        self.contact = vk_scout._vk_dialog_contact_id(4)

    def storage(self, table, filters):
        if table == "agency_vk_candidates":
            return [{"vk_user_id": 4, "first_name": "Тест"}]
        owner = int(filters["owner_telegram_id"].split(".")[1])
        return {
            "agency_vk_assignments": self.assignments,
            "agency_dialog_states": self.states,
            "agency_meetings": self.meetings,
        }[table][owner]

    def test_waiting_reply_draft_restart_and_owner_isolation(self):
        with patch.object(vk_scout, "_sb_get", side_effect=self.storage):
            first, archive = vk_scout.load_vk_private_desk(71)
            self.assertEqual((first["group"], first["id"], archive["group"]), ("waiting", 12, "history"))
            self.states[71].append({
                "contact_telegram_id": self.contact, "stage": "idle",
                "context": {
                    "vk_manual_pending_inbound": "Сколько времени занимает встреча?",
                    "vk_manual_pending_reply": "Обычно 30 минут.",
                    "vk_manual_last_processed_at": self.now,
                },
            })
            first, _ = vk_scout.load_vk_private_desk(71)
            self.assertEqual(first["group"], "active")
            self.assertTrue(first["recent_reply"])
            self.assertEqual(first["draft"], "Обычно 30 минут.")
            self.assertEqual(vk_scout.load_vk_private_desk(72)[0]["group"], "waiting")

            # Existing rows can have only relationship memory after a draft was copied.
            self.states[71][0]["context"] = {
                "vk_manual_last_processed_at": self.now,
                "relationship_memory": {"turns": [{"incoming": "Уточню завтра"}]},
            }
            reopened, _ = vk_scout.load_vk_private_desk(71)
            self.assertEqual(reopened["inbound"], "Уточню завтра")
            self.assertFalse(reopened["draft"])

    def test_community_reply_is_green_with_community_source(self):
        self.states[71].append({
            "contact_telegram_id": self.contact, "stage": "idle",
            "context": {
                "vk_last_incoming_text": "Ответ в сообществе",
                "vk_last_processed_at": self.now,
                "vk_manual_pending_reply": "Черновик личного сообщения",
            },
        })
        with patch.object(vk_scout, "_sb_get", side_effect=self.storage):
            row = vk_scout.load_vk_private_desk(71)[0]
        self.assertEqual((row["group"], row["source"], row["inbound"]),
                         ("active", "community", "Ответ в сообществе"))
        self.assertIsNone(row["draft"])
        self.assertEqual(row["dialog"]["pending_reply"], "Черновик личного сообщения")

    def test_only_confirmed_saved_meeting_for_the_same_contact_is_purple(self):
        state = {
            "contact_telegram_id": self.contact, "stage": "awaiting_confirmation",
            "context": {"meeting_id": 9, "vk_manual_last_inbound_text": "Мне подходит"},
        }
        self.states[71].append(state)
        self.meetings[72].append({"id": 9, "contact_telegram_id": self.contact, "start_at": self.now})
        with patch.object(vk_scout, "_sb_get", side_effect=self.storage) as storage:
            self.assertEqual(vk_scout.load_vk_private_desk(71)[0]["group"], "active")
            state["stage"] = "scheduled"
            self.assertEqual(vk_scout.load_vk_private_desk(71)[0]["group"], "active")
            self.meetings[71] = [{"id": 9, "contact_telegram_id": self.contact - 1, "start_at": self.now}]
            self.assertEqual(vk_scout.load_vk_private_desk(71)[0]["group"], "active")
            self.meetings[71][0]["contact_telegram_id"] = self.contact
            self.assertEqual(vk_scout.load_vk_private_desk(71)[0]["group"], "meeting")
            self.assertEqual(storage.call_args.args[1]["status"], "eq.Подтверждена")
            self.assertTrue(storage.call_args.args[1]["end_at"].startswith("gt."))

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
        def database(table, filters):
            self.assertEqual(table, "agency_vk_candidates")
            i = filters["offset"]
            return [
                {"vk_user_id": uid, "source_community_id": 22}
                for uid in range(i + 1, min(i + 201, target_id + 1))
            ]

        with patch.multiple(
            vk_scout,
            load_vk_sources=lambda owner: [{"community_id": 22}],
            _vk_owner_pages=lambda *args: [{"vk_user_id": uid} for uid in range(1, target_id)],
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

    def test_read_only_ui_order_and_waiting_folder(self):
        from streamlit.testing.v1 import AppTest

        script = """
from unittest.mock import patch
import streamlit as st
import vk_scout_ui as ui
from contextlib import ExitStack
with ExitStack() as mocks:
    mocks.enter_context(patch.object(ui, "_render_today_vk_candidates",
        side_effect=lambda *a, **k: st.markdown("### 💙 VK 5 на сегодня · отбор Неонии")))
    mocks.enter_context(patch.object(ui, "_sb_get", return_value=[]))
    mocks.enter_context(patch.object(ui, "load_vk_private_desk", return_value=[
        {"id": 1, "vk_user_id": 4, "first_name": "Ждущий", "group": "waiting",
         "status": "invited", "meeting": None, "turns": [], "dialog": {}},
        {"id": 2, "vk_user_id": 5, "first_name": "Ответил раньше", "group": "active",
         "status": "invited", "meeting": None, "turns": [], "dialog": {}, "inbound_at": "2026-11-01T09:00:00+00:00"},
        {"id": 3, "vk_user_id": 6, "first_name": "Ответил позже", "group": "active",
         "status": "dialogue", "meeting": None, "turns": [], "dialog": {}, "inbound_at": "2026-11-01T10:00:00+00:00",
         "inbound": "Последний ответ", "draft": "Черновик", "recent_reply": True},
    ]))
    for name in ("_render_vk_dialog_center", "_render_vk_feed_radar",
                 "_render_known_vk_contacts", "_render_vk_source_settings",
                 "_render_vk_private_dialog_followup"):
        mocks.enter_context(patch.object(ui, name))
    ui.render_vk_sources(71)
"""
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertIn("VK 5 на сегодня", app.markdown[0].value)
        self.assertIn("Активные диалоги VK", app.markdown[1].value)
        self.assertEqual([metric.value for metric in app.metric], ["2", "0", "1"])
        self.assertIn("Ответил позже", app.text[0].value)
        self.assertIn("Последний ответ", app.text[1].value)
        self.assertIn("Черновик", app.text[2].value)
        self.assertIn("Ответил раньше", app.text[3].value)
        folder = next(item for item in app.expander if item.label.startswith("📁 Ожидаем ответа ·"))
        self.assertEqual(len(folder.text), 1)
        self.assertIn("Ждущий", folder.text[0].value)

    def test_manual_reply_moves_waiting_card_and_prepares_only_once(self):
        from streamlit.testing.v1 import AppTest

        script = """
from unittest.mock import patch
import streamlit as st
import vk_scout_ui as ui

def load(owner_id):
    reply = st.session_state.get("registered_reply")
    return [{
        "id": 1, "vk_user_id": 4, "first_name": "Контакт", "status": "invited",
        "group": "active" if reply else "waiting", "meeting": None, "turns": [],
        "inbound": reply, "recent_reply": bool(reply), "draft": "Черновик" if reply else "",
        "dialog": {"pending_inbound": reply, "pending_reply": "Черновик" if reply else ""},
    }]

def register(assignment_id, owner_id, text):
    assert (assignment_id, owner_id) == (1, 71)
    st.session_state["registered_reply"] = text
    st.session_state["registration_count"] = st.session_state.get("registration_count", 0) + 1

with patch.object(ui, "load_vk_private_desk", side_effect=load), patch.object(
    ui, "process_manual_vk_private_reply", side_effect=register,
), patch.object(ui, "mark_manual_vk_private_reply_sent", side_effect=AssertionError("Unexpected send")):
    ui._render_vk_private_desk(71)
"""
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertEqual([metric.value for metric in app.metric], ["0", "0", "1"])
        app.text_area[0].set_value("Да, расскажите подробнее").run()
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual([metric.value for metric in app.metric], ["1", "0", "0"])
        self.assertTrue(any("Новый ответ" in element.value for element in app.success))
        self.assertEqual(app.session_state["registration_count"], 1)
        app.run()
        self.assertEqual(app.session_state["registration_count"], 1)


if __name__ == "__main__":
    unittest.main()
