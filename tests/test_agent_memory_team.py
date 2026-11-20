"""Synthetic tenants only. No channel, cloud, model, calendar or Mac-sync calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_memory import AccessDenied, Conflict, Memory, MemoryError, Principal, Scope, digest
from agent_memory_store import Repository
from agent_memory_team import Binding, ROLES, TeamMemory
from content_factory_store import SQLiteStore


class TeamMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStore(Path(self.tmp.name) / "team.sqlite3")
        self.a = self.store.save_project(11, {"name": "Same project name"})
        self.b = self.store.save_project(11, {"name": "Same project name"})
        self.c = self.store.save_project(22, {"name": "Same project name"})
        self.repo = Repository(self.store, initialize=True)
        self.binding = Binding(11, "neona-public", self.a["id"])
        self.allowed = True
        self.team = TeamMemory(
            self.repo, grants=(self.binding,),
            access_check=lambda binding: self.allowed,
        )

    def write(self, binding=None, event_id="event-1", outcome="feedback"):
        return self.team.record_outcome(
            binding or self.binding, outcome, "Synthetic evidence",
            source="synthetic-task", event_id=event_id,
        )

    def test_disabled_by_default_does_not_read_or_write_repository(self):
        from unittest.mock import Mock
        repo = Mock()
        team = TeamMemory(repo)
        for action in (
            lambda: team.context(self.binding),
            lambda: team.record_outcome(self.binding, "feedback", "text",
                                        source="test", event_id="one"),
        ):
            with self.assertRaises(AccessDenied):
                action()
        repo.load.assert_not_called()
        repo.commit.assert_not_called()
        repo.store.get_project.assert_not_called()

    def test_owner_role_channel_and_project_need_exact_grants(self):
        for binding in (
            Binding(22, "neona-public", self.c["id"]),
            Binding(11, "theo", self.a["id"]),
            Binding(11, "neona-telegram", self.a["id"]),
            Binding(11, "neona-public", self.b["id"]),
            Binding(11, "neona-public", self.a["id"], True),
        ):
            with self.subTest(binding=binding), self.assertRaises(AccessDenied):
                self.team.context(binding)

    def test_forged_project_owner_rejected_even_if_granted(self):
        wrong = Binding(11, "theo", self.c["id"])
        team = TeamMemory(self.repo, grants=(wrong,), access_check=lambda binding: True)
        with self.assertRaises(AccessDenied):
            team.context(wrong)

    def test_access_revocation_blocks_existing_adapter(self):
        self.write()
        self.allowed = False
        with self.assertRaises(AccessDenied):
            self.team.context(self.binding)
        with self.assertRaises(AccessDenied):
            self.write(event_id="after-revocation")

    def test_missing_access_check_is_closed(self):
        team = TeamMemory(self.repo, grants=(self.binding,))
        with self.assertRaises(AccessDenied):
            team.context(self.binding)

    def test_all_prepared_roles_keep_separate_scopes(self):
        bindings = [Binding(11, role, self.a["id"]) for role in ROLES]
        team = TeamMemory(self.repo, grants=bindings, access_check=lambda binding: True)
        for binding in bindings:
            team.record_outcome(binding, "success", binding.agent_id,
                                source="synthetic-task", event_id="same-task-id")
        for binding in bindings:
            context = team.context(binding)
            self.assertEqual(context["role"], binding.agent_id)
            self.assertEqual([entry["text"] for entry in context["project"]["knowledge"]],
                             [binding.agent_id])

    def test_outcomes_survive_restart_and_retry_is_idempotent(self):
        for outcome in ("success", "error", "feedback", "correction"):
            self.write(outcome=outcome)
            self.write(outcome=outcome)
        restarted = TeamMemory(Repository(SQLiteStore(self.store.path)),
                               grants=(self.binding,), access_check=lambda binding: True)
        context = restarted.context(self.binding)["project"]
        self.assertEqual(context["revision"], 4)
        self.assertEqual(len(context["knowledge"]), 4)
        self.assertEqual(context["rules"], [])
        self.assertTrue(all(e["certainty"] == "unknown" for e in context["knowledge"]))

    def test_reused_event_with_different_payload_is_a_conflict(self):
        self.write()
        with self.assertRaises(Conflict):
            self.team.record_outcome(self.binding, "feedback", "Changed evidence",
                                     source="synthetic-task", event_id="event-1")

    def test_foreign_project_feedback_never_appears_in_context(self):
        foreign = Binding(11, "neona-public", self.b["id"])
        team = TeamMemory(self.repo, grants=(self.binding, foreign),
                          access_check=lambda binding: True)
        team.record_outcome(foreign, "feedback", "Foreign project note",
                            source="synthetic-task", event_id="foreign")
        self.assertEqual(team.context(self.binding)["project"]["knowledge"], [])

    def test_agent_cannot_create_verified_fact_or_rule_through_adapter(self):
        for outcome in ("rule", "check", "approve", "fact"):
            with self.subTest(outcome=outcome), self.assertRaises(MemoryError):
                self.write(outcome=outcome)
        self.write()
        self.assertEqual(self.team.context(self.binding)["project"]["rules"], [])

    def test_shared_context_contains_only_owner_approved_rules(self):
        scope = Scope(11, "*", "owner")
        owner = Memory(self.repo, Principal(11, "owner:11", "owner"), scope)
        owner.record("feedback", "PRIVATE owner note", source="owner", key="private")
        owner.record("rule", "Approved synthetic rule", source="owner", key="rule")
        owner.record("rule", "Unapproved rule", source="owner", key="pending")
        rule_id = digest({"scope": scope.as_dict(), "key": "rule"})[:32]
        verifier = Memory(self.repo, Principal(11, "test-verifier", "verifier",
                          "*", "owner", frozenset({"read", "append", "verify"})), scope)
        verifier.record("check", "Synthetic check", source="test-suite", key="check",
                        related=[rule_id], evidence={
                            "passed": True, "rule_hash": digest("Approved synthetic rule")})
        check_id = digest({"scope": scope.as_dict(), "key": "check"})[:32]
        owner.validate_rule(rule_id, [check_id], expected=owner.read()["revision"])
        owner.approve_rule(rule_id, expected=owner.read()["revision"])
        binding = Binding(11, "scout", self.a["id"], True)
        team = TeamMemory(self.repo, grants=(binding,), access_check=lambda binding: True)
        parent = team.context(binding)["parents"][0]
        self.assertEqual([r["text"] for r in parent["rules"]], ["Approved synthetic rule"])
        self.assertNotIn("knowledge", parent)
        self.assertNotIn("PRIVATE", str(parent))

    def test_ora_cannot_inherit_agency_rules(self):
        with self.assertRaises(AccessDenied):
            Binding(11, "ora", self.a["id"], True)
        binding = Binding(11, "ora", self.a["id"])
        team = TeamMemory(self.repo, grants=(binding,), access_check=lambda binding: True)
        self.assertEqual(team.context(binding)["parents"], [])

    def test_missing_event_id_is_rejected(self):
        for event_id in ("", "   ", None, "x" * 257):
            with self.subTest(event_id=event_id), self.assertRaises(MemoryError):
                self.write(event_id=event_id)

    def test_credentials_are_rejected(self):
        with self.assertRaises(MemoryError):
            self.team.record_outcome(self.binding, "error", "password=secret-value",
                                     source="test", event_id="secret")

    def test_same_event_in_parallel_creates_one_revision(self):
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(lambda i: self.write(event_id="retry"), range(3)))
        self.assertEqual(self.team.context(self.binding)["project"]["revision"], 1)

    def test_legacy_kadrian_gate_does_not_enable_team_connections(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"AGENCY_MEMORY_V1": "1",
                        "AGENCY_MEMORY_V1_OWNERS": "11",
                        "AGENCY_MEMORY_V1_PROJECT_ID": self.a["id"]}):
            with self.assertRaises(AccessDenied):
                TeamMemory(self.repo).context(self.binding)


if __name__ == "__main__":
    unittest.main()
