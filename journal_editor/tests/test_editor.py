"""Fast, offline tests: no OpenAI calls and no Render deployments."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journal_editor import editor


def sample_article():
    return {
        "title": "Как разумно освободить время для важных дел",
        "summary": "Разбираем, какие повседневные действия стоит пересмотреть, чтобы оставить больше внимания для семьи, отдыха и действительно важных задач.",
        "paragraphs": [
            f"Абзац {i}. Когда мы начинаем внимательно смотреть на обычный день, заметнее становятся повторяющиеся действия. Небольшие изменения можно проверить без спешки. "
            for i in range(6)
        ],
    }


class EditorialChecks(unittest.TestCase):
    def test_valid_article(self):
        editor.validate(sample_article())

    def test_invalid_claim_is_rejected(self):
        article = sample_article()
        article["paragraphs"][0] += " Мы гарантируем доход каждому."
        with self.assertRaises(editor.EditorialError):
            editor.validate(article)

    def test_html_escaped(self):
        article = sample_article()
        article["title"] = 'Проверка <опасного> заголовка для публикации'
        topic = {"slug": "example-title", "category": "Практика"}
        page = editor.page_html(article, topic, "2026-11-11")
        self.assertIn("&lt;опасного&gt;", page)
        self.assertNotIn("<опасного>", page)
        self.assertIn('rel="canonical"', page)

    def test_publish_disabled_by_default_and_requires_explicit_activation(self):
        article = sample_article()
        topic = {"slug": "example-title", "category": "Практика"}
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "journal-public"
            folder = site / "editorial"
            folder.mkdir(parents=True)
            feed = folder / "feed.json"
            feed.write_text('{"version":1,"articles":[]}', encoding="utf-8")
            with mock.patch.object(editor, "SITE", site), mock.patch.object(editor, "FEED", feed):
                with mock.patch.dict(os.environ, {"JOURNAL_AUTOPUBLISH":""}):
                    with self.assertRaises(editor.EditorialError):
                        editor.publish(article, topic)
                self.assertFalse((folder / "example-title.html").exists())
                with mock.patch.dict(os.environ, {"JOURNAL_AUTOPUBLISH":"1"}):
                    path = editor.publish(article, topic)
                    self.assertTrue(path.exists())
                    data = json.loads(feed.read_text(encoding="utf-8"))
                    self.assertEqual(data["articles"][0]["slug"], "example-title")
                    with self.assertRaises(editor.EditorialError):
                        editor.publish(article, topic)

    def test_review_topic_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            topics = base / "topics.json"
            feed = base / "feed.json"
            drafts = base / "drafts"
            drafts.mkdir()
            topics.write_text(json.dumps({"topics":[
                {"slug":"alpha","question":"Question A","category":"Тема"},
                {"slug":"beta","question":"Question B","category":"Тема"},
                {"slug":"gamma","question":"Question C","category":"Тема"}]},ensure_ascii=False),encoding="utf-8")
            feed.write_text(json.dumps({"articles":[{"slug":"alpha"}]}), encoding="utf-8")
            (drafts/"beta.json").write_text("{}",encoding="utf-8")
            with mock.patch.object(editor,"TOPICS",topics),mock.patch.object(editor,"FEED",feed),mock.patch.object(editor,"DRAFTS",drafts):
                self.assertEqual(editor.choose_topic()["slug"],"gamma")


if __name__ == "__main__":
    unittest.main()
