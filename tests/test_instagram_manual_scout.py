"""No-network regression tests for Instagram manual scouting."""
import unittest
from pathlib import Path

from instagram_manual_scout import (
    google_profile_search_url,
    instagram_post_url,
    instagram_profile_url,
    instagram_username,
    suggested_comment_prompt,
)


class InstagramManualScoutTests(unittest.TestCase):
    def test_profile_handle_and_link(self):
        self.assertEqual(instagram_username("@creative.person"), "creative.person")
        self.assertEqual(
            instagram_username("https://www.instagram.com/creative.person/?utm_source=share"),
            "creative.person",
        )
        self.assertEqual(
            instagram_profile_url("creative.person"),
            "https://www.instagram.com/creative.person/",
        )

    def test_reject_fake_or_non_profile_urls(self):
        for raw in (
            "https://instagram.com.evil.example/test",
            "http://www.instagram.com/test",
            "https://www.instagram.com/p/ABC123xyz/",
            "https://www.instagram.com/direct/inbox/",
            "https://www.instagram.com/@fake/",
            "https://www.instagram.com/explore/",
            "@a..b",
            "@.wrong",
            "javascript:alert(1)",
            "",
        ):
            self.assertEqual(instagram_username(raw), "", raw)

    def test_post_urls_are_strict(self):
        self.assertEqual(
            instagram_post_url("https://instagram.com/reel/ABCD012345/?igsh=abc"),
            "https://www.instagram.com/reel/ABCD012345/",
        )
        self.assertEqual(
            instagram_post_url("https://www.instagram.com/p/CAROUSEL123/"),
            "https://www.instagram.com/p/CAROUSEL123/",
        )
        for raw in (
            "https://instagram.com.evil.test/reel/ABCD012345",
            "https://www.instagram.com/random/",
            "https://www.instagram.com/reel/../../secret",
            "http://instagram.com/p/ABCD1234/",
        ):
            self.assertEqual(instagram_post_url(raw), "")

    def test_browser_search_not_instagram_scraping(self):
        url = google_profile_search_url("русскоязычные предприниматели")
        self.assertIn("https://www.google.com/search?", url)
        self.assertIn("site%3Ainstagram.com", url)
        self.assertNotIn("access_token", url)

    def test_pilot_is_root_owner_only_and_precedes_meta_connection(self):
        source = Path("instagram_radar_ui.py").read_text(encoding="utf-8")
        render = source[source.index("def render_instagram_radar("):]
        self.assertIn("if _is_agency_owner(int(owner_telegram_id)):", render)
        self.assertIn("_render_instagram_manual_scout(int(owner_telegram_id), ask_openai_fn)", render)
        self.assertLess(render.index("_render_instagram_manual_scout("),
                        render.index("_load_radar_connection("))
        self.assertIn("from neola_partner_center import _is_agency_owner", source)

    def test_acquaintance_flow_has_no_broken_google_button(self):
        source = Path("instagram_radar_ui.py").read_text(encoding="utf-8")
        start = source.index("def _render_instagram_manual_scout(")
        end = source.index("def render_instagram_radar(")
        ui = source[start:end]
        self.assertIn("Мои знакомые в Instagram", ui)
        self.assertIn("Найти знакомого по username", ui)
        self.assertIn("instagram_profile_url(active)", ui)
        self.assertIn('"Reels", "Пост", "Карусель"', ui)
        self.assertIn("instagram_post_url(raw_post)", ui)
        self.assertIn("suggested_comment_prompt(active, observation)", ui)
        self.assertIn("use_web_search=False", ui)
        self.assertNotIn("Искать профили в браузере самостоятельно", ui)
        self.assertNotIn("_discover_usernames(", ui)
        self.assertNotIn("google_profile_search_url(", ui)
        self.assertIn("Я открыла Instagram и убедилась", ui)
        self.assertIn("Отправка только вручную", ui)

    def test_grounded_comment_requires_observation(self):
        with self.assertRaises(ValueError):
            suggested_comment_prompt("@valid", "маловато")
        system, message = suggested_comment_prompt(
            "@valid", "Автор рассказал о важности свободного времени."
        )
        self.assertIn("Без выдуманных", system)
        self.assertIn("Автор рассказал", message)


if __name__ == "__main__":
    unittest.main()
