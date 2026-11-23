from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore
from kadrian_live_draft import create, editorial, update


class LiveDraftTest(unittest.TestCase):
    def test_approval_order_and_revision_history(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'test.sqlite'
            store = SQLiteStore(db)
            owner = 77
            project = store.save_project(owner, {'name': 'Studio'})['id']
            photo = BytesIO()
            Image.new('RGB', (32, 32), 'green').save(photo, format='PNG')
            job = create(store, owner, project, photo.getvalue())
            with self.assertRaises(ValueError):
                update(store, owner, project, job, 'look', 'Образ')
            with self.assertRaises(ValueError):
                update(store, 78, project, job, 'brief', 'Другая запись')
            brief = 'Ну, я хочу сказать, что время время важно.'
            job = update(store, owner, project, job, 'brief', brief)
            with patch.dict('os.environ', {}, clear=True):
                cleaned = editorial(brief)
            self.assertEqual(cleaned, 'Время важно.')
            job = update(store, owner, project, job, 'script', cleaned)
            job = update(store, owner, project, job, 'approve_script')
            job = update(store, owner, project, job, 'look', 'Мягкий свет')
            with self.assertRaises(ValueError):
                update(store, owner, project, job, 'voice', ('wav', b'voice'))
            job = update(store, owner, project, job, 'approve_look')
            reopened = SQLiteStore(db)
            saved = reopened.get_job(owner, job['id'])
            self.assertEqual(reopened.read_asset(owner, saved['live_photo']), photo.getvalue())
            self.assertTrue(saved['live_script_ok'] and saved['live_look_ok'])
            changed = update(reopened, owner, project, saved, 'script', 'Берегите своё время.')
            self.assertFalse(changed['live_script_ok'] or changed['live_look_ok'])
            self.assertEqual([event['value'] for event in changed['live_history']
                              if event['action'] == 'script'], ['Время важно.', 'Берегите своё время.'])
            self.assertIsNone(reopened.claim_job('worker', iso(utc_now())))

    def test_rewrite_rejects_joined_script_and_spoken_instructions(self):
        old = 'Сколько времени уходит на рутину?'
        direction = 'Сделай вступление ярче и предложи открыть калькулятор.'
        new = 'Куда исчезают часы вашей недели? Откройте калькулятор.'
        with patch('kadrian_live_draft._local_program', return_value=('editor', 'model')), \
             patch('kadrian_live_draft.subprocess.run') as run:
            for output in (old + '\n\n' + new, direction + '\n\n' + new):
                run.return_value.stdout = output
                with self.assertRaises(ValueError):
                    editorial('Время и рутина.', current=old, direction=direction)
            run.return_value.stdout = new
            self.assertEqual(editorial('Время и рутина.', current=old, direction=direction), new)

    def test_photo_hair_and_wardrobe_take_precedence_over_old_look(self):
        direction = ('Причёска как на фотографии, рубашка с длинными рукавами и белыми обшлагами '
                     'и белым стоячим воротником, брюки белые. Всё остальное под твою ответственность. '
                     'Оформление Reels должно быть на высоте.')
        with patch('kadrian_live_draft._local_program') as engine:
            result = editorial('Ролик о времени.', look=True, previous_look='Причёска: коса.',
                               direction=direction)
        engine.assert_not_called()
        for detail in ('исходной фотографии', 'длинными рукавами', 'белыми обшлагами',
                       'белым стоячим воротником', 'брюки белые', '9:16', 'стрелками'):
            self.assertIn(detail, result)
        self.assertNotIn('коса', result)
