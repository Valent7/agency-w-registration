"""Private video-production sources remain scoped and integrity checked."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from content_factory_store import SQLiteStore, SupabaseStore, StoreError


class SourceTests(unittest.TestCase):
    def test_local_source_roundtrip_rejects_other_owner_and_modified_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / 'factory.sqlite3')
            project = store.save_project(101, {'name': 'Agency'})
            job = store.enqueue_job(101, project['id'], {'dedupe_key': 'source', 'format': 'reel'})
            asset = store.put_asset(101, project['id'], job['id'], 'voice.m4a', b'original-aac', 'audio/mp4')
            self.assertEqual(store.read_asset(101, asset), b'original-aac')
            with self.assertRaises(StoreError):
                store.read_asset(202, asset)
            Path(store.resolve_assets(101, [asset])[0]['path']).write_bytes(b'changed')
            with self.assertRaises(StoreError):
                store.read_asset(101, asset)

    def test_source_download_uses_owned_storage_and_checks_hash_without_redirects(self):
        session = Mock()
        response = session.request.return_value
        response.ok = True
        response.iter_content.return_value = iter([b'ab', b'c'])
        store = SupabaseStore('https://project.supabase.co', 'fake-only-service-key', session=session)
        asset = {'storage_path': '101/project/job/voice.m4a', 'size': 3,
                 'sha256': hashlib.sha256(b'abc').hexdigest()}
        self.assertEqual(store.read_asset(101, asset), b'abc')
        call = session.request.call_args
        self.assertEqual(call.args, ('GET', 'https://project.supabase.co/storage/v1/object/content-factory-media/101/project/job/voice.m4a'))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertTrue(call.kwargs['stream'])
        response.close.assert_called_once()
        session.reset_mock()
        with self.assertRaises(StoreError):
            store.read_asset(202, asset)
        session.request.assert_not_called()

    def test_source_download_closes_response_when_content_is_corrupt(self):
        session = Mock()
        response = session.request.return_value
        response.ok = True
        response.iter_content.return_value = iter([b'wrong'])
        store = SupabaseStore('https://project.supabase.co', 'fake-only-service-key', session=session)
        asset = {'storage_path': '101/project/job/frame.jpg', 'size': 5,
                 'sha256': hashlib.sha256(b'right').hexdigest()}
        with self.assertRaises(StoreError):
            store.read_asset(101, asset)
        response.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
