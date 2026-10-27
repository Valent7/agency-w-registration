"""Local HTTP regressions for OAuth pages; no provider or lifespan calls."""
import ast
from contextlib import redirect_stdout
from html import escape
import io
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.testclient import TestClient


class OAuthPageSecurityTests(unittest.TestCase):
    def setUp(self):
        names = {
            "_page", "privacy", "terms", "data_deletion", "_instagram_return_link",
            "instagram_connect", "instagram_oauth_callback", "facebook_connect",
            "facebook_exchange", "facebook_oauth_callback",
        }
        tree = ast.parse(Path("instagram_webhook.py").read_text())
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.ns = {
            "Request": Request, "HTMLResponse": HTMLResponse, "JSONResponse": JSONResponse,
            "RedirectResponse": RedirectResponse, "html_escape": escape, "urlencode": urlencode,
            "CONTACT_EMAIL": "operator@example.com",
            "INSTAGRAM_AGENCY_RETURN_URL": "https://agency.example.com/",
            "FACEBOOK_AGENCY_REDIRECT_URI": "https://agency.example.com/",
            "FACEBOOK_OAUTH_REDIRECT_URI": "https://webhook.example.com/facebook/callback",
            "InstagramAccessNotApproved": type("AccessPending", (RuntimeError,), {}),
            "_instagram_oauth_settings": Mock(), "_facebook_oauth_settings": Mock(),
            "_complete_facebook_radar_oauth": Mock(),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "instagram_webhook.py", "exec"), self.ns)
        handlers = {
            "/instagram/connect": "instagram_connect",
            "/instagram/callback": "instagram_oauth_callback",
            "/facebook/connect": "facebook_connect",
            "/facebook/callback": "facebook_oauth_callback",
            "/privacy": "privacy", "/terms": "terms", "/data-deletion": "data_deletion",
        }
        routes = [Route(path, self.ns[name], methods=["GET"]) for path, name in handlers.items()]
        routes.append(Route("/facebook/exchange", self.ns["facebook_exchange"], methods=["POST"]))
        self.client = TestClient(Starlette(routes=routes), follow_redirects=False)
        self.addCleanup(self.client.close)

    def test_facebook_callback_escapes_unauthenticated_error_markup(self):
        marker = '<script>void(0)</script><strong id="audit-marker">test</strong>'
        for params in ({"error": "denied", "error_description": marker}, {"error": marker}):
            with self.subTest(params=params):
                response = self.client.get("/facebook/callback", params=params)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(marker, response.text)
                self.assertIn(escape(marker), response.text)
        self.ns["_complete_facebook_radar_oauth"].assert_not_called()

    def test_instagram_cancellation_keeps_escaped_error_and_return_link(self):
        marker = '<strong id="audit-marker">test</strong>'
        response = self.client.get("/instagram/callback", params={"error": "denied", "error_description": marker})
        self.assertNotIn(marker, response.text)
        self.assertIn(escape(marker), response.text)
        self.assertIn("https://agency.example.com/?instagram=cancelled", response.text)

    def test_connect_failures_expose_neither_private_exception_nor_markup(self):
        private = "provider-private-token-123<script>void(0)</script>"
        for route, helper in (("/instagram/connect", "_instagram_oauth_settings"),
                              ("/facebook/connect", "_facebook_oauth_settings")):
            with self.subTest(route=route):
                self.ns[helper].side_effect = RuntimeError(private)
                output = io.StringIO()
                with redirect_stdout(output):
                    response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("provider-private-token-123", response.text + output.getvalue())
                self.assertNotIn("<script>", response.text)

    def test_callback_failures_expose_no_private_exception_or_log_details(self):
        private = "provider-private-token-456<script>void(0)</script>"
        for route, helper in (("/instagram/callback", "_instagram_oauth_settings"),
                              ("/facebook/callback", "_complete_facebook_radar_oauth")):
            with self.subTest(route=route):
                self.ns[helper].side_effect = RuntimeError(private)
                output = io.StringIO()
                with redirect_stdout(output):
                    response = self.client.get(route, params={"state": "test-state", "code": "test-code"})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("provider-private-token-456", response.text + output.getvalue())
                self.assertNotIn("<script>", response.text)

    def test_server_exchange_does_not_disclose_provider_exception(self):
        private = "provider-private-token-789<script>void(0)</script>"
        self.ns["_complete_facebook_radar_oauth"].side_effect = RuntimeError(private)
        output = io.StringIO()
        with redirect_stdout(output):
            response = self.client.post("/facebook/exchange", json={
                "state": "test-state", "code": "test-code", "redirect_uri": "https://agency.example.com/",
            })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertNotIn("provider-private-token-789", response.text + output.getvalue())

    def test_html_pages_have_headers_and_preserve_styles_links_and_content(self):
        for route, heading in (("/privacy", "Privacy Policy"), ("/terms", "Terms of Service"),
                               ("/data-deletion", "Data Deletion Instructions")):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn(heading, response.text)
                self.assertIn("<style>", response.text)
                self.assertIn('href="mailto:operator@example.com"', response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                csp = response.headers["content-security-policy"]
                for directive in ("default-src 'none'", "style-src 'unsafe-inline'", "form-action 'none'",
                                  "base-uri 'none'", "frame-ancestors 'none'"):
                    self.assertIn(directive, csp)
        privacy = self.client.get("/privacy").text
        self.assertIn('href="/data-deletion"', privacy)
        self.assertIn('href="/terms"', privacy)

    def test_page_title_is_escaped_without_escaping_trusted_body_template(self):
        marker = '<script>void(0)</script>'
        response = self.ns["_page"](marker, '<h1>Normal page</h1><a href="/privacy">Privacy</a>')
        html = response.body.decode()
        self.assertIn("<title>" + escape(marker) + "</title>", html)
        self.assertNotIn(marker, html)
        self.assertIn("<h1>Normal page</h1>", html)
        self.assertIn('href="/privacy"', html)


if __name__ == "__main__":
    unittest.main()
