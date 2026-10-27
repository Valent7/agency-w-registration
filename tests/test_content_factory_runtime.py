"""Reuse the configured login service without changing signed owner binding."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from content_factory_oauth import _verify, HANDOFF_TTL
from content_factory_runtime import get_connect_url


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


if __name__ == "__main__":
    unittest.main()
