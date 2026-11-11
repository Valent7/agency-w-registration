"""Offline tests of the editorial memory: no API keys and no external services."""
import json
import tempfile
import unittest
from pathlib import Path

from journal_editor import memory


class JournalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"

    def event(self, event_id="event-01", stage="draft_created"):
        return {
            "id": event_id, "date": "2026-11-11", "project": "journal",
            "slug": "example-article", "actor": "Журналист",
            "stage": stage, "status": "confirmed_by_record",
            "notes": "Написана статья про время и золотой песок.",
            "evidence": ["https://example.org/evidence"],
        }

    def test_record_roundtrip(self):
        item = self.event()
        memory.append_record(item, self.path)
        self.assertEqual(memory.read_events(self.path), [item])

    def test_duplicate_id_is_rejected(self):
        memory.append_record(self.event(), self.path)
        with self.assertRaises(memory.MemoryError):
            memory.append_record(self.event(), self.path)

    def test_secret_is_rejected(self):
        item = self.event()
        item["notes"] = "secret_key=not-real-but-should-not-be-saved"
        with self.assertRaises(memory.MemoryError):
            memory.append_record(item, self.path)

    def test_no_evidence_rejected(self):
        item = self.event()
        item["evidence"] = []
        with self.assertRaises(memory.MemoryError):
            memory.append_record(item, self.path)

    def test_published_only_with_feed(self):
        event = self.event(stage="published")
        state = memory.make_state([], [event])
        self.assertFalse(state[0]["published"])
        self.assertNotIn("опубликовано (по feed.json)", state[0]["status"])
        state = memory.make_state(
            [{"slug": "example-article", "title": "Опубликованная статья",
              "url": "editorial/example-article.html", "date": "2026-11-11"}],
            [event])
        self.assertTrue(state[0]["published"])
        self.assertIn("опубликовано", state[0]["status"])

    def test_find_searches_simple_related_words(self):
        state = memory.make_state([], [self.event()])
        found = memory.search("песок и минуты", state)
        self.assertEqual(found[0]["slug"], "example-article")

    def test_unconfirmed_next_step(self):
        state = memory.make_state([], [self.event()])
        self.assertIn("Проверить текст", state[0]["next"])

    def test_corrupt_event_rejected(self):
        self.path.write_text('{"bad": invalid}\n', encoding="utf-8")
        with self.assertRaises(memory.MemoryError):
            memory.read_events(self.path)

    def test_actual_feed_matches_known_article(self):
        items = memory.read_feed()
        self.assertTrue(any(a["slug"] == "kak-ponyat-kuda-uhodit-vremya" for a in items))
        state = memory.make_state(items, memory.read_events())
        self.assertTrue(any(x["published"] for x in state))


if __name__ == "__main__":
    unittest.main()
