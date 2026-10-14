"""Verify Instagram-scoped ID vs native webhook recipient ID without any network requests.

Regression: Oct 14 Instagram OAuth stored scoped id 38544174561840129 while
incoming Direct webhook used native user_id 17841458786862089.
"""
import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

SOURCE = Path("instagram_webhook.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NAMES = {
    "_migrate_native_instagram_account_id",
    "_resolve_instagram_connection",
    "_instagram_connection_by_account_id",
    "_fallback_instagram_connection",
    "_subscribe_instagram_webhooks",
}
NODES = [
    x for x in TREE.body
    if isinstance(x, ast.FunctionDef) and x.name in NAMES
]

OLD_SCOPED_ID = "38544174561840129"
NATIVE_ID = "17841458786862089"
OWNER_ID = 235805138


class InstagramNativeAccountIdTests(unittest.TestCase):
    def ns(self, *, profile_native=NATIVE_ID, conflicting=False):
        updates = []
        stored = {
            "owner_telegram_id": OWNER_ID,
            "instagram_account_id": OLD_SCOPED_ID,
            "status": "connected",
            "access_token_encrypted": "ciphertext",
        }

        def db_get(table, params):
            if params.get("instagram_account_id") == f"eq.{NATIVE_ID}":
                return (
                    [{"owner_telegram_id": 777, "instagram_account_id": NATIVE_ID}]
                    if conflicting else []
                )
            return []

        def db_patch(table, filters, payload):
            self.assertEqual(table, "agency_instagram_connections")
            self.assertEqual(filters["owner_telegram_id"], f"eq.{OWNER_ID}")
            self.assertEqual(filters["instagram_account_id"], f"eq.{OLD_SCOPED_ID}")
            updates.append((dict(filters), dict(payload)))
            stored.update(payload)

        env = {
            "datetime": datetime,
            "timezone": timezone,
            "requests": requests,
            "_sb_get": db_get,
            "_sb_patch": db_patch,
            "fetch_instagram_profile": lambda token: {
                "id": OLD_SCOPED_ID,
                "user_id": profile_native,
                "username": "ai_v_ton",
            },
            "_decrypt_instagram_secret": lambda ciphertext: "verified-token",
            "_ensure_fresh_instagram_token": lambda data: data,
            "_verified_legacy_instagram_account_id": lambda _: "",
            "INSTAGRAM_OWNER_ID": "0",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "INSTAGRAM_API_VERSION": "v25.0",
            "os": SimpleNamespace(getenv=lambda name: ""),
            "requests": requests,
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), "instagram_webhook.py", "exec"), env)
        return env, updates, stored

    def test_verified_native_id_migrates_and_preserves_owner(self):
        env, updates, stored = self.ns()
        migrated = env["_migrate_native_instagram_account_id"](
            stored, "verified-token"
        )
        self.assertEqual(migrated["owner_telegram_id"], OWNER_ID)
        self.assertEqual(migrated["instagram_account_id"], NATIVE_ID)
        self.assertEqual(stored["instagram_account_id"], NATIVE_ID)
        self.assertEqual(len(updates), 1)
        self.assertNotIn("access_token", str(updates))

    def test_missing_user_id_is_not_guessed_from_scoped_id(self):
        env, updates, stored = self.ns(profile_native="")
        result = env["_migrate_native_instagram_account_id"](stored, "verified-token")
        self.assertEqual(result["instagram_account_id"], OLD_SCOPED_ID)
        self.assertEqual(updates, [])

    def test_never_steal_another_partners_verified_id(self):
        env, updates, stored = self.ns(conflicting=True)
        result = env["_migrate_native_instagram_account_id"](stored, "verified-token")
        self.assertEqual(result["instagram_account_id"], OLD_SCOPED_ID)
        self.assertEqual(updates, [])

    def test_after_migration_direct_finds_correct_owner(self):
        env, updates, stored = self.ns()
        env["_migrate_native_instagram_account_id"](stored, "verified-token")

        def lookup(account_id):
            if account_id == stored["instagram_account_id"]:
                return {
                    **stored, "access_token": "verified-token",
                }
            return None
        env["_instagram_connection_by_account_id"] = lookup
        result = env["_resolve_instagram_connection"](NATIVE_ID)
        self.assertEqual(result["owner_telegram_id"], OWNER_ID)
        self.assertEqual(result["access_token"], "verified-token")
        self.assertEqual(env["_resolve_instagram_connection"](NATIVE_ID)["instagram_account_id"], NATIVE_ID)

    def test_messages_subscribe_using_me_not_scoped_or_native_id(self):
        env, _, _ = self.ns()
        fake = Mock()
        fake.ok = True
        fake.text = '{"success": true}'
        fake.json.return_value = {"success": True}
        env["requests"] = SimpleNamespace(post=Mock(return_value=fake))
        result = env["_subscribe_instagram_webhooks"](NATIVE_ID, "verified-token")
        self.assertTrue(result["success"])
        url = env["requests"].post.call_args.args[0]
        self.assertTrue(url.endswith("/me/subscribed_apps"), url)

    def test_missing_token_does_not_send_with_global_token(self):
        self.assertIn(
            'token = str(access_token or "").strip()', SOURCE
        )
        self.assertNotIn(
            "token = str(access_token or INSTAGRAM_ACCESS_TOKEN or "").strip()",
            SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
