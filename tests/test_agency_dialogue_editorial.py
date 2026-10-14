"""Exercise the full editorial draft -> Stagirite approval pipeline offline."""
import ast
import json
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agency_dialogue_video import dialogue_errors, normalise_dialogue_turns

SOURCE = Path("content_factory_ui.py").read_text(encoding="utf-8")
tree = ast.parse(SOURCE)
functions = {
    "_normalise_item",
    "_single_content_prompt",
    "_single_item_errors",
    "_generate_single_package",
    "_stagirite_review",
}
fn_nodes = [
    n for n in tree.body
    if isinstance(n, ast.FunctionDef) and n.name in functions
]
env = {
    "re": re, "json": json, "datetime": datetime, "timezone": timezone,
    "uuid": uuid, "normalise_dialogue_turns": normalise_dialogue_turns,
    "dialogue_errors": dialogue_errors,
    "_clean_text": lambda val, lim=12000: str(val or "").strip()[:lim],
    "_json_from_answer": lambda value: json.loads(value),
    "PROFESSIONAL_PORTRAITS": {"Неона": ""},
    "FORMAT_LABELS": {"dialogue": "🎭 ИИ-диалог"},
    "FACTORY_SYSTEM_PROMPT": "Контент-завод W",
}
exec(compile(ast.Module(body=fn_nodes, type_ignores=[]), "content_factory_ui.py", "exec"), env)

TURNS = [
    {"speaker": "A", "text": "Ты же ещё вчера сама искала клиентов и писала сообщения до позднего вечера. Что изменилось?"},
    {"speaker": "B", "text": "Я попробовала Агентство W. Помощники помогают искать людей и поддерживать деловые разговоры. Это экономит время."},
    {"speaker": "A", "text": "И ты можешь теперь больше внимания уделять встречам и близким, вместо того чтобы листать чаты?"},
    {"speaker": "B", "text": "Именно. Рутина перестала съедать весь мой день. А на что ты потратил бы освободившиеся часы?"},
]
ITEM = {
    "id": "content_1", "format": "dialogue", "title": "Время для жизни",
    "goal": "Показать пользу делегирования рутины",
    "caption": "Вы замечали, сколько времени забирает переписка?",
    "cta": "Напишите, какую рутину вы хотели бы передать",
    "dialogue": TURNS,
}


class DialogueEditorialPipelineTests(unittest.TestCase):
    def test_generated_prompt_has_json_example_not_python_repr(self):
        brief = {
            "format": "dialogue", "topic": "Неона и свободное время",
            "main_idea": "Время — ценность",
            "original_request": "Сделай диалог о времени",
        }
        prompt = env["_single_content_prompt"](brief)
        self.assertIn('"dialogue": [{"speaker":"A"', prompt)
        self.assertNotIn("[{'speaker':", prompt)
        self.assertIn("по очереди", prompt)
        self.assertIn("ВЫБРАННЫЙ", prompt)

    def test_draft_and_review_keep_all_dialogue_turns(self):
        brief = {
            "format": "dialogue", "topic": "Делегирование рутины",
            "main_idea": "Время человеку", "audience": "Предприниматели",
            "cta": ITEM["cta"], "original_request": "ИИ-диалог Агентства W",
        }
        calls = []

        def fake_ai(system, prompt):
            calls.append(system)
            if "финальный контролёр" in system:
                return json.dumps({"summary": "Сценарий готов", "item": ITEM}, ensure_ascii=False)
            return json.dumps({"item": ITEM}, ensure_ascii=False)

        package = env["_generate_single_package"](15, brief, fake_ai)
        self.assertEqual(package["workflow_version"], "single_v2")
        self.assertEqual(package["items"][0]["format"], "dialogue")
        self.assertEqual(package["items"][0]["dialogue"], TURNS)
        self.assertFalse(dialogue_errors(package["items"][0]["dialogue"]))

        reviewed = env["_stagirite_review"](package, package["items"][0], fake_ai)
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(reviewed["dialogue"], TURNS)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
