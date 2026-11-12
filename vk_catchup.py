"""Low-cost fallback for known VK conversations when a Callback API event is missed.

The webhook remains the primary transport. The fallback only considers the
latest inbound TEXT message in EXISTING Agency W VK leads, no more than an
hour old; never starts conversations with strangers or replays old histories.
"""
from __future__ import annotations

import time


def recent_user_messages(api_response: dict, *, now_ts: int | None = None, max_age: int = 3600) -> list[dict]:
    """Extract safe, recent, inbound one-to-one message snapshots."""
    stamp = int(time.time() if now_ts is None else now_ts)
    source = api_response.get("response") if isinstance(api_response, dict) else None
    items = source.get("items") if isinstance(source, dict) else None
    if not isinstance(items, list):
        return []
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        conv = item.get("conversation")
        message = item.get("last_message")
        if not isinstance(conv, dict) or not isinstance(message, dict):
            continue
        peer = conv.get("peer")
        if not isinstance(peer, dict) or peer.get("type") != "user":
            continue
        try:
            peer_id = int(peer.get("id") or 0)
            user_id = int(message.get("from_id") or 0)
            message_id = int(message.get("id") or 0)
            message_ts = int(message.get("date") or 0)
            out = int(message.get("out") or 0)
        except (TypeError, ValueError):
            continue
        content = str(message.get("text") or "").strip()
        if not (peer_id > 0 and peer_id == user_id and message_id > 0 and content):
            continue
        if out or not 0 <= stamp - message_ts <= max_age or message_id in seen:
            continue
        seen.add(message_id)
        result.append({
            "id": message_id,
            "from_id": user_id,
            "peer_id": peer_id,
            "text": content,
            "date": message_ts,
            "out": 0,
        })
    result.sort(key=lambda entry: (entry["date"], entry["id"]))
    return result


def latest_unanswered_history(history: dict, peer_id: int, *,
                              now_ts: int | None = None,
                              max_age: int = 900) -> dict | None:
    """Only last inbound text, and only if no subsequent outbound reply.

    This avoids replaying past messages, including ones already answered before
    the webhook recovered. The caller independently verifies this is an
    existing Agency W contact.
    """
    now = int(time.time() if now_ts is None else now_ts)
    source = history.get("response") if isinstance(history, dict) else None
    items = source.get("items") if isinstance(source, dict) else None
    if not isinstance(items, list) or not items:
        return None
    try:
        peer_id = int(peer_id)
    except (TypeError, ValueError):
        return None
    # VK getHistory returns most recent messages first.
    newest = items[0]
    if not isinstance(newest, dict):
        return None
    try:
        outgoing = int(newest.get("out") or 0)
        from_id = int(newest.get("from_id") or 0)
        timestamp = int(newest.get("date") or 0)
        message_id = int(newest.get("id") or 0)
    except (TypeError, ValueError):
        return None
    content = str(newest.get("text") or "").strip()
    if outgoing or from_id != peer_id or not content or message_id <= 0:
        return None
    if not 0 <= now - timestamp <= max_age:
        return None
    return {"id": message_id, "peer_id": peer_id,
            "from_id": from_id, "out": 0,
            "date": timestamp, "text": content}
