"""Живая память диалога Неоны для Карточки человека 2.0.

Модуль не принимает решений за Неону и не меняет её ответ. После того как
действующая политика Неоны уже сформировала реплику, этот слой аккуратно
сохраняет в существующий context смысл текущего хода разговора.

Принцип: сохраняем не «досье», а рабочую память отношений. Подтверждёнными
считаются только сведения, которые человек сам явно сообщил в текущем
входящем сообщении. Если извлечение через ИИ недоступно, диалог продолжает
работать: память использует безопасный локальный fallback.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests


MEMORY_KEY = "relationship_memory"
MEMORY_VERSION = 1
MAX_FACTS = 12
MAX_NEEDS = 10
MAX_QUESTIONS = 10
MAX_PREFERENCES = 8
MAX_OBJECTIONS = 8
MAX_TURNS = 10
MAX_TEXT = 1200


_KIND_LABELS = {
    "interest": "интерес",
    "question": "вопрос",
    "objection": "возражение",
    "hard_stop": "просьба прекратить общение",
    "other": "обычный ответ",
}


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _unique_merge(existing: Any, new_items: Any, limit: int) -> list[str]:
    result: list[str] = []
    for source in (existing, new_items):
        if not isinstance(source, list):
            continue
        for raw in source:
            text = _clip(raw, 320)
            if not text:
                continue
            key = text.casefold()
            if any(item.casefold() == key for item in result):
                continue
            result.append(text)
    return result[-limit:]


def _parse_json_object(answer: str) -> dict[str, Any]:
    raw = str(answer or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _semantic_extract(config: Any, incoming_text: str) -> dict[str, Any]:
    """Извлекает только явно сообщённые человеком смыслы. Ошибки пробрасываются наружу."""
    if not getattr(config, "openai_api_key", ""):
        return {}

    instructions = """
Ты — внутренний архивариус Агентства W. Твоя задача — извлечь из ОДНОГО
входящего сообщения человека только рабочие смыслы для памяти отношений.

КРИТИЧЕСКИ ВАЖНО:
- не ставь диагнозы и не описывай характер человека;
- не угадывай мотивы, доход, профессию, проект, интересы или проблемы;
- confirmed_facts — только то, что человек сам явно сообщил как факт о себе/своей ситуации;
- goals_or_needs — только явно выраженные цели, желания, задачи или потребности;
- questions — только реально заданные вопросы;
- preferences — только явно выраженные предпочтения общения/формата/условий;
- objection_summary — только если в сообщении есть сомнение, препятствие или возражение;
- summary — одно короткое нейтральное предложение о смысле сообщения, без домыслов;
- если данных нет, возвращай пустые массивы/пустую строку.

Верни ТОЛЬКО JSON без Markdown:
{
  "summary": "",
  "confirmed_facts": [],
  "goals_or_needs": [],
  "questions": [],
  "preferences": [],
  "objection_summary": ""
}
""".strip()

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-5-mini",
            "instructions": instructions,
            "input": _clip(incoming_text, 4000),
            "store": False,
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return _parse_json_object("\n".join(parts))


def _fallback_summary(incoming_text: str, kind: str) -> str:
    text = _clip(incoming_text, 260)
    label = _KIND_LABELS.get(kind, "ответ")
    if not text:
        return f"Получен {label}."
    return f"{label.capitalize()}: {text}"


def _next_step_label(stage: str, context: dict[str, Any]) -> str:
    boundary = str(context.get("contact_boundary") or "").lower()
    if stage == "opted_out" or boundary in {"do_not_contact", "do-not-contact", "no_contact", "blocked"}:
        return "Не инициировать новый контакт. Ответить можно только если человек сам снова обратится."
    if stage == "scheduled":
        return "Встреча назначена. Следующий ключевой шаг — сама встреча."
    if stage in {"invited_to_meeting", "collecting_meeting_details", "awaiting_confirmation", "awaiting_slot_choice"}:
        return "Продолжить согласование встречи только в контексте нового ответа человека."
    return "Ждать следующего входящего сообщения. Самостоятельно напоминание не инициировать."


def remember_dialog_turn(
    config: Any,
    *,
    context: dict[str, Any] | None,
    incoming_text: str,
    reply_text: str,
    classification: dict[str, Any] | None,
    previous_stage: str,
    new_stage: str,
    message_dt: datetime | None,
) -> dict[str, Any]:
    """Возвращает context, дополненный безопасной памятью текущего хода."""
    result_context = dict(context or {})
    memory = result_context.get(MEMORY_KEY)
    memory = dict(memory) if isinstance(memory, dict) else {}

    classification = classification if isinstance(classification, dict) else {}
    kind = str(classification.get("kind") or "other")
    category = str(classification.get("category") or "").strip()

    event_at = message_dt
    if not isinstance(event_at, datetime):
        try:
            from datetime import timezone
            event_at = datetime.now(timezone.utc)
        except Exception:
            event_at = datetime.utcnow()
    try:
        event_iso = event_at.isoformat()
    except Exception:
        event_iso = ""

    extracted: dict[str, Any] = {}
    # Короткие «ок/спасибо» и жёсткий стоп не требуют второго ИИ-вызова.
    compact = _clip(incoming_text, 4000)
    worth_extracting = len(compact) >= 12 and kind != "hard_stop"
    if worth_extracting:
        try:
            extracted = _semantic_extract(config, compact)
        except Exception:
            # Память не имеет права сорвать рабочий диалог Неоны.
            extracted = {}

    summary = _clip(extracted.get("summary"), 320) or _fallback_summary(compact, kind)

    facts = _unique_merge(memory.get("confirmed_facts"), extracted.get("confirmed_facts"), MAX_FACTS)
    needs = _unique_merge(memory.get("goals_or_needs"), extracted.get("goals_or_needs"), MAX_NEEDS)
    questions = _unique_merge(memory.get("questions"), extracted.get("questions"), MAX_QUESTIONS)
    preferences = _unique_merge(memory.get("preferences"), extracted.get("preferences"), MAX_PREFERENCES)

    objections = memory.get("objections")
    objections = list(objections) if isinstance(objections, list) else []
    objection_summary = _clip(extracted.get("objection_summary"), 320)
    if kind in {"objection", "hard_stop"}:
        if not objection_summary:
            objection_summary = _clip(compact, 320)
        item = {
            "category": category or ("hard_stop" if kind == "hard_stop" else "other"),
            "summary": objection_summary,
            "at": event_iso,
            "status": "граница" if kind == "hard_stop" else "зафиксировано",
        }
        fingerprint = (item["category"].casefold(), item["summary"].casefold())
        already = False
        for existing in objections:
            if not isinstance(existing, dict):
                continue
            existing_fp = (
                str(existing.get("category") or "").casefold(),
                str(existing.get("summary") or "").casefold(),
            )
            if existing_fp == fingerprint:
                already = True
                break
        if not already:
            objections.append(item)
    objections = objections[-MAX_OBJECTIONS:]

    turns = memory.get("turns")
    turns = list(turns) if isinstance(turns, list) else []
    turns.append(
        {
            "at": event_iso,
            "kind": kind,
            "category": category,
            "summary": summary,
            "incoming": _clip(compact, 500),
            "neona_reply": _clip(reply_text, 500),
            "stage_before": str(previous_stage or "idle"),
            "stage_after": str(new_stage or previous_stage or "idle"),
        }
    )
    turns = turns[-MAX_TURNS:]

    memory.update(
        {
            "version": MEMORY_VERSION,
            "last_turn_at": event_iso,
            "last_kind": kind,
            "last_category": category,
            "last_summary": summary,
            "last_incoming": _clip(compact, 700),
            "last_reply": _clip(reply_text, 700),
            "confirmed_facts": facts,
            "goals_or_needs": needs,
            "questions": questions,
            "preferences": preferences,
            "objections": objections,
            "turns": turns,
            "next_step": _next_step_label(str(new_stage or previous_stage or "idle"), result_context),
        }
    )
    result_context[MEMORY_KEY] = memory
    return result_context
