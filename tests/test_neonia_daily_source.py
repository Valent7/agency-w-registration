"""Offline tests for the green Neonia Telegram-group source button.

Active group authors are the primary source; personal contacts never fill gaps.
"""
import unittest
from datetime import date
from unittest.mock import patch

import neonia_daily_source as policy
import neonia_chat_daily_worker as worker
import telegram_scout_worker as scout_storage


class DailySourcePolicy(unittest.TestCase):
    def test_group_never_matches_contact_batch(self):
        group=9211
        contact_batch={"candidate_ids":[1,2,3,4,5],
                       "source":"neonia_sequential_queue"}
        self.assertFalse(policy.matches_daily_batch(contact_batch,"chats",group))
        correct={"candidate_ids":[6,7,8,9,10],
                 "source":"neonia_sequential_queue",
                 "source_signature":"chats:9211"}
        self.assertTrue(policy.matches_daily_batch(correct,"chats",group))
        self.assertFalse(policy.matches_daily_batch(correct,"chats",9212))

    def test_group_selected_strictly(self):
        s={"daily_source_mode":"chats",
           "selected_source_chat":9211,
           "chats":[{"chat_id":9211,"type":"Супергруппа","title":"Preferred"},
                    {"chat_id":9212,"type":"Супергруппа","title":"Other"}]}
        self.assertEqual(policy.selected_chat_details(s)["title"],"Preferred")
        s["selected_source_chat"]=0
        self.assertIsNone(policy.selected_chat_details(s))
        self.assertEqual(policy.source_signature(*policy.owner_source(s)),"chats:unset")

    def test_no_contacts_fallback_when_group_missing(self):
        s={"daily_source_mode":"chats","selected_source_chat":9211,
           "chats":[{"chat_id":9212,"title":"Wrong","type":"Группа"}]}
        self.assertIsNone(policy.selected_chat_details(s))


class DummyStorage:
    def __init__(self):
        self.rows={}
        self.tasks={}
        self.task_exists=False
        self.last_payload=None

    def _workspace_row(self, owner_id):
        return {"encrypted_state":"fake","updated_at":"v1"}
    def _decrypt_json(self, value):
        return self.state
    def _encrypt_json(self, data):
        self.state=data
        return "fake"
    def _normalize_candidate_list(self, data):
        return data
    def _merge_candidate_results(self, old, new):
        ids={int(v["telegram_id"]):v for v in old}
        ids.update({int(v["telegram_id"]):v for v in new})
        return list(ids.values())
    def _shown_candidate_ids(self,*args,**kwargs):
        return {111}
    def _telegram_api_credentials(self):
        return (123,"fake")
    def _sent_ids(self,state):
        return set()
    def _blocked_ids(self,state):
        return set()
    def _known_contact_ids(self,state):
        return set()
    def _classify_workability(self,source,**kwargs):
        return {"work_state":"available","selection_blocked":False,"block_reason":""}
    def _split_analysis_value(self,items):
        return (items,[])
    def _analyze_candidates(self,passport,items):
        result=[]
        for item in items:
            result.append({
                "telegram_id":int(item["telegram_id"]),
                "business_relevant":True,
                "business_evidence":["публичный деловой сигнал"],
                "selection_blocked":False,
                "potential_interest":"высокий" if int(item["telegram_id"]) % 2 else "средний",
                "score":75 if int(item["telegram_id"]) % 2 else 50,
                "warmth":"холодный",
                "status":"Проанализирован",
            })
        return result
    def _candidate_priority(self,item):
        return (-int(item.get("score") or 0),int(item["telegram_id"]))
    def _load_tasks(self, owner_id):
        return [self.tasks["row"]] if self.task_exists else []
    def _active_weekly_task(self,tasks,day):
        return None
    def _daily_task(self,tasks):
        return tasks[0] if tasks else None
    def _ensure_daily_task(self, owner_id,tasks):
        self.task_exists=True
        self.tasks["row"]={"id":"abc","result":{"daily_candidate_feed":{"daily_batches":{}}}}
        return self.tasks["row"]
    def _sb_get(self,table,params):
        if table=="agency_stagirite_tasks":
            return [self.tasks["row"]]
        return []
    def _sb_patch(self,table,filters,payload,**kw):
        self.last_payload=payload
        if table=="agency_stagirite_tasks":
            self.tasks["row"]={**self.tasks["row"],**payload}
            return [self.tasks["row"]]
        return [{"updated_at":"v2"}]


class CandidateExclusionTests(unittest.TestCase):
    def test_manual_exclusion_is_a_hard_block_for_future_selection(self):
        state = {
            "blocked_first_messages": [{"telegram_id": 101}],
            "excluded_candidates": [
                {"telegram_id": 202, "reason": "Удалён владельцем"},
            ],
        }
        self.assertEqual(scout_storage._blocked_ids(state), {101, 202})


class DailyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.storage=DummyStorage()
        self.storage.state={
            "schema_version":9, "daily_source_mode":"chats",
            "selected_source_chat":9211,
            "chats":[{"chat_id":9211,"type":"Супергруппа","title":"Preferred Group",
                      "participants_count":12888}],
            "processed_candidates":[111],
            "chat_offsets":{},
            "source_progress":{},
            "candidates":[],
            "passport":{"analysis":{"target":"деловые люди, которым полезна передача рутины ИИ"}},
        }
    def test_group_only_daily_five_comes_from_active_authors_and_is_ranked(self):
        async def fake_active(session,apiid,hash,chat,excluded,desired,message_limit):
            self.assertEqual(chat["title"],"Preferred Group")
            self.assertIn(111,excluded)
            return ([{
                "telegram_id":i,"name":str(i),"source_chat_id":9211,
                "source_chat_title":"Preferred Group","source_type":"telegram_chat",
                "source":"Telegram-чат: Preferred Group",
                "public_info":"Обсуждает бизнес, команду и автоматизацию рабочих процессов в публичном сообщении.",
                "group_messages":["Как автоматизировать работу команды и освободить время для клиентов?"],
                "activity_eligible":True,"telegram_activity_label":"писал сегодня",
                "_stagirite_activity_tier":0,"has_message_history":False,"recent_messages":[],
            } for i in range(201,207)],120)
        async def should_not_need_members(*args,**kwargs):
            raise AssertionError("participant list must not be required when active authors provide five")
        with patch.object(worker,"_read_active_group_authors",new=fake_active), \
             patch.object(worker,"_read_selected_members",new=should_not_need_members):
            result=worker.process(77,"fake",self.storage.state,self.storage)
        self.assertEqual(result["status"],"prepared",result)
        self.assertEqual(result["count"],5)
        stored_batch=self.storage.tasks["row"]["result"]["daily_candidate_feed"]["daily_batches"][
            worker.datetime.now(worker.BERLIN).date().isoformat()]
        self.assertEqual(stored_batch["source_signature"],"chats:9211")
        self.assertEqual(stored_batch["source"],"neonia_group_activity_targeting")
        self.assertEqual(len(stored_batch["candidate_ids"]),5)
        self.assertNotIn(111,stored_batch["candidate_ids"])
        self.assertTrue(all(201 <= cid <= 206 for cid in stored_batch["candidate_ids"]))
        self.assertEqual(self.storage.state["source_progress"]["selection_method"],"active_group_authors")
        self.assertEqual(self.storage.state["source_progress"]["today"],5)

    def test_local_fallback_requires_practical_agency_signal(self):
        for text in (
            "Обсуждение бизнеса и автоматизации команды",
            "Не успеваю отвечать на сообщения клиентов, слишком много рутины",
            "Каждый день веду соцсети, публикации и заявки вручную",
        ):
            with self.subTest(text=text):
                self.assertTrue(worker._local_business_signal(
                    {"public_info":text,"group_messages":[]}))
        self.assertFalse(worker._local_business_signal(
            {"public_info":"Доброе утро всем","group_messages":[]}))

    def test_missing_group_no_fallback(self):
        self.storage.state["selected_source_chat"]=9999
        result=worker.process(77,"fake",self.storage.state,self.storage)
        self.assertEqual(result["status"],"skip")
        self.assertFalse(self.storage.task_exists)

    def test_already_prepared_batch_stays_same(self):
        self.storage.task_exists=True
        day=worker.datetime.now(worker.BERLIN).date().isoformat()
        self.storage.tasks["row"]={"id":"abc",
            "result":{"daily_candidate_feed":{"daily_batches":{day:{
                "candidate_ids":[201,202,203,204,205],
                "source":"neonia_sequential_queue",
                "source_signature":"chats:9211"}}}}}
        result=worker.process(77,"fake",self.storage.state,self.storage)
        self.assertEqual(result["status"],"ready")


if __name__=="__main__":
    unittest.main()
