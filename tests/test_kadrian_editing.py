"""Offline regressions for the owner's music and stop requests."""
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4
from content_factory_core import iso,utc_now
from content_factory_store import SQLiteStore,StoreError
from kadrian_actions import command,enqueue_runway
from kadrian_editing import (local_command,remember_music_uploads,choose_music,assemble_music,
                            assemble_files,pause_production,edit_choices,default_sources)

class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.sqlite';self.store=SQLiteStore(self.path)
        self.project=self.store.save_project(77,{'name':'Studio','kadrian_notes':'Keep this rule'})
        self.pid=self.project['id']
        p=patch('agency_access_control.is_agency_access_revoked',return_value=False)
        p.start();self.addCleanup(p.stop)
        self.clip=self.video('Scene 1');self.clip2=self.video('Scene 2')
    def video(self,title):
        j=self.store.enqueue_job(77,self.pid,{'state':'ready','platform':'studio','format':'reel','dedupe_key':uuid4().hex,'studio_master':True,'video_style':'cartoon','content':{'title':title}})
        a=self.store.put_asset(77,self.pid,j['id'],'scene.mp4',title.encode(),'video/mp4')
        return self.store.update_job(77,j['id'],{'assets':[a]})
    def music(self,name='second.mp3'):
        from kadrian_music import save_uploaded_music
        with patch('kadrian_music.inspect_music',return_value=('audio/mpeg',20)):
            return save_uploaded_music(self.store,77,self.pid,name.encode(),name)
    def test_actual_music_requests_bypass_model(self):
        phrases=['Подключи музыку к этому моменту.','Достаточно десяти секунд, наложи, пожалуйста, теперь музыку, которую я тебе дала.',
                 'Я тебе отправила музыку, пожалуйста, используй её.','Слушай, а где музыка? Музыку, пожалуйста, подключи.',
                 'А где музыка?','Поставь вторую музыку, не первую.','Замени музыкальное сопровождение',
                 'Прекратил создавать сцены. Ты мне музыку подключи!','Не создавай новую сцену, добавь музыку']
        ask=Mock(return_value=json.dumps({'action':'create','scope':'scene','brief':'WRONG new scene'}))
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(command(ask,self.project,phrase,'old history','old script','cartoon')['action'],{'music_edit','stop'})
        ask.assert_not_called()
    def test_explicit_new_scene_is_not_confused_with_existing_music_edit(self):
        self.assertIsNone(local_command('Создай новую сцену, без музыки, кот идёт к столу'))
        self.assertIsNone(local_command('Создай мультфильм с этой музыкой'))
        self.assertIsNone(local_command('Не останавливай, продолжай создание'))
    def test_queue_guard_blocks_music_before_provider_balance_call(self):
        with patch('kadrian_actions.runway_balance') as balance:
            with self.assertRaises(ValueError):
                enqueue_runway(self.store,77,self.project,{'scope':'scene','duration':5},'cartoon','request',request_prompt='Подключи музыку')
        balance.assert_not_called();self.assertEqual(len(self.store.list_jobs(77,self.pid)),2)
    def test_empty_mp3_mime_is_normalized_and_saved(self):
        with patch('kadrian_music.inspect_music',return_value=('audio/mpeg',20)) as inspect:
            result=remember_music_uploads(self.store,77,self.pid,[('second.mp3','',b'mp3')])
        self.assertEqual(result[0][1],'audio/mpeg');inspect.assert_called_once_with(b'mp3','second.mp3')
        p=SQLiteStore(self.path).get_project(77,self.pid)
        self.assertTrue(p['kadrian_music_selected']);self.assertEqual(p['kadrian_notes'],'Keep this rule')
    def test_second_track_selection_survives_restart_without_duplicates(self):
        with patch('kadrian_music.inspect_music',return_value=('audio/mpeg',20)):
            for name,data in [('first.mp3',b'one'),('second.mp3',b'two'),('second.mp3',b'two')]:
                remember_music_uploads(self.store,77,self.pid,[(name,'',data)])
        fresh=SQLiteStore(self.path);p=fresh.get_project(77,self.pid);_,music=edit_choices(fresh,77,self.pid)
        second=choose_music(p,music,'не первую, а вторую музыку')
        self.assertIn('second.mp3',second['content']['title'])
        self.assertEqual(choose_music(p,music,'последнюю музыку')['id'],second['id'])
        self.assertEqual(len(p['kadrian_music_history']),2)
    def test_missing_second_track_never_substitutes_first(self):
        with self.assertRaises(ValueError):choose_music(self.project,[self.music('first.mp3')],'вторую музыку')
    def test_bad_upload_does_not_replace_current_music(self):
        first=self.music('first.mp3');self.store.save_project(77,{'id':self.pid,'kadrian_music_selected':first['id']})
        with patch('kadrian_music.inspect_music',side_effect=ValueError('bad')):
            with self.assertRaises(ValueError):remember_music_uploads(self.store,77,self.pid,[('bad.mp3','',b'not audio')])
        self.assertEqual(self.store.get_project(77,self.pid)['kadrian_music_selected'],first['id'])
    def test_edit_uses_two_selected_clips_and_exact_second_music_without_provider(self):
        self.music('first.mp3');second=self.music()
        with patch('kadrian_editing.assemble_files',return_value=(b'final',10)) as render,patch('content_factory_runtime.resolve_live_video_client') as runway:
            job=assemble_music(self.store,77,self.pid,[self.clip['id'],self.clip2['id']],second)
        self.assertEqual(render.call_args.args,([b'Scene 1',b'Scene 2'],b'second.mp3'))
        self.assertEqual(job['state'],'ready');self.assertEqual(job['production']['generation_calls'],0)
        runway.assert_not_called()
    def test_restart_reuses_finished_edit_instead_of_rendering_again(self):
        track=self.music()
        with patch('kadrian_editing.assemble_files',return_value=(b'final',10)):
            one=assemble_music(self.store,77,self.pid,[self.clip['id']],track)
        with patch('kadrian_editing.assemble_files') as render:
            two=assemble_music(SQLiteStore(self.path),77,self.pid,[self.clip['id']],track)
        render.assert_not_called();self.assertEqual(one['id'],two['id'])
    def test_render_error_keeps_sources_and_never_falls_back_to_generation(self):
        track=self.music()
        with patch('kadrian_editing.assemble_files',side_effect=ValueError('bad media')),patch('content_factory_runtime.resolve_live_video_client') as runway:
            with self.assertRaises(ValueError):assemble_music(self.store,77,self.pid,[self.clip['id']],track)
        runway.assert_not_called();edit=self.store.list_jobs(77,self.pid)[0]
        self.assertEqual(edit['state'],'paused');self.assertIsNone(edit['lease_owner'])
        self.assertEqual(self.store.get_job(77,self.clip['id'])['assets'],self.clip['assets'])
    def test_foreign_sources_cannot_be_mixed(self):
        music=self.music();other=self.store.save_project(77,{'name':'Other'})
        with self.assertRaises(ValueError):assemble_music(self.store,77,other['id'],[self.clip['id']],music)
        with self.assertRaises(ValueError):assemble_music(self.store,88,self.pid,[self.clip['id']],music)
    def test_stop_preserves_paid_task_and_ready_files_without_pausing_unrelated_jobs(self):
        paid=self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'paid','studio_master':True,'production':{'task_id':'already-paid'},'assets':self.clip['assets']})
        ordinary=self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'other-tool'})
        self.assertEqual(pause_production(self.store,77,self.pid),1)
        stopped=self.store.get_job(77,paid['id']);self.assertEqual(stopped['state'],'paused')
        self.assertEqual(stopped['production'],paid['production']);self.assertEqual(stopped['assets'],paid['assets'])
        self.assertEqual(self.store.get_job(77,ordinary['id'])['state'],'queued')
    def test_stop_invalidates_worker_lease_before_another_paid_boundary(self):
        self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'running','studio_master':True})
        claimed=self.store.claim_job('worker',iso(utc_now()),owner_id=77,project_id=self.pid)
        pause_production(self.store,77,self.pid)
        with self.assertRaises(StoreError):self.store.update_job(77,claimed['id'],{'production':{'starting':True}},lease_owner='worker')
        from content_factory_worker import Worker,PauseRequested
        with self.assertRaises(PauseRequested):Worker(self.store,Mock(),worker_id='worker')._update(claimed,production={'starting':True})
    def test_paused_project_blocks_new_scene_but_allows_edit(self):
        pause_production(self.store,77,self.pid)
        with patch('kadrian_actions.runway_balance') as balance:
            with self.assertRaises(ValueError):enqueue_runway(self.store,77,self.project,{'scope':'scene','duration':5},'cartoon','new')
        balance.assert_not_called()
        with patch('kadrian_editing.assemble_files',return_value=(b'final',10)):
            self.assertEqual(assemble_music(self.store,77,self.pid,[self.clip['id']],self.music())['state'],'ready')
    def test_latest_finished_edit_restores_its_two_sources(self):
        j={'id':'edit','production':{'source_job_ids':[self.clip['id'],self.clip2['id']]}}
        self.assertEqual(default_sources([j,self.clip,self.clip2]),[self.clip['id'],self.clip2['id']])
    def app(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_string('import streamlit as st\nfrom kadrian_studio_ui import render_kadrian\nrender_kadrian(77,st.session_state["ask"])')
        app.session_state['ask']=Mock(return_value=json.dumps({'action':'create','scope':'scene','brief':'WRONG'}))
        return app
    def test_chat_music_request_never_calls_model_or_video_provider(self):
        app=self.app();ask=app.session_state['ask']
        with patch('kadrian_studio_ui._runtime',return_value=(self.store,None,None)),patch('kadrian_studio_ui.connection_status',return_value={}),patch('content_factory_runtime.resolve_live_video_client') as runway:
            app.run();app.chat_input[0].set_value('Где музыка? Подключи музыку, а не новую сцену').run()
        self.assertFalse(app.exception);ask.assert_not_called();runway.assert_not_called()
        self.assertEqual(len(self.store.list_jobs(77,self.pid)),2)
        self.assertIn('Прикрепите',self.store.get_project(77,self.pid)['kadrian_chat']['turns'][-1]['answer'])
    def test_chat_stop_plus_music_pauses_scene_and_edits_without_model(self):
        self.music();self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'pending','studio_master':True})
        app=self.app();ask=app.session_state['ask']
        with patch('kadrian_studio_ui._runtime',return_value=(self.store,None,None)),patch('kadrian_studio_ui.connection_status',return_value={}),patch('kadrian_editing.assemble_files',return_value=(b'final',5)):
            app.run();app.chat_input[0].set_value('Прекратил создавать сцены. Ты мне музыку подключи!').run()
        self.assertFalse(app.exception);ask.assert_not_called()
        self.assertTrue(self.store.get_project(77,self.pid)['kadrian_production_paused'])
        self.assertIn('Монтаж готов',self.store.get_project(77,self.pid)['kadrian_chat']['turns'][-1]['answer'])


    def test_new_music_generation_is_not_mistaken_for_an_existing_track_edit(self):
        self.assertEqual(local_command('Сгенерируй новую музыку')['action'],'discuss')
    def test_do_not_choose_second_when_owner_says_not_second_but_first(self):
        first,second=self.music('first.mp3'),self.music('second.mp3')
        self.assertEqual(choose_music(self.project,[first,second],'не вторую, а первую')['id'],first['id'])
    def test_stop_cannot_cross_owner_or_project(self):
        j=self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'cross','studio_master':True})
        other=self.store.save_project(77,{'name':'Other'})
        for owner,pid in [(88,self.pid),(77,other['id'])]:
            with self.assertRaises(StoreError):self.store.pause_studio_job(owner,pid,j['id'],j['updated_at'],'stop')
        self.assertEqual(self.store.get_job(77,j['id'])['state'],'queued')

    def test_unsupported_new_track_cannot_silently_reuse_previous_music(self):
        with self.assertRaises(ValueError):
            remember_music_uploads(self.store,77,self.pid,[('new.ogg','audio/ogg',b'ogg')])

    def test_supabase_stop_is_owner_project_and_revision_scoped(self):
        from content_factory_store import SupabaseStore
        job=self.store.enqueue_job(77,self.pid,{'state':'queued','platform':'studio','format':'reel','dedupe_key':'supabase-stop','studio_master':True,'production':{'task_id':'paid'}})
        cloud=SupabaseStore('https://example.supabase.co','not-a-real-key')
        with patch.object(cloud,'get_job',return_value=job),patch.object(cloud,'_request',return_value=[{**job,'state':'paused'}]) as request:
            cloud.pause_studio_job(77,self.pid,job['id'],job['updated_at'],'stop')
        params=request.call_args.kwargs['params']
        self.assertEqual(params['owner_id'],'eq.77');self.assertEqual(params['project_id'],'eq.'+self.pid)
        self.assertEqual(params['state'],'in.(queued,generating)')
        self.assertEqual(params['data->>studio_master'],'eq.true')
        self.assertEqual(request.call_args.kwargs['json']['data']['production'],{'task_id':'paid'})

@unittest.skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'),'Real media tools required')
class RealEditingTests(unittest.TestCase):
    def test_real_edit_preserves_two_scene_order_frame_count_speed_and_has_music(self):
        ff=shutil.which('ffmpeg')
        with TemporaryDirectory() as d:
            root=Path(d);videos=[]
            for n,color in enumerate(('red','blue')):
                p=root/f'{n}.mp4';subprocess.run([ff,'-v','error','-f','lavfi','-i',f'color=c={color}:s=160x284:r=24:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(p)],check=True,capture_output=True);videos.append(p.read_bytes())
            music=root/'music.wav';subprocess.run([ff,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-ar','48000',str(music)],check=True,capture_output=True)
            data,duration=assemble_files(videos,music.read_bytes());self.assertAlmostEqual(duration,2,places=3)
            out=root/'out.mp4';out.write_bytes(data)
            from kadrian_editing import _probe
            streams=_probe(out)['streams'];self.assertTrue(any(s['codec_type']=='audio' for s in streams))
            self.assertEqual(int(next(s for s in streams if s['codec_type']=='video')['nb_frames']),48)
            for t,channel in ((.5,0),(1.5,2)):
                pixels=subprocess.run([ff,'-v','error','-ss',str(t),'-i',str(out),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'],check=True,capture_output=True).stdout
                self.assertGreater(pixels[(142*160+80)*3+channel],200)

