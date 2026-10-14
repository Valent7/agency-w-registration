"""Offline regression checks: malicious Meta webhook events cannot reach Neona."""
import ast
import asyncio
import hashlib
import hmac
import json
import unittest
from pathlib import Path

from instagram_webhook_security import (
    MAX_WEBHOOK_BYTES,
    safe_instagram_event_summary,
    valid_instagram_signature,
)

SECRET = "a_private_meta_app_secret_from_render"
INBOUND = {
    "object": "instagram",
    "entry": [{
        "id": "17841458786862089",
        "messaging": [{
            "sender": {"id": "2506488783198151"},
            "recipient": {"id": "17841458786862089"},
            "message": {"mid": "not-secret-id", "text": "ПРИВАТНАЯ ПЕРЕПИСКА"},
        }],
    }],
}
RAW = json.dumps(INBOUND, ensure_ascii=False).encode("utf-8")


def sign(raw):
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


class SecurityPrimitivesTests(unittest.TestCase):
    def test_only_raw_body_hmac_is_valid(self):
        self.assertTrue(valid_instagram_signature(RAW, sign(RAW), SECRET))
        self.assertFalse(valid_instagram_signature(RAW + b" ", sign(RAW), SECRET))
        self.assertFalse(valid_instagram_signature(RAW, sign(RAW), "other-app-secret"))
        self.assertFalse(valid_instagram_signature(RAW, "sha256=not-hex", SECRET))

    def test_missing_signature_or_secret_is_rejected(self):
        for header in ("", "sha256=", "sha1=" + "a" * 40, None):
            self.assertFalse(valid_instagram_signature(RAW, header, SECRET))
        self.assertFalse(valid_instagram_signature(RAW, sign(RAW), ""))

    def test_summary_keeps_no_private_text_or_sender_ids(self):
        summary = safe_instagram_event_summary(INBOUND)
        self.assertEqual(summary["messages"], 1)
        self.assertEqual(summary["entries"], 1)
        data = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("ПРИВАТНАЯ", data)
        self.assertNotIn("2506488783198151", data)
        self.assertNotIn("17841458786862089", data)


SOURCE = Path("instagram_webhook.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HANDLER = next(
    node for node in TREE.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "instagram_webhook_receive"
)


class Response:
    def __init__(self, value, status_code=200, background=None):
        self.value, self.status_code, self.background = value, status_code, background


class FakeRequest:
    def __init__(self, raw=RAW, signature=None, length=None):
        self._raw = raw
        self.headers = {}
        if signature is not None:
            self.headers["x-hub-signature-256"] = signature
        if length is not None:
            self.headers["content-length"] = str(length)

    async def body(self):
        return self._raw


class InstagramWebhookHandlerTests(unittest.TestCase):
    def env(self, secret=SECRET):
        jobs = []
        logs = []
        ns = {
            "Request": object,
            "json": json,
            "INSTAGRAM_APP_SECRET": secret,
            "MAX_WEBHOOK_BYTES": MAX_WEBHOOK_BYTES,
            "valid_instagram_signature": valid_instagram_signature,
            "safe_instagram_event_summary": safe_instagram_event_summary,
            "PlainTextResponse": Response,
            "BackgroundTask": lambda handler, payload: jobs.append((handler, payload)),
            "_process_instagram_payload": lambda payload: None,
            "print": lambda *args, **kwargs: logs.append(args),
        }
        exec(compile(ast.Module(body=[HANDLER], type_ignores=[]), "instagram_webhook.py", "exec"), ns)
        return ns, jobs, logs

    def test_unsigned_event_is_forbidden_no_background_dispatch(self):
        ns, jobs, logs = self.env()
        response = asyncio.run(ns["instagram_webhook_receive"](FakeRequest()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(jobs, [])
        self.assertNotIn("ПРИВАТНАЯ", str(logs))

    def test_forged_event_cannot_trigger_reply(self):
        ns, jobs, _ = self.env()
        forged = RAW.replace("ПРИВАТНАЯ".encode(), "ВЗЛОМ".encode())
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=forged, signature=sign(RAW))
        ))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(jobs, [])

    def test_genuine_meta_event_acknowledged_and_dispatched_once(self):
        ns, jobs, logs = self.env()
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=RAW, signature=sign(RAW))
        ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][1]["object"], "instagram")
        self.assertNotIn("ПРИВАТНАЯ", str(logs))
        self.assertNotIn("2506488783198151", str(logs))

    def test_server_must_have_app_secret(self):
        ns, jobs, _ = self.env(secret="")
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=RAW, signature=sign(RAW))
        ))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(jobs)

    def test_oversized_body_dropped_before_processing(self):
        ns, jobs, _ = self.env()
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=RAW, signature=sign(RAW), length=MAX_WEBHOOK_BYTES + 1)
        ))
        self.assertEqual(response.status_code, 413)
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=b"x" * (MAX_WEBHOOK_BYTES + 1), signature=sign(RAW))
        ))
        self.assertEqual(response.status_code, 413)
        self.assertFalse(jobs)

    def test_signed_malformed_or_wrong_object_rejected(self):
        ns, jobs, _ = self.env()
        raw = b"{invalid"
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=raw, signature=sign(raw))
        ))
        self.assertEqual(response.status_code, 400)
        raw = b'{"object":"page","entry":[]}'
        response = asyncio.run(ns["instagram_webhook_receive"](
            FakeRequest(raw=raw, signature=sign(raw))
        ))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(jobs)

    def test_private_message_logging_removed(self):
        self.assertNotIn('"incoming": text', SOURCE)
        self.assertNotIn('"draft": reply_text', SOURCE)
        self.assertNotIn('"reply": reply_text', SOURCE)
        self.assertNotIn('print("INSTAGRAM_WEBHOOK_EVENT:", payload', SOURCE)


if __name__ == "__main__":
    unittest.main()
