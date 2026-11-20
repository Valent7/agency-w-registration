"""Synthetic workspaces and a mocked actual scout provider. No paid/network calls."""
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_memory import AccessDenied, MemoryError, Principal
from agent_memory_store import Repository
from agent_memory_team import Binding, TeamMemory
from agent_workspaces import AgentWorkspace, FILES, local_path, scaffold
from content_factory_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SQLiteStore(self.root / "memory.sqlite3")
        self.project = self.store.save_project(11, {"name": "Synthetic"})
        self.other = self.store.save_project(11, {"name": "Synthetic"})
        self.repo = Repository(self.store, initialize=True)
        self.binding = Binding(11, "scout", self.project["id"])
        self.neighbor = Binding(11, "theo", self.project["id"])
        self.grant = True
        self.team = TeamMemory(self.repo, grants=(self.binding, self.neighbor),
                               access_check=lambda binding: self.grant)
        self.folder = scaffold(self.root / "scout", "scout", ROOT)
        self.workspace = AgentWorkspace(self.folder, ROOT, self.team, self.binding)
        self.owner = Principal(11, "synthetic-owner", "owner")

    def event(self, **changes):
        event = {
            "date": "2026-11-20T12:00:00+00:00", "task": "Synthetic competitor",
            "expected": "Подтверждать характеристики источником",
            "result": "Рекламное заявление ошибочно названо подтверждённой функцией",
            "assessment": "error", "source": "synthetic:marketing-page",
            "cause": "Недостаточная проверка источника", "cause_status": "fact",
            "correction": "Рекламное заявление отделять от подтверждённой функции",
            "check": "Сверить с документацией", "next_application": "Следующий отчёт",
        }
        event.update(changes)
        return event

    def checked_correction(self):
        self.workspace.finish("error", "error-1", self.event())
        self.workspace.finish("correction", "fix-1", self.event(
            result="Заявление помечено как маркетинговое", assessment="success"))
        self.workspace.check("correction", "fix-1", reviewer=self.owner,
                             passed=True, source="synthetic-check:1")

    def test_scaffold_preserves_existing_documents_and_creates_all_files(self):
        original = (self.folder / "Agent.md").read_bytes()
        (self.folder / "context/PROJECT.md").write_text("Existing private project")
        scaffold(self.folder, "scout", ROOT)
        self.assertEqual((self.folder / "Agent.md").read_bytes(), original)
        self.assertEqual((self.folder / "context/PROJECT.md").read_text(),
                         "Existing private project")
        for name in FILES:
            self.assertTrue((self.folder / name).is_file())

    def test_new_task_really_loads_documents_and_checked_fix_after_restart(self):
        self.checked_correction()
        restarted = AgentWorkspace(self.folder, ROOT,
                                   TeamMemory(Repository(SQLiteStore(self.store.path)),
                                   grants=(self.binding,), access_check=lambda binding: True),
                                   self.binding)
        prepared = restarted.begin("next-task", {"topic": "Synthetic competitor"})
        self.assertIn("SCOUT_REGLEMENT.md", str(prepared["materials"]))
        self.assertIn("Разведчик", prepared["materials"]["Agent.md"])
        self.assertEqual(len(prepared["verified_experience"]), 1)
        self.assertIn("Рекламное", prepared["verified_experience"][0]["correction"])
        self.assertEqual(prepared["memory"]["project"]["rules"], [])

    def test_checked_experience_survives_separate_process(self):
        self.checked_correction()
        program = """
import json, sys
from agent_memory_store import Repository
from agent_memory_team import Binding, TeamMemory
from agent_workspaces import AgentWorkspace
from content_factory_store import SQLiteStore
repo = Repository(SQLiteStore(sys.argv[1]))
binding = Binding(11, "scout", sys.argv[2])
team = TeamMemory(repo, grants=(binding,), access_check=lambda binding: True)
workspace = AgentWorkspace(sys.argv[3], sys.argv[4], team, binding)
print(json.dumps(workspace.begin("new-process", {})["verified_experience"],
                 ensure_ascii=False))
"""
        output = subprocess.check_output(
            [sys.executable, "-c", program, str(self.store.path),
             self.project["id"], str(self.folder), str(ROOT)], cwd=ROOT, text=True)
        self.assertEqual(len(json.loads(output)), 1)

    def test_unchecked_and_failed_corrections_are_not_applied(self):
        self.workspace.finish("correction", "fix-1", self.event())
        self.assertEqual(self.workspace.begin("task-1", {})["verified_experience"], [])
        self.workspace.check("correction", "fix-1", reviewer=self.owner,
                             passed=False, source="synthetic-check:failed")
        self.assertEqual(self.workspace.begin("task-2", {})["verified_experience"], [])

    def test_later_failed_check_revokes_previous_experience(self):
        self.checked_correction()
        self.workspace.check("correction", "fix-1", reviewer=self.owner,
                             passed=False, source="synthetic-check:2")
        self.assertEqual(self.workspace.begin("task-3", {})["verified_experience"], [])

    def test_neighbor_cannot_read_scout_workspace_or_its_feedback(self):
        self.checked_correction()
        neighbor = AgentWorkspace(self.folder, ROOT, self.team, self.neighbor)
        with self.assertRaises(AccessDenied):
            neighbor.begin("neighbor-task", {})
        self.assertEqual(self.team.context(self.neighbor)["project"]["knowledge"], [])

    def test_same_role_other_project_cannot_reuse_private_folder(self):
        self.workspace.begin("first-task", {})
        other = Binding(11, "scout", self.other["id"])
        team = TeamMemory(self.repo, grants=(other,), access_check=lambda binding: True)
        with self.assertRaises(AccessDenied):
            AgentWorkspace(self.folder, ROOT, team, other).begin("foreign", {})

    def test_agent_cannot_verify_own_correction(self):
        self.workspace.finish("correction", "fix-1", self.event())
        agent = Principal(11, "agent:scout", "agent", "scout", self.project["id"])
        with self.assertRaises(AccessDenied):
            self.workspace.check("correction", "fix-1", reviewer=agent,
                                 passed=True, source="self-assertion")

    def test_replayed_event_does_not_create_duplicate(self):
        event = self.event()
        self.workspace.finish("error", "error-1", event)
        self.workspace.finish("error", "error-1", event)
        self.assertEqual(self.team.context(self.binding)["project"]["revision"], 1)

    def test_private_workspace_cannot_be_created_inside_public_checkout(self):
        public = self.root / "public-repository"
        public.mkdir()
        with self.assertRaises(MemoryError):
            scaffold(public / "private", "scout", public)
        self.assertFalse((public / "private").exists())

    def test_parent_and_absolute_paths_are_rejected(self):
        for path in ("../foreign.md", "/private/foreign.md"):
            with self.subTest(path=path), self.assertRaises(MemoryError):
                local_path(self.folder, path)

    def test_workspace_symlink_is_rejected(self):
        (self.folder / "context/PROJECT.md").unlink()
        foreign = self.root / "foreign.md"
        foreign.write_text("foreign private material")
        (self.folder / "context/PROJECT.md").symlink_to(foreign)
        with self.assertRaises(MemoryError):
            self.workspace.begin("symlink", {})

    def test_actual_scout_request_uses_checked_experience_and_reuses_saved_result(self):
        import scout_center
        self.checked_correction()
        calls = []

        def provider(*args, **kwargs):
            instructions = kwargs["json"]["instructions"]
            self.assertIn("SCOPED PROFESSIONAL MATERIALS", instructions)
            self.assertIn("verified_experience", instructions)
            self.assertIn("Рекламное заявление отделять", instructions)
            calls.append(instructions)
            response = Mock()
            # Synthetic provider applies the correction found in the actual request.
            report = "[МАРКЕТИНГОВОЕ ЗАЯВЛЕНИЕ] Функция требует проверки.\n**НАБЛЮДАТЬ**"
            response.json.return_value = {"output": [{
                "type": "message", "content": [{
                    "type": "output_text", "text": report, "annotations": [],
                }],
            }]}
            return response

        with patch.object(scout_center, "_headers", return_value={}), patch.object(
                scout_center.requests, "post", side_effect=provider):
            first = scout_center.run_competitor_research(
                "Synthetic competitor", experience_workspace=self.workspace, task_id="demo")
            second = scout_center.run_competitor_research(
                "Synthetic competitor", experience_workspace=self.workspace, task_id="demo")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertIn("МАРКЕТИНГОВОЕ", first["report"])
        events = self.team.context(self.binding)["project"]["knowledge"]
        generated = [json.loads(e["text"]) for e in events if e["kind"] == "execution"]
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["assessment"], "unknown")

    def test_default_scout_call_keeps_existing_path(self):
        import scout_center
        response = Mock()
        response.json.return_value = {"output": [{
            "type": "message", "content": [{
                "type": "output_text", "text": "**НАБЛЮДАТЬ**", "annotations": [],
            }],
        }]}
        with patch.object(scout_center, "_headers", return_value={}), patch.object(
                scout_center.requests, "post", return_value=response) as provider:
            result = scout_center.run_competitor_research("Synthetic")
        self.assertEqual(result["verdict"], "НАБЛЮДАТЬ")
        self.assertNotIn("SCOPED PROFESSIONAL MATERIALS",
                         provider.call_args.kwargs["json"]["instructions"])

    def test_secret_in_local_material_stops_before_task(self):
        (self.folder / "context/FEEDBACK.md").write_text("password=private-local-value")
        with self.assertRaises(MemoryError):
            self.workspace.begin("secret", {})
        self.assertEqual(self.team.context(self.binding)["project"]["revision"], 0)

    def test_provider_error_is_recorded_without_exception_secrets(self):
        import scout_center
        with patch.object(scout_center, "_headers", return_value={}), patch.object(
                scout_center.requests, "post",
                side_effect=RuntimeError("password=private-provider-value")):
            with self.assertRaises(RuntimeError):
                scout_center.run_competitor_research(
                    "Synthetic", experience_workspace=self.workspace, task_id="failed")
        entries = self.team.context(self.binding)["project"]["knowledge"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "analysis")
        self.assertNotIn("private-provider-value", entries[0]["text"])

    def test_replayed_check_is_idempotent(self):
        self.workspace.finish("correction", "fix-1", self.event())
        self.workspace.check("correction", "fix-1", reviewer=self.owner,
                             passed=True, source="same-check")
        revision = self.team.context(self.binding)["project"]["revision"]
        self.workspace.check("correction", "fix-1", reviewer=self.owner,
                             passed=True, source="same-check")
        self.assertEqual(self.team.context(self.binding)["project"]["revision"], revision)

    def test_revoked_access_stops_scout_before_provider(self):
        import scout_center
        self.grant = False
        with patch.object(scout_center.requests, "post") as provider:
            with self.assertRaises(AccessDenied):
                scout_center.run_competitor_research(
                    "Synthetic", experience_workspace=self.workspace, task_id="blocked")
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
