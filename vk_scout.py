from __future__ import annotations

"""Agency W — VK Partner Scout 1.0.

Этап 1:
- поиск кандидатов среди участников выбранных публичных VK-сообществ;
- сохранение только доступных публичных данных;
- оценка Неонией по портрету ЦА;
- резервирование до 5 VK-кандидатов на день за конкретным партнёром;
- подготовка персонального приглашения + ref-ссылки;
- без автоматической холодной рассылки.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import requests

try:
    import streamlit as st
except Exception:  # worker mode
    st = None

UTC = timezone.utc
VK_API_URL = "https://api.vk.com/method"
VK_PROFILE_FIELDS = (
    "city",
    "country",
    "photo_200",
    "domain",
    "status",
    "last_seen",
    "online",
    "can_write_private_message",
)
ACTIVE_ASSIGNMENT_STATUSES = (
    "reserved",
    "prepared",
    "invited",
    "entered",
    "lead",
    "dialogue",
    "meeting",
)


class VKScoutError(RuntimeError):
    pass


def _secret(name: str, default: str = "") -> str:
    if st is not None:
        try:
            value = st.secrets.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        except Exception:
            pass
    return str(os.getenv(name, default) or default).strip()


def _config() -> dict[str, str]:
    cfg = {
        "supabase_url": _secret("SUPABASE_URL").rstrip("/"),
        "supabase_key": _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_SERVICE_ROLE_KEY"),
        "vk_token": _secret("VK_SCOUT_ACCESS_TOKEN") or _secret("VK_ACCESS_TOKEN"),
        "vk_group_id": _secret("VK_GROUP_ID").lstrip("-"),
        "vk_api_version": _secret("VK_API_VERSION", "5.199") or "5.199",
    }
    missing = [
        label
        for label, value in (
            ("SUPABASE_URL", cfg["supabase_url"]),
            ("SUPABASE_SECRET_KEY", cfg["supabase_key"]),
            ("VK_SCOUT_ACCESS_TOKEN / VK_ACCESS_TOKEN", cfg["vk_token"]),
        )
        if not value
    ]
    if missing:
        raise VKScoutError("Не найдены настройки: " + ", ".join(missing))
    return cfg


def _sb_headers(prefer: str | None = None) -> dict[str, str]:
    cfg = _config()
    headers = {
        "apikey": cfg["supabase_key"],
        "Authorization": f"Bearer {cfg['supabase_key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _sb_get(table: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = _config()
    response = requests.get(
        f"{cfg['supabase_url']}/rest/v1/{table}",
        headers=_sb_headers(),
        params=params or {},
        timeout=30,
    )
    if not response.ok:
        raise VKScoutError(f"Supabase GET {table}: {response.status_code}: {response.text[:800]}")
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_post(
    table: str,
    payload: dict[str, Any] | list[dict[str, Any]],
    *,
    on_conflict: str = "",
) -> list[dict[str, Any]]:
    cfg = _config()
    url = f"{cfg['supabase_url']}/rest/v1/{table}"
    prefer = "return=representation"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        prefer = "resolution=merge-duplicates,return=representation"
    response = requests.post(
        url,
        headers=_sb_headers(prefer),
        json=payload,
        timeout=45,
    )
    if not response.ok:
        raise VKScoutError(f"Supabase POST {table}: {response.status_code}: {response.text[:800]}")
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_patch(table: str, filters: dict[str, Any], payload: dict[str, Any]) -> None:
    cfg = _config()
    response = requests.patch(
        f"{cfg['supabase_url']}/rest/v1/{table}",
        headers=_sb_headers("return=minimal"),
        params=filters,
        json=payload,
        timeout=30,
    )
    if not response.ok:
        raise VKScoutError(f"Supabase PATCH {table}: {response.status_code}: {response.text[:800]}")


def _vk_api(method: str, **params: Any) -> Any:
    cfg = _config()
    response = requests.post(
        f"{VK_API_URL}/{method}",
        data={
            **params,
            "access_token": cfg["vk_token"],
            "v": cfg["vk_api_version"],
        },
        timeout=45,
    )
    if not response.ok:
        raise VKScoutError(f"VK API {method}: HTTP {response.status_code}: {response.text[:800]}")
    data = response.json() if response.text.strip() else {}
    if not isinstance(data, dict):
        raise VKScoutError(f"VK API {method}: неожиданный ответ")
    if data.get("error"):
        err = data.get("error") or {}
        raise VKScoutError(f"VK API {method}: {err.get('error_code')} — {err.get('error_msg')}")
    return data.get("response")


def _screen_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:m\.)?vk\.(?:com|ru)/", "", text, flags=re.IGNORECASE)
    return text.split("?", 1)[0].split("#", 1)[0].strip("/")


def resolve_vk_community(value: str | int) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        raise VKScoutError("Не указано VK-сообщество")
    if raw.lstrip("-").isdigit():
        return {"community_id": abs(int(raw)), "screen_name": ""}
    name = _screen_name(raw)
    resolved = _vk_api("utils.resolveScreenName", screen_name=name)
    if not isinstance(resolved, dict):
        raise VKScoutError(f"VK не распознал сообщество: {value}")
    if str(resolved.get("type") or "") not in {"group", "page", "event"}:
        raise VKScoutError("Указана не ссылка на сообщество")
    return {"community_id": int(resolved["object_id"]), "screen_name": name}


def upsert_vk_source(
    owner_id: int,
    community: str | int,
    *,
    community_name: str = "",
    priority: int = 50,
    note: str = "",
) -> dict[str, Any]:
    resolved = resolve_vk_community(community)
    cid = int(resolved["community_id"])
    screen_name = str(resolved.get("screen_name") or "")
    payload = {
        "owner_telegram_id": int(owner_id),
        "community_id": cid,
        "community_name": community_name.strip() or None,
        "community_url": f"https://vk.com/{screen_name}" if screen_name else f"https://vk.com/club{cid}",
        "active": True,
        "priority": max(0, min(100, int(priority))),
        "note": note.strip() or None,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    rows = _sb_post("agency_vk_sources", payload, on_conflict="owner_telegram_id,community_id")
    return rows[0] if rows else payload


def load_vk_sources(owner_id: int) -> list[dict[str, Any]]:
    return _sb_get(
        "agency_vk_sources",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "active": "eq.true",
            "select": "*",
            "order": "priority.desc,created_at.asc",
        },
    )


def _normalize_vk_user(row: dict[str, Any], source_community_id: int | None = None) -> dict[str, Any] | None:
    try:
        uid = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    if uid <= 0 or row.get("deactivated"):
        return None
    city = row.get("city") if isinstance(row.get("city"), dict) else {}
    country = row.get("country") if isinstance(row.get("country"), dict) else {}
    last_seen = row.get("last_seen") if isinstance(row.get("last_seen"), dict) else {}
    return {
        "vk_user_id": uid,
        "first_name": str(row.get("first_name") or "").strip() or None,
        "last_name": str(row.get("last_name") or "").strip() or None,
        "domain": str(row.get("domain") or "").strip() or None,
        "photo_200": str(row.get("photo_200") or "").strip() or None,
        "city_name": str(city.get("title") or "").strip() or None,
        "country_name": str(country.get("title") or "").strip() or None,
        "activity": None,
        "status_text": str(row.get("status") or "").strip() or None,
        "can_write_private_message": (
            bool(row.get("can_write_private_message"))
            if row.get("can_write_private_message") is not None
            else None
        ),
        "source_community_id": int(source_community_id) if source_community_id else None,
        "raw_profile": {
            "online": row.get("online"),
            "last_seen": last_seen or None,
            "is_closed": row.get("is_closed"),
        },
        "last_enriched_at": datetime.now(UTC).isoformat(),
    }


def fetch_vk_members(source: dict[str, Any] | int, *, offset: int = 0, count: int = 200) -> dict[str, Any]:
    cid = int(source.get("community_id")) if isinstance(source, dict) else abs(int(source))
    response = _vk_api(
        "groups.getMembers",
        group_id=cid,
        offset=max(0, int(offset)),
        count=max(1, min(1000, int(count))),
        fields=",".join(VK_PROFILE_FIELDS),
    )
    response = response if isinstance(response, dict) else {}
    members = []
    for row in response.get("items") or []:
        if isinstance(row, dict):
            item = _normalize_vk_user(row, cid)
            if item:
                members.append(item)
    return {
        "community_id": cid,
        "total": int(response.get("count") or 0),
        "members": members,
    }


def enrich_vk_profiles(user_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = []
    for value in user_ids:
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in ids:
            ids.append(uid)
    result = []
    for start in range(0, len(ids), 500):
        rows = _vk_api(
            "users.get",
            user_ids=",".join(str(x) for x in ids[start:start + 500]),
            fields=",".join(VK_PROFILE_FIELDS),
        )
        for row in rows or []:
            if isinstance(row, dict):
                item = _normalize_vk_user(row)
                if item:
                    result.append(item)
    return result


def save_vk_candidates(candidates: Iterable[dict[str, Any]]) -> int:
    payload = []
    for item in candidates:
        if not isinstance(item, dict) or not item.get("vk_user_id"):
            continue
        payload.append({
            "vk_user_id": int(item["vk_user_id"]),
            "first_name": item.get("first_name"),
            "last_name": item.get("last_name"),
            "domain": item.get("domain"),
            "photo_200": item.get("photo_200"),
            "city_name": item.get("city_name"),
            "country_name": item.get("country_name"),
            "activity": item.get("activity"),
            "status_text": item.get("status_text"),
            "can_write_private_message": item.get("can_write_private_message"),
            "source_community_id": item.get("source_community_id"),
            "raw_profile": item.get("raw_profile") or {},
            "last_enriched_at": item.get("last_enriched_at") or datetime.now(UTC).isoformat(),
        })
    saved = 0
    for start in range(0, len(payload), 200):
        batch = payload[start:start + 200]
        _sb_post("agency_vk_candidates", batch, on_conflict="vk_user_id")
        saved += len(batch)
    return saved


def scan_vk_sources(owner_id: int, *, per_source: int = 200, max_sources: int = 10) -> dict[str, Any]:
    sources = load_vk_sources(owner_id)[:max(1, min(50, int(max_sources)))]
    unique: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for source in sources:
        try:
            page = fetch_vk_members(source, count=per_source)
            for item in page["members"]:
                unique[int(item["vk_user_id"])] = item
        except Exception as exc:
            errors.append(f"{source.get('community_name') or source.get('community_id')}: {exc}")
    saved = save_vk_candidates(unique.values())
    return {
        "sources_checked": len(sources),
        "candidates_found": len(unique),
        "candidates_saved": saved,
        "errors": errors,
    }


def _candidate_public_view(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("raw_profile") if isinstance(item.get("raw_profile"), dict) else {}
    return {
        "vk_user_id": item.get("vk_user_id"),
        "name": " ".join(x for x in [str(item.get("first_name") or "").strip(), str(item.get("last_name") or "").strip()] if x),
        "city": item.get("city_name"),
        "country": item.get("country_name"),
        "status": item.get("status_text"),
        "domain": item.get("domain"),
        "online": raw.get("online"),
        "last_seen": raw.get("last_seen"),
        "can_write_private_message": item.get("can_write_private_message"),
        "source_community_id": item.get("source_community_id"),
    }


def _extract_json_array(answer: Any) -> list[dict[str, Any]]:
    if isinstance(answer, list):
        return [x for x in answer if isinstance(x, dict)]
    text = str(answer or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            return []
    return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []


def score_vk_candidates(
    owner_id: int,
    target_profile: str | dict[str, Any],
    *,
    ask_openai_fn: Callable[..., Any] | None,
    limit: int = 30,
) -> dict[str, Any]:
    if ask_openai_fn is None:
        return {"ok": False, "analyzed": 0, "message": "Нужен анализ Неонии (ask_openai_fn)."}

    # В ежедневный анализ допускаем только профили, которым VK прямо разрешает
    # отправить личное сообщение. Профили с закрытыми сообщениями сохраняются
    # в базе как найденные, но Неония не тратит на них анализ и не выдаёт их
    # партнёру в рабочую пятёрку.
    rows = _sb_get(
        "agency_vk_candidates",
        {
            "can_write_private_message": "eq.true",
            "select": "*",
            "order": "last_enriched_at.desc.nullslast,first_discovered_at.desc",
            "limit": max(1, min(100, int(limit))),
        },
    )
    profile_text = json.dumps(target_profile, ensure_ascii=False, indent=2) if isinstance(target_profile, dict) else str(target_profile or "")
    system_prompt = """
Ты — Неония, аналитик целевой аудитории Агентства W.
Оценивай только по предоставленным публичным данным VK.
Не додумывай профессию, доход, здоровье, политику, религию, личную жизнь,
диагнозы или другие чувствительные признаки. Отсутствие данных = неопределённость.
Верни ТОЛЬКО JSON-массив:
[{"vk_user_id":123,"score":0,"fit_summary":"...","positive_signals":["..."],"weak_fit_signals":["..."]}]
""".strip()

    analyzed = 0
    for start in range(0, len(rows), 10):
        batch = rows[start:start + 10]
        request = "ПОРТРЕТ ЦА:\n" + profile_text + "\n\nПУБЛИЧНЫЕ VK-ПРОФИЛИ:\n" + json.dumps([_candidate_public_view(x) for x in batch], ensure_ascii=False, indent=2)
        parsed = _extract_json_array(ask_openai_fn(system_prompt, request))
        by_id = {int(x["vk_user_id"]): x for x in parsed if str(x.get("vk_user_id") or "").isdigit()}
        payload = []
        for source in batch:
            uid = int(source["vk_user_id"])
            item = by_id.get(uid)
            if not item:
                continue
            try:
                score = max(0, min(100, int(item.get("score") or 0)))
            except (TypeError, ValueError):
                score = 0
            payload.append({
                "owner_telegram_id": int(owner_id),
                "vk_user_id": uid,
                "score": score,
                "fit_summary": str(item.get("fit_summary") or "").strip() or None,
                "positive_signals": item.get("positive_signals") if isinstance(item.get("positive_signals"), list) else [],
                "weak_fit_signals": item.get("weak_fit_signals") if isinstance(item.get("weak_fit_signals"), list) else [],
                "target_profile_snapshot": {"profile": profile_text},
                "analyzed_at": datetime.now(UTC).isoformat(),
            })
        if payload:
            _sb_post("agency_vk_candidate_scores", payload, on_conflict="owner_telegram_id,vk_user_id")
            analyzed += len(payload)
    return {"ok": True, "analyzed": analyzed, "message": f"Неония оценила VK-кандидатов: {analyzed}."}


def release_expired_vk_assignments() -> int:
    now = datetime.now(UTC).isoformat()
    rows = _sb_get(
        "agency_vk_assignments",
        {
            "status": "in.(reserved,prepared)",
            "reservation_until": f"lt.{now}",
            "select": "id",
        },
    )
    for row in rows:
        _sb_patch("agency_vk_assignments", {"id": f"eq.{int(row['id'])}"}, {"status": "released", "released_at": now, "updated_at": now})
    return len(rows)


def _messageable_vk_ids(user_ids: Iterable[int]) -> set[int]:
    """Возвращает только VK ID, которым можно написать личное сообщение."""
    ids: list[int] = []
    for value in user_ids:
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in ids:
            ids.append(uid)

    result: set[int] = set()
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        if not chunk:
            continue
        rows = _sb_get(
            "agency_vk_candidates",
            {
                "vk_user_id": "in.(" + ",".join(str(x) for x in chunk) + ")",
                "can_write_private_message": "eq.true",
                "select": "vk_user_id",
                "limit": len(chunk),
            },
        )
        for row in rows:
            try:
                result.add(int(row.get("vk_user_id")))
            except (TypeError, ValueError):
                pass
    return result


def _used_vk_ids() -> set[int]:
    result: set[int] = set()
    for table, params in (
        ("agency_vk_leads", {"select": "vk_user_id", "limit": 5000}),
        ("agency_vk_assignments", {"status": "in.(" + ",".join(ACTIVE_ASSIGNMENT_STATUSES) + ")", "select": "vk_user_id", "limit": 5000}),
    ):
        for row in _sb_get(table, params):
            try:
                result.add(int(row.get("vk_user_id")))
            except (TypeError, ValueError):
                pass
    return result


def ensure_daily_vk_assignments(
    owner_id: int,
    member_code: str,
    *,
    limit: int = 5,
    min_score: int = 60,
    reservation_days: int = 7,
) -> dict[str, Any]:
    owner_id = int(owner_id)
    limit = max(1, min(5, int(limit)))
    today = date.today().isoformat()
    release_expired_vk_assignments()

    existing = _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "assignment_date": f"eq.{today}",
            "status": "not.in.(released,skipped,blocked,not_fit)",
            "select": "*",
            "order": "daily_position.asc",
        },
    )

    # Старые назначения могли быть сформированы до введения правила
    # «только тем, кому можно написать». Автоматически убираем такие карточки
    # из сегодняшней пятёрки и освобождаем место для замены.
    existing_vk_ids = [x.get("vk_user_id") for x in existing if x.get("vk_user_id") is not None]
    messageable_existing = _messageable_vk_ids(existing_vk_ids)
    if existing_vk_ids:
        now_iso = datetime.now(UTC).isoformat()
        for row in existing:
            try:
                uid = int(row.get("vk_user_id"))
            except (TypeError, ValueError):
                continue
            if uid in messageable_existing:
                continue
            assignment_id = row.get("id")
            if assignment_id is None:
                continue
            _sb_patch(
                "agency_vk_assignments",
                {"id": f"eq.{int(assignment_id)}"},
                {
                    "status": "blocked",
                    "released_at": now_iso,
                    "updated_at": now_iso,
                },
            )

        existing = _sb_get(
            "agency_vk_assignments",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "assignment_date": f"eq.{today}",
                "status": "not.in.(released,skipped,blocked,not_fit)",
                "select": "*",
                "order": "daily_position.asc",
            },
        )

    if len(existing) >= limit:
        return {"ok": True, "assignments": existing[:limit], "complete": True, "message": f"VK-пятёрка готова: {limit}/{limit}."}

    used = _used_vk_ids() - {int(x["vk_user_id"]) for x in existing if x.get("vk_user_id") is not None}
    scores = _sb_get(
        "agency_vk_candidate_scores",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "score": f"gte.{max(0, min(100, int(min_score)))}",
            "select": "vk_user_id,score,fit_summary",
            "order": "score.desc,analyzed_at.desc",
            "limit": 300,
        },
    )
    score_vk_ids = [x.get("vk_user_id") for x in scores if x.get("vk_user_id") is not None]
    messageable_score_ids = _messageable_vk_ids(score_vk_ids)

    existing_ids = {int(x["vk_user_id"]) for x in existing if x.get("vk_user_id") is not None}
    positions = {int(x["daily_position"]) for x in existing if x.get("daily_position") is not None}
    free_positions = [x for x in range(1, limit + 1) if x not in positions]
    created = 0
    now = datetime.now(UTC)

    for item in scores:
        if not free_positions:
            break
        try:
            uid = int(item.get("vk_user_id"))
        except (TypeError, ValueError):
            continue
        if uid in used or uid in existing_ids:
            continue
        if uid not in messageable_score_ids:
            continue
        payload = {
            "vk_user_id": uid,
            "owner_telegram_id": owner_id,
            "owner_member_code": str(member_code or "").strip() or None,
            "assignment_date": today,
            "daily_position": free_positions[0],
            "status": "reserved",
            "score": int(item.get("score") or 0),
            "fit_summary": str(item.get("fit_summary") or "").strip() or None,
            "reserved_at": now.isoformat(),
            "reservation_until": (now + timedelta(days=max(1, int(reservation_days)))).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        try:
            _sb_post("agency_vk_assignments", payload)
        except VKScoutError:
            continue
        created += 1
        existing_ids.add(uid)
        free_positions.pop(0)

    final_rows = _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "assignment_date": f"eq.{today}",
            "status": "not.in.(released,skipped,blocked,not_fit)",
            "select": "*",
            "order": "daily_position.asc",
        },
    )[:limit]
    return {
        "ok": True,
        "assignments": final_rows,
        "created": created,
        "complete": len(final_rows) >= limit,
        "message": f"VK-кандидаты на сегодня: {len(final_rows)}/{limit}.",
    }


def personal_vk_invitation_link(member_code: str) -> str:
    group_id = _config().get("vk_group_id") or ""
    if not group_id:
        raise VKScoutError("Не найден VK_GROUP_ID")
    code = str(member_code or "").strip()
    if not code:
        raise VKScoutError("Не указан member_code")
    return f"https://vk.me/club{group_id}?ref={code}&ref_source=agency_w"


def vk_profile_url(candidate: dict[str, Any]) -> str:
    domain = str(candidate.get("domain") or "").strip()
    return f"https://vk.com/{domain}" if domain else f"https://vk.com/id{int(candidate['vk_user_id'])}"


def prepare_vk_invitation(
    assignment_id: int,
    member_code: str,
    *,
    ask_openai_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    rows = _sb_get("agency_vk_assignments", {"id": f"eq.{int(assignment_id)}", "select": "*", "limit": 1})
    if not rows:
        raise VKScoutError("VK-назначение не найдено")
    assignment = rows[0]
    candidates = _sb_get("agency_vk_candidates", {"vk_user_id": f"eq.{int(assignment['vk_user_id'])}", "select": "*", "limit": 1})
    if not candidates:
        raise VKScoutError("VK-кандидат не найден")
    candidate = candidates[0]
    first_name = str(candidate.get("first_name") or "").strip()
    fit = str(assignment.get("fit_summary") or "").strip()
    link = personal_vk_invitation_link(member_code)

    if ask_openai_fn:
        system = """
Ты — Неона. Подготовь короткое первое сообщение для холодного контакта VK.
Текст отправит сам партнёр после просмотра.
Правила: обращаться только по имени; 2–4 коротких предложения; не говорить,
что человека анализировали; не выдумывать факты; мягко пригласить посмотреть
сообщество Agency W; польза — ИИ-команда освобождает время от рутины;
без обещаний дохода/результата; персональную ссылку поставить последней строкой.
""".strip()
        request = f"Имя: {first_name or 'неизвестно'}\nКонтекст Неонии: {fit or 'данных мало'}\nСсылка: {link}"
        message = str(ask_openai_fn(system, request) or "").strip()
    else:
        greeting = f"{first_name}, здравствуйте!" if first_name else "Здравствуйте!"
        message = (
            f"{greeting} Сейчас мы показываем, как ИИ-команда может взять на себя "
            "часть поиска, переписки и организационной рутины и освободить человеку время. "
            "Если тема вам близка, можно просто посмотреть наше сообщество.\n\n" + link
        )

    now = datetime.now(UTC).isoformat()
    _sb_patch(
        "agency_vk_assignments",
        {"id": f"eq.{int(assignment_id)}"},
        {"status": "prepared", "invitation_text": message, "invitation_link": link, "prepared_at": now, "updated_at": now},
    )
    return {
        "assignment_id": int(assignment_id),
        "vk_user_id": int(candidate["vk_user_id"]),
        "name": " ".join(x for x in [str(candidate.get("first_name") or "").strip(), str(candidate.get("last_name") or "").strip()] if x),
        "profile_url": vk_profile_url(candidate),
        "invitation_text": message,
        "invitation_link": link,
    }


def mark_vk_invited(assignment_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    _sb_patch("agency_vk_assignments", {"id": f"eq.{int(assignment_id)}"}, {"status": "invited", "invited_at": now, "updated_at": now})


def skip_vk_assignment(assignment_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    _sb_patch("agency_vk_assignments", {"id": f"eq.{int(assignment_id)}"}, {"status": "skipped", "released_at": now, "updated_at": now})


def load_today_vk_assignments(owner_id: int) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    rows = _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "assignment_date": f"eq.{today}",
            "status": "not.in.(released,skipped,blocked,not_fit)",
            "select": "*",
            "order": "daily_position.asc",
        },
    )
    if not rows:
        return []
    ids = [int(x["vk_user_id"]) for x in rows if x.get("vk_user_id") is not None]
    candidates = _sb_get(
        "agency_vk_candidates",
        {"vk_user_id": "in.(" + ",".join(str(x) for x in ids) + ")", "select": "*"},
    ) if ids else []
    by_id = {int(x["vk_user_id"]): x for x in candidates if x.get("vk_user_id") is not None}
    result = []
    for assignment in rows:
        uid = int(assignment["vk_user_id"])
        candidate = by_id.get(uid, {})
        result.append({
            **assignment,
            "first_name": candidate.get("first_name"),
            "last_name": candidate.get("last_name"),
            "domain": candidate.get("domain"),
            "photo_200": candidate.get("photo_200"),
            "city_name": candidate.get("city_name"),
            "country_name": candidate.get("country_name"),
            "status_text": candidate.get("status_text"),
            "profile_url": vk_profile_url(candidate) if candidate else f"https://vk.com/id{uid}",
        })
    return result
