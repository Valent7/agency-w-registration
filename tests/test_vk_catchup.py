"""Regression tests for the no-extra-hosting VK missed-callback recovery."""
import ast
import unittest
from pathlib import Path
from vk_catchup import recent_user_messages, latest_unanswered_history


class VKCatchupTests(unittest.TestCase):
    def test_only_recent_inbound_user_text(self):
        now = 1794479999
        def item(from_id, message_id, *, peer=None, kind="user", out=0, age=20, text="18:00"):
            return {
                "conversation": {"peer": {"type": kind, "id": peer or from_id}},
                "last_message": {"id": message_id, "from_id": from_id, "peer_id": peer or from_id,
                                 "date": now-age, "out": out, "text": text},
            }
        raw = {"response": {"items": [
            item(125, 90, age=50),
            item(125, 90, age=50),
            item(130, 91, out=1),
            item(131, 92, kind="chat"),
            item(132, 93, age=3601),
            item(133, 94, text=""),
            item(134, 95, peer=135),
            item(145, 96, age=4, text="Перенесём на 18:00 МСК"),
        ]}}
        found = recent_user_messages(raw, now_ts=now)
        self.assertEqual([x["id"] for x in found], [90, 96])
        self.assertEqual(found[1]["text"], "Перенесём на 18:00 МСК")

    def test_bad_payload_is_ignored(self):
        self.assertEqual(recent_user_messages({"response": {"items": None}}), [])
        self.assertEqual(recent_user_messages({"error": {"error_code": 5}}), [])
        self.assertEqual(recent_user_messages({"response": {"items": [None, 0, {}]}}), [])


    def test_latest_unanswered_history_is_safe(self):
        now = 1794480063 + 30
        incoming = {"id": 77, "from_id": 187654, "peer_id": 187654,
                    "date": now - 20, "out": 0, "text": "Сегодня в 18:00 МСК"}
        reply = {"id": 78, "from_id": -77777, "peer_id": 187654,
                 "date": now - 10, "out": 1, "text": "Подтверждаем"}
        self.assertEqual(latest_unanswered_history(
            {"response": {"items": [incoming]}}, 187654,
            now_ts=now)["id"], 77)
        self.assertIsNone(latest_unanswered_history(
            {"response": {"items": [reply, incoming]}}, 187654,
            now_ts=now))
        self.assertIsNone(latest_unanswered_history(
            {"response": {"items": [incoming]}}, 12345, now_ts=now))
        self.assertIsNone(latest_unanswered_history(
            {"response": {"items": [{**incoming, "date": now - 1000}]}},
            187654, now_ts=now))

    def test_history_poll_is_limited_to_known_leads(self):
        source = (Path(__file__).resolve().parents[1] / "instagram_webhook.py").read_text("utf-8")
        tree = ast.parse(source)
        fn = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_vk_catchup_recent_inbound")
        body = ast.get_source_segment(source, fn)
        self.assertIn('"agency_vk_leads"', body)
        self.assertIn('"messages.getHistory"', body)
        self.assertIn("latest_unanswered_history", body)

    def test_vk_webhook_uses_same_serialized_handler_as_poll(self):
        source = (Path(__file__).resolve().parents[1] / "instagram_webhook.py").read_text("utf-8")
        tree = ast.parse(source)
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("_process_vk_message", names)
        self.assertIn("_process_vk_message_unlocked", names)
        self.assertIn("_vk_catchup_recent_inbound", names)
        self.assertIn("_vk_lock_for", names)
        self.assertIn("_vk_catchup_loop", {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)})
        handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_process_vk_message")
        self.assertIn("with _vk_lock_for(peer_id):", ast.get_source_segment(source, handler))
        self.assertNotIn('print("VK_WEBHOOK_EVENT:", payload', source)


if __name__ == "__main__":
    unittest.main()
