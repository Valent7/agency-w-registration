"""Offline regression tests for shared-key Instagram handoff and Render OAuth state.

No Meta, Telegram, Supabase or Instagram network calls.
"""
import ast
import json
import os
import re
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse, urlencode

from cryptography.fernet import Fernet

from instagram_auth_handoff import create_signed_handoff, verify_signed_handoff

KEY = "sb_secret_tests_are_private_and_shared_1234567890"


class InstagramHandoffTests(unittest.TestCase):
    def test_every_partner_uses_same_safe_mechanism(self):
        for owner in (101, 102, 103):
            signed = create_signed_handoff(owner, "Партнёр", KEY, now=10000)
            decoded = verify_signed_handoff(signed, KEY, now=10004)
            self.assertEqual(decoded["owner_id"], str(owner))
            self.assertEqual(decoded["purpose"], "agency_w_instagram_connect")
            self.assertNotIn(KEY, signed)

    def test_tampering_and_other_shared_key_never_work(self):
        signed = create_signed_handoff(101, "Эльза", KEY, now=10000)
        with self.assertRaises(ValueError):
            verify_signed_handoff(signed.replace("h1.", "h1.x", 1), KEY, now=10000)
        with self.assertRaises(ValueError):
            verify_signed_handoff(signed, KEY + "not-matching", now=10000)

    def test_state_expires_and_rejects_future_clock(self):
        signed = create_signed_handoff(101, "Алексей", KEY, now=10000)
        with self.assertRaises(ValueError):
            verify_signed_handoff(signed, KEY, now=13601)
        with self.assertRaises(ValueError):
            verify_signed_handoff(signed, KEY, now=9879)

    def test_state_contains_no_plaintext_password_or_api_key(self):
        signed = create_signed_handoff(101, "Валентина", KEY, now=10000)
        self.assertNotIn(KEY, signed)
        self.assertLess(len(signed), 1000)


SOURCE = open("instagram_webhook.py", encoding="utf-8").read()
UI_SOURCE = open("streamlit_app.py", encoding="utf-8").read()
TREE = ast.parse(SOURCE)


class InstagramServerIssuedStateTests(unittest.TestCase):
    def _backend(self):
        selected = [
            node for node in TREE.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("_decode_instagram_state", "instagram_connect")
        ]
        fernet = Fernet(Fernet.generate_key())
        class Redirect:
            def __init__(self, url, status_code=302):
                self.url = url
                self.status_code = status_code
        env = {
            "json": json, "os": os, "urlencode": urlencode,
            "InvalidToken": __import__("cryptography.fernet", fromlist=["InvalidToken"]).InvalidToken,
            "verify_signed_handoff": verify_signed_handoff,
            "Request": object,
            "_instagram_cipher": lambda: fernet,
            "_instagram_oauth_settings": lambda: {
                "INSTAGRAM_APP_ID": "fake-public-app-id",
                "INSTAGRAM_OAUTH_REDIRECT_URI": "https://test.invalid/instagram/callback",
            },
            "INSTAGRAM_OAUTH_SCOPES": "instagram_business_basic",
            "RedirectResponse": Redirect,
            "_page": lambda *args: {"error": args},
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), "instagram_webhook.py", "exec"), env)
        return env

    def test_oauth_state_created_and_decrypted_with_same_backend_key(self):
        env = self._backend()
        signed = create_signed_handoff(123, "Эльза", KEY)
        with patch.dict(
            os.environ, {"SUPABASE_SECRET_KEY": KEY}
        ):
            query = SimpleNamespace(query_params={"state": signed})
            response = __import__("asyncio").run(env["instagram_connect"](query))
            self.assertEqual(response.status_code, 302)
            url = urlparse(response.url)
            self.assertEqual(url.netloc, "www.instagram.com")
            callback_state = parse_qs(url.query)["state"][0]
            self.assertTrue(callback_state.startswith("gAAAA"))
            self.assertNotEqual(callback_state, signed)
            owner = env["_decode_instagram_state"](callback_state)
            self.assertEqual(owner["owner_id"], "123")
            self.assertEqual(owner["owner_name"], "Эльза")

    def test_invalid_owner_handoff_never_redirects_to_meta(self):
        env = self._backend()
        signed = create_signed_handoff(101, "A", KEY)
        with patch.dict(
            os.environ, {"SUPABASE_SECRET_KEY": KEY + "wrong"}
        ):
            result = __import__("asyncio").run(
                env["instagram_connect"](SimpleNamespace(query_params={"state": signed}))
            )
            self.assertIsInstance(result, dict)
            self.assertIn("error", result)

    def test_streamlit_uses_signed_handoff_and_render_connect_url(self):
        self.assertIn("from instagram_auth_handoff import create_signed_handoff", UI_SOURCE)
        self.assertIn("st.secrets.get(\"SUPABASE_SECRET_KEY\")", UI_SOURCE)
        self.assertIn('return f"{base_url}/instagram/connect?"', UI_SOURCE)
        start = UI_SOURCE.index("def _instagram_connect_state(")
        end = UI_SOURCE.index("def _render_connected_channel_card(", start)
        self.assertNotIn("Fernet(key.encode", UI_SOURCE[start:end])
        self.assertNotIn("https://www.instagram.com/oauth/authorize?", UI_SOURCE[start:end])


if __name__ == "__main__":
    unittest.main()
