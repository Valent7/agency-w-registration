"""Diary retired from navigation without deleting stored records."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DiaryRetirementTests(unittest.TestCase):
    def test_diary_not_in_main_menu_or_dispatch(self):
        app = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
        self.assertNotIn("from agency_diary import render_agency_diary", app)
        self.assertNotIn('elif main_section == "📖 Дневник":', app)
        menu = app.split("main_menu_options = [", 1)[1].split("]", 1)[0]
        self.assertNotIn('"📖 Дневник"', menu)
        self.assertIn('"📅 Календарь"', menu)
        self.assertIn('"🤖 Агенты"', menu)

    def test_legacy_diary_selection_goes_to_day(self):
        app = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
        self.assertIn('if pending_main_section in ("🏭 Контент", locked_content, "📖 Дневник"):', app)
        self.assertIn('if st.session_state.get("main_section") in ("🏭 Контент", locked_content, "📖 Дневник"):', app)

    def test_diary_storage_module_is_preserved(self):
        storage = ROOT / "agency_diary.py"
        self.assertTrue(storage.is_file())
        self.assertIn("agency_diary_entries", storage.read_text(encoding="utf-8"))

    def test_neola_navigation_no_longer_advertises_diary(self):
        guide = (ROOT / "neola_cabinet_map.py").read_text(encoding="utf-8")
        self.assertIn("Дневник убран из меню", guide)
        self.assertNotIn('Путь: «📖 Дневник»', guide)


if __name__ == "__main__":
    unittest.main()
