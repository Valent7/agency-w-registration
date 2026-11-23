"""Exercise the partner's actual studio controls with synthetic media."""
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

from content_factory_autopilot_ui import _live_reels_enabled
from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore


class ReelsPilot(unittest.TestCase):
    def test_allowlist_is_project_specific_and_defaults_off(self):
        with patch.dict('os.environ', {'KADRIAN_LIVE_REELS_PILOT_PROJECTS': ''}):
            self.assertFalse(_live_reels_enabled('project-a'))
        with patch.dict('os.environ', {'KADRIAN_LIVE_REELS_PILOT_PROJECTS': ' project-a, project-c '}):
            self.assertTrue(_live_reels_enabled('project-a'))
            self.assertFalse(_live_reels_enabled('project-b'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and FFprobe required')
    def test_partner_can_edit_preview_and_reopen_without_network(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root/'test.sqlite3')
            first = store.save_project(77, {'name':'Partner A'})['id']
            second = store.save_project(78, {'name':'Partner B'})['id']
            Image.new('RGB',(64,96),'navy').save(root/'photo.png')
            for expression, name in [('color=navy:s=360x640:d=2','clip.mp4'),
                                     ('sine=frequency=440:duration=2','voice.wav')]:
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',expression,str(root/name)],check=True)

            def page(owner, project):
                return AppTest.from_string(f"""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import streamlit as st
from content_factory_store import SQLiteStore
from content_factory_autopilot_ui import render_content_autopilot
root = Path({str(root)!r})
store = SQLiteStore(root/'test.sqlite3')
files = {{'Фото для ролика':'photo.png','Готовый клип MP4':'clip.mp4','Загрузить голос':'voice.wav'}}
def upload(label, **kwargs):
    if label not in files:
        return None
    path = root / files[label]
    data = path.read_bytes()
    uploaded = BytesIO(data)
    uploaded.name, uploaded.size = path.name, len(data)
    return uploaded
with patch.dict('os.environ', {{'KADRIAN_LIVE_REELS_PILOT_PROJECTS':{first + ',' + second!r}}}), \\
     patch('content_factory_autopilot_ui._runtime',return_value=(store,None,{{}})), \\
     patch.object(st,'file_uploader',side_effect=upload), \\
     patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected network call')):
    render_content_autopilot({owner}, 'Partner', section='create', format_filter=['reel'], project_id={project!r})
""",default_timeout=60).run()

            app = page(77, first)
            def click(label):
                next(button for button in app.button if button.label==label).click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
            app.checkbox[0].check()
            click('Сохранить фото и начать')
            next(box for box in app.text_area if box.label.startswith('Какой ролик')).set_value('Хочу рассказать, как беречь время.')
            click('Подготовить сценарий')
            click('Утверждаю сценарий')
            click('Предложить образ')
            click('Утверждаю образ')
            click('Сохранить оригинал голоса')
            click('Сохранить исходники монтажа')
            next(box for box in app.text_input if box.label=='Заголовок').set_value('Сколько стоит час?')
            click('Собрать версию')
            self.assertEqual(len(app.get('video')),1)
            self.assertEqual(sum(x.label == 'Скачать MP4 9:16' for x in app.get('download_button')),1)
            reopened = page(77, first)
            self.assertEqual(len(reopened.get('video')),1)
            outsider = page(78, second)
            self.assertFalse(outsider.get('video'))
            jobs = store.list_jobs(77,first)
            draft = next(j for j in jobs if j.get('kadrian_live_draft'))
            final = next(j for j in jobs if j.get('kadrian_reels_revision'))
            self.assertEqual(store.read_asset(77,draft['live_voice']),(root/'voice.wav').read_bytes())
            self.assertIsNone(store.get_job(78,draft['id']))
            self.assertIsNone(store.claim_job('worker',iso(utc_now())))
            render = root/'render.mp4'
            render.write_bytes(store.read_asset(77,final['assets'][0]))
            subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(render),'-f','null','-'],check=True)
