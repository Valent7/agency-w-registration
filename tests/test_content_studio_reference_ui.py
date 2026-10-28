"""A video reference cannot become paid scene generation without a new click."""
from __future__ import annotations

import unittest
from streamlit.testing.v1 import AppTest


APP = '''
import copy
from hashlib import sha256
from unittest.mock import patch
import streamlit as st
import content_studio_reference_ui as ui

project = {'id':'project-1','owner_id':77,'name':'Агентство W','product':'Помощь предпринимателю',
           'audience':'Предприниматели','formats':['reel'],'platforms':['vk'],'language':'Русский'}
project.update(st.session_state.get('_project_changes', {}))
if '_analyses' not in st.session_state:
    st.session_state['_analyses'] = []
    st.session_state['_saves'] = []
    st.session_state['_creates'] = []
    st.session_state['_save_failures'] = 0
    st.session_state['_create_failures'] = 0

VIDEO = b'owned-reference-video'
def report(style='live'):
    return {'version':1,'reference_style':style,'source_summary':'Контраст начала и результата, затем короткая развязка.',
            'source':{'sha256':sha256(VIDEO).hexdigest(),'duration':12.0,'filename':'reference.mp4'},
            'transcript':'Речь образца.',
            'reference_plan':{'title':'Новая собственная история','text':'Подпись нового ролика.',
             'narration':' '.join(['подготовка'] * 50),
             'visual_plan':{'art_direction':'Тёплая оригинальная история с повторяющимся персонажем.',
              'frames':[{'subject':'Самостоятельная сцена '+str(i),'composition':'Крупный план',
                         'lighting':'Тёплый свет','palette':'Золото и синий'} for i in range(4)]}}}

class Uploaded:
    name='reference.mp4'
    size=len(VIDEO)
    def getvalue(self): return VIDEO

class Store:
    def list_connections(self, owner_id, project_id):
        assert (owner_id,project_id) == (77,'project-1')
        return [] if st.session_state.get('_no_connections') else [{'platform':'vk'}]
    def get_job(self, owner_id, job_id):
        assert owner_id == 77
        return copy.deepcopy(st.session_state.get('_stored', {
            'id':job_id,'owner_id':77,'project_id':'project-1','state':'studio','platform':'studio',
            'studio_tool':'reference','studio_complete':True,'content':report('cartoon')}))

class Service:
    def create_from_video_reference(self, owner_id, project_id, reference_job_id, *, platforms, request_id):
        st.session_state['_creates'].append((owner_id,project_id,reference_job_id,platforms,request_id))
        if st.session_state['_create_failures']:
            st.session_state['_create_failures'] -= 1
            raise RuntimeError('private-create-url?token=secret')
        return [{'id':'new-film','review_required':True}]

def analyze(generator, current_project, video_bytes, **kwargs):
    st.session_state['_analyses'].append((current_project,video_bytes,kwargs))
    if st.session_state.get('_analysis_error'):
        raise RuntimeError('private-model-url?token=secret')
    result = report(kwargs['style'])
    if kwargs.get('target_seconds') == 120:
        result['reference_plan'].update(target_seconds=120, narration=' '.join(['подготовка'] * 200))
        result['reference_plan']['visual_plan']['frames'] = [
            {'subject':'Самостоятельная сцена '+str(i),'composition':'Крупный план',
             'lighting':'Тёплый свет','palette':'Золото и синий'} for i in range(16)]
    return result

def save(store, owner_id, project_id, data, *, request_id):
    st.session_state['_saves'].append((owner_id,project_id,copy.deepcopy(data),request_id))
    if st.session_state['_save_failures']:
        st.session_state['_save_failures'] -= 1
        raise RuntimeError('private-save-url?token=secret')
    return {'id':'reference-saved','studio_complete':True}

def upload(*a, **kw):
    return None if st.session_state.get('_missing_file') else Uploaded()

with patch('content_studio_reference_ui.st.file_uploader',side_effect=upload), \\
     patch('content_studio_reference.analyze_reference_video',side_effect=analyze), \\
     patch('content_studio_catalog.save_reference_result',side_effect=save), \\
     patch('content_factory_runtime.get_generator',return_value=object()), \\
     patch('content_factory_runtime.get_live_video_capabilities',side_effect=lambda *a:{'live_video_configured':not st.session_state.get('_video_missing')}), \\
     patch('agency_access_control.is_agency_access_revoked',return_value=False):
    ui.render_reference_tool(77,project,Store(),Service())
'''


class ReferenceUiTests(unittest.TestCase):
    key = 'studio_reference_77_project-1'

    def app(self, script=APP):
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def analyze(self, app, style='live', goal='Покажи новую историю про свободный вечер предпринимателя'):
        app.text_area(key=self.key + '_goal').input(goal)
        app.radio(key=self.key + '_style').set_value(style)
        next(item for item in app.button if item.label == 'Разобрать и подготовить свой сценарий').click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def create(self, app):
        next(item for item in app.button if item.label == 'Создать моё видео').click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def test_page_view_never_analyzes_saves_or_starts_video(self):
        app = self.app()
        self.assertEqual(app.session_state['_analyses'], [])
        self.assertEqual(app.session_state['_saves'], [])
        self.assertEqual(app.session_state['_creates'], [])
        self.assertFalse(any(item.label == 'Создать моё видео' for item in app.button))

    def test_new_duration_is_selected_independently_and_passed_to_analysis(self):
        app = self.app()
        app.radio(key=self.key + '_target_seconds').set_value(120)
        self.analyze(app, style='cartoon')
        self.assertEqual(app.session_state['_analyses'][0][2]['target_seconds'], 120)
        self.assertEqual(app.session_state['_saves'][0][2]['reference_plan']['target_seconds'], 120)
        self.assertEqual(len(app.session_state['_saves'][0][2]['reference_plan']['visual_plan']['frames']), 16)
        self.assertEqual(app.session_state['_creates'], [])
        self.assertTrue(any('около двух минут' in item.value for item in app.caption))

    def test_reopening_saved_script_keeps_the_same_render_request_identity(self):
        first = self.analyze(self.app(), style='cartoon')
        self.create(first)
        original_id = first.session_state['_creates'][0][-1]
        reopened = self.app()
        reopened.session_state['content_studio_77_reference_job_id'] = 'reference-saved'
        reopened.run()
        self.create(reopened)
        self.assertEqual(reopened.session_state['_creates'][0][-1], original_id)
        self.assertEqual(reopened.session_state['_analyses'], [])

    def test_analysis_uses_chosen_source_goal_and_cartoon_without_starting_scenes(self):
        app = self.analyze(self.app(), 'cartoon')
        analyses = app.session_state['_analyses']
        self.assertEqual(len(analyses), 1)
        self.assertEqual(analyses[0][1], b'owned-reference-video')
        self.assertEqual(analyses[0][2]['style'], 'cartoon')
        self.assertIn('свободный вечер', analyses[0][2]['goal'])
        self.assertEqual(app.session_state['_saves'][0][:2], (77,'project-1'))
        self.assertEqual(app.session_state['_saves'][0][2]['reference_style'], 'cartoon')
        self.assertEqual(app.session_state['_creates'], [])
        self.assertTrue(any('Мультфильм' in item.value for item in app.caption))
        self.assertEqual(len(app.get('download_button')), 1)

    def test_repeated_analysis_submit_and_rerun_reuse_accepted_report(self):
        app = self.analyze(self.app())
        app.run()
        self.analyze(app)
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertEqual(len(app.session_state['_saves']), 1)
        self.assertEqual(app.session_state['_creates'], [])

    def test_storage_retry_reuses_report_and_request_without_new_model_call(self):
        app = self.app()
        app.session_state['_save_failures'] = 1
        self.analyze(app)
        self.assertTrue(any('история пока не сохранилась' in item.value for item in app.warning))
        self.assertTrue(next(item for item in app.button if item.label == 'Создать моё видео').disabled)
        app.run()
        self.assertEqual(len(app.session_state['_saves']), 1)
        app.button(key=self.key + '_retry_save').click().run()
        self.assertFalse(app.exception)
        saves = app.session_state['_saves']
        self.assertEqual(len(saves), 2)
        self.assertEqual(saves[0][3], saves[1][3])
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertFalse(next(item for item in app.button if item.label == 'Создать моё видео').disabled)

    def test_owner_explicitly_creates_new_video_from_saved_scenario(self):
        app = self.create(self.analyze(self.app(), 'cartoon'))
        creates = app.session_state['_creates']
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][:4], (77,'project-1','reference-saved',['vk']))
        self.assertEqual(app.session_state['content_studio_77_next'], 'library')
        self.assertIn('проверки', app.session_state['cf_auto_77_notice'])
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertTrue(next(item for item in app.button if item.label == 'Создать моё видео').disabled)

    def test_unknown_create_response_can_be_retried_with_same_request_id(self):
        app = self.analyze(self.app())
        app.session_state['_create_failures'] = 1
        self.create(app)
        self.assertTrue(app.error)
        self.create(app)
        creates = app.session_state['_creates']
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0][4], creates[1][4])
        self.assertEqual(len(app.session_state['_analyses']), 1)

    def test_saved_report_reopens_without_file_upload_or_new_analysis(self):
        app = self.app()
        app.session_state['_missing_file'] = True
        app.session_state['content_studio_77_reference_job_id'] = 'from-history'
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Новая собственная история' in item.value for item in app.markdown))
        self.assertEqual(app.session_state[self.key + '_result']['saved_job_id'], 'from-history')
        self.assertEqual(app.session_state['_analyses'], [])
        self.assertEqual(app.session_state['_saves'], [])

    def test_another_owners_history_report_is_not_loaded(self):
        app = self.app()
        app.session_state['_stored'] = {
            'id':'foreign','owner_id':88,'project_id':'project-1','state':'studio','platform':'studio',
            'studio_tool':'reference','studio_complete':True,'content':{},
        }
        app.session_state['content_studio_77_reference_job_id'] = 'foreign'
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertFalse(any(item.label == 'Создать моё видео' for item in app.button))
        self.assertEqual(app.session_state['_analyses'], [])

    def test_missing_video_service_blocks_only_scene_creation(self):
        app = self.app()
        app.session_state['_video_missing'] = True
        self.analyze(app)
        self.assertEqual(len(app.session_state['_analyses']), 1)
        self.assertTrue(next(item for item in app.button if item.label == 'Создать моё видео').disabled)
        self.assertEqual(len(app.get('download_button')), 1)
        self.assertTrue(any('Сценарий уже можно' in item.value for item in app.info))

    def test_invalid_input_stops_before_model_and_model_errors_are_sanitized(self):
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
        self.assertEqual(app.session_state['_saves'], [])
        visible = ' '.join(str(item.value) for kind in (app.error,app.warning,app.info) for item in kind)
        self.assertNotIn('private-', visible)
        self.assertNotIn('token=secret', visible)


if __name__ == '__main__':
    unittest.main()
