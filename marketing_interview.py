"""Owner-only draft interview. Published agent knowledge is stored separately."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID


BLOCKS = json.loads(Path(__file__).with_name("marketing_interview_questions.json").read_text(
    encoding="utf-8"
))
QUESTION_IDS = tuple(question["id"] for block in BLOCKS for question in block["questions"])


def pilot_project(owner_id, configured_owner, configured_project):
    try:
        owner = int(configured_owner)
        project = UUID(configured_project)
    except (ValueError, TypeError, AttributeError):
        return None
    if owner > 0 and owner == int(owner_id) and project.int:
        return str(project)
    return None


def change_answer(state, question_id, answer):
    if question_id not in QUESTION_IDS:
        raise ValueError("Unknown interview question")
    if answer is not None:
        answer = answer.strip()
        if not 0 < len(answer) <= 2000:
            raise ValueError("Enter an answer of up to 2,000 characters")
    answers = dict(state["answers"])
    answers[question_id] = answer
    block_id = question_id.rsplit("-", 1)[0]
    confirmed = [block for block in state["confirmed"] if block != block_id]
    return {**state, "answers": answers, "confirmed": confirmed}


class InterviewStore:
    def __init__(self, request, owner_id, project_id, encrypt, decrypt):
        self.request = request
        self.filters = {"owner_id": f"eq.{int(owner_id)}", "project_id": f"eq.{UUID(project_id)}"}
        self.identity = {"owner_id": int(owner_id), "project_id": str(UUID(project_id))}
        self.encrypt = encrypt
        self.decrypt = decrypt

    def load(self):
        projects = self.request("GET", "content_factory_projects", params={
            "id": self.filters["project_id"], "owner_id": self.filters["owner_id"], "select": "id",
        })
        if len(projects) != 1:
            raise LookupError("Interview project is unavailable")
        rows = self.request("GET", "marketing_interviews", params={
            **self.filters, "select": "revision,encrypted_state",
        })
        if not rows:
            return 0, {"schema": 1, "answers": {}, "confirmed": []}
        row = rows[0]
        state = self.decrypt(row["encrypted_state"])
        if state.get("schema") != 1:
            raise ValueError("Interview could not be decrypted")
        return row["revision"], state

    def save(self, revision, state):
        payload = {
            "revision": revision + 1,
            "encrypted_state": self.encrypt(state),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if revision == 0:
            rows = self.request("POST", "marketing_interviews", json={**self.identity, **payload})
        else:
            rows = self.request("PATCH", "marketing_interviews", json=payload, params={
                **self.filters, "revision": f"eq.{revision}",
            })
        if len(rows) != 1:
            raise RuntimeError("Interview was updated in another session")
