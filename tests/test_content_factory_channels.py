"""Offline protocol tests; no real credentials, accounts or publications."""
from copy import deepcopy
from pathlib import Path
import tempfile
import traceback
import unittest

import requests

from content_factory_channels import PublishError, PublishPending, PublishUncertain, publish


class Response:
    def __init__(self, data=None, status=200, *, headers=None, chunks=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks or []
        self.closed = False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return deepcopy(self.data)

    def iter_content(self, _):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *steps, before=None):
        self.steps = list(steps)
        self.calls = []
        self.before = before

    def _call(self, method, url, **kwargs):
        if self.before:
            self.before(method, url, kwargs)
        self.calls.append((method, url, kwargs))
        if not self.steps:
            raise AssertionError("Unexpected network request")
        expected_method, suffix, response = self.steps.pop(0)
        if method != expected_method or not url.endswith(suffix):
            raise AssertionError(f"Unexpected protocol step: {method} {urlsplit_safe(url)}")
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)


def urlsplit_safe(url):
    return url.split("?", 1)[0]


def step(method, suffix, data=None, status=200):
    return method, suffix, Response(data, status)


IMAGE = {"url": "https://media.example.com/assets/one.jpg?signature=first", "mime_type": "image/jpeg"}
VIDEO = {"url": "https://media.example.com/assets/one.mp4", "mime_type": "video/mp4"}
CONNECTION = {"account_id": "123", "access_token": "unit-test-token", "extra": {}}


class ChannelTests(unittest.TestCase):
    def test_http408_after_public_write_keeps_no_resend_barrier(self):
        transport = Transport(
            step("post", "/123/media", {"id": "11"}),
            step("get", "/11", {"status_code": "FINISHED"}),
            step("post", "/123/media_publish", {}, status=408),
        )
        with self.assertRaises(PublishUncertain):
            self.send(transport=transport)
        self.assertTrue(self.checkpoint["publish_started"])
        with self.assertRaises(PublishUncertain):
            self.send(transport=Transport())

    def test_refresh_metadata_does_not_change_publication_identity(self):
        expected = self.send(transport=Transport(*self.instagram_steps()))
        refreshed = {**CONNECTION, "extra": {"token_expires_at": "2030-01-01T00:00:00+00:00", "scopes": ["instagram_business_content_publish"]}}
        self.assertEqual(self.send(connection=refreshed, transport=Transport()), expected)

    def setUp(self):
        self.checkpoint = {}
        self.history = []

    def persist(self, value):
        self.checkpoint = deepcopy(value)
        self.history.append(deepcopy(value))

    def send(self, platform="instagram", *, content=None, assets=None, connection=None, transport=None):
        return publish(platform, connection or CONNECTION,
                       content or {"format": "post", "text": "Содержательный пост"},
                       [IMAGE] if assets is None else assets, self.checkpoint, self.persist,
                       session=transport or Transport())

    def instagram_steps(self):
        return (
            step("post", "/123/media", {"id": "11"}),
            step("get", "/11", {"status_code": "FINISHED"}),
            step("post", "/123/media_publish", {"id": "21"}),
            step("get", "/21", {"permalink": "https://www.instagram.com/p/example/"}),
        )

    def test_instagram_image_checkpoints_before_public_write_and_returns_permalink(self):
        def before(method, url, _):
            if url.endswith("/media_publish"):
                self.assertTrue(self.checkpoint["publish_started"])
                self.assertEqual(self.checkpoint["container_id"], "11")
        transport = Transport(*self.instagram_steps(), before=before)
        result = self.send(transport=transport)
        self.assertEqual(result, {"external_id": "21", "permalink": "https://www.instagram.com/p/example/"})
        self.assertEqual(self.checkpoint["result"], result)
        self.assertNotIn("unit-test-token", str(self.checkpoint))
        self.assertEqual(transport.calls[0][2]["data"]["image_url"], IMAGE["url"])

    def test_completed_result_is_idempotent_without_any_network(self):
        first = self.send(transport=Transport(*self.instagram_steps()))
        self.assertEqual(self.send(transport=Transport()), first)

    def test_pending_container_resumes_with_fresh_signed_url_without_recreate(self):
        first = Transport(step("post", "/123/media", {"id": "11"}),
                          step("get", "/11", {"status_code": "IN_PROGRESS"}))
        with self.assertRaises(PublishPending) as raised:
            self.send(content={"format": "reel", "text": "Ролик"}, assets=[VIDEO], transport=first)
        self.assertEqual(raised.exception.retry_after, 60)
        resumed = Transport(step("get", "/11", {"status_code": "FINISHED"}),
                            step("post", "/123/media_publish", {"id": "21"}),
                            step("get", "/21", {}))
        result = self.send(content={"format": "reel", "text": "Ролик"},
                           assets=[{**VIDEO, "url": VIDEO["url"] + "?signature=fresh"}], transport=resumed)
        self.assertEqual(result["external_id"], "21")
        self.assertEqual(first.calls[0][2]["data"]["media_type"], "REELS")
        self.assertFalse(resumed.steps)

    def test_carousel_persists_children_and_waits_for_all_media(self):
        assets = [IMAGE, {**IMAGE, "url": "https://media.example.com/assets/two.jpg"}]
        initial = Transport(step("post", "/123/media", {"id": "11"}),
                            step("get", "/11", {"status_code": "FINISHED"}),
                            step("post", "/123/media", {"id": "12"}),
                            step("get", "/12", {"status_code": "IN_PROGRESS"}))
        with self.assertRaises(PublishPending):
            self.send(content={"format": "carousel", "text": "Карусель"}, assets=assets, transport=initial)
        self.assertEqual(self.checkpoint["children"], ["11", "12"])
        resumed = Transport(step("get", "/11", {"status_code": "FINISHED"}),
                            step("get", "/12", {"status_code": "FINISHED"}),
                            step("post", "/123/media", {"id": "13"}),
                            step("get", "/13", {"status_code": "FINISHED"}),
                            step("post", "/123/media_publish", {"id": "21"}),
                            step("get", "/21", {}))
        self.send(content={"format": "carousel", "text": "Карусель"}, assets=assets, transport=resumed)
        payload = resumed.calls[2][2]["data"]
        self.assertEqual(payload["media_type"], "CAROUSEL")
        self.assertEqual(payload["children"], "11,12")

    def test_threads_text_publishes_without_media(self):
        transport = Transport(step("post", "/123/threads", {"id": "11"}),
                              step("get", "/11", {"status": "FINISHED"}),
                              step("post", "/123/threads_publish", {"id": "21"}),
                              step("get", "/21", {"permalink": "https://www.threads.net/@owner/post/test"}))
        result = self.send("threads", assets=[], transport=transport)
        self.assertEqual(transport.calls[0][2]["data"]["media_type"], "TEXT")
        self.assertIn("threads.net", result["permalink"])

    def test_threads_carousel_uses_child_and_parent_protocol(self):
        transport = Transport(step("post", "/123/threads", {"id": "11"}),
                              step("get", "/11", {"status": "FINISHED"}),
                              step("post", "/123/threads", {"id": "12"}),
                              step("get", "/12", {"status": "FINISHED"}),
                              step("post", "/123/threads", {"id": "13"}),
                              step("get", "/13", {"status": "FINISHED"}),
                              step("post", "/123/threads_publish", {"id": "21"}),
                              step("get", "/21", {}))
        self.send("threads", content={"format": "carousel", "text": "Slides"},
                  assets=[IMAGE, {**IMAGE, "url": "https://media.example.com/two.jpg"}], transport=transport)
        self.assertEqual(transport.calls[0][2]["data"]["is_carousel_item"], "true")
        self.assertEqual(transport.calls[4][2]["data"]["children"], "11,12")

    def test_timeout_after_public_write_requires_reconciliation_and_never_resends(self):
        transport = Transport(*self.instagram_steps()[:2],
                              ("post", "/123/media_publish", requests.Timeout("token=must-never-be-shown")))
        with self.assertRaises(PublishUncertain) as raised:
            self.send(transport=transport)
        self.assertTrue(self.checkpoint["publish_started"])
        self.assertNotIn("must-never-be-shown", "".join(traceback.format_exception(raised.exception)))
        with self.assertRaises(PublishUncertain):
            self.send(transport=Transport())

    def test_server_error_after_write_is_uncertain(self):
        transport = Transport(*self.instagram_steps()[:2], step("post", "/123/media_publish", {}, 503))
        with self.assertRaises(PublishUncertain):
            self.send(transport=transport)
        self.assertTrue(self.checkpoint["publish_started"])

    def test_malformed_success_after_write_is_uncertain(self):
        transport = Transport(*self.instagram_steps()[:2], step("post", "/123/media_publish", {}))
        with self.assertRaises(PublishUncertain):
            self.send(transport=transport)
        self.assertTrue(self.checkpoint["publish_started"])

    def test_permalink_failure_does_not_republish_successful_content(self):
        transport = Transport(*self.instagram_steps()[:3], step("get", "/21", {}, 503))
        result = self.send(transport=transport)
        self.assertEqual(result, {"external_id": "21", "permalink": ""})
        self.assertEqual(self.send(transport=Transport()), result)

    def test_explicit_rate_limit_is_retryable_and_releases_barrier(self):
        transport = Transport(*self.instagram_steps()[:2], step("post", "/123/media_publish", {}, 429))
        with self.assertRaises(PublishError) as raised:
            self.send(transport=transport)
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(self.checkpoint["publish_started"])

    def test_explicit_rejection_clears_write_barrier_but_hides_provider_error(self):
        transport = Transport(*self.instagram_steps()[:2],
                              step("post", "/123/media_publish", {"error": {"message": "private-token", "code": 190}}))
        with self.assertRaises(PublishError) as raised:
            self.send(transport=transport)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("private-token", str(raised.exception))
        self.assertFalse(self.checkpoint["publish_started"])

    def test_failure_to_persist_barrier_stops_public_write(self):
        def failing_persist(value):
            if value.get("publish_started"):
                raise RuntimeError("database down")
            self.persist(value)
        transport = Transport(*self.instagram_steps()[:2])
        with self.assertRaises(PublishError):
            publish("instagram", CONNECTION, {"format": "post", "text": "text"}, [IMAGE], {}, failing_persist, session=transport)
        self.assertEqual(len(transport.calls), 2)

    def test_failure_to_persist_success_is_uncertain_with_barrier_retained(self):
        def failing_persist(value):
            if value.get("result"):
                raise RuntimeError("database down")
            self.persist(value)
        transport = Transport(*self.instagram_steps()[:3])
        with self.assertRaises(PublishUncertain):
            publish("instagram", CONNECTION, {"format": "post", "text": "text"}, [IMAGE], {}, failing_persist, session=transport)
        self.assertTrue(self.checkpoint["publish_started"])

    def test_bad_media_format_and_urls_rejected_before_network(self):
        bad_urls = ["http://media.example.com/a.jpg", "https://127.0.0.1/a.jpg", "https://10.0.0.1/a.jpg",
                    "https://169.254.169.254/a.jpg", "https://localhost/a.jpg", "https://host.local/a.jpg",
                    "https://127.1/a.jpg", "https://2130706433/a.jpg", "https://[::1]/a.jpg",
                    "https://user:pass@media.example.com/a.jpg"]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(PublishError):
                self.send(assets=[{**IMAGE, "url": url}], transport=Transport())
        for content, assets in (({"format": "unknown", "text": "x"}, []),
                                ({"format": "post", "text": "x"}, [{**IMAGE, "mime_type": "image/png"}]),
                                ({"format": "reel", "text": "x"}, [IMAGE]),
                                ({"format": "carousel", "text": "x"}, [IMAGE]),
                                ({"format": "post", "text": "x"}, [])):
            with self.subTest(content=content), self.assertRaises(PublishError):
                self.send(content=content, assets=assets)

    def test_platform_text_limits(self):
        for platform, limit in (("instagram", 2200), ("threads", 500), ("vk", 16384)):
            with self.subTest(platform=platform), self.assertRaises(PublishError):
                self.send(platform, content={"format": "post", "text": "x" * (limit + 1)})

    def test_checkpoint_cannot_be_reused_for_different_content_or_account(self):
        self.send(transport=Transport(*self.instagram_steps()))
        with self.assertRaises(PublishError):
            self.send(content={"format": "post", "text": "Other version"})
        with self.assertRaises(PublishError):
            self.send(connection={**CONNECTION, "account_id": "999"})

    def test_vk_text_wall_post_and_result_are_idempotent(self):
        transport = Transport(step("post", "/wall.post", {"response": {"post_id": 77}}))
        connection = {**CONNECTION, "extra": {"group_id": 123}}
        result = self.send("vk", assets=[], connection=connection, transport=transport)
        self.assertEqual(result, {"external_id": "-123_77", "permalink": "https://vk.com/wall-123_77"})
        self.assertEqual(transport.calls[0][2]["data"]["owner_id"], -123)
        self.assertEqual(transport.calls[0][2]["data"]["from_group"], 1)
        self.assertEqual(self.send("vk", assets=[], connection=connection), result)

    def test_vk_photo_upload_preserves_destination_and_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(b"sample-jpeg")
            transport = Transport(
                step("post", "/photos.getWallUploadServer", {"response": {"upload_url": "https://upload.vk.com/photo"}}),
                step("post", "/photo", {"photo": "opaque-photo", "server": 1, "hash": "opaque-hash"}),
                step("post", "/photos.saveWallPhoto", {"response": [{"owner_id": -123, "id": 8}]}),
                step("post", "/wall.post", {"response": {"post_id": 77}}),
            )
            self.send("vk", assets=[{**IMAGE, "path": str(path)}],
                      connection={**CONNECTION, "extra": {"group_id": 123}}, transport=transport)
            self.assertEqual(transport.calls[0][2]["data"]["group_id"], 123)
            self.assertIn("photo", transport.calls[1][2]["files"])
            self.assertEqual(transport.calls[-1][2]["data"]["attachments"], "photo-123_8")
            self.assertEqual(self.checkpoint["vk_attachments"], ["photo-123_8"])

    def test_vk_video_upload_posts_wall_video_without_claiming_native_clips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mp4"
            path.write_bytes(b"sample-mp4")
            transport = Transport(
                step("post", "/video.save", {"response": {"upload_url": "https://upload.vk.com/video", "owner_id": -123, "video_id": 8}}),
                step("post", "/video", {"owner_id": -123, "video_id": 8}),
                step("post", "/wall.post", {"response": {"post_id": 77}}),
            )
            result = self.send("vk", content={"format": "reel", "text": "Видео", "title": "Название"},
                               assets=[{**VIDEO, "path": str(path)}],
                               connection={**CONNECTION, "extra": {"group_id": 123}}, transport=transport)
            self.assertIn("video_file", transport.calls[1][2]["files"])
            self.assertEqual(transport.calls[-1][2]["data"]["attachments"], "video-123_8")
            self.assertIn("/wall-123_", result["permalink"])

    def test_vk_timeout_keeps_uploaded_media_and_never_reposts(self):
        transport = Transport(("post", "/wall.post", requests.Timeout("timeout")))
        with self.assertRaises(PublishUncertain):
            self.send("vk", assets=[], transport=transport)
        with self.assertRaises(PublishUncertain):
            self.send("vk", assets=[], transport=Transport())

    def test_vk_remote_media_redirect_to_private_url_is_blocked(self):
        redirect = Response(status=302, headers={"Location": "https://127.0.0.1/secret"})
        transport = Transport(
            step("post", "/photos.getWallUploadServer", {"response": {"upload_url": "https://upload.vk.com/photo"}}),
            ("get", IMAGE["url"], redirect),
        )
        with self.assertRaises(PublishError):
            self.send("vk", transport=transport)
        self.assertFalse(self.checkpoint.get("publish_started", False))


if __name__ == "__main__":
    unittest.main()
