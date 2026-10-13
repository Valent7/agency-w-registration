"""Opt-in Telegram update listener for Neona owners.

A long-lived Telethon client receives new messages and performs a targeted scan.
The old five-minute poller is a safe fallback whenever realtime is unavailable.
Enable for all with NEONA_REALTIME_ALL=true; disabled by default.
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
    def __init__(self, owner_ids: set[int], poll_seconds: int = 300, all_owners: bool = False):
        self.owner_ids = set(owner_ids)
        self.all_owners = bool(all_owners)
        self._started: set[int] = set()
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
        # A supervisor can also pick up new partners after they start a dialog.
        threading.Thread(
            target=self._supervise, daemon=True, name="neona-realtime-supervisor"
        ).start()
        print(
            f"[NeonaRealtime] all_owners={self.all_owners} "
            f"selected_count={len(self.owner_ids)}", flush=True
        )

    def _supervise(self) -> None:
        while True:
            try:
                config = core.load_config()
                for owner_id, owner_name in core._owners(config):
                    owner_id = int(owner_id)
                    if not self.all_owners and owner_id not in self.owner_ids:
                        continue
                    if owner_id in self._started:
                        continue
                    # No first messages or remembered dialogues: there is nothing
                    # to listen for yet. Legacy polling handles these accounts.
                    if not core._allowed_contacts(config, owner_id):
                        continue
                    self._started.add(owner_id)
                    threading.Thread(
                        target=self._run_thread,
                        args=(owner_id, owner_name),
                        daemon=True,
                        name=f"neona-realtime-{owner_id}",
                    ).start()
                    # Stagger Telegram connections to avoid startup rate bursts.
                    time.sleep(2)
            except Exception as exc:
                print(f"[NeonaRealtime] supervisor_error={type(exc).__name__}: {exc}", flush=True)
            time.sleep(self.poll_seconds)

    def _run_thread(self, owner_id: int, owner_name: str) -> None:
        while True:
            reconnect_delay = 30
            try:
                asyncio.run(self._listen(owner_id, owner_name))
            except Exception as exc:
                self._set_ready(owner_id, False)
                reconnect_delay += core._flood_wait_seconds(exc)
                print(
                    f"[NeonaRealtime] owner={owner_id} listener_error={type(exc).__name__}: {exc} "
                    f"reconnect_delay={reconnect_delay}s",
                    flush=True,
                )
            # A failed listener hands the account back to ordinary polling.
            time.sleep(reconnect_delay)

    async def _listen(self, owner_id: int, owner_name: str) -> None:
        config = core.load_config()
        session = core._get_telegram_session(config, owner_id)
        if not session:
            raise core.DialogError("Telegram session missing for realtime owner")

        pending: dict[int, object] = {}
        pending_since: dict[int, float] = {}
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
            pending_since.setdefault(sender_id, time.monotonic())
            changed.set()

        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise core.DialogError("Realtime Telegram session unauthorized")
            me = await client.get_me()
            if int(me.id) != owner_id:
                raise core.DialogError("Realtime Telegram session owner mismatch")

            # Establish state and catch up before taking ownership from polling.
            initial_stats = await self._scan(owner_id, owner_name, client)
            if initial_stats["errors"]:
                raise core.DialogError("Realtime initial scan did not pass safety checks")
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
                first_event_at = min(pending_since.values()) if pending_since else None
                pending.clear()
                pending_since.clear()
                changed.clear()
                full = time.monotonic() >= next_full_scan

                if not targets and not full:
                    continue

                # A modest debounce groups short bursts without a busy poll.
                if targets and not full:
                    await asyncio.sleep(0.35)
                    # Deduplicate senders that sent again during the debounce.
                    targets_by_id = {
                        int(getattr(entity, "id", 0)): entity for entity in targets
                    }
                    targets_by_id.update(pending)
                    targets = list(targets_by_id.values())
                    if pending_since:
                        first_event_at = min(
                            first_event_at or time.monotonic(), min(pending_since.values())
                        )
                    pending.clear()
                    pending_since.clear()
                    changed.clear()

                try:
                    stats = await self._scan(
                        owner_id, owner_name, client,
                        target_entities=None if full else targets,
                    )
                    if targets and first_event_at is not None:
                        print(
                            f"[NeonaRealtimeLatency] owner={owner_id} "
                            f"event_to_scan_finished_s={time.monotonic() - first_event_at:.2f} "
                            f"replied={stats['replied']}",
                            flush=True,
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
    all_owners = os.getenv("NEONA_REALTIME_ALL", "").strip().lower() == "true"
    if not ids and not all_owners:
        return None
    pilots = RealtimePilots(ids, poll_seconds=poll_seconds, all_owners=all_owners)
    pilots.start()
    return pilots
