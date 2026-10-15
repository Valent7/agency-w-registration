"""Offline checks for partner-friendly Instagram onboarding and Meta approval UX."""
import ast
import html
import unittest
from pathlib import Path
from urllib.parse import urlencode

SOURCE = Path("instagram_webhook.py").read_text(encoding="utf-8")
UI = Path("streamlit_app.py").read_text(encoding="utf-8")
DOC = Path("docs/instagram_partner_launch.md").read_text(encoding="utf-8")


class PartnerOnboardingTests(unittest.TestCase):
    def test_every_non_success_page_offers_a_safe_return(self):
        callback = SOURCE[
            SOURCE.index("async def instagram_oauth_callback("):
            SOURCE.index("def _facebook_oauth_settings(")
        ]
        self.assertIn('_instagram_return_link("access_pending")', callback)
        self.assertIn('_instagram_return_link("cancelled")', callback)
        self.assertIn('_instagram_return_link("error")', callback)
        self.assertIn("html_escape(error_description or error)", callback)
        self.assertIn("except InstagramAccessNotApproved:", callback)

    def test_return_url_encodes_status_and_escapes_markup(self):
        tree = ast.parse(SOURCE)
        fn = next(
            x for x in tree.body
            if isinstance(x, ast.FunctionDef) and x.name == "_instagram_return_link"
        )
        ns = {
            "INSTAGRAM_AGENCY_RETURN_URL": "https://agency-w.streamlit.app/?ref=a&other=<script>",
            "html_escape": html.escape,
            "urlencode": urlencode,
        }
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "instagram_webhook.py", "exec"), ns)
        result = ns["_instagram_return_link"]("access_pending")
        self.assertIn("instagram=access_pending", result)
        self.assertIn("&amp;", result)
        self.assertNotIn("<script>", result)
        self.assertIn("Вернуться в Агентство W", result)

    def test_client_shows_pending_without_claiming_connection(self):
        start = UI.index("def render_instagram_connection(")
        end = UI.index("def _load_instagram_comments(", start)
        ui = UI[start:end]
        self.assertIn('if instagram_result == "access_pending":', ui)
        self.assertIn("рабочий токен для Неоны не выдан", ui)
        self.assertIn('if connection:', ui)
        self.assertIn("Регистрация в Facebook", ui) if "Регистрация в Facebook" in ui else self.assertIn("Facebook", ui)

    def test_launch_checklist_does_not_promise_access(self):
        for requirement in (
            "App Review",
            "Tech Provider",
            "без официальной регистрации бизнеса",
            "Instagram Login",
            "native",
            "не помечается успешным",
        ):
            self.assertIn(requirement, DOC)


if __name__ == "__main__":
    unittest.main()
