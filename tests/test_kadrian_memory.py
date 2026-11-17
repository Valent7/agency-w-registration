import tempfile
import unittest
from pathlib import Path

from content_factory_store import SQLiteStore, StoreError
from kadrian_memory import project_context, restore_chat, save_turn


class KadrianMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStore(Path(self.temp.name) / 'factory.sqlite3')
        self.project = self.store.save_project(11, {'name': 'Агентство W', 'facts': 'Исходный факт'})

    def turn(self, turn_id='first', owner=11, **extra):
        return save_turn(self.store, owner, self.project['id'], turn_id,
                         'Финальную фразу сохранить', 'Сохраняю', 'Замысел', 'request', **extra)

    def test_restart_restores_text_and_duplicate_save_is_idempotent(self):
        self.turn()
        self.turn()
        saved = self.store.get_project(11, self.project['id'])
        self.assertEqual(len(saved['kadrian_chat']['turns']), 1)
        self.assertEqual(restore_chat(saved)['messages'][0][1], 'Финальную фразу сохранить')
        self.assertEqual(saved['facts'], 'Исходный факт')

    def test_other_owner_cannot_read_or_write_chat(self):
        self.turn()
        with self.assertRaises(StoreError):
            self.turn(owner=22)
        self.assertIsNone(self.store.get_project(22, self.project['id']))

    def test_two_tabs_append_without_erasing_other_turn(self):
        self.turn('first')
        self.turn('second')
        turns = self.store.get_project(11, self.project['id'])['kadrian_chat']['turns']
        self.assertEqual([t['id'] for t in turns], ['first', 'second'])

    def test_project_artwork_requires_project_ownership_and_survives_reload(self):
        asset = self.store.put_asset(11, self.project['id'], None, 'frame.png', b'png', 'image/png')
        self.assertEqual(self.store.read_asset(11, asset), b'png')
        with self.assertRaises(StoreError):
            self.store.put_asset(22, self.project['id'], None, 'frame.png', b'png', 'image/png')
        with self.assertRaises(StoreError):
            self.store.read_asset(22, asset)
        self.turn(image_asset=asset)
        saved = self.store.get_project(11, self.project['id'])
        self.assertEqual(restore_chat(saved)['messages'][1][1], asset)

    def test_client_context_does_not_inherit_agency_marketing(self):
        self.assertIn('база_агентства', project_context(self.project))
        self.assertNotIn('база_агентства', project_context({'name': 'Керамика'}))


class KadrianProductionTests(unittest.TestCase):
    def test_approved_speech_is_preserved_and_video_cap_stops_images(self):
        from unittest.mock import Mock, patch
        from uuid import uuid4
        from content_factory_editorial import build_reference_seed
        from content_factory_generation import Generator, GenerationError
        from tests.test_content_factory_generation import PROJECT, visual_plan
        narration = ' '.join(['Согласованная'] * 50)
        plan = {'format': 'reel', 'title': 'История', 'text': 'Подпись',
                'narration': narration, 'visual_plan': visual_plan(4)}
        project_id = str(uuid4())
        reference = build_reference_seed(11, project_id, str(uuid4()), 'live', plan, PROJECT)
        client = Mock(model='gen4.5')
        client.preflight.return_value = {'credit_balance': 10000}
        client.estimate_credits.return_value = 721
        generator = Generator('test', session=Mock(), live_video_client_resolver=lambda _: client)
        generator._proofread = Mock(return_value={**plan, 'narration': 'Изменённая речь'})
        generator._review_copy = Mock(return_value={'approved': True, 'issues': [], 'brief_feedback': ''})
        generator._illustration = Mock()
        generator._request = Mock(return_value=Mock(content=b'speech'))
        job = {'platform': 'instagram', 'format': 'reel', 'owner_id': 11, 'project_id': project_id,
               'video_mode': 'live', 'video_style': 'live', 'approved_reference': reference,
               'approved_video_credit_limit': 720}
        with patch('content_factory_generation._font'), patch('content_factory_generation.shutil.which', return_value='tool'), \
                patch('content_factory_live_media.speech_duration', return_value=37):
            with self.assertRaises(GenerationError) as error:
                generator.generate(PROJECT, job, save_production=Mock(), save_source=Mock(), load_source=Mock())
        self.assertIn('оценку видеокредитов', str(error.exception))
        self.assertEqual(error.exception.draft['narration'], narration)
        generator._illustration.assert_not_called()
        client.create_scene.assert_not_called()
