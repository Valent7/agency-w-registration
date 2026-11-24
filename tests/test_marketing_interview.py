import unittest
from copy import deepcopy

from marketing_interview import BLOCKS, QUESTION_IDS, InterviewStore, change_answer, pilot_project


PROJECT = "16aa48d6-6d46-412b-8956-891da63bce28"


class InterviewTests(unittest.TestCase):
    def test_catalog_and_pilot_scope(self):
        self.assertEqual([len(block["questions"]) for block in BLOCKS], [23, 12, 13, 25])
        self.assertEqual(len(set(QUESTION_IDS)), 73)
        self.assertEqual(pilot_project(1, "1", PROJECT), PROJECT)
        self.assertIsNone(pilot_project(2, "1", PROJECT))
        self.assertIsNone(pilot_project(1, "1", PROJECT + "," + PROJECT))

    def test_edit_invalidates_block_confirmation_and_skip_counts(self):
        state = {"schema": 1, "answers": {}, "confirmed": ["personality", "content"]}
        skipped = change_answer(state, QUESTION_IDS[0], None)
        self.assertIn(QUESTION_IDS[0], skipped["answers"])
        edited = change_answer(skipped, QUESTION_IDS[0], " Новая версия ")
        self.assertEqual(edited["answers"][QUESTION_IDS[0]], "Новая версия")
        self.assertEqual(edited["confirmed"], ["content"])
        self.assertEqual(state["answers"], {})
        with self.assertRaises(ValueError):
            change_answer(state, "missing-01", "answer")

    def test_resume_and_reject_stale_write(self):
        rows = []

        def request(method, table, **kwargs):
            if table == "content_factory_projects":
                return [{"id": PROJECT}]
            if method == "GET":
                return deepcopy(rows)
            if method == "POST":
                rows.append(kwargs["json"])
                return deepcopy(rows)
            if kwargs["params"]["revision"] != f"eq.{rows[0]['revision']}":
                return []
            rows[0].update(kwargs["json"])
            return deepcopy(rows)

        store = InterviewStore(request, 1, PROJECT, deepcopy, deepcopy)
        revision, state = store.load()
        store.save(revision, change_answer(state, QUESTION_IDS[0], "Ответ"))
        revision, state = store.load()
        self.assertEqual(revision, 1)
        self.assertEqual(state["answers"][QUESTION_IDS[0]], "Ответ")
        store.save(revision, change_answer(state, QUESTION_IDS[1], None))
        with self.assertRaises(RuntimeError):
            store.save(revision, change_answer(state, QUESTION_IDS[1], "Устаревшая правка"))

    def test_project_membership_checked_before_interview_is_read(self):
        def request(method, table, **kwargs):
            self.assertEqual(table, "content_factory_projects")
            self.assertEqual(kwargs["params"]["owner_id"], "eq.2")
            return []

        store = InterviewStore(request, 2, PROJECT, deepcopy, deepcopy)
        with self.assertRaises(LookupError):
            store.load()


if __name__ == "__main__":
    unittest.main()
