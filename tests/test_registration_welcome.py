"""Offline regression guards for the invited partner's welcome screen.

Never logs in, sends Telegram messages or reaches the network.
"""
from pathlib import Path
import re
import unittest


SOURCE = Path("streamlit_app.py").read_text(encoding="utf-8")


class RegistrationWelcomeTests(unittest.TestCase):
    def test_branded_welcome_is_only_for_unsigned_users(self):
        entry = SOURCE.index("if not _agency_user_logged_in:")
        next_auth = SOURCE.index("# Защищённый вход через Telegram", entry)
        login_entry = SOURCE[entry:next_auth]
        self.assertIn("render_agency_w_logo()", login_entry)
        self.assertIn('<section class="aw-welcome"', login_entry)
        self.assertIn('aria-label="Знакомство с Агентством W"', login_entry)
        self.assertIn("Ваше время — для жизни", login_entry)
        self.assertIn("Неония", login_entry)
        self.assertIn("Неона", login_entry)
        self.assertIn("Стагирит", login_entry)
        self.assertIn("Неола", login_entry)
        self.assertNotIn('<div class="registration-card">', login_entry)
        self.assertEqual(SOURCE.count('<section class="aw-welcome"'), 1)

    def test_existing_telegram_sign_in_not_replaced(self):
        self.assertIn("on_click=st.login", SOURCE)
        self.assertIn("st.user.is_logged_in", SOURCE)
        self.assertIn("referral_code = st.query_params.get(\"ref\", \"\").strip()", SOURCE)
        self.assertIn("social_registration_token = st.query_params.get(\"awt\", \"\").strip()", SOURCE)
        self.assertIn("agency_w_pending_ref", SOURCE)
        self.assertIn("member_code, created = save_member_to_supabase(telegram_data, referral_code)", SOURCE)
        self.assertIn("_render_telegram_oidc_login(referral_code)", SOURCE)
        self.assertIn("partner_link = f\"https://agency-w.streamlit.app/?ref={member_code}\"", SOURCE)

    def test_mobile_firm_screen_and_existing_cta(self):
        self.assertIn("@media (max-width: 620px)", SOURCE)
        self.assertIn("grid-template-columns: 1fr", SOURCE)
        self.assertIn("Войдите через Telegram ниже", SOURCE)
        self.assertIn("Приглашение партнёра принято: {referral_code}", SOURCE)
        self.assertNotIn("window.location.assign('https://t.me/", SOURCE)


if __name__ == "__main__":
    unittest.main()
