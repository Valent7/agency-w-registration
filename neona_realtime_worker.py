"""Opt-in Telegram update listener for selected Neona owners.

Each selected account owns one long-lived Telegram client. New private messages
trigger an inexpensive, single-dialog scan; a 5-minute full scan catches missed
updates. The legacy polling worker remains the automatic fallback for accounts
whose listener is not healthy. No real-time listeners start without explicit IDs.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import neona_telegram_dialogs as core


def _owner_ids(value: str) -> set[int]:
    return {int(item.strip()) for item in value.split(",") if item.strip().isdigit()}


class RealtimePilots:
    def __init__(self, owner_ids: set[int], poll_seconds: int = 300):
        self.owner_ids = set(owner_ids)
        self.poll_seconds = max(core.NEONA_MIN_POLL_SECONDS, int(poll_seconds))
        self._ready: set[int] = set()
        self._guard = threading.Lock()

    def is_ready(self, owner_id: int) -> bool:
        with self._guard:
            return int(owner_id) in self._ready

    def _set_ready(self, owner_id: int, ready: bool) -> None:
        with self._guard:
            if ready:
                self._ready.add(int(owner_id))
            else:
                self._ready.discard(int(owner_id))

    def start(self) -> None:
        config = core.load_config()
        for owner_id, owner_name in core._owners(config):
            if int(owner_id) not in self.owner_ids:
                continue
            threading.Thread(
                target=self._run_thread,
                args=(int(owner_id), owner_name),
                daemon=True,
                name=f"neona-realtime-{owner_id}",
            ).start()
        print(f"[NeonaRealtime] pilots_requested={len(self.owner_ids)}", flush=True)

    def _run_thread(self, owner_id: int, owner_name: str) -> None:
        while True:
            try:
                asyncio.run(self._listen(owner_id, owner_name))
            except Exception as exc:
                self._set_ready(owner_id, False)
                print(
                    f"[NeonaRealtime] owner={owner_id} listener_error={type(exc).__name__}: {exc}",
                    flush=True,
                )
            # A failed listener hands the account back to ordinary polling.
            time.sleep(30)

    async def _listen(self, owner_id: int, owner_name: str) -> None:
        config = core.load_config()
        session = core._get_telegram_session(config, owner_id)
        if not session:
            raise core.DialogError("Telegram session missing for realtime owner")

        pending: dict[int, object] = {}
        changed = asyncio.Event()
        client = TelegramClient(
            StringSession(session), config.telegram_api_id, config.telegram_api_hash
        )

        @client.on(events.NewMessage(incoming=True))
        async def on_incoming(event):
            if not event.is_private:
                return
            message = event.message
            # Do not trigger a scan on an unsupported media-only event.
            kind = core._telegram_message_kind(message)
            if not str(getattr(message, "message", "") or "").strip() and kind not in ("voice", "audio"):
                return
            sender_id = int(event.sender_id or 0)
            if not sender_id:
                return
            try:
                sender = await event.get_sender()
            except Exception:
                return  # Periodic safety scan will recover this message.
            if sender is None or getattr(sender, "bot", False):
                return
            # Only one pending item per sender, even during rapid-fire messages.
            pending[sender_id] = sender
            changed.set()

        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise core.DialogError("Realtime Telegram session unauthorized")
            me = await client.get_me()
            if int(me.id) != owner_id:
                raise core.DialogError("Realtime Telegram session owner mismatch")

            # Establish state and catch up before taking ownership from polling.
            await self._scan(owner_id, owner_name, client)
            self._set_ready(owner_id, True)
            print(f"[NeonaRealtime] owner={owner_id} listener_ready=true", flush=True)
            next_full_scan = time.monotonic() + self.poll_seconds
            consecutive_failures = 0

            while client.is_connected():
                timeout = max(0.0, next_full_scan - time.monotonic())
                if not pending and timeout:
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass

                # Snapshot and clear before awaiting: events arriving during a
                # scan remain queued for the next iteration.
                targets = list(pending.values())
                pending.clear()
                changed.clear()
                full = time.monotonic() >= next_full_scan

                if not targets and not full:
                    continue

                # A modest debounce groups short bursts without a busy poll.
                if targets and not full:
                    await asyncio.sleep(0.35)
                    targets.extend(pending.values())
                    pending.clear()
                    changed.clear()

                try:
                    stats = await self._scan(
                        owner_id, owner_name, client,
                        target_entities=None if full else targets,
                    )
                    if stats["errors"]:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                    if full:
                        next_full_scan = time.monotonic() + self.poll_seconds
                    if consecutive_failures >= 3:
                        raise core.DialogError("Realtime repeated dialog processing errors")
                except Exception as exc:
                    wait = core._flood_wait_seconds(exc)
                    if wait:
                        # Never retry before Telegram's requested cooldown.
                        print(f"[NeonaRealtime] owner={owner_id} floodwait={wait}s", flush=True)
                        await asyncio.sleep(wait + 30)
                        next_full_scan = time.monotonic()
                    else:
                        raise
        finally:
            self._set_ready(owner_id, False)
            await client.disconnect()

    async def _scan(
        self, owner_id: int, owner_name: str, client: TelegramClient,
        target_entities: list | None = None,
    ) -> dict[str, int]:
        started = time.monotonic()
        # Legacy polling may already be finishing a scan during handover.
        # Hold the same lock until its state writes have completed.
        with core.owner_sync_lock(owner_id):
            stats = await core.sync_owner_once(
                owner_id, owner_name, initialize_new_dialogs=True,
                force_full_scan=False, client=client,
                target_entities=target_entities,
            )
        print(
            f"[NeonaRealtime] owner={owner_id} mode={'full' if target_entities is None else 'targeted'} "
            f"duration_s={time.monotonic() - started:.2f} "
            f"processed={stats['processed']} replied={stats['replied']} "
            f"errors={stats['errors']}",
            flush=True,
        )
        return stats


def launch_from_environment(poll_seconds: int) -> RealtimePilots | None:
    ids = _owner_ids(os.getenv("NEONA_REALTIME_OWNER_IDS", ""))
    if not ids:
        return None
    pilots = RealtimePilots(ids, poll_seconds=poll_seconds)
    pilots.start()
    return pilots
