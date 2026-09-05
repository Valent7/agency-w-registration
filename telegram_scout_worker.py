from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import GetContactsRequest, GetStatusesRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)


UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
DEFAULT_INTERVAL_SECONDS = 3600
DAILY_TARGET = 5
RESERVE_TARGET = 50
OUTREACH_COOLDOWN_DAYS = 7


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Не найдена настройка {name}")
    return value


def _supabase_url() -> str:
    return _required_env("SUPABASE_URL").rstrip("/")


def _supabase_key() -> str:
    return _required_env("SUPABASE_SECRET_KEY")


def _headers(prefer: str | None = None) -> dict[str, str]:
    key = _supabase_key()
    result = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        result["Prefer"] = prefer
    return result


def _cipher() -> Fernet:
    return Fernet(_required_env("FERNET_KEY").encode("utf-8"))


def _decrypt_text(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return ""


def _encrypt_json(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _cipher().encrypt(raw).decode("utf-8")


def _decrypt_json(value: str) -> dict[str, Any]:
    raw = _decrypt_text(value)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sb_get(table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.get(
        f"{_supabase_url()}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _sb_post(table: str, payload: dict[str, Any], *, prefer: str = "return=representation") -> list[dict[str, Any]]:
    response = requests.post(
        f"{_supabase_url()}/rest/v1/{table}",
        headers=_headers(prefer),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    if "return=representation" not in prefer:
        return []
    data = response.json()
    return data if isinstance(data, list) else []


def _sb_patch(
    table: str,
    filters: dict[str, Any],
    payload: dict[str, Any],
    *,
    prefer: str = "return=representation",
) -> list[dict[str, Any]]:
    response = requests.patch(
        f"{_supabase_url()}/rest/v1/{table}",
        headers=_headers(prefer),
        params=filters,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    if "return=representation" not in prefer:
        return []
    data = response.json()
    return data if isinstance(data, list) else []


def _owner_sessions() -> list[dict[str, Any]]:
    return _sb_get(
        "telegram_sessions",
        {
            "select": "telegram_id,encrypted_session",
            "order": "telegram_id.asc",
            "limit": 1000,
        },
    )


def _workspace_row(owner_id: int) -> dict[str, Any] | None:
    rows = _sb_get(
        "agency_workspace_states",
        {
            "telegram_id": f"eq.{int(owner_id)}",
            "select": "telegram_id,encrypted_state,updated_at",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _workspace_state(owner_id: int) -> tuple[dict[str, Any], str]:
    row = _workspace_row(owner_id)
    if not row:
        return {}, ""
    return (
        _decrypt_json(str(row.get("encrypted_state") or "")),
        str(row.get("updated_at") or ""),
    )


def _normalize_candidate_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        result.append({**item, "telegram_id": cid})
    return result


def _merge_candidate_results(
    existing: list[dict[str, Any]],
    new_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[int, int] = {}

    for item in _normalize_candidate_list(existing):
        cid = int(item["telegram_id"])
        positions[cid] = len(merged)
        merged.append(item)

    for item in _normalize_candidate_list(new_results):
        cid = int(item["telegram_id"])
        if cid in positions:
            previous = merged[positions[cid]]
            # Сохраняем пользовательский статус/решение, но обновляем фактический контекст.
            preserved_status = previous.get("status")
            combined = {**previous, **item}
            if preserved_status:
                combined["status"] = preserved_status
            merged[positions[cid]] = combined
        else:
            positions[cid] = len(merged)
            merged.append(item)

    return merged


def _save_workspace_candidates(
    owner_id: int,
    contacts: list[dict[str, Any]],
    new_candidates: list[dict[str, Any]],
) -> bool:
    """Сохраняет только обновлённые контакты/кандидатов поверх самой свежей версии workspace."""
    for _attempt in range(3):
        row = _workspace_row(owner_id)
        if not row:
            return False

        current = _decrypt_json(str(row.get("encrypted_state") or ""))
        if not current:
            return False

        current["contacts"] = contacts
        current["contacts_search_done"] = True
        current["candidates"] = _merge_candidate_results(
            current.get("candidates", []),
            new_candidates,
        )
        current["schema_version"] = max(
            5,
            int(current.get("schema_version") or 5),
        )

        old_updated_at = str(row.get("updated_at") or "")
        now_iso = datetime.now(UTC).isoformat()
        filters: dict[str, Any] = {
            "telegram_id": f"eq.{int(owner_id)}",
        }
        if old_updated_at:
            filters["updated_at"] = f"eq.{old_updated_at}"

        changed = _sb_patch(
            "agency_workspace_states",
            filters,
            {
                "encrypted_state": _encrypt_json(current),
                "updated_at": now_iso,
            },
        )
        if changed:
            return True
        time.sleep(0.4)

    return False


def _telegram_session_from_row(row: dict[str, Any]) -> str:
    return _decrypt_text(str(row.get("encrypted_session") or ""))


def _telegram_api_credentials() -> tuple[int, str]:
    return int(_required_env("TELEGRAM_API_ID")), _required_env("TELEGRAM_API_HASH")


def _activity_record(status: Any) -> dict[str, Any]:
    now_local = datetime.now(BERLIN)
    yesterday = now_local.date() - timedelta(days=1)
    result: dict[str, Any] = {
        "activity_eligible": False,
        "telegram_activity_label": "активность не подтверждена",
        "last_seen_at": "",
        "activity_precision": "unknown",
    }

    if isinstance(status, UserStatusOnline):
        result.update(
            {
                "activity_eligible": True,
                "telegram_activity_label": "сейчас онлайн",
                "last_seen_at": now_local.isoformat(),
                "activity_precision": "exact",
            }
        )
        return result

    if isinstance(status, UserStatusOffline):
        was_online = getattr(status, "was_online", None)
        if isinstance(was_online, (int, float)):
            was_online = datetime.fromtimestamp(was_online, tz=UTC)
        if isinstance(was_online, datetime):
            if was_online.tzinfo is None:
                was_online = was_online.replace(tzinfo=UTC)
            local_seen = was_online.astimezone(BERLIN)
            seen_date = local_seen.date()
            if seen_date == now_local.date():
                label = f"сегодня в {local_seen:%H:%M}"
            elif seen_date == yesterday:
                label = f"вчера в {local_seen:%H:%M}"
            else:
                label = f"{local_seen:%d.%m.%Y в %H:%M}"
            result.update(
                {
                    "activity_eligible": seen_date >= yesterday,
                    "telegram_activity_label": label,
                    "last_seen_at": local_seen.isoformat(),
                    "activity_precision": "exact",
                }
            )
        return result

    if isinstance(status, UserStatusRecently):
        result.update(
            {
                "telegram_activity_label": "был недавно — точное время скрыто",
                "activity_precision": "approx_recently",
            }
        )
        return result

    if isinstance(status, UserStatusLastWeek):
        result.update(
            {
                "telegram_activity_label": "был на прошлой неделе",
                "activity_precision": "approx_week",
            }
        )
        return result

    if isinstance(status, UserStatusLastMonth):
        result.update(
            {
                "telegram_activity_label": "был в прошлом месяце",
                "activity_precision": "approx_month",
            }
        )
        return result

    if isinstance(status, UserStatusEmpty) or status is None:
        return result
    return result


def _activity_tier(activity: dict[str, Any]) -> int | None:
    if activity.get("activity_eligible") is True:
        return 0

    precision = str(activity.get("activity_precision") or "").strip().lower()
    if precision == "approx_recently":
        return 1

    if precision == "exact":
        raw_seen = str(activity.get("last_seen_at") or "").strip()
        if raw_seen:
            try:
                seen_at = datetime.fromisoformat(raw_seen.replace("Z", "+00:00"))
                if seen_at.tzinfo is None:
                    seen_at = seen_at.replace(tzinfo=UTC)
                if datetime.now(UTC) - seen_at.astimezone(UTC) <= timedelta(days=7):
                    return 2
            except Exception:
                pass

    if precision == "approx_week":
        return 3
    return None


async def _fetch_contacts_and_statuses(session_string: str) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    api_id, api_hash = _telegram_api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return [], {}

        contacts_result = await client(GetContactsRequest(hash=0))
        contacts: list[dict[str, Any]] = []
        for user in contacts_result.users:
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            first_name = str(getattr(user, "first_name", "") or "").strip()
            last_name = str(getattr(user, "last_name", "") or "").strip()
            name = " ".join(part for part in (first_name, last_name) if part).strip()
            contacts.append(
                {
                    "telegram_id": int(user.id),
                    "name": name or "Без имени",
                    "first_name": first_name,
                    "username": str(getattr(user, "username", "") or "").strip(),
                    "phone": str(getattr(user, "phone", "") or "").strip(),
                }
            )

        statuses = await client(GetStatusesRequest())
        activity_by_id: dict[int, dict[str, Any]] = {}
        for item in statuses:
            try:
                cid = int(getattr(item, "user_id"))
            except (TypeError, ValueError):
                continue
            activity_by_id[cid] = _activity_record(getattr(item, "status", None))

        return contacts, activity_by_id
    finally:
        await client.disconnect()


async def _fetch_contexts(
    session_string: str,
    contacts_batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    api_id, api_hash = _telegram_api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    contexts: list[dict[str, Any]] = []
    try:
        if not await client.is_user_authorized():
            return []

        for contact in contacts_batch:
            context = {
                "telegram_id": int(contact["telegram_id"]),
                "name": str(contact.get("name") or "Без имени"),
                "first_name": str(contact.get("first_name") or ""),
                "username": str(contact.get("username") or ""),
                "about": "",
                "mutual_contact": False,
                "verified": False,
                "telegram_warning": False,
                "is_owner_contact": True,
                "recent_messages": [],
                "has_message_history": False,
                "message_history_count": 0,
                "activity_eligible": bool(contact.get("activity_eligible", False)),
                "telegram_activity_label": str(
                    contact.get("telegram_activity_label") or "активность не подтверждена"
                ),
                "last_seen_at": str(contact.get("last_seen_at") or ""),
                "activity_precision": str(contact.get("activity_precision") or "unknown"),
                "_stagirite_activity_tier": contact.get("_stagirite_activity_tier"),
            }
            try:
                entity = await client.get_entity(int(contact["telegram_id"]))
                context["mutual_contact"] = bool(getattr(entity, "mutual_contact", False))
                context["verified"] = bool(getattr(entity, "verified", False))
                context["telegram_warning"] = bool(
                    getattr(entity, "scam", False) or getattr(entity, "fake", False)
                )

                try:
                    full_user = await client(GetFullUserRequest(entity))
                    context["about"] = str(
                        getattr(full_user.full_user, "about", "") or ""
                    )[:700]
                except Exception:
                    pass

                try:
                    async for message in client.iter_messages(entity, limit=6):
                        context["has_message_history"] = True
                        context["message_history_count"] = int(context.get("message_history_count") or 0) + 1
                        message_text = str(getattr(message, "message", "") or "").strip()
                        context["recent_messages"].append(
                            {
                                "direction": "от владельца" if getattr(message, "out", False) else "от контакта",
                                "text": message_text[:500],
                                "date": message.date.isoformat() if getattr(message, "date", None) else "",
                            }
                        )
                except Exception:
                    pass
            except Exception:
                pass
            contexts.append(context)
        return contexts
    finally:
        await client.disconnect()


def _parse_message_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _latest_owner_outreach_at(source: dict[str, Any]) -> datetime | None:
    latest: datetime | None = None
    for item in source.get("recent_messages", []) if isinstance(source.get("recent_messages"), list) else []:
        if not isinstance(item, dict) or item.get("direction") != "от владельца":
            continue
        dt = _parse_message_time(item.get("date"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _looks_like_explicit_refusal(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    patterns = (
        r"\bнет[, ]+(?:мне )?не ?интерес",
        r"\bне ?интересно\b",
        r"\bнеинтересно\b",
        r"\bне хочу\b",
        r"\bне надо мне\b",
        r"\bне пишите\b",
        r"\bне пиши\b",
        r"\bне беспокойте\b",
        r"\bне беспокой\b",
        r"\bнет[, ]+спасибо\b",
        r"\bkein interesse\b",
        r"\bnicht interessiert\b",
        r"\bnot interested\b",
        r"\bno thanks\b",
        r"\bplease stop\b",
        r"\bstop messaging\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _classify_workability(
    source: dict[str, Any],
    *,
    already_sent_ids: set[int],
    known_contact_ids: set[int],
    blocked_ids: set[int],
) -> dict[str, Any]:
    cid = int(source["telegram_id"])
    if cid in blocked_ids:
        return {
            "work_state": "blocked",
            "work_state_label": "Контакт отложен/заблокирован для первого сообщения",
            "selection_blocked": True,
            "block_reason": "архив первого сообщения",
        }

    recent = source.get("recent_messages", []) if isinstance(source.get("recent_messages"), list) else []
    inbound = [
        item for item in recent
        if isinstance(item, dict)
        and item.get("direction") == "от контакта"
        and str(item.get("text") or "").strip()
    ]
    outbound = [
        item for item in recent
        if isinstance(item, dict)
        and item.get("direction") == "от владельца"
        and str(item.get("text") or "").strip()
    ]

    latest_inbound = str(inbound[0].get("text") or "") if inbound else ""
    if _looks_like_explicit_refusal(latest_inbound):
        return {
            "work_state": "explicit_refusal",
            "work_state_label": "Явный отказ — не предлагать",
            "selection_blocked": True,
            "block_reason": "явный отказ",
        }

    # Для ежедневной холодной пятёрки допускаются только контакты без любой
    # прежней истории личной переписки. Знакомых/старые диалоги владелец может
    # открыть отдельно через сценарий «Найти знакомого».
    if bool(source.get("has_message_history")) or bool(recent):
        return {
            "work_state": "existing_history",
            "work_state_label": "Есть история переписки — не выдавать как новый холодный контакт",
            "selection_blocked": True,
            "block_reason": "есть прежняя история личной переписки",
        }

    if cid in already_sent_ids:
        return {
            "work_state": "already_contacted",
            "work_state_label": "Первое сообщение уже отправлено — ждём ответ",
            "selection_blocked": True,
            "block_reason": "первое сообщение уже отправлено",
        }

    last_outreach = _latest_owner_outreach_at(source)
    if last_outreach is not None:
        age = datetime.now(UTC) - last_outreach.astimezone(UTC)
        cooldown = timedelta(days=OUTREACH_COOLDOWN_DAYS)
        if timedelta(0) <= age < cooldown:
            remaining = cooldown - age
            remaining_days = max(1, int((remaining.total_seconds() + 86399) // 86400))
            return {
                "work_state": "cooldown",
                "work_state_label": f"Пауза после недавнего обращения — ещё {remaining_days} дн.",
                "selection_blocked": True,
                "block_reason": "слишком недавнее обращение",
                "last_owner_outreach_at": last_outreach.astimezone(BERLIN).isoformat(),
                "cooldown_until": (last_outreach.astimezone(UTC) + cooldown).isoformat(),
            }

    if inbound and outbound:
        return {
            "work_state": "dialogue_started",
            "work_state_label": "Диалог уже начат",
            "selection_blocked": True,
            "block_reason": "диалог уже начат",
        }

    if cid in known_contact_ids:
        return {
            "work_state": "warm_contact",
            "work_state_label": "Уже в тёплых контактах владельца",
            "selection_blocked": True,
            "block_reason": "уже в тёплых контактах",
        }

    if bool(source.get("telegram_warning")):
        return {
            "work_state": "telegram_warning",
            "work_state_label": "Профиль Telegram требует осторожности",
            "selection_blocked": True,
            "block_reason": "предупреждение Telegram",
        }

    return {
        "work_state": "available",
        "work_state_label": "Можно начинать новый разговор",
        "selection_blocked": False,
        "block_reason": "",
    }


def _split_analysis_value(contexts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    informative: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []
    for source in contexts:
        chunks: list[str] = []
        for key in ("about", "bio", "description", "context", "dialogue", "messages", "recent_messages", "public_info"):
            value = source.get(key)
            if isinstance(value, list):
                value = " ".join(str(item) for item in value if str(item).strip())
            if value is not None and str(value).strip():
                chunks.append(str(value).strip())
        if len(" ".join(chunks).strip()) >= 40:
            informative.append(source)
        else:
            empty.append(source)
    return informative, empty


def _build_no_data_candidate(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "telegram_id": int(source["telegram_id"]),
        "name": source.get("name") or "Без имени",
        "first_name": source.get("first_name") or "",
        "username": source.get("username") or "",
        "activity_eligible": bool(source.get("activity_eligible", False)),
        "telegram_activity_label": str(source.get("telegram_activity_label") or "активность не подтверждена"),
        "last_seen_at": str(source.get("last_seen_at") or ""),
        "activity_precision": str(source.get("activity_precision") or "unknown"),
        "work_state": str(source.get("work_state") or "available"),
        "work_state_label": str(source.get("work_state_label") or "Можно начинать новый разговор"),
        "selection_blocked": bool(source.get("selection_blocked", False)),
        "block_reason": str(source.get("block_reason") or ""),
        "last_owner_outreach_at": str(source.get("last_owner_outreach_at") or ""),
        "cooldown_until": str(source.get("cooldown_until") or ""),
        "potential_interest": "неясно",
        "actuality": "активен сейчас",
        "warmth": "неясно",
        "obstacles": ["недостаточно данных"],
        "short_portrait": "Данных о человеке пока недостаточно. Неония не видит признаков для уверенной оценки интереса.",
        "owner_hint": "Если вы знаете этого человека лично, решение о разговоре лучше принять на основании вашего знакомства.",
        "message_angle": "Начать с обычного человеческого обращения без предположений об интересах.",
        "project_name": "",
        "project_url": "",
        "project_evidence": "",
        "segment": "Недостаточно данных",
        "score": 0,
        "confidence": "данных недостаточно",
        "reasons": ["Недостаточно содержательных данных для ИИ-анализа"],
        "recommendation": "Решает владелец",
        "status": "Проанализирован без OpenAI",
        "analysis_cost_mode": "local_no_ai",
        "analyzed_at": datetime.now(BERLIN).isoformat(),
        "is_owner_contact": True,
    }


def _extract_response_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                value = str(content.get("text") or "").strip()
                if value:
                    parts.append(value)
    return "\n".join(parts).strip()


def _ask_openai(system_prompt: str, user_message: str) -> str:
    api_key = _required_env("OPENAI_API_KEY")
    model = _env("TELEGRAM_SCOUT_OPENAI_MODEL", "gpt-5-mini") or "gpt-5-mini"
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": system_prompt,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_message}],
                }
            ],
            "store": False,
        },
        timeout=180,
    )
    response.raise_for_status()
    text = _extract_response_text(response.json())
    if not text:
        raise RuntimeError("OpenAI не вернул текст")
    return text


def _json_array_from_text(text: str) -> list[dict[str, Any]]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _analyze_candidates(passport_analysis: Any, contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not contexts:
        return []

    system_prompt = """
Ты — Неония, аналитик людей Агентства W.
У тебя есть сохранённый портрет целевой аудитории проекта владельца и данные контактов Telegram.
Оценивай только по имеющимся данным, ничего не выдумывай и не решай за владельца.

Для каждого контакта верни JSON-объект с полями:
telegram_id;
potential_interest — строго «высокий», «средний» или «низкий»;
actuality — «активен сейчас», «неясно» или «давно неактивен»;
warmth — «знакомый», «поверхностно знакомый» или «холодный»;
obstacles — 0–3 реальных препятствия;
short_portrait — 1–2 предложения;
owner_hint — короткая спокойная рекомендация;
message_angle — с какой человеческой стороны начать разговор;
project_name, project_url, project_evidence — только если явно есть в данных.

Важно: старый интерес к ИИ/криптовалюте/MLM сам по себе не делает человека приоритетным; другой действующий проект обязательно отражай в obstacles; если данных мало, прямо говори об этом; не оценивай по имени, полу, возрасту, национальности или фотографии.
Верни ТОЛЬКО JSON-массив без Markdown.
""".strip()

    request = (
        "ПОРТРЕТ ЦЕЛЕВОЙ АУДИТОРИИ — ОРИЕНТИР:\n"
        + json.dumps(passport_analysis, ensure_ascii=False, default=str)
        + "\n\nКОНТАКТЫ:\n"
        + json.dumps(contexts, ensure_ascii=False, default=str)
    )
    raw = _json_array_from_text(_ask_openai(system_prompt, request))
    source_by_id = {int(item["telegram_id"]): item for item in contexts}
    normalized: list[dict[str, Any]] = []

    allowed_interest = {"высокий", "средний", "низкий"}
    allowed_actuality = {"активен сейчас", "неясно", "давно неактивен"}
    allowed_warmth = {"знакомый", "поверхностно знакомый", "холодный"}

    for item in raw:
        try:
            cid = int(item.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        source = source_by_id.get(cid)
        if not source:
            continue

        interest = str(item.get("potential_interest") or "средний").strip().lower()
        if interest not in allowed_interest:
            interest = "средний"
        actuality = str(item.get("actuality") or "неясно").strip().lower()
        if actuality not in allowed_actuality:
            actuality = "неясно"
        warmth = str(item.get("warmth") or "холодный").strip().lower()
        if warmth not in allowed_warmth:
            warmth = "холодный"
        obstacles = item.get("obstacles") or []
        if isinstance(obstacles, str):
            obstacles = [obstacles]
        if not isinstance(obstacles, list):
            obstacles = []
        obstacles = [str(value).strip()[:250] for value in obstacles[:3] if str(value).strip()]
        score = {"высокий": 75, "средний": 50, "низкий": 25}[interest]

        normalized.append(
            {
                "telegram_id": cid,
                "name": source.get("name") or "Без имени",
                "first_name": source.get("first_name") or "",
                "username": source.get("username") or "",
                "activity_eligible": bool(source.get("activity_eligible", False)),
                "telegram_activity_label": str(source.get("telegram_activity_label") or "активность не подтверждена"),
                "last_seen_at": str(source.get("last_seen_at") or ""),
                "activity_precision": str(source.get("activity_precision") or "unknown"),
                "work_state": str(source.get("work_state") or "available"),
                "work_state_label": str(source.get("work_state_label") or "Можно начинать новый разговор"),
                "selection_blocked": bool(source.get("selection_blocked", False)),
                "block_reason": str(source.get("block_reason") or ""),
                "last_owner_outreach_at": str(source.get("last_owner_outreach_at") or ""),
                "cooldown_until": str(source.get("cooldown_until") or ""),
                "potential_interest": interest,
                "actuality": actuality,
                "warmth": warmth,
                "obstacles": obstacles,
                "short_portrait": str(item.get("short_portrait") or "Данных пока немного.").strip()[:700],
                "owner_hint": str(item.get("owner_hint") or "Решение о разговоре принимает владелец.").strip()[:500],
                "message_angle": str(item.get("message_angle") or "Спокойное человеческое знакомство").strip()[:500],
                "project_name": str(item.get("project_name") or "")[:180],
                "project_url": str(item.get("project_url") or "")[:900],
                "project_evidence": str(item.get("project_evidence") or "")[:500],
                "segment": interest.capitalize() + " потенциальный интерес",
                "score": score,
                "confidence": "аналитическая оценка",
                "reasons": [str(item.get("short_portrait") or "Данных пока немного.")[:300]],
                "recommendation": "Решает владелец",
                "status": "Проанализирован",
                "analysis_cost_mode": "openai",
                "analyzed_at": datetime.now(BERLIN).isoformat(),
                "is_owner_contact": True,
            }
        )

    returned = {int(item["telegram_id"]) for item in normalized}
    for source in contexts:
        cid = int(source["telegram_id"])
        if cid not in returned:
            normalized.append(_build_no_data_candidate(source))
    return normalized


def _candidate_priority(item: dict[str, Any]) -> tuple[Any, ...]:
    interest = str(item.get("potential_interest") or "неясно").strip().lower()
    obstacles_text = " ".join(str(value).lower() for value in (item.get("obstacles") or []))
    competing = any(
        token in obstacles_text
        for token in (
            "другой проект",
            "других сетев",
            "другие сетев",
            "сетевых",
            "заработных предлож",
            "активно продвигает",
            "конкурирующ",
        )
    )
    if competing:
        group = 4
    elif interest == "высокий":
        group = 0
    elif interest == "средний":
        group = 1
    elif item.get("analysis_cost_mode") == "local_no_ai" or interest == "неясно":
        group = 2
    else:
        group = 3

    warmth_rank = {
        "знакомый": 0,
        "поверхностно знакомый": 1,
        "холодный": 2,
        "неясно": 3,
    }.get(str(item.get("warmth") or "").strip().lower(), 3)
    tier = int(item.get("_stagirite_activity_tier") or 0)
    raw_seen = str(item.get("last_seen_at") or "").strip()
    try:
        seen_rank = -datetime.fromisoformat(raw_seen.replace("Z", "+00:00")).timestamp()
    except Exception:
        seen_rank = 0.0
    return (group, warmth_rank, tier, seen_rank, str(item.get("name") or "").lower())


def _ids_from_list(value: Any) -> set[int]:
    result: set[int] = set()
    if not isinstance(value, list):
        return result
    for item in value:
        try:
            if isinstance(item, dict):
                item = item.get("telegram_id")
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _known_contact_ids(state: dict[str, Any]) -> set[int]:
    raw = state.get("owner_known_contacts", [])
    if isinstance(raw, dict):
        values = list(raw.values())
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    return _ids_from_list(values)


def _sent_ids(state: dict[str, Any]) -> set[int]:
    return _ids_from_list(state.get("sent_log", []))


def _blocked_ids(state: dict[str, Any]) -> set[int]:
    return _ids_from_list(state.get("blocked_first_messages", []))


def _load_tasks(owner_id: int) -> list[dict[str, Any]]:
    return _sb_get(
        "agency_stagirite_tasks",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "select": "*",
            "order": "created_at.desc",
            "limit": 100,
        },
    )


def _shown_candidate_ids(tasks: list[dict[str, Any]], *, exclude_day: str = "") -> set[int]:
    shown: set[int] = set()
    for task in tasks:
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        for container_name in ("daily_candidate_feed", "weekly_goal"):
            container = result.get(container_name)
            if not isinstance(container, dict):
                continue
            batches = container.get("daily_batches")
            if not isinstance(batches, dict):
                continue
            for day_key, info in batches.items():
                if exclude_day and str(day_key) == exclude_day:
                    continue
                if not isinstance(info, dict):
                    continue
                shown.update(_ids_from_list(info.get("candidate_ids", [])))
                shown.update(_ids_from_list(info.get("approved_ids", [])))
    return shown


def _active_weekly_task(tasks: list[dict[str, Any]], today) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        goal = result.get("weekly_goal") if isinstance(result.get("weekly_goal"), dict) else None
        if not goal:
            continue
        try:
            start = datetime.fromisoformat(str(goal.get("period_start") or today.isoformat())).date()
            end = datetime.fromisoformat(str(goal.get("period_end") or today.isoformat())).date()
        except Exception:
            start, end = today, today + timedelta(days=6)

        assignment = str(task.get("assignment") or "")
        if start > today and not re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b", assignment):
            start = today
            end = today + timedelta(days=6)
            goal = {**goal, "period_start": start.isoformat(), "period_end": end.isoformat()}
            result = {**result, "weekly_goal": goal}
            task = {**task, "result": result}

        if today < start or today > end:
            continue
        desired = int(goal.get("desired") or 5)
        progress = result.get("progress_summary") if isinstance(result.get("progress_summary"), dict) else {}
        if int(progress.get("scheduled", 0) or 0) >= desired:
            continue
        candidates.append(task)

    candidates.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
    return candidates[0] if candidates else None


def _daily_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in tasks:
        if str(task.get("task_kind") or "") == "daily_candidates":
            return task
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        if isinstance(result.get("daily_candidate_feed"), dict):
            return task
    return None


def _ensure_daily_task(owner_id: int, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    existing = _daily_task(tasks)
    if existing:
        return existing
    now = datetime.now(UTC).isoformat()
    rows = _sb_post(
        "agency_stagirite_tasks",
        {
            "owner_telegram_id": int(owner_id),
            "assignment": "Ежедневная рабочая пятёрка Стагирита",
            "task_kind": "daily_candidates",
            "status": "В работе",
            "plan": {
                "steps": [
                    "Использовать уже найденный пул контактов/кандидатов.",
                    "Подготовить рабочую пятёрку на новый день.",
                    "Передать выбранных владельцем людей Неоне.",
                ]
            },
            "result": {
                "daily_candidate_feed": {
                    "daily_target": DAILY_TARGET,
                    "reserve_target": RESERVE_TARGET,
                    "daily_batches": {},
                    "created_at": now,
                },
                "selected_candidate_ids": [],
            },
            "created_at": now,
            "updated_at": now,
        },
    )
    if rows:
        return rows[0]
    raise RuntimeError("Не удалось создать ежедневную задачу Стагирита")


def _current_batch_info(owner_id: int) -> dict[str, Any]:
    today = datetime.now(BERLIN).date()
    today_key = today.isoformat()
    tasks = _load_tasks(owner_id)
    task = _active_weekly_task(tasks, today) or _daily_task(tasks)
    if not task:
        return {"count": 0, "complete": False, "candidate_ids": [], "approved_ids": []}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    container = result.get("weekly_goal") if isinstance(result.get("weekly_goal"), dict) else result.get("daily_candidate_feed")
    if not isinstance(container, dict):
        return {"count": 0, "complete": False, "candidate_ids": [], "approved_ids": []}
    batches = container.get("daily_batches") if isinstance(container.get("daily_batches"), dict) else {}
    info = batches.get(today_key) if isinstance(batches.get(today_key), dict) else {}
    ids = list(_ids_from_list(info.get("candidate_ids", [])))
    approved_ids = list(_ids_from_list(info.get("approved_ids", [])))
    return {
        "count": len(ids),
        "complete": len(ids) >= int(container.get("daily_target") or DAILY_TARGET),
        "candidate_ids": ids,
        "approved_ids": approved_ids,
    }


def _write_daily_batch(owner_id: int, ranked_ids: list[int], reserve_ids: list[int], *, replace_existing: bool = False) -> dict[str, Any]:
    today = datetime.now(BERLIN).date()
    today_key = today.isoformat()
    tasks = _load_tasks(owner_id)
    task = _active_weekly_task(tasks, today)
    if not task:
        task = _ensure_daily_task(owner_id, tasks)

    task_id = str(task.get("id") or "")
    if not task_id:
        raise RuntimeError("У задачи Стагирита нет id")

    # Повторно читаем самую свежую задачу, чтобы не потерять approved_ids.
    fresh_rows = _sb_get(
        "agency_stagirite_tasks",
        {"id": f"eq.{task_id}", "select": "*", "limit": 1},
    )
    if fresh_rows:
        task = fresh_rows[0]

    result = dict(task.get("result")) if isinstance(task.get("result"), dict) else {}
    if isinstance(result.get("weekly_goal"), dict):
        container_key = "weekly_goal"
        container = dict(result["weekly_goal"])
    else:
        container_key = "daily_candidate_feed"
        container = dict(result.get("daily_candidate_feed") or {})

    daily_target = int(container.get("daily_target") or DAILY_TARGET)
    reserve_target = int(container.get("reserve_target") or RESERVE_TARGET)
    batches = dict(container.get("daily_batches")) if isinstance(container.get("daily_batches"), dict) else {}
    today_info = dict(batches.get(today_key)) if isinstance(batches.get(today_key), dict) else {}

    current_ids = list(_ids_from_list(today_info.get("candidate_ids", [])))
    approved_ids = list(_ids_from_list(today_info.get("approved_ids", [])))
    merged = [] if replace_existing else list(current_ids)
    # Уже утверждённые владельцем кандидаты не исчезают при фоновой перепроверке.
    if replace_existing:
        for cid in approved_ids:
            if cid not in merged:
                merged.append(int(cid))
    for cid in ranked_ids:
        cid = int(cid)
        if cid not in merged:
            merged.append(cid)
        if len(merged) >= daily_target:
            break

    now_iso = datetime.now(UTC).isoformat()
    today_info.update(
        {
            "candidate_ids": merged[:daily_target],
            "approved_ids": [cid for cid in approved_ids if cid in merged],
            "prepared_at": str(today_info.get("prepared_at") or now_iso),
            "last_topup_attempt_at": now_iso,
            "source": "telegram_scout_background",
            "complete": len(merged) >= daily_target,
        }
    )
    if len(merged) >= daily_target:
        today_info["completed_at"] = now_iso

    batches[today_key] = today_info
    container["daily_batches"] = batches
    container["last_daily_prepare_date"] = today_key
    unique_reserve: list[int] = []
    for cid in reserve_ids:
        cid = int(cid)
        if cid not in unique_reserve:
            unique_reserve.append(cid)
        if len(unique_reserve) >= reserve_target:
            break
    container["weekly_pool_ids"] = unique_reserve
    result[container_key] = container

    _sb_patch(
        "agency_stagirite_tasks",
        {"id": f"eq.{task_id}"},
        {
            "status": "В работе",
            "result": result,
            "updated_at": now_iso,
        },
        prefer="return=minimal",
    )
    return {
        "candidate_ids": merged[:daily_target],
        "complete": len(merged) >= daily_target,
        "task_id": task_id,
    }


def process_owner_once(owner_id: int, session_string: str) -> dict[str, Any]:
    owner_id = int(owner_id)
    state, _ = _workspace_state(owner_id)
    passport = state.get("passport") if isinstance(state.get("passport"), dict) else None
    if not passport:
        return {"owner_id": owner_id, "status": "skip", "reason": "нет портрета ЦА"}

    current_batch = _current_batch_info(owner_id)

    contacts, activity_by_id = asyncio.run(_fetch_contacts_and_statuses(session_string))
    if not contacts:
        return {"owner_id": owner_id, "status": "empty", "reason": "Telegram не вернул контакты"}

    sent_ids = _sent_ids(state)
    blocked_ids = _blocked_ids(state)
    known_ids = _known_contact_ids(state)

    reserve_contacts: list[dict[str, Any]] = []
    for contact in contacts:
        cid = int(contact["telegram_id"])
        if cid in sent_ids or cid in blocked_ids or cid in known_ids:
            continue
        activity = activity_by_id.get(cid) or {}
        tier = _activity_tier(activity)
        if tier is None:
            continue
        reserve_contacts.append(
            {
                **contact,
                **activity,
                "is_owner_contact": True,
                "_stagirite_activity_tier": tier,
            }
        )

    def activity_sort(item: dict[str, Any]) -> tuple[Any, ...]:
        tier = int(item.get("_stagirite_activity_tier") or 0)
        raw_seen = str(item.get("last_seen_at") or "").strip()
        try:
            timestamp = datetime.fromisoformat(raw_seen.replace("Z", "+00:00")).timestamp()
        except Exception:
            timestamp = 0.0
        return (tier, -timestamp, str(item.get("name") or "").lower())

    reserve_contacts.sort(key=activity_sort)
    reserve_contacts = reserve_contacts[:RESERVE_TARGET]
    reserve_by_id = {int(item["telegram_id"]): item for item in reserve_contacts}

    tasks = _load_tasks(owner_id)
    today_key = datetime.now(BERLIN).date().isoformat()
    previously_shown = _shown_candidate_ids(tasks, exclude_day=today_key)
    current_today_order = [int(x) for x in (current_batch.get("candidate_ids") or [])]
    approved_today = {int(x) for x in (current_batch.get("approved_ids") or [])}

    # Перепроверяем уже выданную сегодняшнюю пятёрку. Это важно после изменения
    # правил: старый контакт с прежней перепиской должен исчезнуть из холодной пятёрки
    # в тот же день, а не только завтра.
    safe_current_today: list[int] = []
    current_probe = [
        reserve_by_id[cid]
        for cid in current_today_order
        if cid in reserve_by_id and cid not in sent_ids and cid not in blocked_ids and cid not in known_ids
    ]
    current_state_by_id: dict[int, dict[str, Any]] = {}
    for start in range(0, len(current_probe), 10):
        contexts = asyncio.run(_fetch_contexts(session_string, current_probe[start : start + 10]))
        for source in contexts:
            state_info = _classify_workability(
                source,
                already_sent_ids=sent_ids,
                known_contact_ids=known_ids,
                blocked_ids=blocked_ids,
            )
            current_state_by_id[int(source["telegram_id"])] = state_info

    for cid in current_today_order:
        if cid in approved_today:
            safe_current_today.append(cid)
            continue
        state_info = current_state_by_id.get(cid) or {}
        if state_info.get("work_state") == "available" and not bool(state_info.get("selection_blocked")):
            safe_current_today.append(cid)

    if len(safe_current_today) >= DAILY_TARGET and len(current_today_order) >= DAILY_TARGET:
        if safe_current_today[:DAILY_TARGET] != current_today_order[:DAILY_TARGET]:
            reserve_ids = [int(item["telegram_id"]) for item in reserve_contacts]
            _write_daily_batch(
                owner_id,
                safe_current_today[:DAILY_TARGET],
                reserve_ids,
                replace_existing=True,
            )
        return {
            "owner_id": owner_id,
            "status": "ready",
            "count": DAILY_TARGET,
        }

    excluded = set(sent_ids) | set(blocked_ids) | set(known_ids) | set(previously_shown) | set(safe_current_today)

    existing_candidates = _normalize_candidate_list(state.get("candidates", []))
    existing_by_id = {int(item["telegram_id"]): item for item in existing_candidates}

    # Сначала пытаемся добрать уже проанализированными кандидатами, но обязательно
    # заново проверяем историю личной переписки перед выдачей.
    existing_probe: list[dict[str, Any]] = []
    for contact in reserve_contacts:
        cid = int(contact["telegram_id"])
        if cid in excluded:
            continue
        old = existing_by_id.get(cid)
        if not old:
            continue
        if bool(old.get("selection_blocked")) or bool(old.get("telegram_warning")):
            continue
        if str(old.get("status") or "") == "Отправлено":
            continue
        existing_probe.append({**contact, "_old_candidate": old})

    existing_probe.sort(
        key=lambda item: _candidate_priority({**(item.get("_old_candidate") or {}), **item})
    )

    ranked_candidates: list[dict[str, Any]] = []
    need_existing = max(0, DAILY_TARGET - len(safe_current_today))
    for start in range(0, len(existing_probe), 10):
        if len(ranked_candidates) >= need_existing:
            break
        batch_contacts = [
            {k: v for k, v in item.items() if k != "_old_candidate"}
            for item in existing_probe[start : start + 10]
        ]
        contexts = asyncio.run(_fetch_contexts(session_string, batch_contacts))
        context_by_id = {int(item["telegram_id"]): item for item in contexts}
        for probe_item in existing_probe[start : start + 10]:
            cid = int(probe_item["telegram_id"])
            source = context_by_id.get(cid)
            if not source:
                continue
            state_info = _classify_workability(
                source,
                already_sent_ids=sent_ids,
                known_contact_ids=known_ids,
                blocked_ids=blocked_ids,
            )
            if state_info.get("work_state") != "available" or bool(state_info.get("selection_blocked")):
                continue
            old = probe_item.get("_old_candidate") or {}
            ranked_candidates.append(
                {
                    **old,
                    **{k: v for k, v in probe_item.items() if k != "_old_candidate"},
                    **state_info,
                }
            )
            if len(ranked_candidates) >= need_existing:
                break

    needed = max(0, DAILY_TARGET - len(safe_current_today) - len(ranked_candidates))
    new_results: list[dict[str, Any]] = []

    if needed > 0:
        raw_new = [
            item for item in reserve_contacts
            if int(item["telegram_id"]) not in excluded
            and int(item["telegram_id"]) not in existing_by_id
        ]

        for start in range(0, len(raw_new), 10):
            if len(ranked_candidates) + len(new_results) >= DAILY_TARGET - len(safe_current_today):
                break
            contexts = asyncio.run(_fetch_contexts(session_string, raw_new[start : start + 10]))
            workable: list[dict[str, Any]] = []
            for source in contexts:
                state_info = _classify_workability(
                    source,
                    already_sent_ids=sent_ids,
                    known_contact_ids=known_ids,
                    blocked_ids=blocked_ids,
                )
                enriched = {**source, **state_info}
                if enriched.get("work_state") == "available" and not bool(enriched.get("selection_blocked")):
                    workable.append(enriched)

            informative, empty = _split_analysis_value(workable)
            batch_results = [_build_no_data_candidate(item) for item in empty]
            if informative:
                try:
                    passport_analysis = passport.get("analysis") or passport.get("profile") or passport
                    batch_results.extend(_analyze_candidates(passport_analysis, informative))
                except Exception as exc:
                    print(
                        f"[Telegram Scout] {owner_id}: OpenAI analysis failed: {exc}; using local fallback",
                        flush=True,
                    )
                    batch_results.extend(_build_no_data_candidate(item) for item in informative)

            for item in batch_results:
                activity = activity_by_id.get(int(item["telegram_id"])) or {}
                item.update(activity)
                item["_stagirite_activity_tier"] = _activity_tier(activity)
                item["is_owner_contact"] = True
            batch_results = [
                item for item in batch_results
                if item.get("work_state") == "available"
                and not bool(item.get("selection_blocked"))
                and item.get("_stagirite_activity_tier") is not None
            ]
            batch_results.sort(key=_candidate_priority)
            new_results.extend(batch_results)

    all_ranked = ranked_candidates + new_results
    dedup: dict[int, dict[str, Any]] = {}
    for item in all_ranked:
        cid = int(item["telegram_id"])
        if cid not in safe_current_today:
            dedup[cid] = item
    all_ranked = list(dedup.values())
    all_ranked.sort(key=_candidate_priority)

    # Сохраняем новый анализ в общий пул Неонии.
    _save_workspace_candidates(owner_id, contacts, new_results)

    latest_state, _ = _workspace_state(owner_id)
    latest_sent = _sent_ids(latest_state)
    latest_blocked = _blocked_ids(latest_state)
    ranked_ids = list(safe_current_today)
    for item in all_ranked:
        cid = int(item["telegram_id"])
        if cid in latest_sent or cid in latest_blocked:
            continue
        if cid not in ranked_ids:
            ranked_ids.append(cid)
        if len(ranked_ids) >= DAILY_TARGET:
            break

    # В резерв для Стагирита не включаем контакты, которые уже показали историю
    # переписки в текущей проверке. Остальные будут окончательно проверены перед выдачей.
    invalid_history_ids = {
        cid
        for cid, info in current_state_by_id.items()
        if info.get("work_state") == "existing_history"
    }
    reserve_ids = [
        int(item["telegram_id"])
        for item in reserve_contacts
        if int(item["telegram_id"]) not in invalid_history_ids
    ]

    batch = _write_daily_batch(
        owner_id,
        ranked_ids,
        reserve_ids,
        replace_existing=True,
    )
    return {
        "owner_id": owner_id,
        "status": "prepared" if batch.get("candidate_ids") else "empty",
        "count": len(batch.get("candidate_ids") or []),
        "complete": bool(batch.get("complete")),
        "reserve": len(reserve_ids),
        "new_analyzed": len(new_results),
        "removed_old_dialogues": max(0, len(current_today_order) - len(safe_current_today)),
    }


def process_cycle_once() -> dict[str, int]:
    totals = {
        "owners": 0,
        "ready": 0,
        "prepared": 0,
        "empty": 0,
        "skipped": 0,
        "errors": 0,
    }
    for row in _owner_sessions():
        try:
            owner_id = int(row.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        session_string = _telegram_session_from_row(row)
        if not session_string:
            continue

        totals["owners"] += 1
        try:
            result = process_owner_once(owner_id, session_string)
            status = str(result.get("status") or "")
            if status == "ready":
                totals["ready"] += 1
            elif status == "prepared":
                totals["prepared"] += 1
            elif status == "empty":
                totals["empty"] += 1
            else:
                totals["skipped"] += 1
            print(f"[Telegram Scout] {result}", flush=True)
        except Exception as exc:
            totals["errors"] += 1
            print(f"[Telegram Scout] {owner_id}: error={exc}", flush=True)

    return totals


def worker_forever(poll_seconds: int | None = None) -> None:
    interval = int(
        poll_seconds
        or _env("TELEGRAM_SCOUT_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))
        or DEFAULT_INTERVAL_SECONDS
    )
    interval = max(300, interval)
    print(f"Telegram Scout worker started; interval={interval}s", flush=True)

    while True:
        started = time.monotonic()
        try:
            stats = process_cycle_once()
            print(f"[Telegram Scout] cycle complete: {stats}", flush=True)
        except Exception as exc:
            print(f"[Telegram Scout] cycle error={exc}", flush=True)

        elapsed = time.monotonic() - started
        time.sleep(max(30, interval - int(elapsed)))


if __name__ == "__main__":
    worker_forever()
