"""Storage behavior without network credentials or live Supabase writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
import requests

from content_factory_store import SQLiteStore, StoreError, SupabaseStore


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Fernet.generate_key()
        self.path = Path(self.temp.name) / "factory.sqlite3"
        self.store = SQLiteStore(self.path, self.key)
        self.project = self.store.save_project(11, {"name": "First", "automation_enabled": True})
        self.other = self.store.save_project(22, {"name": "Other"})
        self.now = datetime.now(timezone.utc)

    def job(self, key="one", owner=11, project=None, **changes):
        return self.store.enqueue_job(owner, (project or self.project)["id"], {
            "dedupe_key": key, "platform": "threads", "format": "post",
            "scheduled_at": self.now.isoformat(), "available_at": self.now.isoformat(),
            **changes,
        })

    def test_projects_remain_owner_scoped_and_other_owner_cannot_overwrite(self):
        self.assertIsNone(self.store.get_project(22, self.project["id"]))
        self.assertEqual([self.project["id"]], [p["id"] for p in self.store.list_projects(11)])
        with self.assertRaises(StoreError):
            self.store.save_project(22, {"id": self.project["id"], "name": "Hijacked"})
        self.assertEqual("First", self.store.get_project(11, self.project["id"])["name"])
        self.assertEqual([self.project["id"]], [p["id"] for p in self.store.list_active_projects()])
        updated = self.store.save_project(11, {"id": self.project["id"], "name": "Renamed"})
        self.assertTrue(updated["automation_enabled"])

    def test_connection_encrypted_at_rest_and_metadata_never_contains_token(self):
        result = self.store.save_connection(11, self.project["id"], "threads", "account", "Profile", "private-social-token", {"scopes": ["threads_content_publish"]})
        self.assertNotIn("token_encrypted", result)
        self.assertNotIn("access_token", result)
        self.assertNotIn("private-social-token", json.dumps(self.store.list_connections(11, self.project["id"])))
        with sqlite3.connect(self.path) as db:
            encrypted = db.execute("SELECT token_encrypted FROM content_factory_connections").fetchone()[0]
        self.assertNotEqual("private-social-token", encrypted)
        self.assertEqual(b"private-social-token", Fernet(self.key).decrypt(encrypted.encode()))
        self.assertEqual("private-social-token", self.store.get_connection(11, self.project["id"], "threads")["access_token"])
        self.assertIsNone(self.store.get_connection(22, self.project["id"], "threads"))
        self.assertEqual([], self.store.list_connections(22, self.project["id"]))

    def test_missing_or_wrong_encryption_key_fails_without_echoing_credentials(self):
        store = SQLiteStore(self.path)
        with self.assertRaises(StoreError) as result:
            store.save_connection(11, self.project["id"], "instagram", "1", "Profile", "private-social-token")
        self.assertNotIn("private-social-token", str(result.exception))
        self.store.save_connection(11, self.project["id"], "threads", "1", "Profile", "private-social-token")
        wrong = SQLiteStore(self.path, Fernet.generate_key())
        with self.assertRaises(StoreError):
            wrong.get_connection(11, self.project["id"], "threads")

    def test_extra_metadata_cannot_bypass_encrypted_token_storage(self):
        with self.assertRaises(StoreError):
            self.store.save_connection(11, self.project["id"], "threads", "1", "Profile", "protected", {"nested": {"access_token": "plaintext"}})
        self.assertEqual([], self.store.list_connections(11, self.project["id"]))

    def test_refresh_cannot_resurrect_disconnected_or_replace_reconnected_account(self):
        project_id = self.project["id"]
        self.store.save_connection(11, project_id, "threads", "101", "Original", "old-token")
        original = self.store.get_connection(11, project_id, "threads")
        renewed = {**original, "access_token": "refreshed-old-token", "extra": {"token_expires_at": "2099-01-01T00:00:00+00:00"}}
        self.store.delete_connection(11, project_id, "threads")
        self.assertFalse(self.store.refresh_connection_if_unchanged(11, project_id, "threads", original, renewed))
        self.assertIsNone(self.store.get_connection(11, project_id, "threads"))
        self.store.save_connection(11, project_id, "threads", "202", "Replacement", "replacement-token")
        self.assertFalse(self.store.refresh_connection_if_unchanged(11, project_id, "threads", original, renewed))
        current = self.store.get_connection(11, project_id, "threads")
        self.assertEqual("202", current["account_id"])
        self.assertEqual("replacement-token", current["access_token"])

    def test_concurrent_refreshes_only_one_updates_original_connection_version(self):
        project_id = self.project["id"]
        self.store.save_connection(11, project_id, "threads", "101", "Profile", "old-token")
        original = self.store.get_connection(11, project_id, "threads")
        def refresh(number):
            return self.store.refresh_connection_if_unchanged(11, project_id, "threads", original, {**original, "access_token": f"new-token-{number}"})
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(refresh, range(8)))
        self.assertEqual(1, sum(outcomes))
        winner = outcomes.index(True)
        self.assertEqual(f"new-token-{winner}", self.store.get_connection(11, project_id, "threads")["access_token"])

    def test_refresh_rejects_cross_owner_and_account_change(self):
        self.store.save_connection(11, self.project["id"], "threads", "101", "Profile", "old-token")
        original = self.store.get_connection(11, self.project["id"], "threads")
        with self.assertRaises(StoreError):
            self.store.refresh_connection_if_unchanged(22, self.project["id"], "threads", original, {**original, "access_token": "new"})
        with self.assertRaises(StoreError):
            self.store.refresh_connection_if_unchanged(11, self.project["id"], "threads", original, {**original, "access_token": "new", "account_id": "202"})

    def test_cross_owner_job_and_connection_writes_fail(self):
        with self.assertRaises(StoreError):
            self.job(owner=22)
        with self.assertRaises(StoreError):
            self.store.save_connection(22, self.project["id"], "vk", "1", "Profile", "secret")
        job = self.job()
        self.assertIsNone(self.store.get_job(22, job["id"]))
        self.assertEqual([], self.store.list_jobs(22, self.project["id"]))
        with self.assertRaises(StoreError):
            self.store.update_job(22, job["id"], {"state": "published"})
        with self.assertRaises(StoreError):
            self.store.update_job(11, job["id"], {"owner_id": 22})

    def test_concurrent_deduplication_preserves_original_revision(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            jobs = list(executor.map(lambda number: self.job("same", topic=f"Topic {number}"), range(20)))
        self.assertEqual(1, len({job["id"] for job in jobs}))
        self.assertEqual(1, len(self.store.list_jobs(11, self.project["id"])))

    def test_claims_are_atomic_due_only_and_respect_tenant_filters(self):
        jobs = [self.job(str(number)) for number in range(5)]
        self.job("future", available_at=(self.now + timedelta(days=1)).isoformat())
        self.job("review", state="ready")
        self.job("other", owner=22, project=self.other)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda number: self.store.claim_job(f"worker-{number}", self.now.isoformat(), owner_id=11, project_id=self.project["id"]), range(10)))
        claimed = [result for result in results if result]
        self.assertEqual({job["id"] for job in jobs}, {job["id"] for job in claimed})
        self.assertEqual(5, len(claimed))
        self.assertTrue(all(job["attempts"] == 1 for job in claimed))
        self.assertTrue(all(job["state"] == "queued" for job in claimed))
        other = self.store.claim_job("other", self.now.isoformat(), owner_id=22)
        self.assertEqual(22, other["owner_id"])

    def test_lease_blocks_ui_and_other_workers_then_can_be_reclaimed(self):
        job = self.job()
        claimed = self.store.claim_job("first", self.now.isoformat(), lease_seconds=60)
        self.assertEqual(job["id"], claimed["id"])
        with self.assertRaises(StoreError):
            self.store.update_job(11, job["id"], {"state": "published"}, lease_owner="wrong")
        with self.assertRaises(StoreError):
            self.store.update_job(11, job["id"], {"state": "cancelled"})
        self.assertIsNone(self.store.claim_job("second", self.now.isoformat()))
        reclaimed = self.store.claim_job("second", (self.now + timedelta(seconds=61)).isoformat())
        self.assertEqual(2, reclaimed["attempts"])
        with self.assertRaises(StoreError):
            self.store.update_job(11, job["id"], {"state": "published"}, lease_owner="first")
        done = self.store.update_job(11, job["id"], {"state": "published", "lease_owner": None, "lease_until": None}, lease_owner="second")
        self.assertIsNone(done["lease_owner"])
        self.assertEqual("published", done["state"])

    def test_stale_user_action_cannot_resurrect_cancelled_job(self):
        job = self.job()
        cancelled = self.store.update_job(11, job["id"], {"state": "cancelled"}, expected_updated_at=job["updated_at"])
        with self.assertRaises(StoreError):
            self.store.update_job(11, job["id"], {"state": "scheduled"}, expected_updated_at=job["updated_at"])
        self.assertEqual("cancelled", self.store.get_job(11, job["id"])["state"])
        self.assertNotEqual(job["updated_at"], cancelled["updated_at"])

    def test_expired_lease_cannot_persist_public_write_barrier(self):
        job = self.job()
        self.store.claim_job("first", self.now.isoformat(), lease_seconds=60)
        expired_now = (self.now + timedelta(seconds=61)).isoformat()
        with patch("content_factory_store._now", return_value=expired_now):
            with self.assertRaises(StoreError):
                self.store.update_job(11, job["id"], {"checkpoint": {"publish_started": True}}, lease_owner="first")
        self.assertEqual({}, self.store.get_job(11, job["id"])["checkpoint"])

    def test_concurrent_user_actions_only_one_can_change_same_revision(self):
        job = self.job()
        def change(state):
            try:
                self.store.update_job(11, job["id"], {"state": state}, expected_updated_at=job["updated_at"])
                return state
            except StoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(change, ["cancelled", "scheduled"]))
        winner = [state for state in result if state]
        self.assertEqual(1, len(winner))
        self.assertEqual(winner[0], self.store.get_job(11, job["id"])["state"])

    def test_generation_limit_is_atomic_idempotent_and_separate_per_day(self):
        day = self.now.date().isoformat()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda number: self.store.reserve_generation(11, self.project["id"], f"job-{number}", day, 3), range(20)))
        self.assertEqual(3, sum(results))
        winner = next(number for number, result in enumerate(results) if result)
        self.assertTrue(self.store.reserve_generation(11, self.project["id"], f"job-{winner}", day, 3))
        self.assertFalse(self.store.reserve_generation(11, self.project["id"], "another", day, 3))
        self.assertTrue(self.store.reserve_generation(11, self.project["id"], "tomorrow", (self.now.date() + timedelta(days=1)).isoformat(), 3))
        with self.assertRaises(StoreError):
            self.store.reserve_generation(22, self.project["id"], "steal", day, 3)

    def test_media_is_durable_owner_scoped_and_urls_are_not_invented(self):
        job = self.job()
        asset = self.store.put_asset(11, self.project["id"], job["id"], "../../post.png", b"image-content", "image/png")
        self.assertNotIn("..", asset["storage_path"])
        media = self.store.resolve_assets(11, [asset])[0]
        self.assertEqual(b"image-content", Path(media["path"]).read_bytes())
        self.assertEqual("", media["url"])
        with self.assertRaises(StoreError):
            self.store.resolve_assets(22, [asset])
        with self.assertRaises(StoreError):
            self.store.resolve_assets(11, [{"storage_path": "11/../../outside/file"}])
        with self.assertRaises(StoreError):
            self.store.put_asset(22, self.project["id"], job["id"], "post.png", b"image", "image/png")


def response(payload, status=200):
    result = Mock()
    result.ok = 200 <= status < 300
    result.status_code = status
    result.content = json.dumps(payload).encode()
    result.json.return_value = payload
    return result


class SupabaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.key = Fernet.generate_key()
        self.store = SupabaseStore("https://project.supabase.co", "server-only-secret", self.key, session=self.session)
        self.project_id = "12345678-1234-5678-1234-567812345678"

    def test_connection_list_requests_only_safe_owner_scoped_fields(self):
        self.session.request.side_effect = [
            response([{"id": "a", "extra": {}, "platform": "threads"}]),
            response([]),
        ]
        result = self.store.list_connections(11, self.project_id)
        first_call = self.session.request.call_args_list[0]
        params = first_call.kwargs["params"]
        self.assertEqual("eq.11", params["owner_id"])
        self.assertEqual("eq." + self.project_id, params["project_id"])
        self.assertNotIn("token_encrypted", params["select"])
        self.assertTrue(result[0]["has_token"])

    def test_owner_instagram_is_reused_by_project_without_exposing_token(self):
        instagram_key = Fernet.generate_key()
        encrypted = Fernet(instagram_key).encrypt(b"owner-instagram-token").decode()
        self.session.request.side_effect = [
            response([]),
            response([{
                "owner_telegram_id": 11,
                "instagram_account_id": "178414123456789",
                "instagram_username": "agency_owner",
                "access_token_encrypted": encrypted,
                "token_expires_at": "2026-12-01T00:00:00+00:00",
                "status": "connected",
                "connected_at": "2026-10-29T00:00:00+00:00",
                "updated_at": "2026-10-29T00:00:00+00:00",
            }]),
        ]
        with patch.dict(os.environ, {"FERNET_KEY": instagram_key.decode()}, clear=False):
            listed = self.store.list_connections(11, self.project_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["platform"], "instagram")
        self.assertEqual(listed[0]["account_id"], "178414123456789")
        self.assertTrue(listed[0]["extra"]["owner_level"])
        self.assertTrue(listed[0]["extra"]["direct_enabled"])
        self.assertNotIn("access_token", listed[0])
        self.assertNotIn("owner-instagram-token", json.dumps(listed))

        self.session.request.side_effect = [
            response([{
                "owner_telegram_id": 11,
                "instagram_account_id": "178414123456789",
                "instagram_username": "agency_owner",
                "access_token_encrypted": encrypted,
                "token_expires_at": "2026-12-01T00:00:00+00:00",
                "status": "connected",
                "connected_at": "2026-10-29T00:00:00+00:00",
                "updated_at": "2026-10-29T00:00:00+00:00",
            }]),
        ]
        with patch.dict(os.environ, {"FERNET_KEY": instagram_key.decode()}, clear=False):
            connection = self.store.get_connection(11, self.project_id, "instagram")
        self.assertEqual(connection["access_token"], "owner-instagram-token")
        self.assertEqual(connection["project_id"], self.project_id)

    def test_owner_instagram_token_uses_instagram_key_not_factory_key(self):
        instagram_key = Fernet.generate_key()
        encrypted = Fernet(instagram_key).encrypt(b"separate-instagram-token").decode()
        self.session.request.side_effect = [
            response([{
                "owner_telegram_id": 11,
                "instagram_account_id": "178414123456789",
                "instagram_username": "owner",
                "access_token_encrypted": encrypted,
                "status": "connected",
            }]),
        ]
        with patch.dict(os.environ, {"FERNET_KEY": instagram_key.decode()}, clear=False):
            connection = self.store.get_connection(11, self.project_id, "instagram")
        self.assertEqual(connection["access_token"], "separate-instagram-token")

    def test_rpc_claim_is_filtered_and_reservation_uses_atomic_rpc(self):
        now = datetime.now(timezone.utc).isoformat()
        self.session.request.return_value = response([])
        self.assertIsNone(self.store.claim_job("ui", now, owner_id=11, project_id=self.project_id))
        call = self.session.request.call_args
        self.assertTrue(call.args[1].endswith("/rpc/content_factory_claim_job"))
        self.assertEqual(11, call.kwargs["json"]["p_owner_id"])
        self.assertEqual(self.project_id, call.kwargs["json"]["p_project_id"])
        self.session.request.return_value = response(False)
        self.assertFalse(self.store.reserve_generation(11, self.project_id, "job:1", "2026-10-26", 5))
        self.assertTrue(self.session.request.call_args.args[1].endswith("/rpc/content_factory_reserve_generation"))

    def test_refresh_is_update_only_with_identity_and_original_version_conditions(self):
        original = {"id": self.project_id, "owner_id": 11, "project_id": self.project_id, "platform": "threads", "account_id": "101", "updated_at": "2026-10-26T12:00:00+00:00", "access_token": "old-token", "extra": {}}
        self.session.request.return_value = response([])
        self.assertFalse(self.store.refresh_connection_if_unchanged(11, self.project_id, "threads", original, {**original, "access_token": "renewed-token"}))
        call = self.session.request.call_args
        self.assertEqual("PATCH", call.args[0])
        for key in ("id", "owner_id", "project_id", "platform", "account_id", "updated_at"):
            self.assertEqual("eq." + str(original[key]), call.kwargs["params"][key])
        self.assertNotIn("access_token", call.kwargs["json"])
        self.assertEqual(b"renewed-token", Fernet(self.key).decrypt(call.kwargs["json"]["token_encrypted"].encode()))

    def test_update_lease_is_conditional_in_database_not_just_python(self):
        job_id = self.project_id
        self.session.request.side_effect = [response([{"id": job_id, "owner_id": 11, "project_id": self.project_id, "state": "queued", "data": {}, "lease_owner": "worker"}]), response([])]
        with self.assertRaises(StoreError):
            self.store.update_job(11, job_id, {"state": "published"}, lease_owner="wrong-worker")
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual("eq.wrong-worker", params["lease_owner"])
        self.assertEqual("eq.11", params["owner_id"])
        self.assertTrue(params["lease_until"].startswith("gt."))

    def test_supabase_update_compares_version_in_atomic_patch(self):
        version = "2026-10-26T12:00:00+00:00"
        self.session.request.side_effect = [response([{"id": self.project_id, "owner_id": 11, "project_id": self.project_id, "state": "ready", "data": {}, "updated_at": version}]), response([])]
        with self.assertRaises(StoreError):
            self.store.update_job(11, self.project_id, {"state": "cancelled"}, expected_updated_at=version)
        self.assertEqual("eq." + version, self.session.request.call_args.kwargs["params"]["updated_at"])

    def test_signing_checks_owner_before_request_and_uses_short_private_urls(self):
        path = f"11/{self.project_id}/{self.project_id}/post.png"
        asset = {"storage_path": path, "mime_type": "image/png"}
        with self.assertRaises(StoreError):
            self.store.resolve_assets(22, [asset])
        self.session.request.assert_not_called()
        self.session.request.return_value = response({"signedURL": "/object/sign/content-factory-media/" + path + "?token=temporary"})
        result = self.store.resolve_assets(11, [asset])[0]
        self.assertTrue(result["url"].startswith("https://project.supabase.co/storage/v1/object/sign/"))
        self.assertEqual({"expiresIn": 3600}, self.session.request.call_args.kwargs["json"])

    def test_provider_errors_do_not_include_tokens_or_raw_error_bodies(self):
        self.session.request.return_value = response({"error": "server-only-secret leaked detail"}, status=403)
        with self.assertRaises(StoreError) as result:
            self.store.list_projects(11)
        self.assertNotIn("server-only-secret", str(result.exception))
        self.session.request.side_effect = requests.ConnectionError("URL?token=hidden")
        with self.assertRaises(StoreError) as result:
            self.store.list_projects(11)
        self.assertNotIn("hidden", str(result.exception))


if __name__ == "__main__":
    unittest.main()
