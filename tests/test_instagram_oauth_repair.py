"""Zero-network regression tests for Agency W Instagram OAuth + Direct diagnosis."""
import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from instagram_auth_tools import exchange_long_lived_ig_token, fetch_instagram_profile


def http_response(ok: bool, status_code: int, body: dict):
    result = Mock()
    result.ok = ok
    result.status_code = status_code
    result.json.return_value = body
    return result


class InstagramTokenExchangeTests(unittest.TestCase):
    def test_documented_get_success_never_calls_post(self):
        session = Mock()
        session.get.return_value = http_response(
            True, 200, {"access_token": "long-token", "expires_in": 5184000}
        )
        result = exchange_long_lived_ig_token("short", "private", session=session)
        self.assertEqual(result["access_token"], "long-token")
        session.post.assert_not_called()

    def test_exact_unsupported_get_triggers_one_form_post(self):
        session = Mock()
        session.get.return_value = http_response(
            False, 400, {"error": {
                "message": "Unsupported request - method type: get",
                "type": "IGApiException", "code": 100,
            }}
        )
        session.post.return_value = http_response(
            True, 200, {"access_token": "long-token", "expires_in": 5184000}
        )
        actual = exchange_long_lived_ig_token("short", "private", session=session)
        self.assertEqual(actual["access_token"], "long-token")
        session.get.assert_called_once()
        session.post.assert_called_once()
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["data"]["access_token"], "short")
        self.assertEqual(kwargs["data"]["client_secret"], "private")
        self.assertEqual(
            kwargs["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )

    def test_invalid_token_does_not_trigger_retries(self):
        session = Mock()
        session.get.return_value = http_response(
            False, 400,
            {"error": {"code": 190, "message": "Invalid OAuth token"}},
        )
        with self.assertRaisesRegex(RuntimeError, "Instagram long-lived token"):
            exchange_long_lived_ig_token("short", "private", session=session)
        session.post.assert_not_called()

    def test_post_fails_cleanly_without_second_post_or_secret(self):
        session = Mock()
        session.get.return_value = http_response(
            False, 400,
            {"error": {"code": 100, "message": "Unsupported request - method type: get"}},
        )
        session.post.return_value = http_response(
            False, 400,
            {"error": {"code": 100, "message": "Unsupported request - method type: post"}},
        )
        with self.assertRaises(RuntimeError) as context:
            exchange_long_lived_ig_token("short", "top-secret", session=session)
        self.assertNotIn("top-secret", str(context.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_profile_identification_needs_provider_id(self):
        session = Mock()
        session.get.return_value = http_response(
            True, 200, {"id": "1784141234", "username": "AgencyExample"}
        )
        result = fetch_instagram_profile("fake-token", session=session)
        self.assertEqual(result["id"], "1784141234")
        session.get.assert_called_once()
        self.assertIn("access_token", session.get.call_args.kwargs["params"])


SOURCE = Path("instagram_webhook.py").read_text(encoding="utf-8")
UI = Path("streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NEEDED = {
    "_fallback_instagram_connection",
    "_resolve_instagram_connection",
    "instagram_connection_diagnostics",
}
FUNCTIONS = [
    node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NEEDED
]


class JsonResult:
    def __init__(self, value, status_code=200, headers=None):
        self.value = value
        self.status_code = status_code
        self.headers = headers


class InstagramDirectIdentityTests(unittest.TestCase):
    def ns(self):
        env = {
            "INSTAGRAM_OWNER_ID": "123",
            "INSTAGRAM_OWNER_NAME": "Тестовая владелица",
            "INSTAGRAM_ACCESS_TOKEN": "dummy-token",
            "_verified_legacy_instagram_account_id": lambda token: "1784141234",
            "_instagram_connection_by_account_id": lambda ident: None,
            "_ensure_fresh_instagram_token": lambda connection: connection,
            "_decode_instagram_state": lambda token: {
                "owner_id": "123", "purpose": "agency_w_instagram_connect"
            },
            "_sb_get": lambda *args: [{
                "owner_telegram_id": 123,
                "instagram_account_id": "1784141234",
                "instagram_username": "AgencyExample",
                "access_token_encrypted": "fake-ciphertext",
            }],
            "_decrypt_instagram_secret": lambda token: "dummy-token",
            "fetch_instagram_profile": lambda token: {
                "id": "1784141234", "username": "AgencyExample"
            },
            "JSONResponse": JsonResult,
            "Request": object,  # Starlette annotation, not needed for offline test.
            "requests": SimpleNamespace(RequestException=RuntimeError),
        }
        exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), "instagram_webhook.py", "exec"), env)
        return env

    def test_unknown_partner_never_assigned_to_director(self):
        env = self.ns()
        self.assertIsNone(env["_fallback_instagram_connection"]("not-our-account"))
        self.assertIsNone(env["_fallback_instagram_connection"]("1784149999"))
        self.assertEqual(
            env["_fallback_instagram_connection"]("1784141234")["owner_telegram_id"],
            123,
        )

    def test_database_mapping_always_wins_over_legacy(self):
        env = self.ns()
        env["_instagram_connection_by_account_id"] = lambda identifier: {
            "instagram_account_id": identifier, "owner_telegram_id": 777
        }
        result = env["_resolve_instagram_connection"]("1784141234")
        self.assertEqual(result["owner_telegram_id"], 777)

    def test_read_only_diagnostic_matches_real_provider_identity(self):
        env = self.ns()
        request = SimpleNamespace(json=lambda: asyncio.sleep(0, result={"state": "signed"}))
        result = asyncio.run(env["instagram_connection_diagnostics"](request))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.value["identity_verified"])
        self.assertFalse(result.value["direct_replies_verified"])
        self.assertNotIn("dummy-token", json.dumps(result.value))
        self.assertNotIn("fake-ciphertext", json.dumps(result.value))

    def test_diag_finds_wrong_saved_account_without_reassignment(self):
        env = self.ns()
        env["fetch_instagram_profile"] = lambda token: {
            "id": "1784149999", "username": "AnotherAccount"
        }
        request = SimpleNamespace(json=lambda: asyncio.sleep(0, result={"state": "signed"}))
        result = asyncio.run(env["instagram_connection_diagnostics"](request))
        self.assertFalse(result.value["identity_verified"])
        self.assertEqual(result.value["reason"], "different_account")

    def test_signed_diagnostics_cannot_target_another_purpose(self):
        env = self.ns()
        env["_decode_instagram_state"] = lambda token: {
            "owner_id": "123", "purpose": "some_other_flow"
        }
        request = SimpleNamespace(json=lambda: asyncio.sleep(0, result={"state": "signed"}))
        result = asyncio.run(env["instagram_connection_diagnostics"](request))
        self.assertEqual(result.status_code, 403)

    def test_ui_distinguishes_record_from_direct_delivery(self):
        self.assertIn("Зелёный статус означает, что аккаунт записан", UI)
        self.assertIn('instagram/diagnose', UI)
        self.assertIn('Сверить аккаунт с Instagram', UI)
        self.assertIn('"status": "eq.connected"', SOURCE)
        self.assertIn("INSTAGRAM_OAUTH_CALLBACK_ERROR", SOURCE)


if __name__ == "__main__":
    unittest.main()
