"""Reuse the configured login service without changing signed owner binding."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from content_factory_oauth import _verify, HANDOFF_TTL
from content_factory_runtime import get_connect_url, get_owner_instagram_connect_url
import content_factory_runtime as runtime
from content_factory_store import SQLiteStore
from cryptography.fernet import Fernet


class ExistingOAuthServiceTests(unittest.TestCase):
    def link(self, **settings):
        values = {"SUPABASE_SECRET_KEY": "test-only-server-key-" + "x" * 40, **settings}
        with patch.dict(os.environ, values, clear=True), patch("pathlib.Path.is_file", return_value=False):
            return get_connect_url(11, "bd64a64a-c56b-4c84-a12a-3472c4507e8a", "instagram")

    def test_existing_service_keeps_owner_and_project_signed(self):
        link = self.link(INSTAGRAM_OAUTH_SERVICE_URL="https://existing.example.com/")
        parts = urlsplit(link)
        self.assertEqual(parts.netloc, "existing.example.com")
        self.assertEqual(parts.path, "/connect/instagram")
        raw = parse_qs(parts.query)["assertion"][0]
        identity = _verify(raw, "test-only-server-key-" + "x" * 40,
                           purpose="content_factory_connect", platform="instagram", ttl=HANDOFF_TTL)
        self.assertEqual(identity["owner_id"], 11)
        self.assertEqual(identity["project_id"], "bd64a64a-c56b-4c84-a12a-3472c4507e8a")

    def test_dedicated_factory_service_overrides_existing_login(self):
        link = self.link(INSTAGRAM_OAUTH_SERVICE_URL="https://existing.example.com",
                         CONTENT_FACTORY_SERVICE_URL="https://factory.example.com")
        self.assertEqual(urlsplit(link).netloc, "factory.example.com")

    def test_no_service_does_not_show_unusable_connect_link(self):
        self.assertIsNone(self.link())


    def test_owner_instagram_link_is_not_project_scoped_and_carries_owner_name(self):
        values = {
            "SUPABASE_SECRET_KEY": "test-only-server-key-" + "x" * 40,
            "INSTAGRAM_OAUTH_SERVICE_URL": "https://existing.example.com/",
        }
        with patch.dict(os.environ, values, clear=True), patch("pathlib.Path.is_file", return_value=False):
            link = get_owner_instagram_connect_url(11, "Валентина")
        parts = urlsplit(link)
        self.assertEqual(parts.path, "/instagram/connect")
        self.assertIn("state", parse_qs(parts.query))
        from instagram_auth_handoff import verify_signed_handoff
        identity = verify_signed_handoff(
            parse_qs(parts.query)["state"][0],
            values["SUPABASE_SECRET_KEY"],
        )
        self.assertEqual(identity["owner_id"], "11")
        self.assertEqual(identity["owner_name"], "Валентина")


class LiveVideoRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = {"CONTENT_FACTORY_DATA_DIR": self.directory.name,
                    "SUPABASE_SECRET_KEY": "server-only-test-credential-" + "x" * 32,
                    "FERNET_KEY": Fernet.generate_key().decode()}
        self.config = patch.object(runtime, "_configuration", return_value=self.cfg)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.store = runtime.build_store()
        self.project = self.store.save_project(77, {"name": "Тестовый проект"})
        self.other = self.store.save_project(88, {"name": "Другой проект"})

    def test_provider_key_is_sealed_separately_and_bound_to_owner_project(self):
        key = "private-runway-key-for-tests"
        runtime.save_live_video_connection(77, self.project["id"], key)
        provider = runtime._provider_store(self.cfg)
        self.assertNotEqual(provider.fernet_key, self.store.fernet_key)
        with provider._db() as db:
            row = db.execute("SELECT token_encrypted FROM content_factory_connections WHERE owner_id=77").fetchone()
        self.assertNotIn(key, row["token_encrypted"])
        self.assertEqual(runtime._saved_video_key(77, self.project["id"], self.cfg), key)
        self.assertEqual(runtime._saved_video_key(88, self.other["id"], self.cfg), "")
        caps = runtime.get_live_video_capabilities(77, self.project["id"])
        self.assertTrue(caps["live_video_configured"])
        self.assertNotIn(key, json.dumps(caps))

    def test_copied_ciphertext_cannot_authorize_another_owner(self):
        runtime.save_live_video_connection(77, self.project["id"], "first-private-runway-key")
        runtime.save_live_video_connection(88, self.other["id"], "second-private-runway-key")
        provider = runtime._provider_store(self.cfg)
        with provider._db() as db:
            encrypted = db.execute("SELECT token_encrypted FROM content_factory_connections WHERE owner_id=77").fetchone()[0]
            db.execute("UPDATE content_factory_connections SET token_encrypted=? WHERE owner_id=88", (encrypted,))
        with self.assertRaises(ValueError) as caught:
            runtime._saved_video_key(88, self.other["id"], self.cfg)
        self.assertNotIn("first-private-runway-key", str(caught.exception))
        self.assertFalse(runtime.get_live_video_capabilities(88, self.other["id"])["live_video_configured"])

    def test_existing_global_secret_is_synced_once_without_rewriting_each_view(self):
        provider = runtime._provider_store(self.cfg)
        key = "already-configured-runway-key"
        with patch.object(runtime, "_provider_store", return_value=provider), patch.object(provider, "save_connection", wraps=provider.save_connection) as save:
            self.assertTrue(runtime.sync_live_video_connection(77, self.project["id"], key))
            self.assertTrue(runtime.sync_live_video_connection(77, self.project["id"], key))
            self.assertEqual(save.call_count, 1)
        self.assertEqual(runtime._saved_video_key(77, self.project["id"], self.cfg), key)

    def test_provider_bridge_survives_different_social_connection_keys(self):
        runtime.save_live_video_connection(77, self.project["id"], "shared-provider-private-key")
        changed = {**self.cfg, "FERNET_KEY": Fernet.generate_key().decode()}
        self.assertEqual(runtime._saved_video_key(77, self.project["id"], changed), "shared-provider-private-key")

    def test_missing_provider_does_not_create_any_connection(self):
        self.assertFalse(runtime.sync_live_video_connection(77, self.project["id"]))
        self.assertFalse(runtime.get_live_video_capabilities(77, self.project["id"])["live_video_configured"])
        self.assertEqual(self.store.list_connections(77, self.project["id"]), [])

    def test_global_key_has_priority_without_reading_another_connection(self):
        self.cfg["RUNWAYML_API_SECRET"] = "configured-global-provider-key"
        with patch("content_factory_live_video.RunwayVideoClient") as client, patch.object(runtime, "_saved_video_key", side_effect=AssertionError("Must not read a scoped key")):
            runtime.resolve_live_video_client({"owner_id": 77, "id": self.project["id"]})
        client.assert_called_once_with("configured-global-provider-key", model="gen4.5")


if __name__ == "__main__":
    unittest.main()
