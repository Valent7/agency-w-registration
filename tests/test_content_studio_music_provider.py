import unittest
from unittest.mock import Mock, patch

import content_studio_music_provider as provider


class MusicProviderTests(unittest.TestCase):
    def test_missing_key_is_rejected_without_network(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(provider.requests, "post") as post:
            with self.assertRaises(provider.MusicProviderError):
                provider.generate_music("calm instrumental background music", api_key="")
            post.assert_not_called()

    def test_success_returns_mp3_bytes_and_hides_key_from_payload(self):
        response = Mock()
        response.status_code = 200
        response.content = b"x" * 2000
        response.headers = {"content-type": "audio/mpeg"}
        with patch.object(provider.requests, "post", return_value=response) as post:
            result = provider.generate_music(
                "calm instrumental background music",
                duration_ms=30000,
                instrumental=True,
                api_key="secret-key",
            )
        self.assertEqual(result, b"x" * 2000)
        args, kwargs = post.call_args
        self.assertEqual(args[0], provider.API_URL)
        self.assertEqual(kwargs["headers"]["xi-api-key"], "secret-key")
        self.assertNotIn("secret-key", str(kwargs["json"]))
        self.assertTrue(kwargs["json"]["force_instrumental"])
        self.assertEqual(kwargs["json"]["music_length_ms"], 30000)
        response.close.assert_called_once()

    def test_auth_and_rate_errors_are_user_safe(self):
        for status, expected in ((401, "повторного подключения"), (429, "Лимит")):
            with self.subTest(status=status):
                response = Mock()
                response.status_code = status
                response.content = b""
                response.headers = {"content-type": "application/json"}
                with patch.object(provider.requests, "post", return_value=response):
                    with self.assertRaises(provider.MusicProviderError) as error:
                        provider.generate_music(
                            "calm instrumental background music",
                            api_key="secret-key",
                        )
                self.assertIn(expected, str(error.exception))
                response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
