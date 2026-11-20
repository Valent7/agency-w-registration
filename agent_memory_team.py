"""Preparation-only adapters to the existing Memory store.

No production entry point imports this module. Grants default to empty. Trusted
server authentication must supply the binding and recheck access on every call.
Neither model output nor URL/session payloads may construct grants or identities.
"""
from dataclasses import dataclass

from agent_memory import AccessDenied, Memory, MemoryError, Principal, Scope


ROLES = frozenset({
    "stagirite", "neonia", "neona-public", "neona-telegram", "neona-instagram",
    "neona-vk", "neola", "theo", "content-master", "scout", "ora",
})
OUTCOMES = {
    "success": "execution",
    "error": "analysis",
    "feedback": "feedback",
    "correction": "correction",
}


@dataclass(frozen=True)
class Binding:
    """Exact server-approved tenant, role and project, with no wildcard grant."""
    owner_id: int
    agent_id: str
    project_id: str
    read_shared_rules: bool = False

    def __post_init__(self):
        if self.agent_id not in ROLES:
            raise MemoryError("Unsupported team role")
        Scope(self.owner_id, self.agent_id, self.project_id, self.project_id)
        if type(self.read_shared_rules) is not bool:
            raise MemoryError("Explicit shared-rule permission required")
        if self.agent_id == "ora" and self.read_shared_rules:
            raise AccessDenied("Ora requires an isolated project knowledge profile")

    def scope(self):
        return Scope(self.owner_id, self.agent_id, self.project_id, self.project_id)


class TeamMemory:
    """Reuse a server-owned Repository, never initialize a database or install sync."""

    def __init__(self, repository, *, grants=(), access_check=None):
        self.repository = repository
        self.grants = frozenset(grants)
        self.access_check = access_check

    def _authorize(self, binding):
        if not isinstance(binding, Binding) or binding not in self.grants:
            raise AccessDenied("Team memory connection is disabled")
        if self.access_check is None or self.access_check(binding) is not True:
            raise AccessDenied("Current server authorization required")
        project = self.repository.store.get_project(binding.owner_id, binding.project_id)
        if (not project or project.get("owner_id") != binding.owner_id
                or project.get("id") != binding.project_id):
            raise AccessDenied("Project does not belong to this owner")

    def _memory(self, binding):
        self._authorize(binding)
        scope = binding.scope()
        principal = Principal(
            binding.owner_id, "agent:" + binding.agent_id, "agent",
            binding.agent_id, scope.workspace_id, frozenset({"read", "append"}),
        )
        return Memory(self.repository, principal, scope)

    def context(self, binding):
        """Return own scoped evidence and approved rules, plus opt-in shared rules.

        Evidence is untrusted data and cannot change professional duties, grant
        permissions or become a rule without the existing owner approval flow.
        Other agents' private evidence is never read or inherited.
        """
        own = self._memory(binding).context()
        parents = []
        if binding.read_shared_rules:
            self._authorize(binding)
            scope = Scope(binding.owner_id, "*", "owner")
            reader = Principal(
                binding.owner_id, "shared-reader:" + binding.agent_id,
                "agent", "*", "owner", frozenset({"read"}),
            )
            shared = Memory(self.repository, reader, scope).context()
            parents.append({
                "scope": shared["scope"], "revision": shared["revision"],
                "hash": shared["hash"], "rules": shared["rules"],
            })
        return {"role": binding.agent_id, "project": own, "parents": parents,
                "boundary": own["boundary"]}

    def record_outcome(self, binding, outcome, text, *, source, event_id,
                       related=None):
        """Append sourced evidence with existing CAS and idempotency semantics.

        The stable event_id must come from the application's durable task/outbox.
        Caller retains that outbox until this write is acknowledged. Retry uses
        the same event_id and payload. No rule or verified check is fabricated.
        """
        if outcome not in OUTCOMES:
            raise MemoryError("Unsupported outcome")
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
            raise MemoryError("Stable application event ID required")
        return self._memory(binding).record(
            OUTCOMES[outcome], text, source=source,
            key="team:" + outcome + ":" + event_id, related=related,
        )
