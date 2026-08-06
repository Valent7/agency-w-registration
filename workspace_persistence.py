import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from cryptography.fernet import Fernet, InvalidToken


def _get_cipher():
    key = st.secrets.get("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "FERNET_KEY не найден в Streamlit Secrets."
        )
    return Fernet(str(key).encode("utf-8"))


def get_workspace_state_keys(telegram_id):
    telegram_id = str(telegram_id)
    return {
        "passport": (
            f"neonia_target_audience_passport_{telegram_id}"
        ),
        "contacts": (
            f"neonia_telegram_contacts_{telegram_id}"
        ),
        "contacts_search_done": (
            f"neonia_contacts_search_done_{telegram_id}"
        ),
        "chats": (
            f"neonia_telegram_chats_{telegram_id}"
        ),
        "chats_search_done": (
            f"neonia_chats_search_done_{telegram_id}"
        ),
        "candidates": (
            f"neonia_candidates_{telegram_id}"
        ),
        "selection_offset": (
            f"neonia_selection_offset_{telegram_id}"
        ),
        "selected_candidates": (
            f"neonia_selected_candidates_{telegram_id}"
        ),
        "owner_known_contacts": (
            f"neonia_owner_known_contacts_{telegram_id}"
        ),
        "neona_drafts": (
            f"neona_first_message_drafts_{telegram_id}"
        ),
        "sent_log": (
            f"neona_first_message_sent_log_{telegram_id}"
        ),
    }


def collect_workspace_state(telegram_id):
    keys = get_workspace_state_keys(telegram_id)

    owner_contacts = st.session_state.get(
        keys["owner_known_contacts"],
        {},
    )
    drafts = st.session_state.get(
        keys["neona_drafts"],
        {},
    )
    sent_log = st.session_state.get(
        keys["sent_log"],
        [],
    )

    return {
        "schema_version": 3,
        "passport": st.session_state.get(
            keys["passport"]
        ),
        "contacts": st.session_state.get(
            keys["contacts"],
            [],
        ),
        "contacts_search_done": bool(
            st.session_state.get(
                keys["contacts_search_done"],
                False,
            )
        ),
        "chats": st.session_state.get(
            keys["chats"],
            [],
        ),
        "chats_search_done": bool(
            st.session_state.get(
                keys["chats_search_done"],
                False,
            )
        ),
        "candidates": st.session_state.get(
            keys["candidates"],
            [],
        ),
        "selection_offset": int(
            st.session_state.get(
                keys["selection_offset"],
                0,
            )
            or 0
        ),
        "selected_candidates": [
            int(contact_id)
            for contact_id in st.session_state.get(
                keys["selected_candidates"],
                [],
            )
        ],
        "owner_known_contacts": [
            contact
            for contact in owner_contacts.values()
        ],
        "neona_drafts": [
            {
                "telegram_id": int(contact_id),
                **draft,
            }
            for contact_id, draft in drafts.items()
        ],
        "sent_log": [
            event
            for event in sent_log
            if isinstance(event, dict)
        ],
    }


def _normalize_workspace_state(state):
    state = state if isinstance(state, dict) else {}

    contacts = (
        state.get("contacts")
        if isinstance(state.get("contacts"), list)
        else []
    )
    for contact in contacts:
        if isinstance(contact, dict):
            try:
                contact["telegram_id"] = int(
                    contact.get("telegram_id")
                )
            except (TypeError, ValueError):
                pass

    chats = (
        state.get("chats")
        if isinstance(state.get("chats"), list)
        else []
    )
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        try:
            chat["chat_id"] = int(chat.get("chat_id"))
        except (TypeError, ValueError):
            pass
        for numeric_key in (
            "participants_count",
            "unread_count",
        ):
            try:
                chat[numeric_key] = int(
                    chat.get(numeric_key, 0) or 0
                )
            except (TypeError, ValueError):
                chat[numeric_key] = 0

    candidates = (
        state.get("candidates")
        if isinstance(state.get("candidates"), list)
        else []
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            try:
                candidate["telegram_id"] = int(
                    candidate.get("telegram_id")
                )
            except (TypeError, ValueError):
                pass

    owner_raw = state.get(
        "owner_known_contacts",
        [],
    )
    if isinstance(owner_raw, dict):
        owner_raw = list(owner_raw.values())
    if not isinstance(owner_raw, list):
        owner_raw = []

    owner_contacts = {}
    for contact in owner_raw:
        if not isinstance(contact, dict):
            continue
        try:
            contact_id = int(contact.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        contact["telegram_id"] = contact_id
        owner_contacts[contact_id] = contact

    drafts_raw = state.get("neona_drafts", [])
    if isinstance(drafts_raw, dict):
        drafts_raw = [
            {
                "telegram_id": contact_id,
                **draft,
            }
            for contact_id, draft in drafts_raw.items()
            if isinstance(draft, dict)
        ]
    if not isinstance(drafts_raw, list):
        drafts_raw = []

    drafts = {}
    for draft in drafts_raw:
        if not isinstance(draft, dict):
            continue
        try:
            contact_id = int(draft.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        drafts[contact_id] = {
            key: value
            for key, value in draft.items()
            if key != "telegram_id"
        }

    selected_candidates = []
    for contact_id in state.get(
        "selected_candidates",
        [],
    ):
        try:
            selected_candidates.append(int(contact_id))
        except (TypeError, ValueError):
            continue

    try:
        selection_offset = int(
            state.get("selection_offset", 0) or 0
        )
    except (TypeError, ValueError):
        selection_offset = 0

    sent_log_raw = state.get("sent_log", [])
    if not isinstance(sent_log_raw, list):
        sent_log_raw = []

    sent_log = []
    for event in sent_log_raw:
        if not isinstance(event, dict):
            continue
        try:
            event_contact_id = int(event.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        sent_log.append(
            {
                **event,
                "telegram_id": event_contact_id,
            }
        )

    return {
        "passport": (
            state.get("passport")
            if isinstance(state.get("passport"), dict)
            else None
        ),
        "contacts": contacts,
        "contacts_search_done": bool(
            state.get("contacts_search_done", False)
        ),
        "chats": chats,
        "chats_search_done": bool(
            state.get("chats_search_done", False)
        ),
        "candidates": candidates,
        "selection_offset": max(0, selection_offset),
        "selected_candidates": selected_candidates,
        "owner_known_contacts": owner_contacts,
        "neona_drafts": drafts,
        "sent_log": sent_log,
    }


def _state_digest(state):
    canonical = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def _encrypt_state(state):
    raw_state = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _get_cipher().encrypt(
        raw_state
    ).decode("utf-8")


def _decrypt_state(encrypted_state):
    if not encrypted_state:
        return {}

    try:
        raw_state = _get_cipher().decrypt(
            encrypted_state.encode("utf-8")
        )
        return json.loads(raw_state.decode("utf-8"))
    except (
        InvalidToken,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {}


def _save_to_supabase(telegram_id, state):
    response = requests.post(
        (
            f"{st.secrets['SUPABASE_URL']}"
            "/rest/v1/agency_workspace_states"
            "?on_conflict=telegram_id"
        ),
        headers={
            "apikey": st.secrets["SUPABASE_SECRET_KEY"],
            "Authorization": (
                "Bearer "
                f"{st.secrets['SUPABASE_SECRET_KEY']}"
            ),
            "Content-Type": "application/json",
            "Prefer": (
                "resolution=merge-duplicates,"
                "return=minimal"
            ),
        },
        json={
            "telegram_id": int(telegram_id),
            "encrypted_state": _encrypt_state(state),
            "updated_at": datetime.now(
                ZoneInfo("UTC")
            ).isoformat(),
        },
        timeout=30,
    )
    response.raise_for_status()


def _load_from_supabase(telegram_id):
    response = requests.get(
        (
            f"{st.secrets['SUPABASE_URL']}"
            "/rest/v1/agency_workspace_states"
        ),
        headers={
            "apikey": st.secrets["SUPABASE_SECRET_KEY"],
            "Authorization": (
                "Bearer "
                f"{st.secrets['SUPABASE_SECRET_KEY']}"
            ),
        },
        params={
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "encrypted_state",
            "limit": 1,
        },
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()

    if not rows:
        return {}

    return _decrypt_state(
        rows[0].get("encrypted_state", "")
    )


def hydrate_workspace_state_once(telegram_id):
    loaded_flag = (
        f"agency_workspace_loaded_{telegram_id}"
    )
    ready_flag = (
        f"agency_workspace_persistence_ready_{telegram_id}"
    )
    digest_key = (
        f"agency_workspace_digest_{telegram_id}"
    )

    if st.session_state.get(loaded_flag):
        return

    try:
        raw_state = _load_from_supabase(telegram_id)
        state = _normalize_workspace_state(raw_state)
        keys = get_workspace_state_keys(telegram_id)

        if state["passport"] is not None:
            st.session_state[
                keys["passport"]
            ] = state["passport"]

        st.session_state[
            keys["contacts"]
        ] = state["contacts"]
        st.session_state[
            keys["contacts_search_done"]
        ] = state["contacts_search_done"]
        st.session_state[
            keys["chats"]
        ] = state["chats"]
        st.session_state[
            keys["chats_search_done"]
        ] = state["chats_search_done"]
        st.session_state[
            keys["candidates"]
        ] = state["candidates"]
        st.session_state[
            keys["selection_offset"]
        ] = state["selection_offset"]
        st.session_state[
            keys["selected_candidates"]
        ] = state["selected_candidates"]
        st.session_state[
            keys["owner_known_contacts"]
        ] = state["owner_known_contacts"]
        st.session_state[
            keys["neona_drafts"]
        ] = state["neona_drafts"]
        st.session_state[
            keys["sent_log"]
        ] = state["sent_log"]

        current_state = collect_workspace_state(
            telegram_id
        )
        st.session_state[digest_key] = (
            _state_digest(current_state)
        )
        st.session_state[ready_flag] = True
        st.session_state.pop(
            f"agency_workspace_load_error_{telegram_id}",
            None,
        )

    except Exception as exc:
        st.session_state[ready_flag] = False
        st.session_state[
            f"agency_workspace_load_error_{telegram_id}"
        ] = str(exc)

    finally:
        st.session_state[loaded_flag] = True


def persist_workspace_if_changed(
    telegram_id,
    force=False,
):
    ready_flag = (
        f"agency_workspace_persistence_ready_{telegram_id}"
    )
    digest_key = (
        f"agency_workspace_digest_{telegram_id}"
    )

    if not st.session_state.get(ready_flag, False):
        return False

    state = collect_workspace_state(telegram_id)
    digest = _state_digest(state)

    if (
        not force
        and st.session_state.get(digest_key) == digest
    ):
        return False

    _save_to_supabase(telegram_id, state)
    st.session_state[digest_key] = digest
    st.session_state[
        f"agency_workspace_last_saved_{telegram_id}"
    ] = datetime.now(
        ZoneInfo("Europe/Berlin")
    ).isoformat()

    return True
