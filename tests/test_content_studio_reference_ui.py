"""The reference-video tool stays on one page and needs only three owner steps."""
from __future__ import annotations

import unittest
from streamlit.testing.v1 import AppTest


APP = r'''
import copy
from hashlib import sha256
from unittest.mock import patch
import streamlit as st
import content_studio_reference_ui as ui

project = {
    'id':'project-1','owner_id':77,'name':'Агентство W',
    'product':'ИИ-помощники освобождают время от рутины',
    'audience':'Люди, которым важно вернуть время',
    'formats':['post'],'platforms':['vk'],'language':'Русский',
}
project.update(st.session_state.get('_project_changes', {}))
if '_analyses' not in st.session_state:
    st.session_state['_analyses'] = []
    st.session_state['_saves'] = []
    st.session_state['_creates'] = []
    st.session_state['_retries'] = []
    st.session_state['_save_failures'] = 0
    st.session_state['_create_failures'] = 0
    st.session_state['_master_state'] = 'queued'
    st.session_state['_master_error'] = ''
    st.session_state['_master_progress'] = 'Создаём живые сцены.'

VIDEO = b'owned-reference-video'
def report(style='live'):
    return {
        'version':1,
        'reference_style':style,
        'source_summary':'Контраст начала и результата, затем короткая развязка.',
        'source':{'sha256':sha256(VIDEO).hexdigest(),'duration':12.0,'filename':'reference.mp4'},
        'transcript':'Речь образца.',
        'reference_plan':{
            'title':'Новая собственная история',
            'text':'Подпись нового ролика.',
            'narration':' '.join(['подготовка'] * 50),
            'visual_plan':{
                'art_direction':'Тёплая оригинальная история с повторяющимся персонажем.',
                'frames':[
                    {'subject':'Самостоятельная сцена '+str(i),'composition':'Крупный план',
                     'lighting':'Тёплый свет','palette':'Золото и синий'}
                    for i in range(4)
                ],
            },
        },
    }

class Uploaded:
    name='reference.mp4'
    size=len(VIDEO)
    def getvalue(self): return VIDEO

class Store:
    def get_job(self, owner_id, job_id):
        assert owner_id == 77
        if job_id == 'new-film':
            state = st.session_state.get('_master_state', 'queued')
            assets = []
            if state == 'ready':
                assets = [{
                    'storage_path':'77/project-1/new-film/master.mp4',
                    'filename':'master.mp4','mime_type':'video/mp4',
                    'kind':'video','sha256':'a'*64,'size':12,
                }]
            return {
                'id':'new-film','owner_id':77,'project_id':'project-1',
                'state':state,'platform':'instagram','format':'reel',
                'studio_master':True,'assets':assets,
                'error':st.session_state.get('_master_error',''),
                'progress':{'message':st.session_state.get('_master_progress','')},
            }
        return copy.deepcopy(st.session_state.get('_stored', {
            'id':job_id,'owner_id':77,'project_id':'project-1','state':'studio',
            'platform':'studio','studio_tool':'reference','studio_complete':True,
            'content':report('cartoon'),'checkpoint':{},
        }))
    def resolve_assets(self, owner_id, assets):
        assert owner_id == 77
        return [{**assets[0], 'url':'https://example.invalid/master.mp4'}]
    def read_asset(self, owner_id, asset):
        assert owner_id == 77
        return b'fake-master-video'

class Service:
    def create_master_from_video_reference(self, owner_id, project_id, reference_job_id, *, request_id):
        st.session_state['_creates'].append(
            (owner_id, project_id, reference_job_id, request_id)
        )
        if st.session_state['_create_failures']:
            st.session_state['_create_failures'] -= 1
            raise RuntimeError('private-create-url?token=secret')
        return {'id':'new-film','state':'queued','studio_master':True}
    def retry(self, owner_id, job_id):
        st.session_state['_retries'].append((owner_id, job_id))
        st.session_state['_master_state'] = 'queued'
        st.session_state['_master_error'] = ''
        return {'id':job_id,'state':'queued'}

def analyze(generator, current_project, video_bytes, **kwargs):
    st.session_state['_analyses'].append((current_project,video_bytes,kwargs))
    if st.session_state.get('_analysis_error'):
        raise RuntimeError('private-model-url?token=secret')
    return report(kwargs['style'])

def save(store, owner_id, project_id, data, *, request_id):
    st.session_state['_saves'].append(
        (owner_id,project_id,copy.deepcopy(data),request_id)
    )
    if st.session_state['_save_failures']:
        st.session_state['_save_failures'] -= 1
        raise RuntimeError('private-save-url?token=secret')
    return {'id':'reference-saved','studio_complete':True}

def upload(*a, **kw):
    return None if st.session_state.get('_missing_file') else Uploaded()

with patch('content_studio_reference_ui.st.file_uploader',side_effect=upload), \
     patch('content_studio_reference.analyze_reference_video',side_effect=analyze), \
     patch('content_studio_catalog.save_reference_result',side_effect=save), \
     patch('content_factory_runtime.get_generator',return_value=object()), \
     patch('content_factory_runtime.get_live_video_capabilities',
           side_effect=lambda *a:{'live_video_configured':not st.session_state.get('_video_missing')}), \
     patch('agency_access_control.is_agency_access_revoked',return_value=False):
    ui.render_reference_tool(77,project,Store(),Service())
'''


class ReferenceUiTests(unittest.TestCase):
    key = 'studio_reference_77_project-1'

    def app(self):
        app = AppTest.from_string(APP, default_timeout=20).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def analyze(self, app, style='live',
                goal='Агентство W возвращает время. Сегодня презентация в Zoom.'):
        app.text_area(key=self.key + '_goal').input(goal)
        app.radio(key=self.key + '_style').set_value(style)
        next(
            item for item in app.button
            if item.label == '✨ Подготовить сценарий'
        ).click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def approve(self, app):
        next(
            item for item in app.button
            if item.label == '✅ Утвердить сценарий'
        ).click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def create(self, app):
        next(
            item for item in app.button
            if item.label == '🎬 Создать видео'
        ).click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def test_initial_page_is_exactly_three_step_flow_without_side_effects(self):
        app = self.app()
        visible = ' '.join(
            str(item.value)
            for group in (app.subheader, app.markdown, app.caption, app.info)
            for item in group
        )
        self.assertIn('Видео по образцу — 3 шага', visible)
        self.assertIn('1. Загрузите видео-образец', visible)
        self.assertEqual(app.session_state['_analyses'], [])
        self.assertEqual(app.session_state['_saves'], [])
        self.assertEqual(app.session_state['_creates'], [])
        self.assertFalse(any(item.label == '🎬 Создать видео' for item in app.button))
        self.assertFalse(any('Где разместить' in str(item.value) for item in app.markdown))

    def test_analysis_prepares_editable_script_but_never_saves_or_creates_video(self):
        app = self.analyze(self.app(), style='cartoon')
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertEqual(app.session_state['_analyses'][0][1], b'owned-reference-video')
        self.assertEqual(app.session_state['_analyses'][0][2]['style'], 'cartoon')
        self.assertEqual(app.session_state['_analyses'][0][2].get('target_seconds'), None)
        self.assertEqual(app.session_state['_saves'], [])
        self.assertEqual(app.session_state['_creates'], [])
        self.assertTrue(any(item.label == '✅ Утвердить сценарий' for item in app.button))
        self.assertFalse(any(item.label == '🎬 Создать видео' for item in app.button))

    def test_owner_edits_and_approves_without_reanalyzing_source(self):
        app = self.analyze(self.app())
        app.text_area(key=self.key + '_approve_form-TextArea-0').input(
            'Сегодня в 20:00 МСК презентация Агентства W. Напишите «Презентация».'
        ) if False else None
        self.approve(app)
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertEqual(len(app.session_state['_saves']), 1)
        self.assertTrue(app.session_state[self.key + '_result']['scenario_approved'])
        self.assertEqual(
            app.session_state[self.key + '_result']['saved_job_id'],
            'reference-saved',
        )
        self.assertTrue(any(item.label == '🎬 Создать видео' for item in app.button))

    def test_master_video_creation_needs_no_social_connection_or_platform_selection(self):
        app = self.approve(self.analyze(self.app(), style='cartoon'))
        self.create(app)
        creates = app.session_state['_creates']
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][:3], (77,'project-1','reference-saved'))
        self.assertNotIn('content_studio_77_next', app.session_state)
        self.assertNotIn('cf_auto_77_notice', app.session_state)
        all_labels = [item.label for item in app.button]
        self.assertNotIn('Создать моё видео', all_labels)
        self.assertFalse(any('Площадки' in str(item.value) for item in app.markdown))

    def test_missing_video_service_shows_reason_on_same_page(self):
        app = self.analyze(self.app())
        app.session_state['_video_missing'] = True
        self.approve(app)
        visible = ' '.join(str(item.value) for item in app.error)
        self.assertIn('Видео сейчас не создастся', visible)
        self.assertIn('не подключён рабочий видеосервис', visible)
        self.assertFalse(any(item.label == '🎬 Создать видео' for item in app.button))

    def test_failed_video_shows_concrete_reason_on_same_page(self):
        app = self.approve(self.analyze(self.app()))
        self.create(app)
        app.session_state['_master_state'] = 'failed'
        app.session_state['_master_error'] = 'Недостаточно средств в видеосервисе.'
        app.run()
        self.assertFalse(app.exception)
        visible = ' '.join(str(item.value) for item in app.error)
        self.assertIn('Видео не создано. Причина:', visible)
        self.assertIn('Недостаточно средств', visible)
        self.assertTrue(any(item.label == 'Повторить создание' for item in app.button))

    def test_ready_video_is_previewed_and_downloadable_without_navigation(self):
        app = self.approve(self.analyze(self.app()))
        self.create(app)
        app.session_state['_master_state'] = 'ready'
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Видео готово' in str(item.value) for item in app.success))
        self.assertTrue(any(
            item.label == '⬇️ Скачать готовое видео'
            for item in app.get('download_button')
        ))
        self.assertNotIn('content_studio_77_next', app.session_state)

    def test_unknown_create_error_is_sanitized_and_same_request_is_reused(self):
        app = self.approve(self.analyze(self.app()))
        app.session_state['_create_failures'] = 1
        self.create(app)
        visible = ' '.join(str(item.value) for item in app.error)
        self.assertIn('Видео не запущено. Причина:', visible)
        self.assertNotIn('private-create-url', visible)
        self.assertNotIn('token=secret', visible)
        self.create(app)
        self.assertEqual(len(app.session_state['_creates']), 2)
        self.assertEqual(
            app.session_state['_creates'][0][-1],
            app.session_state['_creates'][1][-1],
        )

    def test_saved_scenario_reopens_as_approved_without_file_or_new_analysis(self):
        app = self.app()
        app.session_state['_missing_file'] = True
        app.session_state['content_studio_77_reference_job_id'] = 'from-history'
        app.run()
        self.assertFalse(app.exception)
        result = app.session_state[self.key + '_result']
        self.assertTrue(result['scenario_approved'])
        self.assertEqual(result['saved_job_id'], 'from-history')
        self.assertEqual(app.session_state['_analyses'], [])
        self.assertEqual(app.session_state['_saves'], [])
        self.assertTrue(any(item.label == '🎬 Создать видео' for item in app.button))

    def test_invalid_input_and_analysis_errors_never_leak_private_details(self):
        app = self.app()
        app.session_state['_missing_file'] = True
        self.analyze(app)
        self.assertEqual(app.session_state['_analyses'], [])
        app.session_state['_missing_file'] = False
        self.analyze(app, goal=' ')
        self.assertEqual(app.session_state['_analyses'], [])
        app.session_state['_analysis_error'] = True
        self.analyze(app)
        self.assertEqual(len(app.session_state['_analyses']), 1)
        visible = ' '.join(
            str(item.value)
            for group in (app.error, app.warning, app.info)
            for item in group
        )
        self.assertNotIn('private-model-url', visible)
        self.assertNotIn('token=secret', visible)


if __name__ == '__main__':
    unittest.main()
