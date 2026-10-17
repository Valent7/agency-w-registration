import unittest
from pathlib import Path
from agency_access_control import is_agency_access_revoked

class AccessRevocationTests(unittest.TestCase):
    def test_revoke_specific_account(self):
        self.assertTrue(is_agency_access_revoked(291173537))
        self.assertTrue(is_agency_access_revoked("291173537"))
        self.assertTrue(is_agency_access_revoked(6541575252))
        self.assertTrue(is_agency_access_revoked("6541575252"))
        self.assertFalse(is_agency_access_revoked(291173538))
        self.assertFalse(is_agency_access_revoked("Aleksey_Balakin"))
        self.assertFalse(is_agency_access_revoked(None))

    def test_former_partner_archived_not_counted_as_active(self):
        center = Path("neola_partner_center.py").read_text(encoding="utf-8")
        team = Path("team_center.py").read_text(encoding="utf-8")
        self.assertIn("archived_members = [", center)
        self.assertIn("if not is_agency_access_revoked(member.get(\"telegram_id\"))", center)
        self.assertIn("📦 Архив выбывших участников", center)
        self.assertIn("if not is_agency_access_revoked(m.get(\"telegram_id\"))", team)
        self.assertIn("if not is_agency_access_revoked(member.get(\"telegram_id\"))", team)

    def test_background_workers_skip_offboarded_accounts(self):
        for file in (
            "neona_telegram_dialogs.py",
            "telegram_scout_worker.py",
            "vk_scout_worker.py",
            "team_telegram_delivery.py",
        ):
            code = Path(file).read_text(encoding="utf-8")
            self.assertIn("is_agency_access_revoked", code, file)
        neona = Path("neona_telegram_dialogs.py").read_text(encoding="utf-8")
        self.assertLess(
            neona.index("ids = [id_ for id_ in ids if not is_agency_access_revoked(id_)]"),
            neona.index("    members = requests.get(", neona.index("def _owners(")),
        )

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
