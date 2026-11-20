"""Cabinet entry-point acceptance with synthetic projects and a local model mock."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import scout_center as scout
from agent_memory import AccessDenied
from agent_memory_store import Repository
from agent_workspaces import scaffold
from content_factory_store import SQLiteStore


class ScoutCabinetTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.db = root / "test.sqlite3"
        store = SQLiteStore(self.db)
        self.project = store.save_project(11, {"name": "Synthetic Scout"})["id"]
        self.foreign = store.save_project(22, {"name": "Synthetic foreign"})["id"]
        Repository(store, initialize=True)
        self.folder = scaffold(root / "scout", "scout", scout.APP_DIR)
        self.config = {
            "SCOUT_MEMORY_ENABLED": "1", "SCOUT_MEMORY_OWNER_ID": "11",
            "SCOUT_MEMORY_PROJECT_ID": self.project,
            "SCOUT_MEMORY_WORKSPACE": str(self.folder),
        }
        self.env = patch.dict(os.environ, self.config)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.patch("content_factory_runtime.get_capabilities", return_value={"storage": "supabase"})
        self.patch("content_factory_runtime.build_store", side_effect=lambda: SQLiteStore(self.db))
        self.patch("scout_center._headers", return_value={})
        self.provider = self.patch("scout_center.requests.post")
        self.patch("scout_center.load_reports", return_value=[])
        self.legacy_save = self.patch("scout_center.save_report", return_value=(True, ""))
        self.ui = self.patch("scout_center.st")
        self.ui.session_state = {}
        self.ui.button.side_effect = lambda *args, **kwargs: kwargs.get("key") == "scout_run"
        self.ui.text_input.side_effect = lambda *args, **kwargs: (
            "Synthetic service" if kwargs.get("key") == "scout_competitor_name" else ""
        )
        self.ui.form_submit_button.return_value = False

    def patch(self, *args, **kwargs):
        mock = patch(*args, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def provider_report(self, text):
        response = MagicMock()
        response.json.return_value = {"output": [{"type": "message", "content": [{
            "type": "output_text", "text": text, "annotations": [{
                "type": "url_citation", "url": "https://example.invalid/review",
                "title": "Synthetic review", "start_index": 0, "end_index": len(text),
            }],
        }]}]}
        self.provider.return_value = response

    def test_cabinet_reads_and_saves_scoped_report_then_applies_checked_feedback(self):
        self.provider_report("Функция подтверждена")
        scout.render_scout_center(11, "Director")
        self.provider.assert_called_once()
        prompt = self.provider.call_args.kwargs["json"]["instructions"]
        self.assertIn("SCOUT_REGLEMENT.md", prompt)
        self.assertIn('"verified_experience": []', prompt)
        self.legacy_save.assert_not_called()

        workspace = scout.cabinet_workspace(11)
        (report_id, report), = scout.scoped_reports(workspace).items()
        self.assertEqual(report["sources"][0]["url"], "https://example.invalid/review")
        feedback = "Нельзя выдавать неподтверждённую информацию за установленный факт"
        scout.save_director_feedback(workspace, report_id, feedback)
        self.assertEqual(workspace.begin("comments-only", {})["verified_experience"], [])

        self.ui.form_submit_button.return_value = True
        self.ui.checkbox.return_value = True
        self.ui.button.return_value = False
        self.ui.button.side_effect = None
        self.ui.text_area.side_effect = lambda label, **kwargs: (
            feedback if label == "Замечание директора" else "Статус — не подтверждено"
        )
        self.ui.text_input.side_effect = lambda label, **kwargs: (
            "https://example.invalid/review" if label == "Источник проверки исправления" else ""
        )
        scout.render_scout_center(11, "Director")
        scout.render_scout_center(11, "Director")
        entries = workspace.team.context(workspace.binding)["project"]["knowledge"]
        self.assertEqual(sum(e["kind"] == "feedback" for e in entries), 2)
        self.assertEqual(sum(e["kind"] == "correction" for e in entries), 1)
        self.assertEqual(sum(e["kind"] == "check" for e in entries), 1)

        self.ui.form_submit_button.return_value = False
        self.ui.button.return_value = True
        self.ui.text_input.side_effect = lambda *args, **kwargs: (
            "Second synthetic service" if kwargs.get("key") == "scout_competitor_name" else ""
        )

        def apply_correction(*args, **kwargs):
            instructions = kwargs["json"]["instructions"]
            self.assertIn(feedback, instructions)
            self.assertIn("Статус — не подтверждено", instructions)
            return self.provider.return_value

        self.provider_report("Сторонний обзор сообщает о функции. Статус — не подтверждено.")
        self.provider.side_effect = apply_correction
        scout.render_scout_center(11, "Director")
        self.assertEqual(self.provider.call_count, 2)
        reopened = scout.cabinet_workspace(11)
        reports = scout.scoped_reports(reopened).values()
        self.assertTrue(any("Статус — не подтверждено" in report["report"] for report in reports))

    def test_disabled_uses_legacy_path_and_revokes_existing_workspace(self):
        workspace = scout.cabinet_workspace(11)
        self.provider_report("Legacy report")
        with patch.dict(os.environ, {"SCOUT_MEMORY_ENABLED": "0"}):
            scout.render_scout_center(11, "Director")
            self.legacy_save.assert_called_once()
            self.assertNotIn("SCOPED PROFESSIONAL MATERIALS",
                             self.provider.call_args.kwargs["json"]["instructions"])
            with self.assertRaises(AccessDenied):
                workspace.begin("revoked", {})

    def test_foreign_owner_and_project_are_denied(self):
        self.assertIsNone(scout.cabinet_workspace(22))
        with patch.dict(os.environ, {"SCOUT_MEMORY_PROJECT_ID": self.foreign}):
            scout.render_scout_center(11, "Director")
        self.provider.assert_not_called()
        self.legacy_save.assert_not_called()

    def test_missing_local_material_stops_before_provider(self):
        (self.folder / "context" / "FEEDBACK.md").unlink()
        scout.render_scout_center(11, "Director")
        self.provider.assert_not_called()
        self.legacy_save.assert_not_called()
