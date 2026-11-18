import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from content_factory_store import SQLiteStore
from content_factory_live_video import LiveVideoPending, LiveVideoUncertain
from kadrian_actions import command, enqueue_runway, process_auto_job, status_text


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name)/'actions.sqlite')
        self.project = self.store.save_project(77, {'name':'Film','product':'Story','audience':'Owner'})
        self.client = Mock(model='gen4.5')
        self.client.preflight.return_value = {'credit_balance':529}
        self.client.estimate_credits.side_effect = lambda seconds: 12*sum(seconds)
        self.client.create_scene.return_value = 'task-owned-1'
        self.client.task_status.return_value = {'status':'pending'}
        self.client.download.return_value = b'video'
        for target, kwargs in [
            ('content_factory_runtime.resolve_live_video_client', {'return_value':self.client}),
            ('agency_access_control.is_agency_access_revoked', {'return_value':False}),
            ('content_factory_billing.billing_enforced', {'return_value':False})]:
            p = patch(target, **kwargs); p.start(); self.addCleanup(p.stop)
        self.spec = {'action':'create','scope':'scene','brief':'Кот потягивается, встаёт и идёт к столу. Без надписей.', 'duration':5, 'aspect':'9:16'}
        self.worker = Mock(store=self.store, generator=Mock())
        self.worker.generator._illustration.return_value = b'image'
        self.worker._update.side_effect = lambda job, **changes: self.store.update_job(77, job['id'], changes)
        self.worker._finish.side_effect = self.worker._update.side_effect
        self.worker.clock.return_value = datetime.now(timezone.utc)

    def enqueue(self, request='request-one'):
        return enqueue_runway(self.store,77,self.project,self.spec,'cartoon',request)

    def test_chat_command_plans_action_without_claiming_balance(self):
        ask=Mock(return_value=json.dumps(self.spec))
        self.assertEqual(command(ask,self.project,'Создавай','Кот в кабинете','','cartoon')['scope'],'scene')
        self.assertIn('только JSON',ask.call_args.args[0])
        self.assertIn('529',status_text(self.store,77,self.project))

    def test_repeated_command_keeps_one_active_job(self):
        one,created=self.enqueue()
        two,created_again=self.enqueue('request-two')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(one['id'],two['id'])

    def test_shortage_queues_and_resumes_after_topup_without_paid_retry(self):
        self.client.preflight.return_value={'credit_balance':10}
        job,_=self.enqueue()
        self.assertIn('не хватает 50',job['progress']['message'])
        with self.assertRaises(LiveVideoPending): process_auto_job(self.worker,job,self.project)
        self.worker.generator._illustration.assert_not_called()
        self.client.create_scene.assert_not_called()
        self.client.preflight.return_value={'credit_balance':529}
        with self.assertRaises(LiveVideoPending): process_auto_job(self.worker,job,self.project)
        self.assertEqual(self.client.create_scene.call_count,1)

    def test_image_and_video_are_created_once_then_result_saved(self):
        job,_=self.enqueue()
        with self.assertRaises(LiveVideoPending): process_auto_job(self.worker,job,self.project)
        saved=self.store.get_job(77,job['id'])
        self.assertEqual(saved['production']['task_id'],'task-owned-1')
        self.client.task_status.return_value={'status':'succeeded','url':'https://video.example/result.mp4'}
        self.assertEqual(process_auto_job(self.worker,saved,self.project),'generated')
        self.assertEqual(self.client.create_scene.call_count,1)
        self.worker.generator._illustration.assert_called_once()
        ready=self.store.get_job(77,job['id'])
        self.assertEqual(ready['state'],'ready')
        self.assertEqual(self.store.read_asset(77,ready['assets'][0]),b'video')

    def test_unknown_create_ack_cannot_order_second_paid_scene(self):
        job,_=self.enqueue()
        self.client.create_scene.side_effect=LiveVideoUncertain('unknown')
        with self.assertRaises(LiveVideoUncertain): process_auto_job(self.worker,job,self.project)
        saved=self.store.get_job(77,job['id'])
        with self.assertRaises(LiveVideoUncertain): process_auto_job(self.worker,saved,self.project)
        self.assertEqual(self.client.create_scene.call_count,1)

    def test_references_from_another_project_never_enqueue(self):
        with self.assertRaises(ValueError):
            enqueue_runway(self.store,77,self.project,self.spec,'cartoon','request-one',{'storage_path':'88/other/job/frame.png'})
        self.client.create_scene.assert_not_called()

    def test_known_openai_quota_failure_does_not_repeat_successful_image(self):
        from content_factory_generation import GenerationError
        from kadrian_production import process_provider_job
        job,_=self.enqueue()
        self.worker.generator._illustration.side_effect=GenerationError('quota',provider_details={'code':'insufficient_quota'})
        with self.assertRaises(LiveVideoPending) as error: process_provider_job(self.worker,job,self.project)
        self.assertIn('OpenAI',str(error.exception))
        saved=self.store.get_job(77,job['id'])
        self.assertFalse(saved['production']['image_starting'])
        self.client.create_scene.assert_not_called()

    def test_film_waits_without_images_and_reuses_its_plan_after_topup(self):
        from tests.test_content_factory_generation import visual_plan
        plan = {'format':'reel','title':'Film','text':'Story',
                'narration':' '.join(['История']*50),'visual_plan':visual_plan(4)}
        self.spec = {**self.spec,'scope':'film'}
        job,_ = self.enqueue()
        with patch('kadrian_actions.production_plan',return_value=plan) as planner:
            self.client.preflight.return_value={'credit_balance':100}
            with self.assertRaises(LiveVideoPending): process_auto_job(self.worker,job,self.project)
            self.worker.generator._illustration.assert_not_called()
            self.worker._generate.assert_not_called()
            saved=self.store.get_job(77,job['id'])
            self.client.preflight.return_value={'credit_balance':529}
            process_auto_job(self.worker,saved,self.project)
            planner.assert_called_once()
            self.worker._generate.assert_called_once()
            handed=self.worker._generate.call_args.args[0]
            self.assertIsNone(handed['kadrian_provider'])
            self.assertEqual(handed['approved_reference'],saved['approved_reference'])

    def test_chat_message_enqueues_without_an_approval_button(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_string('import streamlit as st\nfrom kadrian_studio_ui import render_kadrian\nrender_kadrian(77,st.session_state["ask"])')
        app.session_state['ask']=Mock(return_value=json.dumps(self.spec))
        with patch('kadrian_studio_ui._runtime',return_value=(self.store,None,None)),patch('kadrian_studio_ui.connection_status',return_value={}):
            app.run()
            app.chat_input[0].set_value('Создай первую сцену').run()
            self.assertFalse(app.exception)
            jobs=self.store.list_jobs(77,self.project['id'])
            self.assertEqual(len(jobs),1)
            self.assertEqual(jobs[0]['kadrian_provider'],'runway_auto')
            self.assertEqual(len(self.store.get_project(77,self.project['id'])['kadrian_chat']['turns']),1)
