"""Studio history and durable results must stay private and unpublished."""
import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet

from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore, StoreError
from content_factory_worker import Worker
from content_studio_catalog import list_manual_packages, save_studio_result, save_reference_result, set_favorite


class ManualCatalogTests(unittest.TestCase):
    def setUp(self):
        for target, value in (
            ("content_studio_catalog.is_agency_access_revoked", False),
            ("content_factory_ui._supabase_config", ("https://db.example.test", "server-private-key")),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, rows, status=200):
        result = Mock(status_code=status)
        result.json.return_value = rows
        return result

    @patch("content_studio_catalog.requests.get")
    @patch("content_studio_catalog.requests.post")
    def test_history_is_owner_filtered_and_checks_nested_owner(self, post, get):
        package = {"owner_telegram_id": 101, "package_id": "original", "items": [{"title": "Мой материал"}]}
        get.return_value = self.response([
            {"package": package}, {"package": {"owner_telegram_id": 202, "items": []}},
            {"package": {"owner_telegram_id": True}}, {"package": "broken"},
            {"package": {"owner_telegram_id": "101", "package_id": "also-mine"}},
        ])
        rows = list_manual_packages(101, limit=1000, offset=25)
        self.assertEqual(rows, [package, {"owner_telegram_id": "101", "package_id": "also-mine"}])
        call = get.call_args
        self.assertEqual(call.kwargs["params"], {
            "owner_telegram_id": "eq.101", "select": "package",
            "order": "updated_at.desc,package_id.desc", "limit": 100, "offset": 25,
        })
        self.assertFalse(call.kwargs["allow_redirects"])
        get.return_value.close.assert_called_once()
        post.assert_not_called()

    @patch("content_studio_catalog.requests.get")
    def test_unavailable_or_malformed_history_does_not_leak_secrets(self, get):
        for reply in (self.response([], status=404), self.response([], status=302),
                      self.response({"error": "server-private-key"}),
                      requests.Timeout("server-private-key signed-url")):
            with self.subTest(reply=type(reply).__name__):
                get.side_effect = reply if isinstance(reply, Exception) else None
                get.return_value = reply
                self.assertEqual(list_manual_packages(101), [])

    @patch("content_studio_catalog.requests.get")
    @patch("content_factory_ui._supabase_config", return_value=("", ""))
    def test_missing_storage_is_empty_history_without_network(self, config, get):
        self.assertEqual(list_manual_packages(101), [])
        get.assert_not_called()

    @patch("content_studio_catalog.requests.get")
    def test_invalid_owner_or_pagination_never_reads(self, get):
        for owner in (True, -2, 1.5, "101 OR 1=1"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                list_manual_packages(owner)
        for kwargs in ({"limit": 0}, {"offset": -1}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                list_manual_packages(101, **kwargs)
        get.assert_not_called()


class StudioResultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "studio.sqlite3", Fernet.generate_key())
        self.project = self.store.save_project(101, {"name": "Мой проект", "automation_enabled": True,
                                                    "platforms": ["vk"], "formats": ["post"]})
        for target in ("content_studio_catalog.is_agency_access_revoked",
                       "content_factory_worker.is_agency_access_revoked",
                       "content_factory_core.is_agency_access_revoked"):
            patcher = patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, kind="text", prompt="Расскажи о продукте", **kwargs):
        return save_studio_result(self.store, 101, self.project["id"], kind, prompt,
                                  request_id=kwargs.pop("request_id", "request-12345"), **kwargs)

    def test_text_is_durable_and_never_claimed_for_generation_or_publication(self):
        job = self.save(text="Готовый самостоятельный текст")
        self.assertEqual((job["state"], job["platform"], job["format"]), ("studio", "studio", "text"))
        self.assertTrue(job["studio_complete"])
        self.assertEqual(job["content"]["prompt"], "Расскажи о продукте")
        reopened = SQLiteStore(self.store.path, self.store.fernet_key)
        self.assertEqual(reopened.get_job(101, job["id"])["content"]["text"], "Готовый самостоятельный текст")
        self.assertIsNone(reopened.get_job(202, job["id"]))
        self.assertIsNone(self.store.claim_job("background-worker", iso(utc_now())))
        generator, publisher = Mock(), Mock()
        # Do not plan scheduled work: select the explicit studio project with
        # automation paused while still proving the studio row stays inert.
        self.store.save_project(101, {**self.project, "automation_enabled": False})
        result = Worker(self.store, generator, publisher=publisher).run_once(owner_id=101, project_id=self.project["id"])
        self.assertEqual(result["processed"], 0)
        generator.generate.assert_not_called()
        publisher.assert_not_called()

    def test_same_request_returns_first_complete_result_without_rewriting(self):
        first = self.save(text="Первая версия")
        with patch.object(self.store, "update_job", wraps=self.store.update_job) as update:
            second = self.save(text="Эту версию не подменять")
        self.assertEqual(second, first)
        update.assert_not_called()
        self.assertEqual(len(self.store.list_jobs(101, self.project["id"])), 1)
        with self.assertRaisesRegex(ValueError, "другими условиями"):
            self.save(prompt="Совсем другая задача", text="Новый текст")

    def test_image_and_audio_use_private_owner_project_job_paths(self):
        for kind, mime, data in (("image", "image/png", b"stored-image-bytes"),
                                 ("audio", "audio/mpeg", b"stored-voice-bytes")):
            with self.subTest(kind=kind):
                job = self.save(kind, data=data, mime_type=mime, text="Подпись", request_id="request-" + kind)
                self.assertTrue(job["studio_complete"])
                asset = job["assets"][0]
                self.assertTrue(asset["storage_path"].startswith(f"101/{self.project['id']}/{job['id']}/"))
                self.assertEqual(self.store.read_asset(101, asset), data)
                with self.assertRaises(StoreError):
                    self.store.read_asset(202, asset)

    def test_upload_failure_preserves_safe_incomplete_draft_and_allows_same_request_retry(self):
        with patch.object(self.store, "put_asset", side_effect=RuntimeError("secret-api-key signed-url")):
            draft = self.save("audio", text="Речь диктора", data=b"voice", mime_type="audio/mpeg")
        self.assertFalse(draft["studio_complete"])
        self.assertEqual(draft["state"], "studio")
        self.assertEqual(draft["content"]["text"], "Речь диктора")
        self.assertNotIn("secret", draft["error"])
        self.assertIsNone(draft["lease_owner"])
        self.assertEqual(draft["assets"], [])
        result = self.save("audio", text="Речь диктора", data=b"voice", mime_type="audio/mpeg")
        self.assertEqual(result["id"], draft["id"])
        self.assertTrue(result["studio_complete"])
        self.assertEqual(result["error"], "")

    def test_concurrent_save_observes_active_writer_and_does_not_upload_twice(self):
        nested_results = []
        put_asset = self.store.put_asset

        def interleave(*args, **kwargs):
            nested_results.append(self.save("image", data=b"other-image", mime_type="image/png"))
            return put_asset(*args, **kwargs)

        with patch.object(self.store, "put_asset", side_effect=interleave) as put:
            result = self.save("image", data=b"first-image", mime_type="image/png")
        self.assertEqual(put.call_count, 1)
        self.assertFalse(nested_results[0]["studio_complete"])
        self.assertEqual(result["id"], nested_results[0]["id"])
        self.assertTrue(result["studio_complete"])
        self.assertEqual(self.store.read_asset(101, result["assets"][0]), b"first-image")

    def test_foreign_project_or_invalid_payload_never_saves(self):
        with self.assertRaises(ValueError):
            save_studio_result(self.store, 202, self.project["id"], "text", "Чужой проект", text="Текст")
        for kind, kwargs in (("text", {"text": ""}), ("image", {"data": b"image", "mime_type": "audio/mpeg"}),
                             ("audio", {"data": b"", "mime_type": "audio/mpeg"}), ("video", {})):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.save(kind, **kwargs)
        self.assertEqual(self.store.list_jobs(101, self.project["id"]), [])

    def test_reference_analysis_is_durable_private_idempotent_and_inert(self):
        from test_content_factory_reference_pipeline import PLAN
        report = {"version": 1, "reference_style": "cartoon", "reference_plan": copy.deepcopy(PLAN),
                  "source_summary": "Начало с вопроса, действие, развязка.", "transcript": "Речь образца",
                  "source": {"sha256": "a" * 64, "duration": 20.0, "filename": "source.mp4"}}
        first = save_reference_result(self.store, 101, self.project["id"], report, request_id="reference-12345")
        second = save_reference_result(self.store, 101, self.project["id"], report, request_id="reference-12345")
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "studio")
        self.assertEqual(first["studio_tool"], "reference")
        self.assertTrue(first["studio_complete"])
        self.assertEqual(first["assets"], [])
        self.assertEqual(self.store.get_job(101, first["id"])["content"]["reference_style"], "cartoon")
        self.assertIsNone(self.store.get_job(202, first["id"]))
        self.assertIsNone(self.store.claim_job("factory", iso(utc_now())))
        with self.assertRaises(ValueError):
            save_reference_result(self.store, 202, self.project["id"], report, request_id="reference-foreign")
        changed = copy.deepcopy(report)
        changed["reference_style"] = "live"
        with self.assertRaisesRegex(ValueError, "другими условиями"):
            save_reference_result(self.store, 101, self.project["id"], changed, request_id="reference-12345")

    def test_favorites_are_owner_scoped_and_preserve_media_and_state(self):
        job = self.save("image", data=b"owned-image", mime_type="image/png")
        favorite = set_favorite(self.store, 101, job["id"], True)
        self.assertTrue(favorite["favorite"])
        self.assertEqual(favorite["state"], job["state"])
        self.assertEqual(favorite["assets"], job["assets"])
        with self.assertRaises(ValueError):
            set_favorite(self.store, 202, job["id"], False)
        self.assertFalse(set_favorite(self.store, 101, job["id"], False)["favorite"])


if __name__ == "__main__":
    unittest.main()
