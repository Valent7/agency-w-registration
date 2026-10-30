"""Regression checks for first-message style without starting Streamlit UI."""
import ast
import re
import unittest
from pathlib import Path


def load_editor_functions():
    source = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {
        "candidate_first_name",
        "normalize_neona_first_greeting",
        "neona_first_message_blockers",
    }
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in selected} == names
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace


class FirstMessageQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fns = load_editor_functions()

    def test_repeated_full_name_after_greeting_removed(self):
        contact = {
            "first_name": "Валентина",
            "name": "Валентина Якусевич",
            "username": "yava369",
        }
        text = (
            "Валентина, здравствуйте! Валентина Якусевич. "
            "Я Неона, секретарь-референт. Хотели бы узнать больше?"
        )
        result = self.fns["normalize_neona_first_greeting"](text, contact)
        self.assertEqual(result.count("Валентина"), 1)
        self.assertNotIn("Якусевич.", result)

    def test_factual_and_length_gates(self):
        check = self.fns["neona_first_message_blockers"]
        self.assertFalse(check(
            "Здравствуйте! Я Неона, секретарь-референт. "
            "Помогаем освободить время от рутины. Интересно узнать больше?"
        ))
        self.assertTrue(check("Давайте сравним компенсационные планы и монетизацию."))
        self.assertTrue(check("У нас есть бесплатный тест вашего AI-ассистента."))
        self.assertTrue(check("А" * 481))
        self.assertTrue(check("Первый вопрос? Второй вопрос?"))


if __name__ == "__main__":
    unittest.main()
