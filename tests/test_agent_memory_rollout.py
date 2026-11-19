"""Single-project pilot: runtime, legacy chat, worker and UI stay isolated.

Only synthetic local SQLite is used. A trap Repository asserts that excluded
projects never even open the new memory persistence adapter.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from agent_memory import Memory, Principal, Scope, MemoryError
from agent_memory_store import Repository
from agent_memory_runtime import (enabled, prepare_project, project_memory, before_job,
    drain_turns, migrate_legacy, reconcile_project, record_job_state)
from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore
from kadrian_memory import save_turn


class ProjectRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SQLiteStore(Path(self.tmp.name)/'rollout.sqlite3')
        self.a=self.store.save_project(11,{'name':'Same name','facts':'Legacy A'})
        self.b=self.store.save_project(11,{'name':'Same name','facts':'Legacy B','kadrian_notes':'Keep B'})
        self.c=self.store.save_project(22,{'name':'Other owner'})
        self.env=patch.dict(os.environ,{'AGENCY_MEMORY_V1':'1','AGENCY_MEMORY_V1_OWNERS':'11,22',
                                      'AGENCY_MEMORY_V1_PROJECT_ID':self.a['id']})
        self.env.start();self.addCleanup(self.env.stop)
        self.repo=Repository(self.store,initialize=True)

    def count(self,project):
        with self.store._db() as db:
            return db.execute('SELECT count(*) FROM agent_memory_revisions r JOIN agent_memory_workspaces w USING(owner_id,agent_id,workspace_id) WHERE w.project_id=?',(project['id'],)).fetchone()[0]

    def job(self,project):
        return self.store.enqueue_job(project['owner_id'],project['id'],{'platform':'instagram','format':'reel',
            'state':'queued','scheduled_at':iso(utc_now()),'dedupe_key':uuid4().hex,
            'kadrian_auto':True,'requested_by_owner':True,'topic':'Synthetic task'})

    def test_owner_allowlist_alone_never_enables_any_project(self):
        with patch.dict(os.environ,{'AGENCY_MEMORY_V1_PROJECT_ID':''}):
            self.assertFalse(enabled());self.assertFalse(enabled(11))
            self.assertFalse(enabled(11,self.a['id']));self.assertFalse(enabled(11,self.b['id']))

    def test_exact_one_uuid_even_with_same_owner_and_same_display_name(self):
        self.assertTrue(enabled(11,self.a['id']))
        self.assertFalse(enabled(11,self.b['id']))
        self.assertFalse(enabled(22,self.c['id']))
        self.assertFalse(enabled(None,self.a['id']))
        self.assertFalse(enabled('11',self.a['id']))
        with patch.dict(os.environ,{'AGENCY_MEMORY_V1_OWNERS':'22'}):
            self.assertFalse(enabled(11,self.a['id']))

    def test_missing_invalid_list_and_wildcard_selectors_fail_closed(self):
        for selector in ['', '*', self.a['name'], self.a['id']+','+self.b['id'],
                         json.dumps([self.a['id']]), '../workspace', 'not-a-uuid']:
            with self.subTest(selector=selector),patch.dict(os.environ,{'AGENCY_MEMORY_V1_PROJECT_ID':selector}):
                self.assertFalse(enabled(11,self.a['id']));self.assertFalse(enabled(11,self.b['id']))
        with patch.dict(os.environ,{'AGENCY_MEMORY_V1':'0'}):
            self.assertFalse(enabled(11,self.a['id']))

    def test_selected_project_reads_and_writes_but_other_projects_are_untouched(self):
        original_a=deepcopy(self.store.get_project(11,self.a['id']))
        original_b=deepcopy(self.store.get_project(11,self.b['id']))
        result=prepare_project(self.store,11,self.a,'a-task',{'task':'A'})
        self.assertIn('_agent_memory',result);self.assertGreater(self.count(self.a),0)
        with patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
            self.assertIs(prepare_project(self.store,11,self.b,'b-task',{}),self.b)
            self.assertIs(before_job(self.store,self.job(self.b),self.b),self.b)
            drain_turns(self.store,11,self.b)
            reconcile_project(self.store,11,self.b)
            record_job_state(self.store,self.job(self.b))
            with self.assertRaises(MemoryError):project_memory(self.store,11,self.b)
            with self.assertRaises(MemoryError):migrate_legacy(self.store,11,self.b)
        self.assertEqual(self.count(self.b),0);self.assertEqual(self.count(self.c),0)
        self.assertEqual(self.store.get_project(11,self.a['id']),original_a)
        self.assertEqual(self.store.get_project(11,self.b['id']),original_b)

    def test_other_project_keeps_legacy_chat_without_new_memory(self):
        with patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
            save_turn(self.store,11,self.b['id'],'b-turn','Owner B question','Answer B','Brief B','Request B')
        b=self.store.get_project(11,self.b['id'])
        self.assertEqual(b['kadrian_chat']['turns'][0]['prompt'],'Owner B question')
        self.assertEqual(b['kadrian_notes'],'Keep B');self.assertEqual(b['facts'],'Legacy B')
        self.assertEqual(self.count(self.b),0)

    def test_worker_other_project_keeps_existing_outbox_and_adds_no_memory_fields(self):
        from content_factory_worker import Worker
        queued=self.job(self.b)
        self.store.update_job(11,queued['id'],{'memory_outbox':[{'id':'old-history'}],
                                              'memory_snapshot':{'legacy':'preserve'}})
        claimed=self.store.claim_job('rollout-worker',iso(utc_now()),owner_id=11,project_id=self.b['id'])
        worker=Worker(self.store,worker_id='rollout-worker')
        with patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
            saved=worker._update(claimed,progress={'stage':'legacy'})
        self.assertEqual(saved['memory_outbox'],[{'id':'old-history'}])
        self.assertEqual(saved['memory_snapshot'],{'legacy':'preserve'})
        self.assertNotIn('memory_journaled',saved);self.assertEqual(self.count(self.b),0)

    def test_worker_selected_project_records_outbox_and_prepares_snapshot(self):
        from content_factory_worker import Worker
        queued=self.job(self.a)
        claimed=self.store.claim_job('rollout-worker',iso(utc_now()),owner_id=11,project_id=self.a['id'])
        worker=Worker(self.store,worker_id='rollout-worker')
        saved=worker._update(claimed,progress={'stage':'accepted'})
        self.assertGreater(self.count(self.a),0)
        stored=self.store.get_job(11,queued['id'])
        self.assertEqual(stored['memory_outbox'],[]);self.assertTrue(stored['memory_journaled'])
        prepared=before_job(self.store,saved,self.a)
        self.assertEqual(prepared['_agent_memory']['project']['scope']['project_id'],self.a['id'])
        self.assertEqual(self.count(self.b),0)

    def test_switching_project_preserves_a_history_and_stops_its_integration(self):
        prepared=prepare_project(self.store,11,self.a,'original-task',{'task':'A'})
        first_count=self.count(self.a)
        with patch.dict(os.environ,{'AGENCY_MEMORY_V1_PROJECT_ID':self.b['id']}):
            self.assertFalse(enabled(11,self.a['id']));self.assertTrue(enabled(11,self.b['id']))
            with patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
                plain=prepare_project(self.store,11,prepared,'stale-cache',{})
                self.assertNotIn('_agent_memory',plain)
                self.assertIn('_agent_memory',prepared)  # original object/history not destroyed
                plain=before_job(self.store,self.job(self.a),prepared)
                self.assertNotIn('_agent_memory',plain)
            prepare_project(self.store,11,self.b,'new-b-task',{'task':'B'})
        self.assertEqual(self.count(self.a),first_count);self.assertGreater(self.count(self.b),0)
        self.assertTrue(enabled(11,self.a['id']));self.assertFalse(enabled(11,self.b['id']))

    def test_disabling_project_does_not_erase_history(self):
        prepare_project(self.store,11,self.a,'original-task',{})
        count=self.count(self.a)
        with patch.dict(os.environ,{'AGENCY_MEMORY_V1_PROJECT_ID':''}):
            prepare_project(self.store,11,self.a,'disabled-task',{})
        self.assertEqual(self.count(self.a),count)

    def test_selected_uuid_does_not_bypass_owner_authentication(self):
        with self.assertRaises(MemoryError):project_memory(self.store,22,self.a)
        forged={**self.a,'owner_id':22}
        with self.assertRaises(MemoryError):project_memory(self.store,22,forged)
        self.assertEqual(self.count(self.a),0)

    def test_memory_ui_hidden_outside_selected_project(self):
        from streamlit.testing.v1 import AppTest
        script=f'''
import streamlit as st
from content_factory_store import SQLiteStore
from agent_memory_ui import render_memory
store=SQLiteStore({str(self.store.path)!r})
render_memory(store,11,store.get_project(11,st.session_state['project_id']))
'''
        app=AppTest.from_string(script)
        app.session_state['project_id']=self.a['id'];app.run(timeout=20)
        self.assertFalse(app.exception);self.assertFalse(app.error)
        self.assertTrue(any(s.label=='Область памяти' for s in app.selectbox))
        app.session_state['project_id']=self.b['id']
        with patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
            app.run(timeout=20)
        self.assertFalse(app.exception);self.assertFalse(app.error)
        self.assertFalse(app.selectbox);self.assertFalse(app.file_uploader)
        self.assertEqual(self.count(self.b),0)

    def test_full_chat_other_project_renders_without_memory_adapter(self):
        from streamlit.testing.v1 import AppTest
        self.store.list_projects=Mock(return_value=[self.b])
        app=AppTest.from_string('import streamlit as st\nfrom kadrian_studio_ui import render_kadrian\nrender_kadrian(11,st.session_state["ask"])')
        app.session_state['ask']=Mock(return_value=json.dumps({'action':'discuss','reply':'Legacy answer'}))
        with patch('kadrian_studio_ui._runtime',return_value=(self.store,None,None)), \
             patch('kadrian_studio_ui.connection_status',return_value={}), \
             patch('agent_memory_runtime.Repository',side_effect=AssertionError('Excluded memory accessed')):
            app.run(timeout=20)
            app.chat_input[0].set_value('Обсудим замысел').run(timeout=20)
        self.assertFalse(app.exception);self.assertFalse(app.error)
        self.assertEqual(self.count(self.b),0)
        self.assertEqual(self.store.get_project(11,self.b['id'])['kadrian_chat']['turns'][0]['answer'],'Legacy answer')

    def test_memory_outage_does_not_hide_owner_stop(self):
        from streamlit.testing.v1 import AppTest
        self.store.list_projects=Mock(return_value=[self.a])
        ask=Mock(side_effect=AssertionError('A stop must not call a model'))
        app=AppTest.from_string('import streamlit as st\nfrom kadrian_studio_ui import render_kadrian\nrender_kadrian(11,st.session_state["ask"])')
        app.session_state['ask']=ask
        with patch('kadrian_studio_ui._runtime',return_value=(self.store,None,None)), \
             patch('kadrian_studio_ui.connection_status',return_value={}), \
             patch('agent_memory_runtime.reconcile_project',side_effect=MemoryError('offline')):
            app.run(timeout=20)
            stop=next(b for b in app.button if b.label=='⏹ Остановить создание сцен')
            stop.click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(self.store.get_project(11,self.a['id'])['kadrian_production_paused'])
        ask.assert_not_called()

    def test_memory_enabled_worker_still_honors_owner_pause(self):
        from content_factory_worker import Worker, PauseRequested
        queued=self.job(self.a)
        queued=self.store.update_job(11,queued['id'],{'studio_master':True})
        claimed=self.store.claim_job('paused-worker',iso(utc_now()),owner_id=11,project_id=self.a['id'])
        p=self.store.get_project(11,self.a['id'])
        self.store.update_project_if_current(11,p['id'],{'kadrian_production_paused':True},p['updated_at'])
        with self.assertRaises(PauseRequested):
            Worker(self.store,worker_id='paused-worker')._update(claimed,progress={'stage':'must not advance'})
        self.assertEqual(self.count(self.a),0)
