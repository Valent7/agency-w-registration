"""Offline safety review for Neona's opt-in realtime listener.

All Telegram interactions are mocked. This suite never opens sessions, sends
messages, accesses partner data, or calls OpenAI.
"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import neona_realtime_worker as realtime
import neona_telegram_dialogs as core


class StopAfterBackoff(BaseException):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"Wait {seconds} seconds")


class RealtimeSafetyReview(unittest.TestCase):
    def test_floodwait_detection_and_backoff(self):
        self.assertEqual(core._flood_wait_seconds(FloodWaitError(85)), 85)
        self.assertEqual(core._flood_wait_seconds(ValueError("unrelated")), 0)
        pilot = realtime.RealtimePilots({101}, poll_seconds=300)
        pilot._set_ready(101, True)

        async def fail_listener(*_args):
            raise FloodWaitError(85)

        with patch.object(pilot, "_listen", side_effect=fail_listener), patch.object(
            realtime.time, "sleep", side_effect=StopAfterBackoff
        ) as sleep:
            with self.assertRaises(StopAfterBackoff):
                pilot._run_thread(101, "Owner")

        self.assertFalse(pilot.is_ready(101))
        self.assertGreaterEqual(sleep.call_args.args[0], 115)

    def test_owner_lock_serializes_polling_and_realtime(self):
        lock = core.owner_sync_lock(101)
        entered = threading.Event()
        finished = threading.Event()

        def competing_worker():
            with core.owner_sync_lock(101):
                entered.set()
            finished.set()

        with lock:
            thread = threading.Thread(target=competing_worker, daemon=True)
            thread.start()
            self.assertFalse(entered.wait(0.05))
        self.assertTrue(finished.wait(2))
        thread.join(timeout=2)

    @unittest.expectedFailure
    def test_unapproved_sender_must_not_resolve_telegram_entity(self):
        """Known gap: current event handler resolves unapproved senders."""
        class StubClient:
            def __init__(self, *_args, **_kwargs):
                self.connected = False
                self.handler = None

            def on(self, _event):
                def register(callback):
                    self.handler = callback
                    return callback
                return register

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

        class UnapprovedEvent:
            is_private = True
            sender_id = 999
            message = SimpleNamespace(message="Hello", voice=False, audio=False, document=None)
            get_sender_calls = 0

            async def get_sender(self):
                self.get_sender_calls += 1
                return SimpleNamespace(id=999, first_name="Stranger", bot=False)

        pilot = realtime.RealtimePilots({101}, poll_seconds=300)
        event = UnapprovedEvent()
        calls = []

        async def fake_scan(owner_id, owner_name, client, target_entities=None):
            calls.append(target_entities)
            if len(calls) == 1:
                await client.handler(event)
            else:
                client.connected = False
            return {"processed": 0, "replied": 0, "errors": 0}

        # Terminate after first targeted scan without waiting on the network.
        with (
            patch.object(core, "load_config", return_value=SimpleNamespace(
                telegram_api_id=1, telegram_api_hash="unused")),
            patch.object(core, "_get_telegram_session", return_value="session"),
            patch.object(realtime, "StringSession", side_effect=lambda value: value),
            patch.object(realtime, "TelegramClient", StubClient),
            patch.object(pilot, "_scan", side_effect=fake_scan),
        ):
            asyncio.run(pilot._listen(101, "Owner"))

        self.assertEqual(event.get_sender_calls, 0)


if __name__ == "__main__":
    unittest.main()
