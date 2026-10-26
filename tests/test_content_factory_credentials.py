"""Token renewal protocol tests with no provider requests or real tokens."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import traceback
import unittest
from unittest.mock import Mock

import requests

from content_factory_credentials import CredentialError, refresh_connection


NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


def connection(platform="instagram", days=3):
    return {
        "id": "connection-id", "owner_id": 11, "project_id": "project-id", "platform": platform,
        "account_id": "123", "display_name": "Partner", "access_token": "fake-old-token",
        "extra": {"token_expires_at": (NOW + timedelta(days=days)).isoformat(),
                  "scopes": ["publish"], "api_version": "v23.0", "group_id": 123},
    }


def transport(data=None, status=200):
    session = Mock()
    response = Mock(status_code=status)
    response.json.return_value = {"access_token": "fake-refreshed-token", "expires_in": 5184000} if data is None else data
    session.get.return_value = response
    return session


class CredentialRefreshTests(unittest.TestCase):
    def test_instagram_refresh_preserves_owner_account_and_metadata(self):
        original = connection()
        before = deepcopy(original)
        session = transport()
        result = refresh_connection(original, NOW, session)
        self.assertIsNot(result, original)
        self.assertEqual(original, before)
        self.assertEqual(result["access_token"], "fake-refreshed-token")
        for name in ("owner_id", "project_id", "account_id", "display_name", "platform"):
            self.assertEqual(result[name], original[name])
        self.assertEqual(result["extra"]["scopes"], ["publish"])
        self.assertEqual(result["extra"]["api_version"], "v23.0")
        self.assertEqual(result["extra"]["token_expires_at"], (NOW + timedelta(seconds=5184000)).isoformat())
        session.get.assert_called_once_with(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": "fake-old-token"},
            timeout=(15, 30), allow_redirects=False)
        self.assertNotIn("verify", session.get.call_args.kwargs)

    def test_threads_uses_threads_refresh_contract(self):
        session = transport()
        result = refresh_connection(connection("threads"), NOW, session)
        self.assertEqual(result["platform"], "threads")
        self.assertEqual(session.get.call_args.args[0], "https://graph.threads.net/refresh_access_token")
        self.assertEqual(session.get.call_args.kwargs["params"]["grant_type"], "th_refresh_token")

    def test_no_network_until_seven_day_window_and_returns_original(self):
        original = connection(days=8)
        session = transport()
        self.assertIs(refresh_connection(original, NOW, session), original)
        session.get.assert_not_called()

    def test_exactly_seven_days_renews(self):
        session = transport()
        refresh_connection(connection(days=7), NOW, session)
        session.get.assert_called_once()

    def test_expired_token_fails_without_attempting_renewal(self):
        for days in (0, -1):
            with self.subTest(days=days):
                session = transport()
                with self.assertRaises(CredentialError) as raised:
                    refresh_connection(connection(days=days), NOW, session)
                self.assertIn("истёк", str(raised.exception))
                session.get.assert_not_called()

    def test_vk_and_manually_connected_credentials_without_expiry_are_unchanged(self):
        for platform in ("vk", "instagram", "threads", "other"):
            with self.subTest(platform=platform):
                original = connection(platform)
                original["extra"].pop("token_expires_at")
                session = transport()
                self.assertIs(refresh_connection(original, NOW, session), original)
                session.get.assert_not_called()

    def test_unsupported_expiring_or_expired_platform_requires_reconnect(self):
        for days in (0, 3):
            session = transport()
            with self.assertRaises(CredentialError):
                refresh_connection(connection("vk", days=days), NOW, session)
            session.get.assert_not_called()

    def test_network_exception_never_discloses_request_token_or_url(self):
        session = transport()
        session.get.side_effect = requests.Timeout("https://provider.example/?token=private-provider-detail")
        with self.assertRaises(CredentialError) as raised:
            refresh_connection(connection(), NOW, session)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn("private-provider-detail", formatted)
        self.assertNotIn("fake-old-token", formatted)
        session.get.assert_called_once()

    def test_rejected_or_redirected_response_is_sanitized_without_retry(self):
        for status in (302, 400, 429, 503):
            with self.subTest(status=status):
                session = transport({"error": {"message": "private-provider-detail"}}, status)
                with self.assertRaises(CredentialError) as raised:
                    refresh_connection(connection(), NOW, session)
                self.assertNotIn("private-provider-detail", str(raised.exception))
                session.get.assert_called_once()

    def test_invalid_or_missing_refresh_fields_never_replace_existing_connection(self):
        samples = [[], {}, {"error": {"message": "private-provider-detail"}},
                   {"access_token": "", "expires_in": 10}, {"access_token": "new", "expires_in": 0},
                   {"access_token": "new", "expires_in": -10}, {"access_token": "new", "expires_in": True},
                   {"access_token": "new", "expires_in": "invalid"}, {"access_token": "new", "expires_in": 1.5}]
        for data in samples:
            with self.subTest(data=data):
                original = connection()
                before = deepcopy(original)
                with self.assertRaises(CredentialError):
                    refresh_connection(original, NOW, transport(data))
                self.assertEqual(original, before)

    def test_same_token_new_expiry_still_returns_updated_connection(self):
        original = connection()
        result = refresh_connection(original, NOW, transport({"access_token": "fake-old-token", "expires_in": "5184000"}))
        self.assertIsNot(result, original)
        self.assertEqual(result["access_token"], original["access_token"])
        self.assertNotEqual(result["extra"]["token_expires_at"], original["extra"]["token_expires_at"])

    def test_invalid_expiry_or_naive_clock_fails_before_network(self):
        for expiry in ("not-a-date", "2026-10-29T10:00:00", 123):
            session = transport()
            original = connection()
            original["extra"]["token_expires_at"] = expiry
            with self.assertRaises(CredentialError):
                refresh_connection(original, NOW, session)
            session.get.assert_not_called()
        with self.assertRaises(CredentialError):
            refresh_connection(connection(), NOW.replace(tzinfo=None), transport())

    def test_missing_access_token_cannot_make_provider_request(self):
        original = connection()
        original["access_token"] = ""
        session = transport()
        with self.assertRaises(CredentialError):
            refresh_connection(original, NOW, session)
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
