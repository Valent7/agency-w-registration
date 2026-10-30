"""Offline regression tests for deleting unwanted candidates from Neona cards."""
import ast
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def load_nested_function(filename, function_name, namespace):
    source = Path(__file__).resolve().parents[1] / filename
    raw = source.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    assert len(nodes) == 1, function_name
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[function_name]


class DeleteCandidateTests(unittest.TestCase):
    def test_one_click_action_is_inside_card_and_keeps_sent_disabled(self):
        source = (Path(__file__).resolve().parents[1] /
                  "streamlit_app.py").read_text(encoding="utf-8")
        start = source.index("for number, contact in enumerate(\n                                selected_contacts,")
        card = source[start:source.index("render_person_card_2_0(", start)]
        self.assertIn("neona_remove_candidate_card_", card)
        self.assertIn("🗑️ Удалить кандидата", card)
        self.assertIn("disabled=already_contacted", card)
        self.assertIn("or contact_id in sent_ids", card)
        self.assertIn("exclude_neonia_candidate(", card)
        self.assertIn("remove_candidate_from_daily_neonia(", card)
        self.assertIn("st.rerun()", card)

    def test_delete_removes_candidate_draft_and_recommendations(self):
        store = {
            "neonia_excluded_candidates_7": [],
            "neonia_candidates_7": [
                {"telegram_id": 11, "name": "Unwanted"},
                {"telegram_id": 12, "name": "Keep"},
            ],
            "neonia_selected_candidates_7": [11, 12],
            "neonia_chat_candidates_7": {
                "21": [{"telegram_id":11}, {"telegram_id":12}],
            },
            "neona_first_message_drafts_7": {
                11: {"message":"draft","sent":False},
                12: {"message":"keep","sent":False},
            },
        }
        persisted = []
        ns = {
            "st":SimpleNamespace(session_state=store),
            "datetime":datetime,
            "ZoneInfo":ZoneInfo,
            "persist_workspace_if_changed":lambda *a,**k:persisted.append((a,k)),
            "telegram_id":7,
            "excluded_candidates_key":"neonia_excluded_candidates_7",
            "candidates_key":"neonia_candidates_7",
            "selected_candidates_key":"neonia_selected_candidates_7",
            "neona_drafts_key":"neona_first_message_drafts_7",
        }
        delete = load_nested_function(
            "streamlit_app.py", "exclude_neonia_candidate", ns)
        delete(11)
        delete(11)
        self.assertEqual([x["telegram_id"] for x in
                          store["neonia_excluded_candidates_7"]],[11])
        self.assertEqual([x["telegram_id"] for x in
                          store["neonia_candidates_7"]],[12])
        self.assertEqual(store["neonia_selected_candidates_7"],[12])
        self.assertEqual([x["telegram_id"] for x in
                          store["neonia_chat_candidates_7"]["21"]],[12])
        self.assertNotIn(11,store["neona_first_message_drafts_7"])
        self.assertIn(12,store["neona_first_message_drafts_7"])
        self.assertEqual(len(persisted),2)

    def test_sent_draft_is_not_erased_from_archive(self):
        store={
            "neonia_candidates_7":[{"telegram_id":11}],
            "neona_first_message_drafts_7":{
                11: {"message":"sent conversation","sent":True},
            },
        }
        ns={"st":SimpleNamespace(session_state=store),
            "datetime":datetime,"ZoneInfo":ZoneInfo,
            "persist_workspace_if_changed":lambda *a,**k:None,
            "telegram_id":7,
            "excluded_candidates_key":"neonia_excluded_candidates_7",
            "candidates_key":"neonia_candidates_7",
            "selected_candidates_key":"neonia_selected_candidates_7",
            "neona_drafts_key":"neona_first_message_drafts_7",
        }
        delete=load_nested_function(
            "streamlit_app.py","exclude_neonia_candidate",ns)
        delete(11)
        self.assertTrue(store["neona_first_message_drafts_7"][11]["sent"])

    def test_removal_cleans_daily_queue_and_keeps_other_leads(self):
        saved = []
        day = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
        task={"id":"t1","result":{"daily_candidate_feed":{
            "daily_target":5,
            "daily_batches":{day:{
                "candidate_ids":[11,12,13],"approved_ids":[11,12],
            }}
        },"selected_candidate_ids":[11,12],
          "contact_progress":{"11":{"status":"waiting"},"12":{"status":"waiting"}}}}
        ns={
            "datetime":datetime,"BERLIN":ZoneInfo("Europe/Berlin"),"UTC":timezone.utc,
            "_load_tasks":lambda owner:([task],None),
            "_active_weekly_meeting_task":lambda owner:task,
            "_daily_candidate_task":lambda owner:task,
            "_candidate_feed_container":lambda result:("daily_candidate_feed",dict(result["daily_candidate_feed"])),
            "_normalize_int_ids":lambda seq:[int(x) for x in seq],
            "_update_task":lambda owner,task_id,change:saved.append(change),
        }
        remove=load_nested_function(
            "stagirite_center.py","remove_candidate_from_daily_neonia",ns)
        result=remove(7,11,"t1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertEqual(result["candidate_ids"],[12,13])
        self.assertEqual(result["approved_ids"],[12])
        updated=saved[0]["result"]
        self.assertNotIn(11,updated["selected_candidate_ids"])
        self.assertNotIn("11",updated["contact_progress"])
        self.assertIn(11,updated["daily_candidate_feed"]["daily_batches"][day]["removed_candidate_ids"])


if __name__ == "__main__":
    unittest.main()
