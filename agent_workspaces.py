"""Private Codex workspaces backed by the existing scoped team memory."""
import json
from datetime import datetime
from pathlib import Path

from agent_memory import AccessDenied, Memory, MemoryError, digest, safe_content
from agent_memory_team import Binding

FILES = (
    "AGENTS.md", "Agent.md", "context/PROJECT.md", "context/DECISIONS.md",
    "context/FEEDBACK.md", "context/CHECKS.md",
    "tasks/README.md", "knowledge/SOURCES.md", "results/README.md",
)
PROFILES = {
    "stagirite": ("Стагирит", "Координация поручений директора", ("STAGIRITE_SETUP.md",)),
    "neonia": ("Неония", "Поиск и анализ подходящих людей", ("NEONIA_SCOUT_STAGE1_README.txt",)),
    "neola": ("Неола", "Сопровождение подтверждённого партнёра", ("NEOLA_CABINET_MAP.md",)),
    "theo": ("Тео", "Консультация посетителей VK", ("THEO_CONSTITUTION.txt", "THEO_KNOWLEDGE_BASE.txt")),
    "content-master": ("Контент-мастер", "Подготовка контента по поручению директора", ()),
    "scout": ("Разведчик", "Конкурентная разведка по открытым источникам",
              ("SCOUT_CORE.md", "SCOUT_REGLEMENT.md")),
}
for channel in ("public", "telegram", "instagram", "vk"):
    PROFILES["neona-" + channel] = (
        "Неона · " + channel, "Диалог и осознанно согласованная встреча",
        ("docs/NEONA_CORE_V4_REFERENCE.md", "NEONA_KNOWLEDGE_BASE.txt",
         "docs/NEONA_PUBLIC_INTAKE.md"),
    )
RUNTIME_SOURCES = {
    "stagirite": "stagirite_center.py", "neonia": "neonia_intelligence_v2.py",
    "neola": "neola_role_policy.py", "theo": "theo_consultant.py",
    "content-master": "stagirite_center.py", "scout": "scout_center.py",
    "neona-public": "public_neona_webchat.py",
    "neona-telegram": "neona_dialog_policy_v3.py",
    "neona-instagram": "instagram_webhook.py", "neona-vk": "instagram_webhook.py",
}


def local_path(root, relative):
    """Fixed paths within a trusted root, rejecting workspace symlink redirects."""
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise MemoryError("Workspace path escapes its root")
    path = root / relative
    if path != root and root not in path.parents:
        raise MemoryError("Workspace path escapes its root")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise MemoryError("Workspace symlink is forbidden")
    return path


def scaffold(root, role, repository_root):
    """Create missing professional files only. Existing files are preserved."""
    if role not in PROFILES:
        raise MemoryError("Use the existing Kadrian or separate Ora project")
    root = Path(root)
    if root.is_symlink():
        raise MemoryError("Workspace root symlink is forbidden")
    root = root.resolve()
    repository_root = Path(repository_root).resolve()
    if root == repository_root or repository_root in root.parents:
        raise MemoryError("Private workspace must be outside the public checkout")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name, duty, references = PROFILES[role]
    marker = local_path(root, ".agent-profile.json")
    profile = {"role": role}
    if marker.exists() and json.loads(marker.read_text()) != profile:
        raise AccessDenied("Workspace belongs to another role")
    sources = ("AGENCY_W_CORE.md", *references)
    links = "\n".join("- " + str(repository_root / ref) for ref in sources)
    if role.startswith("neona-") and root.parent.name == "channels":
        links += "\n- Общий профессиональный профиль Неоны — " + str(root.parent.parent / "Agent.md")
    texts = {
        "AGENTS.md": (
            f"# {name}\n\nПеред каждым заданием прочитай Agent.md и файлы context. "
            "Используй только проверенный проект и свою область роли/канала. "
            "Перед действием загрузи scoped-память через AgentWorkspace.begin; "
            "ошибка чтения останавливает подключённое задание. "
            "При завершении запиши результат со стабильным event_id через "
            "AgentWorkspace.finish. Проверенное исправление учитывай в следующем задании. "
            "Общие правила утверждает директор. Чужие папки и данные других проектов "
            "не используй.\n"
        ),
        "Agent.md": (
            f"# {name}\n\nПрофессиональная обязанность — {duty}.\n\n"
            f"Авторские источники\n\n{links}\n\n"
            f"Действующая реализация — {repository_root / RUNTIME_SOURCES[role]}.\n\n"
            "Перед профессиональным заданием прочитай относящиеся к нему инструкции "
            "этой реализации. Локальная папка хранит указатели и опыт, авторские "
            "профессиональные инструкции определяют полномочия. Отделяй факты, "
            "гипотезы и неизвестное. Успех подтверждай проверкой и источником. "
            "Секреты и личные переписки храни только в разрешённой закрытой области.\n"
        ),
        "context/PROJECT.md": (
            f"# Проект {name}\n\nПапка профессиональных материалов. "
            "Владелец и canonical project UUID задаются проверенной серверной "
            "привязкой Binding при начале задания. До такой привязки клиентские "
            "данные сюда не импортируются. Задачи и результаты проекта хранятся "
            "в существующем Memory/Repository.\n"
        ),
        "context/DECISIONS.md": "# Решения\n\nУтверждённые решения записываются с датой, автором и источником.\n",
        "context/FEEDBACK.md": (
            "# Обратная связь\n\nРезультаты проекта сохраняются через "
            "AgentWorkspace.finish в существующей scoped-памяти. Формат — дата, "
            "область, задача, ожидание, результат, оценка, источник, причина, "
            "исправление, проверка и следующее применение. Подтверждённые "
            "исправления читаются перед новым заданием.\n"
        ),
        "context/CHECKS.md": (
            "# Проверки\n\nПроверяющий сохраняет исход проверки в существующей "
            "памяти с ссылкой на исправление и хешем проверенного текста. "
            "Поздняя неудачная проверка исключает исправление из следующего задания.\n"
        ),
        "tasks/README.md": (
            "# Задачи\n\nПеред началом передай проверенное задание и стабильный task_id "
            "в AgentWorkspace.begin. Снимок задачи сохраняется существующим Memory.prepare_task.\n"
        ),
        "knowledge/SOURCES.md": "# Источники знаний\n\n" + links + "\n",
        "results/README.md": (
            "# Результаты\n\nРезультаты и проверки связаны со стабильными событиями "
            "в существующем Repository. Для синтетической приёмки используй отдельный "
            "тестовый проект. Успешная генерация текста требует проверки качества.\n"
        ),
        ".agent-profile.json": json.dumps(profile) + "\n",
    }
    for relative, content in texts.items():
        path = local_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            path.chmod(0o600)
        except FileExistsError:
            pass
    return root


class AgentWorkspace:
    """Trusted caller selects a private root and an authorized Binding per task."""

    def __init__(self, root, repository_root, team, binding):
        if not isinstance(binding, Binding) or binding.agent_id not in PROFILES:
            raise AccessDenied("Unsupported workspace binding")
        if Path(root).is_symlink():
            raise MemoryError("Workspace root symlink is forbidden")
        self.root = Path(root).resolve()
        self.repository_root = Path(repository_root).resolve()
        if self.root == self.repository_root or self.repository_root in self.root.parents:
            raise MemoryError("Private workspace must be outside the public checkout")
        self.team, self.binding = team, binding

    def _bind(self):
        self.team._authorize(self.binding)
        profile = json.loads(local_path(self.root, ".agent-profile.json").read_text())
        if profile != {"role": self.binding.agent_id}:
            raise AccessDenied("Workspace role mismatch")
        path = local_path(self.root, ".agent-binding.json")
        expected = self.binding.scope().as_dict()
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(expected))
            path.chmod(0o600)
        except FileExistsError:
            pass
        if json.loads(path.read_text()) != expected:
            raise AccessDenied("Workspace belongs to another owner or project")

    def _materials(self):
        marker = json.loads(local_path(self.root, ".agent-profile.json").read_text())
        if marker != {"role": self.binding.agent_id}:
            raise AccessDenied("Workspace role mismatch")
        materials = {}
        for name in FILES:
            path = local_path(self.root, name)
            if path.stat().st_size > 100_000:
                raise MemoryError("Workspace material too large")
            materials[name] = path.read_text(encoding="utf-8")
        if self.binding.agent_id.startswith("neona-") and self.root.parent.name == "channels":
            common = local_path(self.root.parent.parent, "Agent.md")
            materials["profile:neona"] = common.read_text(encoding="utf-8")
        for name in ("AGENCY_W_CORE.md", *PROFILES[self.binding.agent_id][2]):
            materials["source:" + name] = local_path(
                self.repository_root, name).read_text(encoding="utf-8")
        return materials

    def begin(self, task_id, request):
        self._bind()
        context = self.team.context(self.binding)
        materials = safe_content(self._materials(), max_bytes=1_000_000)
        snapshot = self.team._memory(self.binding).prepare_task(
            task_id, {"request": request, "materials_hash": digest(materials)},
            key="workspace-task:" + task_id)
        entries = context["project"]["knowledge"]
        checks = {}
        for entry in entries:
            if entry["kind"] == "check":
                for related in entry["related"]:
                    evidence = entry["evidence"]
                    revision = evidence.get("checked_revision")
                    if (type(revision) is int and 0 <= revision <= context["project"]["revision"]
                            and revision > checks.get(related, {}).get("checked_revision", -1)):
                        checks[related] = evidence
        experience = []
        for entry in entries:
            check = checks.get(entry["id"], {})
            if (entry["kind"] in ("correction", "execution")
                    and check.get("passed") is True
                    and check.get("entry_hash") == digest(entry["text"])):
                try:
                    event = json.loads(entry["text"])
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict) and event.get("scope") == self.binding.scope().as_dict():
                    experience.append(event)
        completed_id = digest({
            "scope": self.binding.scope().as_dict(),
            "key": "team:success:task:" + task_id,
        })[:32]
        completed = next((entry for entry in entries if entry["id"] == completed_id), None)
        result = json.loads(completed["text"])["result"] if completed else None
        return {"materials": materials, "memory": context,
                "started_at": snapshot["created_at"], "completed_result": result,
                "verified_experience": experience,
                "boundary": "Experience is scoped evidence, not new permissions or general rules."}

    def finish(self, outcome, event_id, event):
        self._bind()
        required = {
            "date", "task", "expected", "result", "assessment", "source",
            "cause", "cause_status", "correction", "check", "next_application",
        }
        if (not isinstance(event, dict) or set(event) != required
                or any(not isinstance(value, str) for value in event.values())):
            raise MemoryError("Complete structured experience event required")
        datetime.fromisoformat(event["date"])
        if event["assessment"] not in {"success", "error", "unknown"}:
            raise MemoryError("Invalid assessment")
        if event["cause_status"] not in {"fact", "hypothesis", "unknown"}:
            raise MemoryError("Invalid cause status")
        text = json.dumps({**event, "scope": self.binding.scope().as_dict()},
                          ensure_ascii=False, sort_keys=True)
        return self.team.record_outcome(
            self.binding, outcome, text, source=event["source"], event_id=event_id)

    def check(self, outcome, event_id, *, reviewer, passed, source):
        """Reviewer is a principal from trusted owner/verifier authentication."""
        self._bind()
        if type(passed) is not bool or reviewer.kind not in {"owner", "verifier"}:
            raise AccessDenied("Trusted review required")
        memory = self.team._memory(self.binding)
        reviewed = Memory(memory.repository, reviewer, memory.scope)
        entry_id = digest({"scope": memory.scope.as_dict(),
                           "key": "team:" + outcome + ":" + event_id})[:32]
        snapshot = reviewed.read()
        entries = snapshot["document"]["entries"]
        entry = entries.get(entry_id)
        if not entry:
            raise MemoryError("Experience event is missing")
        key = "workspace-check:" + outcome + ":" + event_id + ":" + digest([passed, source])
        check_id = digest({"scope": memory.scope.as_dict(), "key": key})[:32]
        if check_id in entries:
            if entries[check_id]["evidence"].get("entry_hash") != digest(entry["text"]):
                raise MemoryError("Review was for another experience version")
            return snapshot
        return reviewed.record(
            "check", "Experience check", source=source,
            key=key, expected=snapshot["revision"],
            related=[entry_id], evidence={"passed": passed, "entry_hash": digest(entry["text"]),
                                         "checked_revision": snapshot["revision"]},
        )
