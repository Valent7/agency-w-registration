import os
from unittest import TestCase
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


APP = """from marketing_interview_ui import render_marketing_interview
render_marketing_interview(101)
"""
PROJECT = "00000000-0000-0000-0000-000000000001"


class MarketingInterviewUiTests(TestCase):
    def test_pilot_owner_can_save_and_resume(self):
        rows = []

        def request(method, table, **kwargs):
            if table == "content_factory_projects":
                return [{"id": PROJECT}]
            if method == "POST":
                rows.append(kwargs["json"])
            return rows

        with (
            patch.dict(os.environ, {"AGENCY_MARKETING_PILOT_OWNER": "101",
                                    "AGENCY_MARKETING_PILOT_PROJECT": PROJECT}),
            patch("marketing_interview_ui._request", side_effect=request),
            patch("marketing_interview_ui._encrypt_state", side_effect=lambda value: value),
            patch("marketing_interview_ui._decrypt_state", side_effect=lambda value: value),
            patch("marketing_interview_ui.voice_engine", return_value=None),
        ):
            app = AppTest.from_string(APP).run()
            self.assertFalse(app.exception)
            app.text_area[0].set_value("Помогать предпринимателям.")
            next(button for button in app.button if button.label == "Сохранить").click().run()
            self.assertFalse(app.exception)
            self.assertNotIn("ближайшие жизненные цели", app.text_area[0].label)
            self.assertEqual(rows[0]["encrypted_state"]["answers"]["personality-01"],
                             "Помогать предпринимателям.")
            self.assertEqual(rows[0]["project_id"], PROJECT)
