"""Offline safety tests: never use real Telegram sessions, DB or OpenAI."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import neona_realtime_worker as realtime
import neona_telegram_dialogs as core


class FakeClient:
    def __init__(self):
        self.disconnects = 0
        self.dialog_reads = 0

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=101)

    async def iter_dialogs(self, limit=None):
        self.dialog_reads += 1
        raise AssertionError("Targeted mode must not enumerate all dialogs")
        yield  # pragma: no cover

    async def disconnect(self):
        self.disconnects += 1


class NeonaRealtimeTests(unittest.TestCase):
    def test_owner_id_parser(self):
        self.assertEqual(realtime._owner_ids("123, 456, ,bad, 123"), {123, 456})
        self.assertEqual(realtime._owner_ids(""), set())

    def test_cold_start_uses_polling_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(realtime.launch_from_environment(300))

    def test_ready_only_after_explicit_confirmation(self):
        pilots = realtime.RealtimePilots({101, 202}, poll_seconds=300)
        self.assertFalse(pilots.is_ready(101))
        pilots._set_ready(101, True)
        self.assertTrue(pilots.is_ready(101))
        self.assertFalse(pilots.is_ready(202))
        pilots._set_ready(101, False)
        self.assertFalse(pilots.is_ready(101))

    def test_realtime_events_are_coalesced_and_fallback_restored(self):
        class StubClient:
            last = None

            def __init__(self, *args, **kwargs):
                StubClient.last = self
                self.connected = False
                self.handler = None

            def on(self, _):
                def decorator(callback):
                    self.handler = callback
                    return callback
                return decorator

            async def connect(self):
                self.connected = True

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return SimpleNamespace(id=101)

            def is_connected(self):
                return self.connected

            async def disconnect(self):
                self.connected = False

        class FakeEvent:
            is_private = True
            sender_id = 555
            message = SimpleNamespace(message="Hello", voice=False, audio=False, document=None)

            async def get_sender(self):
                return SimpleNamespace(id=555, first_name="User", bot=False)

        calls = []
        pilot = realtime.RealtimePilots({101}, poll_seconds=300)

        async def fake_scan(owner_id, owner_name, client, target_entities=None):
            calls.append(None if target_entities is None else [e.id for e in target_entities])
            if len(calls) == 1:
                # Two Telegram updates must cause only one targeted scan.
                await client.handler(FakeEvent())
                await client.handler(FakeEvent())
            else:
                self.assertTrue(pilot.is_ready(101))
                client.connected = False
            return {"processed": 0, "replied": 0, "errors": 0}

        config = SimpleNamespace(telegram_api_id=1, telegram_api_hash="unused")
        with (
            patch.object(core, "load_config", return_value=config),
            patch.object(core, "_get_telegram_session", return_value="session"),
            patch.object(realtime, "StringSession", side_effect=lambda x: x),
            patch.object(realtime, "TelegramClient", StubClient),
            patch.object(pilot, "_scan", side_effect=fake_scan),
        ):
            asyncio.run(pilot._listen(101, "Owner"))
        self.assertEqual(calls, [None, [555]])
        self.assertFalse(pilot.is_ready(101))

    def test_targeted_scan_rejects_unapproved_contact_without_network_read(self):
        client = FakeClient()
        config = SimpleNamespace()
        with (
            patch.object(core, "load_config", return_value=config),
            patch.object(core, "_allowed_contacts", return_value={123: {"message_id": 5}}),
            patch.object(core, "_load_partner_lifecycle_map", return_value={}),
        ):
            result = asyncio.run(
                core.sync_owner_once(
                    101, "Test", client=client,
                    target_entities=[SimpleNamespace(id=999, first_name="Unapproved", bot=False)],
                )
            )
        self.assertEqual(result["replied"], 0)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(client.dialog_reads, 0)
        self.assertEqual(client.disconnects, 0)


if __name__ == "__main__":
    unittest.main()
