import io
from pathlib import Path
import shutil
import tempfile
import unittest
import wave
from unittest.mock import patch

from content_factory_store import SQLiteStore
from kadrian_music import inspect_music, save_uploaded_music
from kadrian_providers import shotstack_payload


def sound():
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 8000)
    return out.getvalue()


class MusicTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_uploaded_music_is_durable_idempotent_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as folder, patch('content_studio_catalog.is_agency_access_revoked', return_value=False):
            store = SQLiteStore(Path(folder) / 'music.sqlite')
            project = store.save_project(77, {'name': 'Проект', 'product': 'Видео', 'audience': 'Владелец'})
            first = save_uploaded_music(store, 77, project['id'], sound(), 'мой трек.wav')
            again = save_uploaded_music(store, 77, project['id'], sound(), 'мой трек.wav')
            self.assertEqual(first['id'], again['id'])
            self.assertTrue(first['studio_complete'])
            self.assertEqual(first['content']['audio_duration'], 1)
            self.assertEqual(first['assets'][0]['mime_type'], 'audio/wav')
            self.assertEqual(store.read_asset(77, first['assets'][0]), sound())
            with self.assertRaises(ValueError):
                save_uploaded_music(store, 88, project['id'], sound(), 'мой трек.wav')

    @unittest.skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_invalid_or_disguised_audio_is_rejected(self):
        for data, name in [(b'not audio', 'track.mp3'), (sound(), 'track.mp3'), (sound(), 'track.exe')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_music(data, name)

    def test_montage_keeps_volume_and_stops_short_track_at_its_end(self):
        spec = {'clips': [{'url': 'https://storage.example/video.mp4', 'duration': 5}],
                'music_url': 'https://storage.example/track.wav', 'music_volume': 0.35, 'music_duration': 1}
        audio = shotstack_payload(spec)['timeline']['tracks'][1]['clips'][0]
        self.assertEqual(audio['asset']['volume'], 0.35)
        self.assertEqual(audio['length'], 1)
        for volume in [-1, 2, True, float('nan')]:
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                shotstack_payload({**spec, 'music_volume': volume})
