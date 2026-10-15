import unittest
from pathlib import Path
from agency_access_control import is_agency_access_revoked

class AccessRevocationTests(unittest.TestCase):
    def test_revoke_specific_account(self):
        self.assertTrue(is_agency_access_revoked(291173537))
        self.assertTrue(is_agency_access_revoked("291173537"))
        self.assertFalse(is_agency_access_revoked(291173538))
        self.assertFalse(is_agency_access_revoked("Aleksey_Balakin"))
        self.assertFalse(is_agency_access_revoked(None))

    def test_guard_precedes_any_partner_registration(self):
        app = Path("streamlit_app.py").read_text(encoding="utf-8")
        start = app.index('telegram_id = telegram_data.get("id", "")')
        guard = app.index("if is_agency_access_revoked(telegram_id):", start)
        stop = app.index("st.stop()", guard)
        registration = app.index("save_member_to_supabase(telegram_data, referral_code)", start)
        self.assertLess(start, guard)
        self.assertLess(guard, stop)
        self.assertLess(stop, registration)
        self.assertIn("telegram_login_valid or remembered_data:", app)

if __name__ == "__main__":
    unittest.main()
