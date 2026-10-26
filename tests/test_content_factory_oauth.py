"""Offline OAuth end-to-end tests over an actual encrypted owner store."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
import requests
from starlette.testclient import TestClient

from content_factory_oauth import create_app, make_connect_url
from content_factory_store import SQLiteStore


CONFIG = {
    "CONTENT_FACTORY_SERVICE_URL": "https://factory.example.com",
    "CONTENT_FACTORY_RETURN_URL": "https://agency.example.com/cabinet?tab=content",
    "CONTENT_FACTORY_OAUTH_SIGNING_KEY": "unit-test-only-signing-key-" + "x" * 32,
    "INSTAGRAM_APP_ID": "1001", "INSTAGRAM_APP_SECRET": "fake-instagram-app-secret",
    "THREADS_APP_ID": "1002", "THREADS_APP_SECRET": "fake-threads-app-secret",
}


class ProviderResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return deepcopy(self.data)


class Provider:
    def __init__(self, platform, *, profile=None, permissions=None, expiry=5184000):
        scopes = (["instagram_business_basic", "instagram_business_content_publish"] if platform == "instagram"
                  else ["threads_basic", "threads_content_publish"])
        self.steps = [
            ("post", "/oauth/access_token", ProviderResponse({"access_token": "fake-short-lived-token", "scope": scopes if permissions is None else permissions})),
            ("get", "/access_token", ProviderResponse({"access_token": "fake-long-lived-token", "expires_in": expiry})),
            ("get", "/me", ProviderResponse(profile or ({"id": "555", "user_id": "777", "username": "partner_ig"}
                                                       if platform == "instagram" else {"id": "888", "username": "partner_threads"}))),
        ]
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.steps:
            raise AssertionError("Unexpected provider call")
        expected, suffix, response = self.steps.pop(0)
        if method != expected or not url.endswith(suffix):
            raise AssertionError("Incorrect OAuth protocol step")
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)


class ContentFactoryOAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "factory.sqlite3"
        self.store = SQLiteStore(self.path, Fernet.generate_key())
        self.project = self.store.save_project(11, {"name": "Первый проект"})
        self.other = self.store.save_project(22, {"name": "Другой владелец"})
        self.config = dict(CONFIG)
        self.app = create_app(self.store, self.config)
        self.client = TestClient(self.app, base_url=CONFIG["CONTENT_FACTORY_SERVICE_URL"], follow_redirects=False)
        self.addCleanup(self.client.close)

    def begin(self, platform="instagram", *, owner=11, project=None, client=None):
        link = make_connect_url(owner, project or self.project["id"], platform, self.config)
        response = (client or self.client).get(link)
        return response

    def state(self, response):
        return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]

    def finish(self, state, platform="instagram", *, code="fake-authorization-code", client=None, extra=None):
        params = {"state": state, "code": code, **(extra or {})}
        return (client or self.client).get(f"/oauth/{platform}/callback?" + urlencode(params))

    def test_connect_redirect_has_required_scopes_fixed_callback_and_secure_cookie(self):
        response = self.begin()
        self.assertEqual(response.status_code, 302)
        parts = urlsplit(response.headers["location"])
        self.assertEqual(parts.netloc, "www.instagram.com")
        query = parse_qs(parts.query)
        self.assertEqual(query["redirect_uri"], ["https://factory.example.com/oauth/instagram/callback"])
        self.assertIn("instagram_business_content_publish", query["scope"][0])
        cookie = response.headers["set-cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600", "Path=/oauth/instagram/callback"):
            self.assertIn(attribute, cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(CONFIG["INSTAGRAM_APP_SECRET"], response.headers["location"])

    def test_instagram_success_saves_encrypted_long_token_and_native_account(self):
        provider = Provider("instagram")
        self.app.state.oauth_session = provider
        response = self.finish(self.state(self.begin()))
        self.assertEqual(response.status_code, 303)
        target = urlsplit(response.headers["location"])
        self.assertEqual(target.netloc, "agency.example.com")
        self.assertEqual(parse_qs(target.query)["content_factory_connected"], ["instagram"])
        row = self.store.get_connection(11, self.project["id"], "instagram")
        self.assertEqual(row["account_id"], "777")  # Native ID, not app-scoped 555.
        self.assertEqual(row["access_token"], "fake-long-lived-token")
        self.assertTrue(row["extra"]["scopes_verified"])
        self.assertGreater(datetime.fromisoformat(row["extra"]["token_expires_at"]), datetime.now(timezone.utc))
        self.assertNotIn(b"fake-long-lived-token", self.path.read_bytes())
        self.assertNotIn("fake-long-lived-token", response.text + str(dict(response.headers)))
        self.assertNotIn("access_token", str(self.store.list_connections(11, self.project["id"])))
        self.assertFalse(provider.steps)

    def test_threads_success_uses_threads_provider_and_scoped_project(self):
        provider = Provider("threads")
        self.app.state.oauth_session = provider
        begin = self.begin("threads", owner=22, project=self.other["id"])
        self.assertEqual(urlsplit(begin.headers["location"]).netloc, "threads.net")
        self.assertIn("threads_content_publish", parse_qs(urlsplit(begin.headers["location"]).query)["scope"][0])
        response = self.finish(self.state(begin), "threads")
        self.assertEqual(response.status_code, 303)
        row = self.store.get_connection(22, self.other["id"], "threads")
        self.assertEqual(row["account_id"], "888")
        self.assertIsNone(self.store.get_connection(11, self.project["id"], "threads"))
        self.assertTrue(all("graph.threads.net" in call[1] for call in provider.calls))

    def test_wrong_owner_project_assertion_rejected_even_if_signed(self):
        response = self.begin(owner=22)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("location", response.headers)
        self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_tampered_assertion_rejected(self):
        link = make_connect_url(11, self.project["id"], "instagram", self.config)
        assertion = parse_qs(urlsplit(link).query)["assertion"][0]
        response = self.client.get("/connect/instagram?" + urlencode({"assertion": assertion[:-1] + ("a" if assertion[-1] != "a" else "b")}))
        self.assertEqual(response.status_code, 403)

    def test_handoff_expires_after_two_minutes(self):
        with patch("content_factory_oauth.time.time", return_value=100000):
            link = make_connect_url(11, self.project["id"], "instagram", self.config)
        with patch("content_factory_oauth.time.time", return_value=100121):
            response = self.client.get(link)
        self.assertEqual(response.status_code, 403)

    def test_provider_state_expires_after_ten_minutes(self):
        with patch("content_factory_oauth.time.time", return_value=100000):
            state = self.state(self.begin())
        with patch("content_factory_oauth.time.time", return_value=100601):
            response = self.finish(state)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_missing_or_cross_browser_cookie_blocks_exchange(self):
        provider = Provider("instagram")
        self.app.state.oauth_session = provider
        state = self.state(self.begin())
        with TestClient(self.app, base_url=CONFIG["CONTENT_FACTORY_SERVICE_URL"], follow_redirects=False) as other_browser:
            response = self.finish(state, client=other_browser)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(provider.calls, [])

    def test_wrong_platform_state_blocks_exchange(self):
        provider = Provider("threads")
        self.app.state.oauth_session = provider
        state = self.state(self.begin())
        response = self.finish(state, "threads")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(provider.calls, [])

    def test_state_from_another_project_browser_flow_cannot_replace_owner(self):
        provider = Provider("instagram")
        self.app.state.oauth_session = provider
        old_state = self.state(self.begin())
        self.begin(owner=22, project=self.other["id"])
        response = self.finish(old_state)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(provider.calls, [])

    def test_connect_assertion_cannot_be_used_as_provider_state(self):
        link = make_connect_url(11, self.project["id"], "instagram", self.config)
        assertion = parse_qs(urlsplit(link).query)["assertion"][0]
        self.begin()
        response = self.finish(assertion)
        self.assertEqual(response.status_code, 400)

    def test_callback_replay_rejected_even_with_copied_cookie(self):
        provider = Provider("instagram")
        self.app.state.oauth_session = provider
        state = self.state(self.begin())
        cookie = self.client.cookies.get("cf_oauth_instagram")
        self.assertEqual(self.finish(state).status_code, 303)
        self.client.cookies.set("cf_oauth_instagram", cookie, domain="factory.example.com", path="/oauth/instagram/callback")
        replay = self.finish(state)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(len(provider.calls), 3)

    def test_cancelled_consent_is_sanitized_and_never_saves_token(self):
        provider = Provider("instagram")
        self.app.state.oauth_session = provider
        response = self.finish(self.state(self.begin()), extra={"error": "access_denied", "error_description": "never-show-this-token"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("never-show-this-token", response.text)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_provider_errors_and_timeout_do_not_expose_secret_details(self):
        for failure in (ProviderResponse({"error": {"message": "fake-secret-provider-detail"}}, 400),
                        requests.Timeout("https://provider.example/?token=fake-secret-provider-detail")):
            with self.subTest(failure=type(failure).__name__):
                provider = Provider("threads")
                provider.steps[0] = ("post", "/oauth/access_token", failure)
                self.app.state.oauth_session = provider
                response = self.finish(self.state(self.begin("threads")), "threads")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("fake-secret-provider-detail", response.text)
                self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_missing_native_instagram_id_does_not_save_app_scoped_id(self):
        self.app.state.oauth_session = Provider("instagram", profile={"id": "555", "username": "app_scoped_only"})
        response = self.finish(self.state(self.begin()))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_partial_permissions_are_rejected_before_save(self):
        self.app.state.oauth_session = Provider("threads", permissions=["threads_basic"])
        response = self.finish(self.state(self.begin("threads")), "threads")
        self.assertEqual(response.status_code, 400)
        self.assertIn("публикацию", response.text)
        self.assertEqual(self.store.list_connections(11, self.project["id"]), [])

    def test_arbitrary_return_url_query_is_ignored(self):
        self.app.state.oauth_session = Provider("threads")
        begin = self.begin("threads")
        response = self.finish(self.state(begin), "threads", extra={"return_url": "https://attacker.example/capture"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(urlsplit(response.headers["location"]).netloc, "agency.example.com")

    def test_automatic_vk_url_is_absent_and_frontend_needs_no_app_secret(self):
        self.assertIsNone(make_connect_url(11, self.project["id"], "vk", self.config))
        cfg = {key: value for key, value in self.config.items() if not key.endswith("APP_SECRET")}
        self.assertIsNotNone(make_connect_url(11, self.project["id"], "instagram", cfg))
        cfg.pop("CONTENT_FACTORY_OAUTH_SIGNING_KEY")
        self.assertIsNone(make_connect_url(11, self.project["id"], "instagram", cfg))
        cfg["SUPABASE_SECRET_KEY"] = "unit-test-fallback-key-" + "x" * 32
        self.assertIsNotNone(make_connect_url(11, self.project["id"], "instagram", cfg))

    def test_non_https_service_url_and_invalid_project_are_rejected(self):
        for service in ("http://factory.example.com", "https://127.0.0.1", "https://user:secret@factory.example.com", "https://factory.example.com/other?key=secret"):
            self.assertIsNone(make_connect_url(11, self.project["id"], "instagram", {**CONFIG, "CONTENT_FACTORY_SERVICE_URL": service}))
        self.assertIsNone(make_connect_url(11, "not-a-project-id", "instagram", self.config))

    def test_health_contains_no_config_or_credentials(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
