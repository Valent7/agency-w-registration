"""Creation happens only on explicit submit; persistence retries never rebill."""
import copy
import io
import unittest
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

from content_studio_tools import StudioToolError, _accepted_text, _checked_image


APP = '''
import io
import copy
from unittest.mock import patch
import streamlit as st
from PIL import Image
import content_studio_tools as ui

kind = KIND
project = {'id':'project-1','owner_id':77,'name':'Агентство W',
           'product':'Помощь предпринимателю','audience':'Эксперты',
           'facts':'Только проверенные факты','tone':'Ясно','visual_style':'Тёплый свет'}
project.update(st.session_state.get('_project_changes', {}))
if '_calls' not in st.session_state:
    st.session_state['_calls'] = []
    st.session_state['_saves'] = []
    st.session_state['_save_failures'] = 0

def save(store, owner_id, project_id, result_kind, prompt, **kwargs):
    st.session_state['_saves'].append((owner_id, project_id, result_kind, prompt, copy.deepcopy(kwargs)))
    if st.session_state['_save_failures']:
        st.session_state['_save_failures'] -= 1
        raise RuntimeError('private-storage-url?token=do-not-render')
    return {'id':'saved-1','studio_complete':True}

def ask(system, prompt, **kwargs):
    st.session_state['_calls'].append(('text', system, prompt, kwargs))
    return st.session_state.get('_text_response', 'Готовый живой текст для владельца.')

def image(prompt, **kwargs):
    st.session_state['_calls'].append(('image', prompt, kwargs))
    output = io.BytesIO()
    Image.new('RGB',(16,16),'gold').save(output, 'PNG')
    return {'ok':True,'image_bytes':output.getvalue(),'mime_type':'image/png'}

class SpeechResponse:
    content = b'ID3-test-owned-audio'
    def close(self):
        st.session_state['_response_closed'] = True

class Speech:
    voice = 'alloy'
    def _speech_payload(self, project, narration):
        return {'model':'configured-tts','voice':self.voice,'input':narration,'instructions':project['tone']}
    def _request(self, endpoint, payload):
        st.session_state['_calls'].append(('audio',endpoint,payload))
        return SpeechResponse()

with patch('content_studio_catalog.save_studio_result',side_effect=save), patch('content_factory_runtime.get_generator',return_value=Speech()):
    if kind == 'text':
        ui.render_text_tool(77,project,object(),ask)
    elif kind == 'image':
        ui.render_image_tool(77,project,object(),image)
    else:
        ui.render_speech_tool(77,project,object())
'''


class StudioToolsTests(unittest.TestCase):
    def app(self, kind='text'):
        app = AppTest.from_string(APP.replace('KIND', repr(kind)), default_timeout=15).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def submit(self, app, kind='text', prompt='Напиши ясный текст с конкретным примером'):
        app.text_area(key=f'studio_tool_77_project-1_{kind}_prompt').input(prompt)
        label = {'text':'Подготовить текст','image':'Создать изображение','audio':'Озвучить'}[kind]
        next(button for button in app.button if button.label == label).click().run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def test_rendering_each_form_never_purchases_or_saves_anything(self):
        for kind in ('text', 'image', 'audio'):
            with self.subTest(kind=kind):
                app = self.app(kind)
                self.assertEqual(app.session_state['_calls'], [])
                self.assertEqual(app.session_state['_saves'], [])
                self.assertEqual(len(app.text_area), 1)

    def test_text_is_created_from_project_context_and_saved_for_exact_owner(self):
        app = self.submit(self.app())
        calls = app.session_state['_calls']
        self.assertEqual(len(calls), 1)
        self.assertIn('Только проверенные факты', calls[0][1])
        self.assertEqual(calls[0][3], {'use_web_search':False})
        saves = app.session_state['_saves']
        self.assertEqual(saves[0][:3], (77,'project-1','text'))
        self.assertEqual(saves[0][4]['text'], 'Готовый живой текст для владельца.')
        self.assertTrue(any('Мои генерации' in item.value for item in app.success))
        self.assertEqual(len(app.get('download_button')), 1)
        app.run()
        self.assertEqual(len(app.session_state['_calls']), 1)
        self.submit(app)
        self.assertEqual(len(app.session_state['_calls']), 1, 'double submit must reuse accepted output')

    def test_storage_retry_keeps_response_and_request_id_without_new_paid_call(self):
        app = self.app()
        app.session_state['_save_failures'] = 1
        self.submit(app)
        self.assertEqual(len(app.session_state['_calls']), 1)
        self.assertEqual(len(app.session_state['_saves']), 1)
        self.assertTrue(any('не сохранилась' in item.value for item in app.warning))
        app.run()
        self.assertEqual(len(app.session_state['_saves']), 1, 'render must not reattempt a failed write')
        app.button(key='studio_tool_77_project-1_text_retry_save').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.session_state['_calls']), 1)
        saves = app.session_state['_saves']
        self.assertEqual(len(saves), 2)
        self.assertEqual(saves[0][4]['request_id'], saves[1][4]['request_id'])
        self.assertTrue(any('Сохранено' in item.value for item in app.success))
        visible = ' '.join(str(item.value) for kind in (app.warning,app.error,app.markdown,app.caption) for item in kind)
        self.assertNotIn('private-storage-url', visible)

    def test_incomplete_storage_result_is_not_labelled_saved(self):
        script = APP.replace("return {'id':'saved-1','studio_complete':True}", "return {'id':'saved-1','studio_complete':False}")
        app = AppTest.from_string(script.replace('KIND', repr('text')), default_timeout=15).run()
        self.submit(app)
        self.assertFalse(app.success)
        self.assertTrue(app.warning)
        self.assertEqual(len(app.session_state['_calls']), 1)

    def test_image_uses_real_size_and_disables_automatic_redraw(self):
        app = self.app('image')
        app.selectbox(key='studio_tool_77_project-1_image_size').select('1536x1024')
        self.submit(app, 'image', 'Тёплый вечерний свет в уютной комнате')
        call = app.session_state['_calls'][0]
        self.assertEqual(call[0], 'image')
        self.assertEqual(call[2]['size'], '1536x1024')
        self.assertTrue(call[2]['fast_mode'])
        self.assertEqual(call[2]['change_request'], 'Тёплый свет')
        self.assertEqual(call[2]['reference_images'], [])
        saved = app.session_state['_saves'][0]
        self.assertEqual(saved[2], 'image')
        self.assertEqual(saved[4]['mime_type'], 'image/png')
        self.assertTrue(saved[4]['data'].startswith(b'\x89PNG'))
        self.assertEqual(len(app.get('image')), 1)
        self.assertEqual(len(app.get('download_button')), 1)

    def test_speech_reuses_existing_provider_and_never_reads_directions_aloud(self):
        app = self.app('audio')
        app.selectbox(key='studio_tool_77_project-1_audio_voice').select('nova')
        app.text_input(key='studio_tool_77_project-1_audio_direction').input('Энергично и с короткими паузами')
        self.submit(app, 'audio', 'Это точные слова владельца.')
        call = app.session_state['_calls'][0]
        self.assertEqual(call[:2], ('audio','audio/speech'))
        self.assertEqual(call[2]['voice'], 'nova')
        self.assertEqual(call[2]['input'], 'Это точные слова владельца.')
        self.assertEqual(call[2]['instructions'], 'Энергично и с короткими паузами')
        self.assertTrue(app.session_state['_response_closed'])
        self.assertEqual(app.session_state['_saves'][0][2], 'audio')
        self.assertEqual(len(app.get('audio')), 1)
        self.assertEqual(len(app.get('download_button')), 1)

    def test_wrong_owner_and_revoked_owner_cannot_submit(self):
        app = self.app()
        app.session_state['_project_changes'] = {'owner_id':88}
        app.run()
        self.assertTrue(next(button for button in app.button if button.label == 'Подготовить текст').disabled)
        self.assertEqual(app.session_state['_calls'], [])
        script = APP.replace('77', '291173537')
        app = AppTest.from_string(script.replace('KIND', repr('text')), default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertTrue(next(button for button in app.button if button.label == 'Подготовить текст').disabled)
        self.assertEqual(app.session_state['_calls'], [])

    def test_blank_prompt_and_provider_error_do_not_save_fake_text(self):
        app = self.app()
        self.submit(app, prompt='   ')
        self.assertEqual(app.session_state['_calls'], [])
        app.session_state['_text_response'] = 'Ошибка OpenAI: private-key-must-not-appear'
        self.submit(app)
        self.assertEqual(len(app.session_state['_calls']), 1)
        self.assertEqual(app.session_state['_saves'], [])
        self.assertTrue(app.error)
        self.assertFalse(any('private-key' in item.value for item in app.error))

    def test_image_validation_rejects_non_image_without_editing_the_file(self):
        with self.assertRaises(StudioToolError):
            _checked_image(b'not-an-image')
        output = io.BytesIO()
        Image.new('RGB',(2,2),'gold').save(output, 'JPEG')
        original = output.getvalue()
        self.assertEqual(_checked_image(original), 'image/jpeg')
        self.assertEqual(output.getvalue(), original)


if __name__ == '__main__':
    unittest.main()
