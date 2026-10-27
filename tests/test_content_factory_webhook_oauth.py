"""Offline checks for sharing the existing webhook server with publishing OAuth."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.testclient import TestClient

import instagram_webhook
from content_factory_oauth import make_connect_url
from content_factory_store import SQLiteStore


CONFIG = {
    "CONTENT_FACTORY_SERVICE_URL": "https://webhook.example.com",
    "SUPABASE_URL": "https://store.example.com",
    "SUPABASE_SECRET_KEY": "unit-test-signing-fallback-" + "x" * 32,
    "FERNET_KEY": Fernet.generate_key().decode(),
    "INSTAGRAM_APP_ID": "1001",
    "INSTAGRAM_APP_SECRET": "fake-app-secret",
}


class SharedWebhookOAuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteStore(Path(directory.name) / "factory.sqlite3", CONFIG["FERNET_KEY"])
        self.project = self.store.save_project(11, {"name": "Example project"})

    def app(self, *, enabled=True):
        application = Starlette(routes=instagram_webhook.routes, lifespan=instagram_webhook._app_lifespan)
        with patch.dict(os.environ, CONFIG if enabled else {}, clear=True), \
                patch("content_factory_runtime._configuration", return_value=dict(CONFIG)), \
                patch("content_factory_runtime.build_store", return_value=self.store):
            instagram_webhook._attach_content_factory_oauth(application)
        return application

    def client(self, application):
        # Do not run the existing production subscription-restoration lifespan.
        client = TestClient(application, base_url=CONFIG["CONTENT_FACTORY_SERVICE_URL"], follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def test_optional_configuration_leaves_existing_routes_untouched(self):
        for absent in CONFIG.keys() & {"CONTENT_FACTORY_SERVICE_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY"}:
            application = Starlette(routes=instagram_webhook.routes)
            original = list(application.routes)
            config = {key: value for key, value in CONFIG.items() if key != absent}
            with self.subTest(absent=absent), patch.dict(os.environ, config, clear=True), \
                    patch("content_factory_oauth.runtime_app") as factory:
                instagram_webhook._attach_content_factory_oauth(application)
                factory.assert_not_called()
                self.assertEqual(application.routes, original)

    def test_existing_health_pages_methods_and_route_identity_are_preserved(self):
        application = self.app()
        legacy = self.client(Starlette(routes=instagram_webhook.routes))
        combined = self.client(application)
        self.assertIs(application.router.lifespan_context, instagram_webhook._app_lifespan)
        self.assertEqual(application.routes[:len(instagram_webhook.routes)], instagram_webhook.routes)
        self.assertEqual([route.path for route in application.routes[len(instagram_webhook.routes):]],
                         ["/connect/{platform}", "/oauth/{platform}/callback"])
        for method, path in (("GET", "/health"), ("GET", "/privacy"), ("GET", "/terms"),
                             ("GET", "/data-deletion"), ("GET", "/instagram/webhook"),
                             ("PATCH", "/health"), ("PATCH", "/instagram/callback"),
                             ("GET", "/not-a-route")):
            with self.subTest(method=method, path=path):
                before, after = legacy.request(method, path), combined.request(method, path)
                self.assertEqual(after.status_code, before.status_code)
                self.assertEqual(after.content, before.content)

    def test_instagram_handoff_and_callback_save_the_project_connection(self):
        client = self.client(self.app())
        link = make_connect_url(11, self.project["id"], "instagram", CONFIG)
        with patch("content_factory_oauth.is_agency_access_revoked", return_value=False):
            started = client.get(link)
            self.assertEqual(started.status_code, 302)
            params = parse_qs(urlsplit(started.headers["location"]).query)
            self.assertEqual(params["redirect_uri"],
                             [CONFIG["CONTENT_FACTORY_SERVICE_URL"] + "/oauth/instagram/callback"])
            self.assertIn("instagram_business_content_publish", params["scope"][0])
            self.assertIn("Path=/oauth/instagram/callback", started.headers["set-cookie"])
            with patch("content_factory_oauth._exchange", return_value=("777", "demo", "fake-token", {})) as exchange:
                finished = client.get("/oauth/instagram/callback", params={"state": params["state"][0], "code": "fake-code"})
                self.assertEqual(finished.status_code, 303)
                exchange.assert_called_once()
            connections = self.store.list_connections(11, self.project["id"])
            self.assertEqual(len(connections), 1)
            self.assertEqual(connections[0]["account_id"], "777")
            # Threads setup is optional and does not block the Instagram route.
            threads = client.get(make_connect_url(11, self.project["id"], "threads", CONFIG))
            self.assertEqual(threads.status_code, 403)
            replay = client.get("/oauth/instagram/callback", params={"state": params["state"][0], "code": "fake-code"})
            self.assertEqual(replay.status_code, 400)

    def test_optional_initialization_failure_keeps_webhooks_running_and_redacts_details(self):
        application = Starlette(routes=instagram_webhook.routes)
        original_routes = list(application.routes)
        original_lifespan = application.router.lifespan_context
        output = io.StringIO()
        with patch.dict(os.environ, CONFIG, clear=True), \
                patch("content_factory_oauth.runtime_app", side_effect=RuntimeError("private-provider-value")), \
                redirect_stdout(output):
            instagram_webhook._attach_content_factory_oauth(application)
        self.assertEqual(application.routes, original_routes)
        self.assertIs(application.router.lifespan_context, original_lifespan)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("private-provider-value", output.getvalue())
        self.assertEqual(self.client(application).get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
