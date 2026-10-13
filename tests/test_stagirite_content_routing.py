"""Offline regression tests for Stagirite editorial routing and the media desk.

No Telegram, Supabase, OpenAI, Claude, Zoom or Streamlit calls are made.
"""
import ast
import datetime as dt
import re
import unittest
from pathlib import Path
from typing import Any

SOURCE = Path("stagirite_center.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

NAMES = {
    "_editorial_output_requested",
    "_zoom_link_stays_in_cabinet",
    "_detect_intents",
    "_needs_semantic_director",
    "_normalize_stagirite_intents",
    "_content_mode_from_assignment",
    "_announcement_time_hint",
    "_generate_announcement_content",
    "_process_assignment",
}
FUNCS = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
NS = {
    "re": re,
    "Any": Any,
    "datetime": dt.datetime,
    "UTC": dt.timezone.utc,
    "STAGIRITE_EXECUTION_INTENTS": ("meetings", "content", "team", "general"),
    "CONTENT_MASTER_CORE": "You are a responsible copywriter.",
    "_load_stagirite_settings": lambda owner_id: {"zoom_link": "https://us06web.zoom.us/j/987654321?pwd=SECRET"},
    "_make_plan": lambda intents, count, **kwargs: [],
    "_generate_content": lambda ask_openai_fn, ask_claude_fn, owner_id, owner_name, assignment: {
        "text": "⏰ Через 30 минут встречаемся в ZOOM на встрече «Вопросы — Ответы». Ссылка в кабинете Агентства W.",
        "master_engine": "test",
    },
}
exec(compile(ast.Module(body=FUNCS, type_ignores=[]), "stagirite_center.py", "exec"), NS)

ASSIGNMENT = (
    'Напиши напоминание, что через полчаса начнётся наша встреча '
    'в ZOOM "Вопросы - Ответы" Ссылка в кабинете Агентства W'
)


class StagiriteContentRoutingTests(unittest.TestCase):
    def classify(self, prompt):
        return NS["_detect_intents"](prompt)

    def test_reported_bug_is_editorial_not_meeting_campaign(self):
        self.assertEqual(self.classify(ASSIGNMENT), ["content"])
        self.assertFalse(NS["_needs_semantic_director"](ASSIGNMENT, ["content"]))
        self.assertEqual(NS["_content_mode_from_assignment"](ASSIGNMENT), "announcement")

    def test_synonymous_write_requests(self):
        for prompt in (
            "Составь напоминание для участников о встрече в Zoom",
            "Сделай объявление о завтрашней Zoom-встрече",
            "Подготовь анонс сегодняшней Zoom-встречи",
            "Напиши пост о встрече с партнёрами",
            "Создай иллюстрацию для анонса",
            "Сделай видеоролик о времени для семьи",
        ):
            with self.subTest(prompt=prompt):
                self.assertIn("content", self.classify(prompt))
                self.assertNotIn("meetings", self.classify(prompt))

    def test_actual_organize_meeting_still_uses_calendar(self):
        for prompt in (
            "Организуй Zoom-встречу с кандидатом",
            "Назначь встречу с клиентом",
            "Согласуй встречу с партнёром",
            "Найди время для созвона",
        ):
            with self.subTest(prompt=prompt):
                self.assertIn("meetings", self.classify(prompt))

    def test_combined_content_and_explicit_booking_can_use_both(self):
        result = self.classify("Организуй Zoom-встречу и напиши анонс")
        self.assertIn("meetings", result)
        self.assertIn("content", result)

    def test_process_does_not_contact_candidates_for_reminder(self):
        NS["_find_meeting_day"] = lambda *args, **kwargs: self.fail("Calendar booking must not run")
        NS["_candidate_snapshot"] = lambda *args, **kwargs: self.fail("Neonia must not run")
        task = NS["_process_assignment"](
            123,
            "Тестовый директор",
            ASSIGNMENT,
            lambda *args, **kwargs: "Текст",
            ask_claude_fn=None,
        )
        self.assertEqual(task["task_kind"], "content")
        self.assertTrue(task["result"]["content"])
        self.assertNotIn("meetings", task["result"])
        self.assertIn("content_master", task["result"])

    def test_no_cabinet_only_zoom_link_leak(self):
        NS["_load_stagirite_settings"] = lambda owner_id: {
            "zoom_link": "https://us06web.zoom.us/j/987654321?pwd=SECRET"
        }
        raw = (
            "Друзья, через полчаса Zoom. "
            "https://us06web.zoom.us/j/987654321?pwd=SECRET"
        )
        result = NS["_generate_announcement_content"](
            lambda *args, **kwargs: raw,
            lambda *args, **kwargs: {"ok": True, "text": raw, "model": "stub"},
            123, "Директор", ASSIGNMENT,
        )
        self.assertNotIn("zoom.us", result["text"])
        self.assertIn("кабинете Агентства W", result["text"])
        self.assertTrue(result["announcement"])

    def test_normal_zoom_announcement_can_have_link(self):
        result = NS["_generate_announcement_content"](
            None,
            lambda *args, **kwargs: {"ok": True, "text": "До встречи!", "model": "stub"},
            123, "Директор", "Напиши анонс встречи в Zoom сегодня в 17:00 МСК",
        )
        self.assertIn("https://us06web.zoom.us/j/987654321", result["text"])

    def test_legacy_task_can_be_repaired_and_media_tools_present(self):
        self.assertIn("🔄 Исправить маршрут: передать Мастеру контента", SOURCE)
        self.assertIn("Загрузить картинку", SOURCE)
        self.assertIn("Загрузить ролик", SOURCE)
        self.assertIn("🎨 Художник", SOURCE)
        self.assertIn("Утвердить материал", SOURCE)
        self.assertIn("Мастер контента", SOURCE)
        self.assertIn("_editorial_output_requested(assignment_text)", SOURCE)


if __name__ == "__main__":
    unittest.main()
