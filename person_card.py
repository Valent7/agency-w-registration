"""Карточка человека 2.0 — read-only слой общей памяти Агентства W.

Карточка сама ничего не отправляет и не принимает решений. Она собирает
существующий контекст и живую память диалога Неоны в одну понятную карточку
для Директора.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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


_NEONA_MAGNET_REFERENCE = (
    (
        "Вернуть человеку время",
        "Показать, какую часть повторяющейся работы можно снять с человека и освободить его время.",
    ),
    (
        "Своя ИИ-команда",
        "Показать идею команды специализированных ИИ-помощников, а не одного универсального чата.",
    ),
    (
        "Усилить существующий проект",
        "Не предлагать человеку бросать своё дело, а показать, как ИИ может усилить то, что он уже делает.",
    ),
    (
        "Найти подходящих людей",
        "Показать, что не обязательно писать всем подряд: сначала можно понять, с кем разговор действительно имеет смысл.",
    ),
    (
        "Начать разговор без навязывания",
        "Начать с нормального человеческого разговора и пользы для конкретного человека, а не с рекламной рассылки.",
    ),
    (
        "Сопровождение новичка",
        "Показать, что часть первых шагов, навигации и рутинного сопровождения нового партнёра может взять ИИ-наставник.",
    ),
    (
        "Агентство понимает конкретный проект",
        "Показать, что помощники работают в контексте конкретного проекта и его аудитории, а не одинаково для всех.",
    ),
    (
        "Человек остаётся главным",
        "Подчеркнуть: ИИ снимает подготовку и рутину, а отношения, выбор и важные решения остаются за человеком.",
    ),
)


def render_neona_magnets_reference(selected_magnet: str = "") -> None:
    """Показывает справочник магнитов без возможности изменить уже отправленное сообщение."""
    current = str(selected_magnet or "").strip()
    with st.expander("🧲 Посмотреть все 8 магнитов"):
        st.caption(
            "Это справочник. После отправки первого сообщения магнит не меняется "
            "и ничего человеку отсюда не отправляется."
        )
        for name, meaning in _NEONA_MAGNET_REFERENCE:
            marker = " — **использован в этом сообщении**" if name == current else ""
            st.markdown(f"**{name}**{marker}")
            st.write(meaning)


def _safe_text(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback


_BERLIN_TZ = ZoneInfo("Europe/Berlin")


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_BERLIN_TZ)
        return parsed.astimezone(_BERLIN_TZ)
    except Exception:
        return None


def _format_dt(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed is not None:
        return parsed.strftime("%d.%m.%Y %H:%M")
    raw = str(value or "").strip()
    return raw[:16].replace("T", " ") if raw else ""


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
        return "⛔ Человек попросил не писать. Инициировать новый контакт нельзя.", True
    if raw:
        return raw, False
    if stage == "scheduled":
        return "Встреча согласована. Не инициировать дополнительные сообщения без необходимости.", False
    return "Ждём ответа. Повторно не писать без нового входящего сообщения.", False


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
    candidates: list[tuple[datetime | None, str]] = []

    analyzed_raw = contact.get("analyzed_at")
    analyzed_at = _format_dt(analyzed_raw)
    if analyzed_at:
        candidates.append((_parse_dt(analyzed_raw), f"Неония обновила анализ · {analyzed_at}"))

    if sent_event:
        sent_raw = sent_event.get("sent_at")
        sent_at = _format_dt(sent_raw)
        candidates.append((_parse_dt(sent_raw), f"Первое сообщение отправлено · {sent_at}"))
    elif draft:
        status = _safe_text(draft.get("status"), "Сообщение подготовлено")
        candidates.append((None, status))

    # idle создаётся технически сразу после первого сообщения и не является
    # отдельным значимым событием для Директора.
    stage = _safe_text(dialog_state.get("stage"), "")
    updated_raw = dialog_state.get("updated_at")
    updated_at = _format_dt(updated_raw)
    if updated_at and stage and stage != "idle":
        label = _STAGE_LABELS.get(stage, stage)
        candidates.append((_parse_dt(updated_raw), f"Неона: {label} · {updated_at}"))

    dated = [item for item in candidates if item[0] is not None]
    if dated:
        return max(dated, key=lambda item: item[0])[1]
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


def _relationship_memory(context: dict[str, Any]) -> dict[str, Any]:
    raw = context.get("relationship_memory")
    return dict(raw) if isinstance(raw, dict) else {}


def _memory_objections(memory: dict[str, Any]) -> list[str]:
    result: list[str] = []
    raw = memory.get("objections")
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, dict):
            text = _safe_text(item.get("summary"), "")
        else:
            text = _safe_text(item, "")
        if text and text not in result:
            result.append(text)
    return result[-5:]


def _render_memory_section(memory: dict[str, Any]) -> None:
    if not memory:
        st.caption("Живая память появится после первого нового ответа человека.")
        return

    st.write("**Последний смысл ответа человека:**")
    st.write(_safe_text(memory.get("last_summary"), "Получен новый ответ."))

    facts = memory.get("confirmed_facts") if isinstance(memory.get("confirmed_facts"), list) else []
    needs = memory.get("goals_or_needs") if isinstance(memory.get("goals_or_needs"), list) else []
    questions = memory.get("questions") if isinstance(memory.get("questions"), list) else []
    preferences = memory.get("preferences") if isinstance(memory.get("preferences"), list) else []

    if facts:
        st.write("**Что человек сам сообщил:**")
        for item in facts[-6:]:
            st.write(f"• {_safe_text(item)}")
    if needs:
        st.write("**Цели / задачи / потребности, прозвучавшие в разговоре:**")
        for item in needs[-5:]:
            st.write(f"• {_safe_text(item)}")
    if questions:
        st.write("**Вопросы человека:**")
        for item in questions[-5:]:
            st.write(f"• {_safe_text(item)}")
    if preferences:
        st.write("**Явно выраженные предпочтения:**")
        for item in preferences[-4:]:
            st.write(f"• {_safe_text(item)}")

    last_reply = _safe_text(memory.get("last_reply"), "")
    if last_reply:
        with st.expander("Последний ответ Неоны"):
            st.write(last_reply)

    turns = memory.get("turns") if isinstance(memory.get("turns"), list) else []
    if turns:
        with st.expander("Живая история диалога"):
            for turn in turns[-8:]:
                if not isinstance(turn, dict):
                    continue
                at = _format_dt(turn.get("at"))
                summary = _safe_text(turn.get("summary"), "Новый ход диалога")
                kind = _safe_text(turn.get("kind"), "other")
                labels = {
                    "interest": "интерес",
                    "question": "вопрос",
                    "objection": "возражение",
                    "hard_stop": "граница общения",
                    "other": "ответ",
                }
                label = labels.get(kind, kind)
                prefix = f"{at} — " if at else ""
                st.write(f"• {prefix}{label}: {summary}")


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
    relationship_memory = _relationship_memory(context)
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
        hypothesis = _safe_text(contact.get("short_portrait"), "")
        reasons = contact.get("reasons") if isinstance(contact.get("reasons"), list) else []
        filtered_reasons: list[str] = []
        for reason in reasons:
            text = _safe_text(reason, "")
            if not text or text == hypothesis or text in filtered_reasons:
                continue
            filtered_reasons.append(text)

        if owner_hint or filtered_reasons:
            st.write("**Почему человек оказался в работе:**")
            if owner_hint:
                st.write(owner_hint)
            for text in filtered_reasons[:3]:
                st.write(f"• {text}")

        confirmed_facts: list[str] = []
        profile_about = _safe_text(contact.get("profile_about"), "")
        if profile_about:
            confirmed_facts.append(f"Bio профиля: {profile_about}")

        project_name = _safe_text(contact.get("project_name"), "")
        project_evidence = _safe_text(contact.get("project_evidence"), "")
        if project_name and project_evidence:
            confirmed_facts.append(f"Проект/компания: {project_name}")
            confirmed_facts.append(f"Подтверждение связи: {project_evidence}")

        st.write("**Подтверждено сейчас:**")
        if confirmed_facts:
            for fact in confirmed_facts[:5]:
                st.write(f"• {fact}")
        else:
            st.caption("Подтверждённых сведений пока немного.")

        if hypothesis:
            st.write("**Гипотеза Неонии:**")
            st.write(hypothesis)
            st.caption("Это аналитический вывод, а не подтверждённый факт о человеке.")

        objections = _memory_objections(relationship_memory) or _collect_objections(context)
        st.write(f"**Границы общения:** {boundary_label}")
        if objections:
            st.write("**Зафиксированные возражения:**")
            for item in objections:
                st.write(f"• {item}")
        else:
            st.caption("Возражения пока не зафиксированы в общей карточке.")

        st.divider()
        st.write("### 💬 Живая память Неоны")
        _render_memory_section(relationship_memory)

        timeline: list[str] = []
        analyzed_at = _format_dt(contact.get("analyzed_at"))
        if analyzed_at:
            timeline.append(f"{analyzed_at} — анализ Неонии")
        if sent_event:
            timeline.append(f"{_format_dt(sent_event.get('sent_at'))} — первое сообщение отправлено")
        elif draft:
            timeline.append(f"{_safe_text(draft.get('status'), 'Первое сообщение подготовлено')}")

        dialog_stage = _safe_text(dialog_state.get("stage"), "")
        memory_at = _format_dt(relationship_memory.get("last_turn_at")) if relationship_memory else ""
        if memory_at:
            memory_summary = _safe_text(relationship_memory.get("last_summary"), "получен новый ответ")
            timeline.append(f"{memory_at} — диалог Неоны: {memory_summary}")
        elif dialog_state and dialog_stage and dialog_stage != "idle":
            updated = _format_dt(dialog_state.get("updated_at"))
            timeline.append(f"{updated} — {stage_label}" if updated else stage_label)

        if timeline:
            with st.expander("История ключевых событий"):
                for event in timeline:
                    if event:
                        st.write(f"• {event}")

        st.caption(
            "Карточка сама ничего не отправляет и не принимает решений. "
            "Живую память записывает Неона после реального входящего сообщения."
        )
