"""Unit tests for the shared numbered instruction catalog (no Streamlit needed)."""

import unittest

from agency_help_guides import (
    GUIDES,
    GUIDE_BY_NUMBER,
    INSTRUCTIONS_ROUTE,
    find_instruction,
    instruction_index_text,
)


class NumberedGuideTests(unittest.TestCase):
    def test_numbers_are_stable_and_unique(self):
        self.assertEqual([g["number"] for g in GUIDES],
                         ["00", "01", "02", "03", "04", "05", "06", "07"])
        self.assertEqual(len(GUIDES), len(GUIDE_BY_NUMBER))

    def test_every_guide_has_practical_steps(self):
        for guide in GUIDES:
            self.assertTrue(guide["title"])
            self.assertTrue(guide["steps"])
            self.assertTrue(guide["success"])
            self.assertTrue(guide["trouble"])

    def test_route_is_exact_for_all_users(self):
        self.assertIn("👥 Команда", INSTRUCTIONS_ROUTE)
        self.assertIn("🧰 Инструменты команды", INSTRUCTIONS_ROUTE)
        self.assertIn("📚 Инструкции", INSTRUCTIONS_ROUTE)

    def test_search_by_number_and_title(self):
        self.assertEqual(find_instruction("03")[0]["number"], "03")
        self.assertEqual(find_instruction("№ 03")[0]["number"], "03")
        self.assertEqual(find_instruction("номер 3")[0]["number"], "03")
        self.assertEqual(find_instruction("telegram")[0]["number"], "02")

    def test_voice_index_matches_interface(self):
        index = instruction_index_text()
        for guide in GUIDES:
            self.assertIn(
                f"Инструкция № {guide['number']} — {guide['title']}", index
            )


if __name__ == "__main__":
    unittest.main()
