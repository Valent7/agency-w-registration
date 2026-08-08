from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

import neona_telegram_dialogs as nd


UTC = timezone.utc
POLL_SECONDS = 15
MAX_BATCH = 50


def _get(
    config: nd.Config,
    table: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    response = requests.get(
        f"{config.supabase_url}/rest/v1/{table}",
        headers=nd._headers(config),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _post(
    config: nd.Config,
    table: str,
    payload: dict[str, Any] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    response = requests.post(
        f"{config.supabase_url}/rest/v1/{table}",
        headers=nd._headers(config, "return=representation"),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _patch(
    config: nd.Config,
    table: str,
    filters: dict[str, str],
    payload: dict[str, Any],
    *,
    return_representation: bool = False,
) -> list[dict[str, Any]]:
    prefer = "return=representation" if return_representation else "return=minimal"
    response = requests.patch(
        f"{config.supabase_url}/rest/v1/{table}",
        headers=nd._headers(config, prefer),
        params=filters,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    if not return_representation:
        return []
    data = response.json()
    return data if isinstance(data, list) else []


def _pending_messages(
    config: nd.Config,
    *,
    limit: int = MAX_BATCH,
) -> list[dict[str, Any]]:
    return _get(
        config,
        "agency_team_messages",
        {
            "delivery_status": "eq.pending",
            "select": "*",
            "order": "created_at.asc",
            "limit": int(limit),
        },
    )


def _member(
    config: nd.Config,
    telegram_id: int,
) -> dict[str, Any] | None:
    rows = _get(
        config,
        "agency_members",
        {
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "telegram_id,first_name,username,member_code,referrer_code",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _owner_member_code(
    config: nd.Config,
    owner_id: int,
) -> str:
    member = _member(config, int(owner_id))
    return str((member or {}).get("member_code") or "").strip()


def _direct_partners(
    config: nd.Config,
    owner_id: int,
) -> list[dict[str, Any]]:
    code = _owner_member_code(config, owner_id)
    if not code:
        return []
    return _get(
        config,
        "agency_members",
        {
            "referrer_code": f"eq.{code}",
            "select": "telegram_id,first_name,username,member_code,referrer_code",
            "order": "created_at.asc",
        },
    )


def _is_direct_partner(
    config: nd.Config,
    owner_id: int,
    partner_id: int,
) -> bool:
    return any(
        int(item.get("telegram_id") or 0) == int(partner_id)
        for item in _direct_partners(config, int(owner_id))
    )


def _claim_pending(
    config: nd.Config,
    row: dict[str, Any],
) -> bool:
    """
    Атомарно переводим pending -> sending.
    Если другой процесс уже забрал запись, PATCH вернёт пустой список.
    """
    message_id = int(row["id"])
    attempts = int(row.get("telegram_attempts") or 0) + 1
    claimed = _patch(
        config,
        "agency_team_messages",
        {
            "id": f"eq.{message_id}",
            "delivery_status": "eq.pending",
        },
        {
            "delivery_status": "sending",
            "telegram_attempts": attempts,
            "telegram_last_attempt_at": datetime.now(UTC).isoformat(),
            "telegram_error": None,
        },
        return_representation=True,
    )
    return bool(claimed)


def _mark_sent(
    config: nd.Config,
    row_id: int,
    telegram_message_id: int,
) -> None:
    now = datetime.now(UTC).isoformat()
    _patch(
        config,
        "agency_team_messages",
        {"id": f"eq.{int(row_id)}"},
        {
            "delivery_status": "sent",
            "telegram_message_id": int(telegram_message_id),
            "telegram_sent_at": now,
            "telegram_error": None,
        },
    )


def _mark_error(
    config: nd.Config,
    row_id: int,
    exc: Exception,
) -> None:
    text = f"{type(exc).__name__}: {exc}"
    _patch(
        config,
        "agency_team_messages",
        {"id": f"eq.{int(row_id)}"},
        {
            "delivery_status": "error",
            "telegram_error": text[:1000],
        },
    )


def _reset_stuck_sending(
    config: nd.Config,
) -> None:
    """
    После внезапного рестарта запись могла остаться в sending.
    Возвращаем в pending только то, что зависло более 10 минут.
    """
    cutoff = datetime.now(UTC).timestamp() - 600
    rows = _get(
        config,
        "agency_team_messages",
        {
            "delivery_status": "eq.sending",
            "select": "id,telegram_last_attempt_at",
            "limit": 100,
        },
    )
    for row in rows:
        raw = str(row.get("telegram_last_attempt_at") or "")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.timestamp() >= cutoff:
                continue
        except Exception:
            continue
        _patch(
            config,
            "agency_team_messages",
            {"id": f"eq.{int(row['id'])}"},
            {"delivery_status": "pending"},
        )


def _compose_message(row: dict[str, Any]) -> str:
    parts: list[str] = []

    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body") or "").strip()
    zoom_url = str(row.get("zoom_url") or "").strip()

    if subject:
        parts.append(subject)
    if body:
        parts.append(body)
    if zoom_url:
        parts.append(f"Zoom: {zoom_url}")

    return "\n\n".join(parts).strip()


async def _resolve_entity(
    client: TelegramClient,
    config: nd.Config,
    recipient_id: int,
):
    member = _member(config, int(recipient_id))
    username = str((member or {}).get("username") or "").strip().lstrip("@")

    # Самый надёжный вариант для новой StringSession — username.
    if username:
        try:
            return await client.get_entity(username)
        except Exception:
            pass

    # Если Telegram уже знает access_hash этого человека.
    try:
        return await client.get_entity(int(recipient_id))
    except Exception:
        pass

    # Последняя страховка: ищем человека среди реальных диалогов владельца.
    async for dialog in client.iter_dialogs(limit=1000):
        entity = dialog.entity
        if int(getattr(entity, "id", 0) or 0) == int(recipient_id):
            return entity

    raise RuntimeError(
        "Не удалось найти получателя в Telegram. "
        "Нужен Telegram username или существующий личный диалог/контакт."
    )


def _known_telegram_ids(
    config: nd.Config,
    owner_id: int,
    partner_id: int,
) -> set[int]:
    known: set[int] = set()

    for sender_id, recipient_id in (
        (owner_id, partner_id),
        (partner_id, owner_id),
    ):
        rows = _get(
            config,
            "agency_team_messages",
            {
                "sender_telegram_id": f"eq.{int(sender_id)}",
                "recipient_telegram_id": f"eq.{int(recipient_id)}",
                "telegram_message_id": "not.is.null",
                "select": "telegram_message_id",
                "limit": 500,
            },
        )
        for row in rows:
            try:
                known.add(int(row.get("telegram_message_id")))
            except (TypeError, ValueError):
                pass

    return known


def _first_sent_telegram_time(
    config: nd.Config,
    owner_id: int,
    partner_id: int,
) -> datetime | None:
    rows = _get(
        config,
        "agency_team_messages",
        {
            "sender_telegram_id": f"eq.{int(owner_id)}",
            "recipient_telegram_id": f"eq.{int(partner_id)}",
            "delivery_status": "eq.sent",
            "telegram_sent_at": "not.is.null",
            "select": "telegram_sent_at",
            "order": "telegram_sent_at.asc",
            "limit": 1,
        },
    )
    if not rows:
        return None

    raw = str(rows[0].get("telegram_sent_at") or "")
    if not raw:
        return None

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def _reply_to_internal_id(
    config: nd.Config,
    owner_id: int,
    partner_id: int,
    telegram_reply_to_id: int | None,
) -> int | None:
    if not telegram_reply_to_id:
        return None

    rows = _get(
        config,
        "agency_team_messages",
        {
            "sender_telegram_id": f"eq.{int(owner_id)}",
            "recipient_telegram_id": f"eq.{int(partner_id)}",
            "telegram_message_id": f"eq.{int(telegram_reply_to_id)}",
            "select": "id",
            "limit": 1,
        },
    )
    return int(rows[0]["id"]) if rows else None


async def _capture_partner_replies(
    config: nd.Config,
    client: TelegramClient,
    owner_id: int,
) -> int:
    """
    Копирует НОВЫЕ входящие сообщения прямых зарегистрированных партнёров
    в agency_team_messages.

    Историю до первого командного сообщения НЕ импортируем.
    """
    imported = 0

    for partner in _direct_partners(config, int(owner_id)):
        try:
            partner_id = int(partner.get("telegram_id"))
        except (TypeError, ValueError):
            continue

        baseline_time = _first_sent_telegram_time(
            config,
            int(owner_id),
            partner_id,
        )
        if baseline_time is None:
            # Пока Директор ни разу не написал партнёру через Центр команды,
            # старую личную переписку не трогаем.
            continue

        try:
            entity = await _resolve_entity(
                client,
                config,
                partner_id,
            )
        except Exception:
            continue

        known_ids = _known_telegram_ids(
            config,
            int(owner_id),
            partner_id,
        )

        recent = []
        async for message in client.iter_messages(entity, limit=50):
            if message.out or not message.message:
                continue
            if message.date.astimezone(UTC) <= baseline_time:
                continue
            if int(message.id) in known_ids:
                continue
            recent.append(message)

        recent.sort(key=lambda item: int(item.id))

        for message in recent:
            reply_to_telegram_id = None
            try:
                reply_to_telegram_id = int(
                    getattr(message, "reply_to_msg_id", 0) or 0
                ) or None
            except Exception:
                reply_to_telegram_id = None

            reply_to_id = _reply_to_internal_id(
                config,
                int(owner_id),
                partner_id,
                reply_to_telegram_id,
            )

            _post(
                config,
                "agency_team_messages",
                {
                    "sender_telegram_id": partner_id,
                    "recipient_telegram_id": int(owner_id),
                    "subject": "Ответ из Telegram",
                    "body": str(message.message or "").strip(),
                    "zoom_url": None,
                    "reply_to_id": reply_to_id,
                    "created_at": message.date.astimezone(UTC).isoformat(),
                    "delivery_status": "received",
                    "telegram_message_id": int(message.id),
                    "telegram_sent_at": message.date.astimezone(UTC).isoformat(),
                    "telegram_error": None,
                    "telegram_attempts": 0,
                },
            )
            imported += 1

    return imported


async def process_owner_once(
    config: nd.Config,
    owner_id: int,
    pending_rows: list[dict[str, Any]] | None = None,
    *,
    capture_incoming: bool = True,
) -> dict[str, int]:
    """
    Одна Telegram-сессия владельца:
    1) отправляет pending-сообщения его прямым партнёрам;
    2) копирует новые ответы партнёров обратно в кабинет.
    """
    owner_id = int(owner_id)
    pending_rows = list(pending_rows or [])

    stats = {
        "queued": len(pending_rows),
        "sent": 0,
        "received": 0,
        "errors": 0,
    }

    session = nd._get_telegram_session(config, owner_id)
    if not session:
        for row in pending_rows:
            try:
                _mark_error(
                    config,
                    int(row["id"]),
                    RuntimeError("Telegram-сессия Директора не найдена."),
                )
            except Exception:
                pass
        stats["errors"] += len(pending_rows)
        return stats

    client = TelegramClient(
        StringSession(session),
        config.telegram_api_id,
        config.telegram_api_hash,
    )
    await client.connect()

    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram-сессия Директора больше не авторизована."
            )

        me = await client.get_me()
        if int(me.id) != owner_id:
            raise RuntimeError(
                "Подключена Telegram-сессия другого пользователя."
            )

        for row in pending_rows:
            row_id = int(row["id"])
            recipient_id = int(row["recipient_telegram_id"])

            try:
                if not _is_direct_partner(
                    config,
                    owner_id,
                    recipient_id,
                ):
                    raise RuntimeError(
                        "Получатель не является прямым зарегистрированным "
                        "партнёром этого Директора."
                    )

                if not _claim_pending(config, row):
                    continue

                text = _compose_message(row)
                if not text:
                    raise RuntimeError("Пустое сообщение.")

                entity = await _resolve_entity(
                    client,
                    config,
                    recipient_id,
                )

                sent = await client.send_message(
                    entity,
                    text,
                    parse_mode=None,
                    link_preview=False,
                )

                _mark_sent(
                    config,
                    row_id,
                    int(sent.id),
                )
                stats["sent"] += 1

            except Exception as exc:
                try:
                    _mark_error(config, row_id, exc)
                finally:
                    stats["errors"] += 1

        if capture_incoming:
            try:
                stats["received"] += await _capture_partner_replies(
                    config,
                    client,
                    owner_id,
                )
            except Exception:
                stats["errors"] += 1

        return stats

    finally:
        await client.disconnect()


async def process_cycle_async() -> dict[str, int]:
    config = nd.load_config()
    _reset_stuck_sending(config)

    pending = _pending_messages(config)
    by_owner: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in pending:
        try:
            by_owner[int(row["sender_telegram_id"])].append(row)
        except (TypeError, ValueError):
            continue

    owner_ids = {
        int(owner_id)
        for owner_id, _owner_name in nd._owners(config)
    }
    owner_ids.update(by_owner.keys())

    total = {
        "owners": 0,
        "queued": 0,
        "sent": 0,
        "received": 0,
        "errors": 0,
    }

    for owner_id in sorted(owner_ids):
        total["owners"] += 1
        stats = await process_owner_once(
            config,
            owner_id,
            by_owner.get(owner_id, []),
            capture_incoming=True,
        )
        for key in ("queued", "sent", "received", "errors"):
            total[key] += int(stats.get(key, 0))

    return total


def process_cycle_once() -> dict[str, int]:
    return asyncio.run(process_cycle_async())


def worker_forever(poll_seconds: int = POLL_SECONDS) -> None:
    print("Agency W team Telegram delivery worker started", flush=True)

    while True:
        try:
            stats = process_cycle_once()
            if stats["sent"] or stats["received"] or stats["errors"]:
                print(f"team telegram stats={stats}", flush=True)
        except Exception as exc:
            print(f"team telegram worker error={exc}", flush=True)

        time.sleep(max(5, int(poll_seconds)))


if __name__ == "__main__":
    worker_forever(
        int(
            __import__("os").getenv(
                "TEAM_TELEGRAM_POLL_SECONDS",
                str(POLL_SECONDS),
            )
        )
    )
