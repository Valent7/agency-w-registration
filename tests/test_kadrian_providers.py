import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from content_factory_live_video import LiveVideoPending, LiveVideoUncertain
from content_factory_store import SQLiteStore
from kadrian_providers import HeyGenClient, ShotstackClient, heygen_payload, shotstack_payload
from kadrian_production import process_provider_job, resolve_spec
from kadrian_production_ui import enqueue_provider, _quote


def interview():
    return {'kind': 'interview', 'title': 'Разговор', 'aspect_ratio': '16:9',
            'speakers': [{'avatar_id': 'person-a', 'voice_id': 'voice-a'},
                         {'avatar_id': 'person-b', 'voice_id': 'voice-b'}],
            'dialogue': [{'speaker': 0, 'text': 'Ваш вопрос?'}, {'speaker': 1, 'text': 'Мой точный ответ.'}]}


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class ProviderContractTests(unittest.TestCase):
    def test_interview_preserves_speaker_order_and_exact_words(self):
        payload = heygen_payload(interview())
        self.assertEqual(payload['type'], 'studio')
        self.assertEqual([s['input']['avatar_id'] for s in payload['scenes']], ['person-a', 'person-b'])
        self.assertEqual(payload['scenes'][1]['input']['script'], 'Мой точный ответ.')
        self.assertEqual(payload['resolution'], '1080p')

    def test_interview_requires_both_speakers(self):
        spec = interview()
        spec['dialogue'].pop()
        with self.assertRaises(ValueError):
            heygen_payload(spec)

    def test_scene_uses_current_model_endpoint_and_idempotency_key(self):
        session = Mock()
        session.request.return_value = response({'data': {'video_id': 'video-123'}}, 202)
        client = HeyGenClient('private-token', session)
        self.assertEqual(client.create({'kind': 'scene', 'duration': 30, 'prompt': 'Анимация'}, 'job-123'), 'video-123')
        args, kwargs = session.request.call_args
        self.assertEqual(args, ('POST', 'https://api.heygen.com/v3/models/videos'))
        self.assertEqual(kwargs['headers']['Idempotency-Key'], 'job-123')
        self.assertEqual(kwargs['json']['resolution'], '1080p')
        self.assertFalse(kwargs['allow_redirects'])

    def test_avatar_catalog_uses_public_pagination_and_voice_catalog_is_public(self):
        session = Mock()
        session.request.return_value = response({'data': [{'id': 'public-avatar'}], 'next_token': 'next-page'})
        client = HeyGenClient('private-token', session)
        page = client.avatars()
        self.assertEqual(page['items'][0]['id'], 'public-avatar')
        self.assertEqual(page['next_token'], 'next-page')
        self.assertIn('ownership=public&limit=50', session.request.call_args.args[1])
        client.voices()
        self.assertIn('type=public', session.request.call_args.args[1])
        self.assertIn('language=ru', session.request.call_args.args[1])

    def test_unknown_create_result_and_timeout_are_uncertain(self):
        session = Mock()
        client = HeyGenClient('private-token', session)
        session.request.return_value = response({'data': {}}, 202)
        with self.assertRaises(LiveVideoUncertain):
            client.create(interview(), 'job-123')
        session.request.side_effect = requests.Timeout('private-token https://private-url')
        with self.assertRaises(LiveVideoUncertain) as error:
            client.create(interview(), 'job-123')
        self.assertNotIn('private-token', str(error.exception))
        self.assertNotIn('private-url', str(error.exception))

    def test_shotstack_timeline_keeps_order_and_mix_volume(self):
        payload = shotstack_payload({'clips': [{'url': 'https://media.example/1', 'duration': 4},
                                               {'url': 'https://media.example/2', 'duration': 7}],
                                     'music_url': 'https://media.example/music'})
        self.assertEqual([c['start'] for c in payload['timeline']['tracks'][0]['clips']], [0, 4])
        self.assertEqual(payload['timeline']['tracks'][1]['clips'][0]['length'], 11)
        self.assertEqual(payload['timeline']['tracks'][1]['clips'][0]['asset']['volume'], 0.12)

    def test_shotstack_rejects_invalid_duration_before_network(self):
        session = Mock()
        with self.assertRaises(ValueError):
            ShotstackClient('key', session).create({'clips': [{'url': 'https://media.example/1', 'duration': float('nan')}]}, 'job')
        session.request.assert_not_called()

    def test_unconfigured_rates_never_make_a_zero_dollar_quote(self):
        self.assertIsNone(_quote({}, 'heygen', 'interview', 30, interview()['speakers']))
        self.assertEqual(_quote({'KADRIAN_HEYGEN_VIDEO_USD_PER_MINUTE': '4'}, 'heygen', 'scene', 30), 4)


class DurableProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStore(Path(self.temp.name)/'factory.sqlite3')
        self.project = self.store.save_project(11, {'name': 'Проект'})
        self.job = enqueue_provider(self.store, 11, self.project['id'], 'heygen', interview(), 'live', 'request-123', 4)
        self.worker = Mock(store=self.store)
        self.worker._update.side_effect = lambda job, **changes: self.store.update_job(11, job['id'], changes)
        self.worker._finish.side_effect = self.worker._update.side_effect
        self.client = Mock()
        self.client.create.return_value = 'provider-task'
        self.runtime = types.SimpleNamespace(_configuration=lambda: {})

    def test_enqueue_retry_keeps_one_job_and_owner_boundaries(self):
        duplicate = enqueue_provider(self.store, 11, self.project['id'], 'heygen', interview(), 'live', 'request-123', 4)
        self.assertEqual(duplicate['id'], self.job['id'])
        with self.assertRaises(ValueError):
            enqueue_provider(self.store, 22, self.project['id'], 'heygen', interview(), 'live', 'request-123', 4)

    def test_worker_resumes_one_paid_order_and_saves_mp4(self):
        self.client.status.side_effect = [{'status': 'processing'}, {'status': 'completed', 'video_url': 'https://media.example/result.mp4'}]
        with patch.dict(sys.modules, {'content_factory_runtime': self.runtime}), \
                patch('kadrian_production.provider_client', return_value=self.client), \
                patch('kadrian_production.download_result', return_value=b'mp4'):
            with self.assertRaises(LiveVideoPending):
                process_provider_job(self.worker, self.job, self.project)
            fresh = self.store.get_job(11, self.job['id'])
            self.assertEqual(fresh['production']['task_id'], 'provider-task')
            self.assertEqual(process_provider_job(self.worker, fresh, self.project), 'generated')
        self.client.create.assert_called_once()
        saved = self.store.get_job(11, self.job['id'])
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(self.store.read_asset(11, saved['assets'][0]), b'mp4')

    def test_worker_does_not_repeat_create_after_interrupted_confirmation(self):
        self.store.update_job(11, self.job['id'], {'production': {'starting': True}})
        fresh = self.store.get_job(11, self.job['id'])
        with patch.dict(sys.modules, {'content_factory_runtime': self.runtime}), \
                patch('kadrian_production.provider_client', return_value=self.client):
            with self.assertRaises(LiveVideoUncertain):
                process_provider_job(self.worker, fresh, self.project)
        self.client.create.assert_not_called()

    def test_assembly_cannot_read_another_project_asset(self):
        job = {**self.job, 'kadrian_provider': 'shotstack',
               'provider_spec': {'clips': [{'asset': {'storage_path': '11/another-project/job/file.mp4'}, 'duration': 5}]}}
        with self.assertRaises(ValueError):
            resolve_spec(self.store, job)

    def test_normal_worker_entry_resumes_provider_without_publication(self):
        from datetime import timedelta
        from content_factory_core import utc_now
        from content_factory_worker import Worker
        now = utc_now()
        worker = Worker(self.store, clock=lambda: now, worker_id='provider-worker')
        publisher = Mock()
        worker.publisher = publisher
        self.client.status.side_effect = [{'status': 'processing'}, {'status': 'completed', 'video_url': 'https://media.example/result.mp4'}]
        with patch.dict(sys.modules, {'content_factory_runtime': self.runtime}), \
                patch('content_factory_worker.is_agency_access_revoked', return_value=False), \
                patch('kadrian_production.provider_client', return_value=self.client), \
                patch('kadrian_production.download_result', return_value=b'mp4'):
            first = worker.run_once(owner_id=11, project_id=self.project['id'])
            self.assertEqual(first['pending'], 1)
            worker.clock = lambda: now + timedelta(seconds=61)
            second = worker.run_once(owner_id=11, project_id=self.project['id'])
            self.assertEqual(second['generated'], 1)
        self.client.create.assert_called_once()
        publisher.assert_not_called()

    def test_large_completed_result_keeps_provider_delivery_without_reordering(self):
        from content_factory_live_video import LiveVideoError
        self.client.status.return_value = {'status': 'completed', 'video_url': 'https://media.example/result.mp4'}
        with patch.dict(sys.modules, {'content_factory_runtime': self.runtime}), \
                patch('kadrian_production.provider_client', return_value=self.client), \
                patch('kadrian_production.download_result', side_effect=LiveVideoError('Большой файл', code='video_too_large')):
            self.assertEqual(process_provider_job(self.worker, self.job, self.project), 'generated')
        saved = self.store.get_job(11, self.job['id'])
        self.assertEqual(saved['state'], 'ready')
        self.assertTrue(saved['external_delivery'])
        self.assertEqual(saved['production']['task_id'], 'provider-task')
        self.client.create.assert_called_once()

    def test_saved_sketch_becomes_first_frame(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new('RGB', (64, 64), 'blue').save(output, format='PNG')
        asset = self.store.put_asset(11, self.project['id'], None, 'frame.png', output.getvalue(), 'image/png')
        spec = {'kind': 'scene', 'prompt': 'Персонаж улыбается', 'duration': 10, 'reference_asset': asset}
        job = {**self.job, 'provider_spec': spec}
        resolved = resolve_spec(self.store, job)
        self.assertEqual(resolved['image']['type'], 'base64')
        self.assertEqual(heygen_payload(resolved)['mode'], 'image_to_video')

    def test_missing_wallet_quote_stops_before_paid_call(self):
        from content_factory_billing import BillingError
        with patch.dict(sys.modules, {'content_factory_runtime': self.runtime}), \
                patch('kadrian_production.provider_client', return_value=self.client), \
                patch('content_factory_billing.billing_enforced', return_value=True):
            with self.assertRaises(ValueError):
                process_provider_job(self.worker, self.job, self.project)
        self.client.create.assert_not_called()
