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
