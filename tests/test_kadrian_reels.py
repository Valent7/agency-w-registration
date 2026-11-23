"""Offline acceptance for source ownership, versioning, and the real media render."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore
from kadrian_reels import save_revision, save_sources


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and FFprobe required')
class ReelsTest(unittest.TestCase):
    def test_original_voice_and_multiple_versions_survive_reopen(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / 'test.db'
            store = SQLiteStore(database)
            project = store.save_project(77, {'name': 'Studio'})['id']
            clip = root / 'clip.mp4'
            voice = root / 'voice.wav'
            music = root / 'music.wav'
            for source, path in [('color=blue:s=360x640:d=1', clip),
                                 ('sine=frequency=440:duration=1', voice),
                                 ('sine=frequency=220:duration=2', music)]:
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, str(path)], check=True)
            original = voice.read_bytes()
            inputs = dict(clip=('mp4', clip.read_bytes()), voice=('wav', original),
                          music=('wav', music.read_bytes()))
            source = save_sources(store, 77, project, **inputs)
            self.assertEqual(save_sources(store, 77, project, **inputs)['id'], source['id'])
            options = dict(title='Голос', caption='Первый монтаж', style='Элегантный', music_level=8)
            first = save_revision(store, 77, project, source['id'], **options)
            self.assertEqual(save_revision(store, 77, project, source['id'], **options)['id'], first['id'])

            reopened = SQLiteStore(database)
            second = save_revision(reopened, 77, project, source['id'], **{**options, 'title': 'Новый титр'})
            self.assertNotEqual(first['id'], second['id'])
            self.assertNotEqual(first['assets'][0]['sha256'], second['assets'][0]['sha256'])
            saved_voice = reopened.get_job(77, source['id'])['reels_sources']['voice']
            self.assertEqual(saved_voice['sha256'], sha256(original).hexdigest())
            self.assertEqual(reopened.read_asset(77, saved_voice), original)
            self.assertIsNone(reopened.claim_job('worker', iso(utc_now())))
            self.assertIsNone(reopened.get_job(78, first['id']))
            with self.assertRaises(ValueError):
                save_revision(reopened, 78, project, source['id'], **options)
            for job in (first, second):
                export = root / 'render.mp4'
                export.write_bytes(reopened.read_asset(77, job['assets'][0]))
                data = json.loads(subprocess.check_output(
                    ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(export)]
                ))
                video, audio = data['streams']
                self.assertEqual((video['codec_name'], video['width'], video['height']), ('h264', 1080, 1920))
                self.assertEqual(audio['codec_name'], 'aac')
                self.assertAlmostEqual(float(audio['duration']), 1, places=2)
