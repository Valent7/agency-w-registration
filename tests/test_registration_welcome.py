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
        self.assertNotIn('render_agency_w_logo(compact=True)', login_entry)
        logo_start = SOURCE.index('def render_agency_w_logo(')
        logo_end = SOURCE.index('def get_session_cipher(', logo_start)
        logo = SOURCE[logo_start:logo_end]
        self.assertIn('aria-label="Агентство W"', logo)
        self.assertIn('Мы создаём своё настоящее', logo)
        self.assertIn('Acta, non verba.', logo)

    def test_existing_telegram_sign_in_not_replaced(self):
        self.assertIn("Telegram.Login.auth(", SOURCE)
        self.assertIn("st.user.is_logged_in", SOURCE)
        self.assertIn("referral_code = st.query_params.get(\"ref\", \"\").strip()", SOURCE)
        self.assertIn("social_registration_token = st.query_params.get(\"awt\", \"\").strip()", SOURCE)
        self.assertIn("agency_w_pending_ref", SOURCE)
        self.assertIn("member_code, created = save_member_to_supabase(telegram_data, referral_code)", SOURCE)
        self.assertIn("_render_telegram_widget_login(referral_code)", SOURCE)
        self.assertIn('partner_link = f"{AGENCY_W_PUBLIC_URL}/?ref={member_code}"', SOURCE)

    def test_mobile_firm_screen_and_existing_cta(self):
        self.assertIn("@media (max-width: 768px)", SOURCE)
        self.assertIn("flex-direction: column", SOURCE)
        self.assertIn("Войти через Telegram", SOURCE)
        self.assertIn("Приглашение партнёра принято: {referral_code}", SOURCE)
        self.assertNotIn("window.location.assign('https://t.me/", SOURCE)


if __name__ == "__main__":
    unittest.main()
