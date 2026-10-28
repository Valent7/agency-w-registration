"""Offline Runway API and safe output-download contracts."""
import base64
import io
import socket
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

from content_factory_live_video import (
    API_VERSION, BASE_URL, MAX_CLIP_BYTES, LiveVideoError, LiveVideoPending,
    LiveVideoUncertain, RunwayVideoClient,
)


def artwork():
    data = io.BytesIO()
    Image.new("RGB", (1024, 1536), "#c9a970").save(data, "PNG")
    return data.getvalue()


def response(payload=None, *, status=200, chunks=(), headers=None):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.headers = headers or {}
    result.iter_content.return_value = iter(chunks)
    return result


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class RunwayClientTests(unittest.TestCase):
    def client(self, replies=(), *, model="gen4.5", downloads=()):
        session, download = Mock(), Mock()
        session.post.side_effect = replies
        download.get.side_effect = downloads
        return RunwayVideoClient("private-provider-key", model=model, session=session,
                                 download_session=download), session, download

    def test_constructor_does_not_contact_provider_and_real_session_has_no_retries(self):
        client, session, download = self.client()
        session.get.assert_not_called()
        session.post.assert_not_called()
        download.get.assert_not_called()
        real = RunwayVideoClient("private-provider-key")
        self.assertEqual(real.session.get_adapter(BASE_URL).max_retries.total, 0)
        real.session.close()
        real.download_session.close()

    def test_single_creation_preserves_actual_motion_prompt_and_provider_contract(self):
        client, session, _ = self.client([response({"id": "scene-1"}, status=201)])
        task = client.create_scene(artwork(), "The person closes the laptop, stands up and walks to the door.", 10)
        self.assertEqual(task, "scene-1")
        session.post.assert_called_once()
        call = session.post.call_args
        self.assertEqual(call.args[0], BASE_URL + "/image_to_video")
        self.assertEqual(call.kwargs["headers"]["X-Runway-Version"], API_VERSION)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer private-provider-key")
        self.assertFalse(call.kwargs["allow_redirects"])
        payload = call.kwargs["json"]
        self.assertEqual((payload["model"], payload["ratio"], payload["duration"]), ("gen4.5", "720:1280", 10))
        self.assertIn("closes the laptop", payload["promptText"])
        encoded = payload["promptImage"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as source:
            self.assertEqual(source.mode, "RGB")
            self.assertLessEqual(source.width, 1280)
            self.assertLessEqual(source.height, 1536)
        self.assertLess(len(payload["promptImage"]), 5 * 1024 * 1024)

    def test_invalid_local_inputs_never_start_paid_creation(self):
        invalid = [(b"not-image", "Move naturally", 5), (artwork(), "", 5),
                   (artwork(), "a" * 1001, 5), (artwork(), "😀" * 501, 5),
                   (artwork(), "Move", True), (artwork(), "Move", 1), (artwork(), "Move", 11)]
        for image, prompt, duration in invalid:
            with self.subTest(duration=duration, prompt_length=len(prompt)):
                client, session, _ = self.client()
                with self.assertRaises(LiveVideoError):
                    client.create_scene(image, prompt, duration)
                session.post.assert_not_called()

    def test_utf16_prompt_limit_and_legacy_turbo_duration(self):
        client, session, _ = self.client([response({"id": "ok"})])
        self.assertEqual(client.create_scene(artwork(), "😀" * 500, 2), "ok")
        client, session, _ = self.client(model="gen4_turbo")
        with self.assertRaises(LiveVideoError):
            client.create_scene(artwork(), "A person walks away", 8)
        session.post.assert_not_called()

    def test_missing_key_or_unknown_model_never_calls_provider(self):
        for key, model in (("", "gen4.5"), ("secret\nheader", "gen4.5"), ("secret-key", "unknown")):
            with self.subTest(key_present=bool(key), model=model), self.assertRaises(LiveVideoError):
                RunwayVideoClient(key, model=model, session=Mock())

    def test_uncertain_creation_is_not_retried_and_hides_raw_provider_details(self):
        malformed = response()
        malformed.json.side_effect = ValueError("private-provider-key signed-token")
        replies = [requests.Timeout("private-provider-key signed-token"), response(status=503),
                   response(status=307), response({"unrelated": "private-provider-key"}), malformed,
                   response({"id": "../../private-provider-key"})]
        for reply in replies:
            with self.subTest(reply_type=type(reply).__name__):
                client, session, _ = self.client([reply])
                with self.assertRaises(LiveVideoUncertain) as caught:
                    client.create_scene(artwork(), "The person walks into the garden", 8)
                self.assertTrue(caught.exception.creation_uncertain)
                self.assertNotIn("private-provider-key", str(caught.exception))
                self.assertNotIn("signed-token", str(caught.exception))
                self.assertEqual(session.post.call_count, 1)

    def test_known_rejection_is_not_an_uncertain_or_retried_order(self):
        for status in (400, 401, 402, 403, 422, 429):
            client, session, _ = self.client([response({"error": "private-provider-key"}, status=status)])
            with self.subTest(status=status), self.assertRaises(LiveVideoError) as caught:
                client.create_scene(artwork(), "The person takes a cup", 5)
            self.assertNotIsInstance(caught.exception, LiveVideoUncertain)
            self.assertNotIn("private-provider-key", str(caught.exception))
            self.assertEqual(session.post.call_count, 1)

    def test_preflight_reads_available_credits_and_does_not_create_video(self):
        client, session, _ = self.client()
        session.get.return_value = response({"creditBalance": 125.5, "private": "private-provider-key"})
        self.assertEqual(client.preflight(), {"key_valid": True, "credit_balance": 125.5, "supported": True})
        self.assertEqual(session.get.call_args.args[0], BASE_URL + "/organization")
        session.post.assert_not_called()

    def test_estimate_uses_observed_gen45_cost_without_requests_or_unknown_tariff(self):
        client, session, _ = self.client()
        self.assertEqual(client.estimate_credits([10] * 4), 480)
        self.assertEqual(client.estimate_credits([9] * 4), 432)
        self.assertEqual(client.estimate_credits([2, 3, 4]), 108)
        session.get.assert_not_called()
        session.post.assert_not_called()
        turbo, session, _ = self.client(model="gen4_turbo")
        self.assertIsNone(turbo.estimate_credits([5, 10]))
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_invalid_credit_plans_never_contact_provider(self):
        client, session, _ = self.client()
        for seconds in (None, [], [True], [1], [11], [5] * 7, [5.0], "10"):
            with self.subTest(seconds=seconds), self.assertRaises(LiveVideoError):
                client.estimate_credits(seconds)
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_preflight_unknown_endpoint_is_not_reported_as_invalid_key(self):
        for status in (404, 405):
            client, session, _ = self.client()
            session.get.return_value = response(status=status)
            self.assertEqual(client.preflight(), {"key_valid": None, "credit_balance": None, "supported": False})
            session.post.assert_not_called()

    def test_preflight_rejects_invalid_access_without_exposing_error_body(self):
        client, session, _ = self.client()
        session.get.return_value = response({"error": "private-provider-key"}, status=401)
        with self.assertRaises(LiveVideoError) as caught:
            client.preflight()
        self.assertNotIn("private-provider-key", str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_task_status_is_read_only_and_maps_terminal_states(self):
        client, session, _ = self.client()
        for state in ("PENDING", "THROTTLED", "RUNNING"):
            session.get.return_value = response({"status": state})
            self.assertEqual(client.task_status("scene-1"), {"status": "pending"})
        for state in ("FAILED", "CANCELLED"):
            session.get.return_value = response({"status": state, "failure": "private-provider-key"})
            result = client.task_status("scene-1")
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("private-provider-key", result["error"])
        session.post.assert_not_called()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=PUBLIC_DNS)
    def test_completed_task_returns_a_validated_output_url(self, _dns):
        client, session, _ = self.client()
        session.get.return_value = response({"status": "SUCCEEDED", "output": ["https://cdn.example.com/scene.mp4?signature=private-token"]})
        result = client.task_status("scene-1")
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("signature=private-token", result["url"])
        self.assertEqual(session.get.call_count, 1)
        session.post.assert_not_called()

    def test_task_status_temporary_error_is_read_retryable_without_recreation(self):
        client, session, _ = self.client()
        session.get.side_effect = requests.Timeout("private-provider-key")
        with self.assertRaises(LiveVideoError) as caught:
            client.task_status("scene-1")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-provider-key", str(caught.exception))
        session.post.assert_not_called()

    def test_new_task_not_visible_yet_is_read_retryable_without_new_order(self):
        client, session, _ = self.client()
        session.get.return_value = response(status=404)
        with self.assertRaises(LiveVideoError) as caught:
            client.task_status("scene-1")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "video_task_not_visible")
        session.post.assert_not_called()

    def test_invalid_task_id_does_not_make_requests(self):
        client, session, _ = self.client()
        with self.assertRaises(LiveVideoError):
            client.task_status("../unsafe")
        session.get.assert_not_called()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=PUBLIC_DNS)
    def test_download_streams_on_a_separate_session_and_never_forwards_bearer(self, _dns):
        first = response(status=302, headers={"Location": "https://second.example.com/file.mp4?signed=token"})
        final = response(chunks=[b"mp4", b"-data"])
        client, session, download = self.client(downloads=[first, final])
        self.assertEqual(client.download("https://cdn.example.com/scene.mp4?signature=private-token"), b"mp4-data")
        session.get.assert_not_called()
        self.assertFalse(download.trust_env)
        self.assertIsNone(download.auth)
        for call in download.get.call_args_list:
            self.assertIsNone(call.kwargs["headers"]["Authorization"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
        first.close.assert_called_once()
        final.close.assert_called_once()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=PUBLIC_DNS)
    def test_redirect_to_private_address_is_rejected_before_fetch(self, _dns):
        first = response(status=302, headers={"Location": "https://127.0.0.1/private?token=private-token"})
        client, _, download = self.client(downloads=[first])
        with self.assertRaises(LiveVideoError) as caught:
            client.download("https://cdn.example.com/source.mp4")
        self.assertEqual(download.get.call_count, 1)
        self.assertNotIn("private-token", str(caught.exception))

    def test_unsafe_initial_download_urls_make_no_requests(self):
        urls = ["http://cdn.example.com/a.mp4", "https://user:private-token@cdn.example.com/a.mp4",
                "https://localhost/a", "https://127.0.0.1/a", "https://[::1]/a", "https://10.0.0.1/a",
                "https://169.254.169.254/metadata", "https://cdn.example.com:8443/a", "https://cdn.example.com/a#fragment"]
        for url in urls:
            client, _, download = self.client()
            with self.subTest(url=url), self.assertRaises(LiveVideoError):
                client.download(url)
            download.get.assert_not_called()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))])
    def test_public_looking_hostname_resolving_to_private_ip_is_rejected(self, _dns):
        client, _, download = self.client()
        with self.assertRaises(LiveVideoError):
            client.download("https://cdn.example.com/scene.mp4")
        download.get.assert_not_called()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=PUBLIC_DNS)
    def test_download_size_limits_apply_to_headers_and_actual_stream(self, _dns):
        for reply in (response(headers={"Content-Length": str(MAX_CLIP_BYTES + 1)}),
                      response(chunks=[b"x" * (MAX_CLIP_BYTES + 1)])):
            client, session, download = self.client(downloads=[reply])
            with self.assertRaises(LiveVideoError) as caught:
                client.download("https://cdn.example.com/a.mp4")
            self.assertEqual(caught.exception.code, "video_too_large")
            self.assertEqual(download.get.call_count, 1)
            session.post.assert_not_called()

    @patch("content_factory_live_video.socket.getaddrinfo", return_value=PUBLIC_DNS)
    def test_download_failure_does_not_expose_signed_url_or_recreate_task(self, _dns):
        client, session, download = self.client(downloads=[requests.Timeout("https://cdn.example.com/file?signature=private-token")])
        with self.assertRaises(LiveVideoError) as caught:
            client.download("https://cdn.example.com/file?signature=private-token")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-token", str(caught.exception))
        session.post.assert_not_called()

    def test_pending_exception_has_a_bounded_retry_delay(self):
        self.assertEqual(LiveVideoPending(retry_after=1).retry_after, 15)
        self.assertEqual(LiveVideoPending(retry_after=9000).retry_after, 3600)


if __name__ == "__main__":
    unittest.main()
