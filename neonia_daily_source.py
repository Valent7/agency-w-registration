"""One source of truth for the Neonia daily five.

Choosing Telegram chat means *only that chat*. Never quietly fall back to
personal contacts or another chat. Read and compare the source of saved
Stagirite daily batches against the owner's durable workspace preference.
"""

def owner_source(state):
    mode = str((state or {}).get("daily_source_mode") or "contacts").strip().lower()
    if mode != "chats":
        return "contacts", None
    value = (state or {}).get("selected_source_chat")
    try:
        chat_id = int(value)
    except (TypeError, ValueError):
        return "chats", None
    return "chats", chat_id if chat_id > 0 else None


def source_signature(mode, chat_id=None):
    if mode == "contacts":
        return "contacts"
    if mode == "chats" and chat_id is not None:
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            return "chats:unset"
        return f"chats:{cid}" if cid > 0 else "chats:unset"
    return "chats:unset"


def matches_daily_batch(batch, mode, chat_id=None):
    if not isinstance(batch, dict) or not batch.get("candidate_ids"):
        return False
    expected = source_signature(mode, chat_id)
    actual = str(batch.get("source_signature") or "")
    if mode == "chats":
        # Unmarked legacy contact batches are NEVER treated as chat members.
        return expected != "chats:unset" and actual == expected
    if actual:
        return actual == "contacts"
    # Existing personal-contact batches remain supported for old partners.
    return str(batch.get("source") or "") in (
        "neonia_sequential_queue", "telegram_scout_background"
    )


def selected_chat_details(state):
    mode, selected = owner_source(state)
    if mode != "chats" or selected is None:
        return None
    for chat in (state or {}).get("chats", []) or []:
        if not isinstance(chat, dict):
            continue
        try:
            if int(chat.get("chat_id")) != selected:
                continue
        except (TypeError, ValueError):
            continue
        if chat.get("type") in {"Группа", "Супергруппа"}:
            return chat
    return None
