"""Regression checks for first-message style without starting Streamlit UI."""
import ast
import re
import json
import unittest
from types import SimpleNamespace
from pathlib import Path


def load_editor_functions():
    source = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {
        "candidate_first_name",
        "normalize_neona_first_greeting",
        "neona_first_message_blockers",
        "generate_neona_first_message",
    }
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in selected} == names
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re, "json": json}
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

    def test_bad_ai_drafts_fail_closed_without_generic_promotion(self):
        namespace = dict(self.fns)
        calls = []
        def fake_api(prompt, request, model=None):
            calls.append(model)
            return ("Здравствуйте! У нас бесплатный тест ИИ, "
                    "мы покажем ваши компенсационные планы и монетизацию.")
        namespace.update({
            "st":SimpleNamespace(secrets={"NEONA_FIRST_MESSAGE_MODEL":"gpt-6-astra"}),
            "build_neona_first_message_system_prompt":lambda owner:"quality first",
            "choose_neona_magnet":lambda contact:"Вернуть человеку время",
            "finalize_neona_first_message":lambda msg,owner,contact:msg,
            "ask_openai":fake_api,
        })
        func = self.fns["generate_neona_first_message"]
        isolated = type(func)(func.__code__,namespace)
        with self.assertRaisesRegex(RuntimeError,"Черновик не создан"):
            isolated("Валентина",{"target":"time"},
                     {"telegram_id":21,"first_name":"Ирина","name":"Ирина"})
        self.assertEqual(calls,["gpt-6-astra","gpt-6-astra"])

    def test_valid_ai_first_message_uses_strong_model(self):
        namespace = dict(self.fns)
        chosen = []
        def fake_api(prompt, request, model=None):
            chosen.append(model)
            return ("Ирина, здравствуйте! Я Неона, секретарь-референт. "
                    "Мы помогаем сократить рутину. Вам интересно?")
        namespace.update({
            "st":SimpleNamespace(secrets={}),
            "build_neona_first_message_system_prompt":lambda owner:"quality first",
            "choose_neona_magnet":lambda contact:"Вернуть человеку время",
            "finalize_neona_first_message":lambda msg,owner,contact:msg,
            "ask_openai":fake_api,
        })
        func = self.fns["generate_neona_first_message"]
        isolated = type(func)(func.__code__,namespace)
        result=isolated("Валентина",{"target":"time"},
                        {"telegram_id":22,"first_name":"Ирина","name":"Ирина"})
        self.assertIn("Мы помогаем",result)
        self.assertEqual(chosen,["gpt-6-astra"])

    def test_factual_and_length_gates(self):
        check = self.fns["neona_first_message_blockers"]
        self.assertFalse(check(
            "Здравствуйте! Я Неона, секретарь-референт. "
            "Помогаем освободить время от рутины. Интересно узнать больше?"
        ))
        self.assertTrue(check("Давайте сравним компенсационные планы и монетизацию."))
        self.assertTrue(check("У нас есть бесплатный тест вашего AI-ассистента."))
        self.assertTrue(check("А" * 361))
        self.assertTrue(check("Первый вопрос? Второй вопрос?"))


if __name__ == "__main__":
    unittest.main()
