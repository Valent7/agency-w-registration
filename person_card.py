"""Карточка человека 2.0 — read-only слой общей памяти Агентства W.

Первая версия ничего не меняет в данных и не вмешивается в работу Неонии,
Неоны, Стагирита или Неолы. Она только собирает уже существующий контекст
в одну понятную карточку для Директора.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import streamlit as st


_STAGE_LABELS = {
    "idle": "Ждём ответа / обычный диалог",
    "invited_to_meeting": "Переход к встрече",
    "collecting_meeting_details": "Согласование встречи",
    "awaiting_confirmation": "Ждём подтверждения времени",
    "awaiting_slot_choice": "Ждём выбора времени",
    "scheduled": "Встреча назначена",
    "opted_out": "Общение остановлено по просьбе человека",
}


def _safe_text(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _format_dt(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return raw[:16].replace("T", " ")


@st.cache_data(ttl=10, show_spinner=False)
def _load_owner_dialog_states(owner_telegram_id: int) -> dict[int, dict[str, Any]]:
    """Читает уже существующие состояния диалогов Неоны одним запросом."""
    try:
        url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
        key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
        if not url or not key:
            return {}

        response = requests.get(
            f"{url}/rest/v1/agency_dialog_states",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
            },
            params={
                "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
                "select": "contact_telegram_id,stage,context,updated_at",
            },
            timeout=10,
        )
        if not response.ok:
            return {}
        rows = response.json()
        if not isinstance(rows, list):
            return {}

        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                contact_id = int(row.get("contact_telegram_id"))
            except (TypeError, ValueError):
                continue
            result[contact_id] = row
        return result
    except Exception:
        # Карточка — вспомогательный read-only слой. Ошибка чтения не должна
        # ломать рабочий кабинет или диалог Неоны.
        return {}


def _contact_type(contact: dict[str, Any]) -> str:
    source = _safe_text(contact.get("source"), "")
    warmth = _safe_text(contact.get("warmth"), "").lower()
    if source == "Знакомый — выбран директором" or "знаком" in warmth:
        return "Знакомый контакт"
    if "партн" in source.lower():
        return "Партнёр"
    return "Холодный контакт"


def _first_message_event(contact_id: int, sent_log: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = []
    for event in sent_log or []:
        if not isinstance(event, dict):
            continue
        try:
            event_id = int(event.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        if event_id == int(contact_id) and event.get("kind") == "first_message":
            matches.append(event)
    return matches[-1] if matches else None


def _boundary(context: dict[str, Any], stage: str) -> tuple[str, bool]:
    raw = _safe_text(context.get("contact_boundary"), "")
    lowered = raw.lower()
    if stage == "opted_out" or lowered in {
        "do_not_contact",
        "do-not-contact",
        "no_contact",
        "blocked",
    }:
        return "⛔ Не писать по собственной инициативе", True
    if raw:
        return raw, False
    return "Общение разрешено в рамках действующего диалога", False


def _next_action(
    contact: dict[str, Any],
    draft: dict[str, Any],
    stage: str,
    hard_boundary: bool,
) -> str:
    if hard_boundary:
        return "Ждать только нового обращения самого человека."
    if stage == "scheduled":
        return "Встреча назначена. Следующий человеческий шаг — сама встреча."
    if stage in {
        "invited_to_meeting",
        "collecting_meeting_details",
        "awaiting_confirmation",
        "awaiting_slot_choice",
    }:
        return "Неона продолжает согласование встречи в контексте ответа человека."
    if bool(draft.get("sent")):
        return "Ждать ответа человека. Не инициировать напоминание."
    if bool(draft.get("approved")):
        return "Первое сообщение утверждено — Директор может отправить его."
    if draft:
        return "Проверить, при необходимости отредактировать и утвердить первое сообщение."
    status = _safe_text(contact.get("status"), "")
    if status in {"Выбран владельцем", "Выбран владельцем повторно"}:
        return "Подготовить персональное первое сообщение Неоны."
    return "Решение о следующем шаге принимает Директор."


def _current_stage(contact: dict[str, Any], draft: dict[str, Any], dialog_state: dict[str, Any]) -> tuple[str, str]:
    stage = _safe_text(dialog_state.get("stage"), "")
    if stage:
        return stage, _STAGE_LABELS.get(stage, stage)
    if bool(draft.get("sent")):
        return "first_message_sent", "Первое сообщение отправлено — ждём ответа"
    if bool(draft.get("approved")):
        return "first_message_approved", "Первое сообщение утверждено"
    if draft:
        return "first_message_draft", "Первое сообщение подготовлено"
    status = _safe_text(contact.get("status"), "Выбран владельцем")
    return "selected", status


def _last_event(
    contact: dict[str, Any],
    draft: dict[str, Any],
    dialog_state: dict[str, Any],
    sent_event: dict[str, Any] | None,
) -> str:
    candidates: list[tuple[str, str]] = []

    analyzed_at = _format_dt(contact.get("analyzed_at"))
    if analyzed_at:
        candidates.append((str(contact.get("analyzed_at")), f"Неония обновила анализ · {analyzed_at}"))

    if sent_event:
        sent_at = _format_dt(sent_event.get("sent_at"))
        candidates.append((str(sent_event.get("sent_at") or ""), f"Первое сообщение отправлено · {sent_at}"))
    elif draft:
        status = _safe_text(draft.get("status"), "Сообщение подготовлено")
        candidates.append(("", status))

    updated_at = _format_dt(dialog_state.get("updated_at"))
    if updated_at:
        stage = _safe_text(dialog_state.get("stage"), "диалог обновлён")
        label = _STAGE_LABELS.get(stage, stage)
        candidates.append((str(dialog_state.get("updated_at") or ""), f"Неона: {label} · {updated_at}"))

    # ISO-строки сортируются хронологически; пустые уходят вниз.
    dated = [item for item in candidates if item[0]]
    if dated:
        return sorted(dated, key=lambda item: item[0])[-1][1]
    return candidates[-1][1] if candidates else "Карточка создана из текущих данных"


def _collect_objections(context: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key, value in context.items():
        key_lower = str(key).lower()
        if "objection" not in key_lower and "возраж" not in key_lower:
            continue
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text not in values:
                values.append(text)
        elif isinstance(value, list):
            for item in value:
                text = _safe_text(item, "")
                if text and text not in values:
                    values.append(text)
    return values[:5]


def render_person_card_2_0(
    owner_telegram_id: int,
    contact: dict[str, Any],
    draft: dict[str, Any] | None = None,
    sent_log: list[dict[str, Any]] | None = None,
) -> None:
    """Показывает единую read-only карточку человека внутри рабочего кабинета."""
    contact = contact if isinstance(contact, dict) else {}
    draft = draft if isinstance(draft, dict) else {}
    sent_log = sent_log if isinstance(sent_log, list) else []

    try:
        contact_id = int(contact.get("telegram_id"))
    except (TypeError, ValueError):
        return

    dialog_state = _load_owner_dialog_states(int(owner_telegram_id)).get(contact_id, {})
    context = dialog_state.get("context") if isinstance(dialog_state.get("context"), dict) else {}
    stage_code, stage_label = _current_stage(contact, draft, dialog_state)
    boundary_label, hard_boundary = _boundary(context, stage_code)
    sent_event = _first_message_event(contact_id, sent_log)

    with st.expander("🧭 Карточка человека 2.0", expanded=False):
        c1, c2 = st.columns(2)
        c1.write(f"**Тип контакта:** {_contact_type(contact)}")
        c2.write(f"**Этап:** {stage_label}")

        st.write(f"**Последнее значимое событие:** {_last_event(contact, draft, dialog_state, sent_event)}")
        st.info(f"➡️ **Следующий допустимый шаг:** {_next_action(contact, draft, stage_code, hard_boundary)}")

        owner_hint = _safe_text(contact.get("owner_hint"), "")
        reasons = contact.get("reasons") if isinstance(contact.get("reasons"), list) else []
        if owner_hint or reasons:
            st.write("**Почему человек оказался в работе:**")
            if owner_hint:
                st.write(owner_hint)
            for reason in reasons[:3]:
                text = _safe_text(reason, "")
                if text:
                    st.write(f"• {text}")

        facts: list[str] = []
        short_portrait = _safe_text(contact.get("short_portrait"), "")
        if short_portrait:
            facts.append(short_portrait)
        project_name = _safe_text(contact.get("project_name"), "")
        if project_name:
            facts.append(f"Проект/компания: {project_name}")
        project_evidence = _safe_text(contact.get("project_evidence"), "")
        if project_evidence:
            facts.append(f"Основание: {project_evidence}")
        profile_about = _safe_text(contact.get("profile_about"), "")
        if profile_about:
            facts.append(f"Bio: {profile_about}")

        if facts:
            st.write("**Что известно сейчас:**")
            for fact in facts[:5]:
                st.write(f"• {fact}")

        objections = _collect_objections(context)
        st.write(f"**Границы общения:** {boundary_label}")
        if objections:
            st.write("**Зафиксированные возражения:**")
            for item in objections:
                st.write(f"• {item}")
        else:
            st.caption("Возражения пока не зафиксированы в общей карточке.")

        timeline: list[str] = []
        analyzed_at = _format_dt(contact.get("analyzed_at"))
        if analyzed_at:
            timeline.append(f"{analyzed_at} — анализ Неонии")
        if draft:
            timeline.append(f"{_safe_text(draft.get('status'), 'Первое сообщение подготовлено')}")
        if sent_event:
            timeline.append(f"{_format_dt(sent_event.get('sent_at'))} — первое сообщение отправлено")
        if dialog_state:
            updated = _format_dt(dialog_state.get("updated_at"))
            timeline.append(f"{updated} — {stage_label}" if updated else stage_label)

        if timeline:
            with st.expander("История ключевых событий"):
                for event in timeline:
                    if event:
                        st.write(f"• {event}")

        st.caption(
            "Первая версия карточки только читает уже существующие данные. "
            "Она ничего не отправляет и не меняет решения агентов."
        )
