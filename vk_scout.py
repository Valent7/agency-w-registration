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

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from neonia_candidate_policy import BUSINESS_GATE_RULES, apply_business_gate

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

KNOWN_CONTACT_LABEL = "Знакомый владельца"


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

    owner_id = int(owner_id)
    analysis_limit = max(1, min(100, int(limit)))

    # В ежедневный анализ допускаем только профили, которым VK прямо разрешает
    # отправить личное сообщение. Профили с закрытыми сообщениями сохраняются
    # в базе как найденные, но Неония не тратит на них анализ и не выдаёт их
    # партнёру в рабочую пятёрку.
    #
    # ВАЖНО: сначала берём профили, которые этот владелец ЕЩЁ НЕ анализировал.
    # Раньше каждый цикл снова оценивал одни и те же последние 30 профилей,
    # поэтому VK Scout мог навсегда застрять на 0/5, даже если в общем пуле
    # были сотни других кандидатов.
    scored_rows = _sb_get(
        "agency_vk_candidate_scores",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "select": "vk_user_id,analyzed_at",
            "limit": 5000,
        },
    )
    already_scored: set[int] = set()
    for item in scored_rows:
        try:
            already_scored.add(int(item.get("vk_user_id")))
        except (TypeError, ValueError):
            pass

    rows: list[dict[str, Any]] = []
    page_size = 200
    offset = 0
    # Ограничиваем число страниц, чтобы worker не мог случайно читать бесконечный пул.
    for _ in range(25):
        page = _sb_get(
            "agency_vk_candidates",
            {
                "can_write_private_message": "eq.true",
                "select": "*",
                "order": "last_enriched_at.desc.nullslast,first_discovered_at.desc",
                "limit": page_size,
                "offset": offset,
            },
        )
        if not page:
            break
        for candidate in page:
            try:
                uid = int(candidate.get("vk_user_id"))
            except (TypeError, ValueError):
                continue
            if uid in already_scored:
                continue
            rows.append(candidate)
            if len(rows) >= analysis_limit:
                break
        if len(rows) >= analysis_limit or len(page) < page_size:
            break
        offset += page_size

    # Когда весь доступный пул уже пройден, разрешаем повторный анализ свежих
    # профилей. Это полезно после обновления публичных данных или портрета ЦА.
    reanalysis = False
    if not rows:
        reanalysis = True
        rows = _sb_get(
            "agency_vk_candidates",
            {
                "can_write_private_message": "eq.true",
                "select": "*",
                "order": "last_enriched_at.desc.nullslast,first_discovered_at.desc",
                "limit": analysis_limit,
            },
        )

    profile_text = json.dumps(target_profile, ensure_ascii=False, indent=2) if isinstance(target_profile, dict) else str(target_profile or "")
    system_prompt = f"""
Ты — Неония, аналитик и селектор целевой аудитории Агентства W.
Оценивай только по предоставленным публичным данным VK.
Не додумывай профессию, доход, здоровье, политику, религию, личную жизнь,
диагнозы или другие чувствительные признаки. Отсутствие данных = неопределённость.

{BUSINESS_GATE_RULES}

Верни ТОЛЬКО JSON-массив:
[{{"vk_user_id":123,"score":0,"fit_summary":"...","positive_signals":["..."],
"weak_fit_signals":["..."],"business_relevant":false,"business_evidence":[],
"business_gate_reason":"..."}}]
""".strip()

    analyzed = 0
    scores_this_cycle: list[int] = []
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

            score, _, business_evidence, business_gate_reason = apply_business_gate(
                item,
                score,
            )
            # Сохраняем объяснение жёсткого бизнес-фильтра в уже существующие поля,
            # поэтому миграция Supabase не нужна.
            positive_signals = (
                item.get("positive_signals")
                if isinstance(item.get("positive_signals"), list)
                else []
            )
            weak_fit_signals = (
                item.get("weak_fit_signals")
                if isinstance(item.get("weak_fit_signals"), list)
                else []
            )
            if business_evidence:
                positive_signals = business_evidence + positive_signals
            elif business_gate_reason:
                weak_fit_signals = [business_gate_reason] + weak_fit_signals

            scores_this_cycle.append(score)
            payload.append({
                "owner_telegram_id": owner_id,
                "vk_user_id": uid,
                "score": score,
                "fit_summary": str(item.get("fit_summary") or "").strip() or None,
                "positive_signals": positive_signals[:6],
                "weak_fit_signals": weak_fit_signals[:6],
                "target_profile_snapshot": {"profile": profile_text},
                "analyzed_at": datetime.now(UTC).isoformat(),
            })
        if payload:
            _sb_post("agency_vk_candidate_scores", payload, on_conflict="owner_telegram_id,vk_user_id")
            analyzed += len(payload)

    best = max(scores_this_cycle) if scores_this_cycle else None
    mode = "повторный анализ" if reanalysis else "новые профили"
    best_text = str(best) if best is not None else "—"
    return {
        "ok": True,
        "analyzed": analyzed,
        "best_score": best,
        "mode": mode,
        "message": f"Неония оценила VK-кандидатов: {analyzed}; режим: {mode}; лучший балл цикла: {best_text}.",
    }


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



def resolve_vk_person(value: str | int) -> int:
    """Разрешает ссылку/короткое имя/ID VK именно в ID пользователя."""
    raw = str(value or "").strip()
    if not raw:
        raise VKScoutError("Не указан VK-профиль")

    screen_name = _screen_name(raw)
    if screen_name.lower().startswith("id") and screen_name[2:].isdigit():
        return int(screen_name[2:])
    if screen_name.isdigit():
        return int(screen_name)

    resolved = _vk_api("utils.resolveScreenName", screen_name=screen_name)
    if not isinstance(resolved, dict):
        raise VKScoutError(f"VK не распознал профиль: {value}")
    if str(resolved.get("type") or "") != "user":
        raise VKScoutError("Указана не ссылка на личный VK-профиль")
    return int(resolved["object_id"])


def add_known_vk_contact(
    owner_id: int,
    member_code: str,
    profile: str | int,
    *,
    familiarity_note: str = "",
) -> dict[str, Any]:
    """Добавляет знакомого владельца в работу Неоны отдельно от ежедневной пятёрки."""
    owner_id = int(owner_id)
    vk_user_id = resolve_vk_person(profile)

    profiles = enrich_vk_profiles([vk_user_id])
    if not profiles:
        raise VKScoutError("Не удалось получить VK-профиль этого человека")
    candidate = profiles[0]
    save_vk_candidates([candidate])

    active = _sb_get(
        "agency_vk_assignments",
        {
            "vk_user_id": f"eq.{vk_user_id}",
            "status": "in.(" + ",".join(ACTIVE_ASSIGNMENT_STATUSES) + ")",
            "select": "*",
            "limit": 1,
        },
    )
    if active:
        row = active[0]
        if int(row.get("owner_telegram_id") or 0) != owner_id:
            raise VKScoutError(
                "Этот VK-контакт уже находится в активной работе у другого партнёра Агентства W."
            )
        return {**row, **candidate, "assignment_id": int(row.get("id") or 0), "known_contact": True}

    now = datetime.now(UTC)
    note = re.sub(r"\s+", " ", str(familiarity_note or "")).strip()
    fit_summary = KNOWN_CONTACT_LABEL + (f": {note}" if note else "")
    payload = {
        "vk_user_id": vk_user_id,
        "owner_telegram_id": owner_id,
        "owner_member_code": str(member_code or "").strip() or None,
        "assignment_date": date.today().isoformat(),
        # NULL специально отделяет знакомых владельца от ежедневной VK-пятёрки 1..5.
        "daily_position": None,
        "status": "reserved",
        "score": None,
        "fit_summary": fit_summary,
        "reserved_at": now.isoformat(),
        # Знакомый владельца не должен автоматически выпадать через 7 дней.
        "reservation_until": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    created = _sb_post("agency_vk_assignments", payload)
    row = created[0] if created else payload
    return {**row, **candidate, "assignment_id": int(row.get("id") or 0), "known_contact": True}


def load_known_vk_contacts(owner_id: int) -> list[dict[str, Any]]:
    """Активные знакомые владельца — отдельный поток, не входящий в VK 5 на сегодня."""
    rows = _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "daily_position": "is.null",
            "status": "not.in.(released,skipped,blocked,not_fit)",
            "select": "*",
            "order": "created_at.desc",
            "limit": 100,
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
            **candidate,
            # У candidate тоже есть поле id. Храним id назначения отдельно,
            # чтобы UI не перепутал его с id профиля-кандидата.
            "assignment_id": int(assignment.get("id") or 0),
            "profile_url": vk_profile_url(candidate) if candidate else f"https://vk.com/id{uid}",
            "known_contact": True,
        })
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
    min_score = max(0, min(100, int(min_score)))
    today = date.today().isoformat()
    release_expired_vk_assignments()

    existing = _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "assignment_date": f"eq.{today}",
            "daily_position": "not.is.null",
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

    # Диагностика нужна даже когда пятёрка уже готова: по ней видно,
    # сколько профилей реально прошло порог Неонии.
    all_owner_scores = _sb_get(
        "agency_vk_candidate_scores",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "select": "vk_user_id,score,fit_summary,analyzed_at",
            "order": "score.desc,analyzed_at.desc",
            "limit": 5000,
        },
    )
    best_score = None
    for item in all_owner_scores:
        try:
            value = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        best_score = value if best_score is None else max(best_score, value)

    if len(existing) >= limit:
        return {
            "ok": True,
            "assignments": existing[:limit],
            "complete": True,
            "analyzed_total": len(all_owner_scores),
            "best_score": best_score,
            "message": f"VK-пятёрка готова: {limit}/{limit}; всего оценено: {len(all_owner_scores)}; лучший балл: {best_score if best_score is not None else '—'}.",
        }

    used = _used_vk_ids() - {int(x["vk_user_id"]) for x in existing if x.get("vk_user_id") is not None}
    scores = [
        item
        for item in all_owner_scores
        if str(item.get("score") if item.get("score") is not None else "").lstrip("-").isdigit()
        and int(item.get("score")) >= min_score
    ]
    score_vk_ids = [x.get("vk_user_id") for x in scores if x.get("vk_user_id") is not None]
    messageable_score_ids = _messageable_vk_ids(score_vk_ids)

    existing_ids = {int(x["vk_user_id"]) for x in existing if x.get("vk_user_id") is not None}
    available_ids: list[int] = []
    for item in scores:
        try:
            uid = int(item.get("vk_user_id"))
        except (TypeError, ValueError):
            continue
        if uid in used or uid in existing_ids or uid not in messageable_score_ids:
            continue
        if uid not in available_ids:
            available_ids.append(uid)

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
            "daily_position": "not.is.null",
            "status": "not.in.(released,skipped,blocked,not_fit)",
            "select": "*",
            "order": "daily_position.asc",
        },
    )[:limit]

    diagnostic = (
        f"всего оценено: {len(all_owner_scores)}; "
        f">={min_score}: {len(scores)}; "
        f"можно написать: {len(messageable_score_ids)}; "
        f"свободных: {len(available_ids)}; "
        f"лучший балл: {best_score if best_score is not None else '—'}"
    )
    return {
        "ok": True,
        "assignments": final_rows,
        "created": created,
        "complete": len(final_rows) >= limit,
        "analyzed_total": len(all_owner_scores),
        "qualified": len(scores),
        "messageable_qualified": len(messageable_score_ids),
        "available": len(available_ids),
        "best_score": best_score,
        "message": f"VK-кандидаты на сегодня: {len(final_rows)}/{limit}; {diagnostic}.",
    }


def _vk_post_material(post: dict[str, Any]) -> str:
    """Текстовое содержание публичного VK-поста без домыслов о медиа."""
    parts: list[str] = []
    text = re.sub(r"\s+", " ", str(post.get("text") or "")).strip()
    if text:
        parts.append(text)

    for attachment in post.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("type") or "").strip()
        obj = attachment.get(kind) if kind and isinstance(attachment.get(kind), dict) else {}
        if kind == "photo":
            caption = re.sub(r"\s+", " ", str(obj.get("text") or "")).strip()
            parts.append(f"Фото. Подпись: {caption}" if caption else "Фото без подписи.")
        elif kind == "video":
            title = re.sub(r"\s+", " ", str(obj.get("title") or "")).strip()
            description = re.sub(r"\s+", " ", str(obj.get("description") or "")).strip()
            details = ". ".join(x for x in (title, description) if x)
            parts.append(f"Видео: {details}" if details else "Видео без описания.")
        elif kind == "link":
            title = re.sub(r"\s+", " ", str(obj.get("title") or "")).strip()
            description = re.sub(r"\s+", " ", str(obj.get("description") or "")).strip()
            details = ". ".join(x for x in (title, description) if x)
            parts.append(f"Ссылка: {details}" if details else "Публикация со ссылкой.")
        elif kind == "poll":
            question = re.sub(r"\s+", " ", str(obj.get("question") or "")).strip()
            parts.append(f"Опрос: {question}" if question else "Опрос.")
        elif kind:
            parts.append(f"Вложение: {kind}.")

    for copied in post.get("copy_history") or []:
        if not isinstance(copied, dict):
            continue
        copied_text = re.sub(r"\s+", " ", str(copied.get("text") or "")).strip()
        if copied_text:
            parts.append(f"Репост: {copied_text}")
            break

    return " ".join(parts).strip()


def _vk_user_api(account_owner_id: int, method: str, **params: Any) -> Any:
    """Вызывает VK API именно пользовательским VK ID токеном владельца кабинета.

    Для wall.get групповой токен не подходит: VK API 5.199 допускает user/service.
    Токен берём из уже существующего VK Scout OAuth и при необходимости обновляем.
    """
    from vk_scout_oauth import (
        force_refresh_vk_scout_access_token,
        get_valid_vk_scout_access_token,
    )

    cfg = _config()

    def request_with(token: str) -> tuple[Any, int | None]:
        response = requests.post(
            f"{VK_API_URL}/{method}",
            data={
                **params,
                "access_token": token,
                "v": cfg["vk_api_version"],
            },
            timeout=45,
        )
        if not response.ok:
            raise VKScoutError(
                f"VK API {method}: HTTP {response.status_code}: {response.text[:800]}"
            )
        data = response.json() if response.text.strip() else {}
        if not isinstance(data, dict):
            raise VKScoutError(f"VK API {method}: неожиданный ответ")
        if data.get("error"):
            err = data.get("error") or {}
            code = int(err.get("error_code") or 0) or None
            return err, code
        return data.get("response"), None

    token = get_valid_vk_scout_access_token(int(account_owner_id))
    result, error_code = request_with(token)
    if error_code == 5:
        # VK иногда привязывает свежий access token к IP выдачи. Наш OAuth-модуль
        # уже умеет безопасно обновлять rotating refresh token с IP текущего worker.
        token = force_refresh_vk_scout_access_token(int(account_owner_id))
        result, error_code = request_with(token)

    if error_code is not None:
        err = result if isinstance(result, dict) else {}
        raise VKScoutError(
            f"VK API {method}: {error_code} — {err.get('error_msg') or 'ошибка VK'}"
        )
    return result


def fetch_public_vk_posts(
    owner_id: int,
    vk_user_id: int,
    *,
    count: int = 5,
    max_age_days: int = 180,
) -> list[dict[str, Any]]:
    """Читает свежие публичные записи, под которыми владелец кабинета реально может комментировать.

    Для утепления мало просто увидеть пост: у него должны быть открыты комментарии.
    Также не используем очень старые публикации — комментарий к записи многолетней давности
    выглядит не как естественное знакомство.
    """
    wanted = max(1, min(10, int(count)))
    # Берём запас, потому что часть записей может быть закреплена, слишком стара
    # или иметь закрытые комментарии.
    raw_count = max(20, wanted * 4)
    response = _vk_user_api(
        int(owner_id),
        "wall.get",
        owner_id=int(vk_user_id),
        count=min(50, raw_count),
        filter="owner",
        extended=0,
    )
    if not isinstance(response, dict):
        return []

    now_ts = int(datetime.now(UTC).timestamp())
    max_age_seconds = max(1, int(max_age_days)) * 24 * 60 * 60

    result: list[dict[str, Any]] = []
    for post in response.get("items") or []:
        if not isinstance(post, dict) or not post.get("id"):
            continue

        post_date = int(post.get("date") or 0)
        if not post_date or now_ts - post_date > max_age_seconds:
            continue

        comments = post.get("comments") if isinstance(post.get("comments"), dict) else {}
        try:
            can_post_comment = int(comments.get("can_post") or 0) == 1
        except (TypeError, ValueError):
            can_post_comment = False
        if not can_post_comment:
            continue

        post_id = int(post["id"])
        material = _vk_post_material(post)
        result.append({
            "post_id": post_id,
            "owner_id": int(post.get("owner_id") or vk_user_id),
            "date": post_date,
            "material": material,
            "url": f"https://vk.com/wall{int(post.get('owner_id') or vk_user_id)}_{post_id}",
            "can_comment": True,
        })
        if len(result) >= wanted:
            break
    return result


def _extract_json_object(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        return answer
    text = str(answer or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
    return {}


def prepare_vk_warmup_comment(
    assignment_id: int,
    *,
    ask_openai_fn: Callable[..., Any] | None = None,
    posts_limit: int = 5,
) -> dict[str, Any]:
    """Выбирает свежий публичный пост и готовит мягкий человеческий комментарий."""
    rows = _sb_get(
        "agency_vk_assignments",
        {"id": f"eq.{int(assignment_id)}", "select": "*", "limit": 1},
    )
    if not rows:
        raise VKScoutError("VK-назначение не найдено")
    assignment = rows[0]
    vk_user_id = int(assignment["vk_user_id"])

    candidates = _sb_get(
        "agency_vk_candidates",
        {"vk_user_id": f"eq.{vk_user_id}", "select": "*", "limit": 1},
    )
    if not candidates:
        raise VKScoutError("VK-кандидат не найден")
    candidate = candidates[0]
    first_name = str(candidate.get("first_name") or "").strip()

    try:
        posts = fetch_public_vk_posts(
            int(assignment.get("owner_telegram_id") or 0),
            vk_user_id,
            count=posts_limit,
        )
    except VKScoutError as exc:
        raise VKScoutError(f"Не удалось прочитать публичную ленту VK: {exc}") from exc
    if not posts:
        raise VKScoutError("У этого профиля сейчас нет свежих публичных постов с открытыми комментариями. Для утепления выберем другого кандидата.")

    # Неона получает до пяти последних публикаций и сама выбирает лучшую точку касания.
    public_posts = [
        {
            "post_id": item["post_id"],
            "date": item["date"],
            "content": item["material"] or "Публикация без текста; доступно только само наличие публикации.",
        }
        for item in posts
    ]

    parsed: dict[str, Any] = {}
    if ask_openai_fn is not None:
        system = """
Ты — Неона. Сейчас ты не продаёшь и не предлагаешь Агентство W.
Твоя задача — мягко познакомить владельца кабинета с человеком через его публичный контент VK.

Из нескольких свежих публикаций выбери ОДНУ, к которой естественнее всего оставить доброжелательный комментарий.
Комментарий должен дать человеку маленькую приятность: показать, что его публикацию действительно заметили и прочитали.

Правила:
- 1–2 естественных предложения;
- комментарий пишется от лица обычного человека, НЕ от имени Неоны;
- не упоминать Агентство W, ИИ, бизнес-предложение, партнёрство, доход или встречу;
- не льстить чрезмерно и не писать шаблонное «Отличный пост!»;
- опираться только на реально предоставленное содержание;
- если пост простой или бытовой — всё равно можно тепло откликнуться на реальную мысль, подпись или сам факт, что человек делится моментом;
- если указано «фото/видео без описания», НЕ придумывать, что именно изображено или сказано;
- вопрос НЕ обязателен. Добавляй один короткий вопрос только если он звучит совершенно естественно и помогает разговору;
- не использовать фамилию человека.

Верни ТОЛЬКО JSON:
{"post_id":123,"comment":"..."}
""".strip()
        request = (
            f"Имя человека: {first_name or 'неизвестно'}\n"
            "Свежие публичные публикации:\n"
            + json.dumps(public_posts, ensure_ascii=False, indent=2)
        )
        parsed = _extract_json_object(ask_openai_fn(system, request))

    available_by_id = {int(item["post_id"]): item for item in posts}
    try:
        chosen_id = int(parsed.get("post_id"))
    except (TypeError, ValueError):
        chosen_id = int(posts[0]["post_id"])
    chosen = available_by_id.get(chosen_id) or posts[0]

    comment = re.sub(r"\s+", " ", str(parsed.get("comment") or "")).strip()
    if not comment:
        if chosen.get("material"):
            comment = "Спасибо, что поделились — приятно встретить в ленте живую мысль, а не просто очередную публикацию."
        else:
            comment = "Спасибо, что делитесь такими моментами — иногда даже небольшая публикация делает ленту чуточку теплее."

    return {
        "assignment_id": int(assignment_id),
        "vk_user_id": vk_user_id,
        "first_name": first_name,
        "post_id": int(chosen["post_id"]),
        "post_url": str(chosen["url"]),
        "post_preview": str(chosen.get("material") or "").strip(),
        "comment": comment,
    }



def fetch_vk_candidate_feed(
    owner_id: int,
    *,
    count: int = 10,
    pool_limit: int = 30,
    min_score: int = 60,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """Собирает нашу собственную «ленту» из свежих постов подходящих кандидатов.

    Домашний newsfeed.get для текущего типа VK-профиля недоступен, поэтому Радар
    не зависит от него. Берём людей, которых Неония уже оценила по ЦА, читаем их
    доступные публичные стены и собираем самые свежие посты с открытыми комментариями.
    Для разнообразия в один проход берём не более одного свежего поста от человека.
    """
    owner_id = int(owner_id)
    wanted = max(1, min(20, int(count)))
    pool_limit = max(wanted, min(60, int(pool_limit)))
    min_score = max(0, min(100, int(min_score)))

    scores = _sb_get(
        "agency_vk_candidate_scores",
        {
            "owner_telegram_id": f"eq.{owner_id}",
            "score": f"gte.{min_score}",
            "select": "vk_user_id,score,fit_summary,analyzed_at",
            "order": "score.desc,analyzed_at.desc",
            "limit": pool_limit,
        },
    )
    if not scores:
        return {
            "items": [],
            "checked": 0,
            "checked_candidates": 0,
            "pool_size": 0,
            "people": 0,
            "commentable": 0,
            "errors": [],
        }

    ids: list[int] = []
    score_by_id: dict[int, dict[str, Any]] = {}
    for row in scores:
        try:
            uid = int(row.get("vk_user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid <= 0 or uid in score_by_id:
            continue
        ids.append(uid)
        score_by_id[uid] = row

    candidates = _sb_get(
        "agency_vk_candidates",
        {
            "vk_user_id": "in.(" + ",".join(str(x) for x in ids) + ")",
            "select": "*",
            "limit": max(1, len(ids)),
        },
    ) if ids else []
    candidate_by_id = {
        int(row["vk_user_id"]): row
        for row in candidates
        if row.get("vk_user_id") is not None
    }

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    checked_candidates = 0

    for uid in ids:
        candidate = candidate_by_id.get(uid)
        if not candidate:
            continue
        checked_candidates += 1
        try:
            posts = fetch_public_vk_posts(
                owner_id,
                uid,
                count=1,
                max_age_days=max_age_days,
            )
        except Exception as exc:
            # Закрытая стена или отдельная ошибка одного профиля не должна ломать весь радар.
            errors.append(f"{uid}: {exc}")
            continue
        if not posts:
            continue

        post = posts[0]
        first_name = str(candidate.get("first_name") or "").strip()
        last_name = str(candidate.get("last_name") or "").strip()
        author_name = " ".join(x for x in (first_name, last_name) if x) or f"VK user {uid}"
        domain = str(candidate.get("domain") or "").strip()
        score_row = score_by_id.get(uid, {})

        items.append({
            "post_id": int(post["post_id"]),
            "owner_id": uid,
            "date": int(post.get("date") or 0),
            "is_person": True,
            "can_comment": True,
            "author_name": author_name,
            "first_name": first_name,
            "profile_status": str(candidate.get("status_text") or "").strip(),
            "profile_url": f"https://vk.com/{domain}" if domain else f"https://vk.com/id{uid}",
            "post_url": str(post.get("url") or ""),
            "material": str(post.get("material") or "").strip(),
            "score": int(score_row.get("score") or 0),
            "fit_summary": str(score_row.get("fit_summary") or "").strip(),
        })

    # Это и есть наша «верхняя десятка»: не по баллу, а по свежести публикации.
    items.sort(key=lambda item: int(item.get("date") or 0), reverse=True)
    items = items[:wanted]

    return {
        "items": items,
        "checked": len(items),
        "checked_candidates": checked_candidates,
        "pool_size": len(ids),
        "people": len(items),
        "commentable": len(items),
        "errors": errors,
    }


def prepare_vk_feed_radar(
    owner_id: int,
    *,
    ask_openai_fn: Callable[..., Any] | None = None,
    count: int = 10,
) -> dict[str, Any]:
    """Собирает свежую десятку из пула Неонии и выбирает естественные касания.

    Это тестовый ручной режим: Неона читает, оценивает и готовит комментарии,
    но сама ничего в VK не публикует.
    """
    feed = fetch_vk_candidate_feed(int(owner_id), count=count)
    items = list(feed.get("items") or [])
    eligible = [
        item for item in items
        if item.get("is_person") and item.get("can_comment") and int(item.get("post_id") or 0) > 0
    ]

    if not items:
        return {
            **feed,
            "recommendations": [],
            "message": (
                "Среди подходящих кандидатов сейчас не найдено свежих публичных постов "
                "с открытыми комментариями."
            ),
        }

    public_view = [
        {
            "post_id": int(item["post_id"]),
            "owner_id": int(item["owner_id"]),
            "author_name": item.get("author_name") or "",
            "first_name": item.get("first_name") or "",
            "profile_status": item.get("profile_status") or "",
            "score": int(item.get("score") or 0),
            "fit_summary": item.get("fit_summary") or "",
            "date": int(item.get("date") or 0),
            "content": item.get("material") or "Публикация без доступного текста.",
        }
        for item in eligible
    ]

    parsed: list[dict[str, Any]] = []
    if ask_openai_fn is not None:
        system = """
Ты — Неона, социальный радар Агентства W. Сейчас ты НЕ продаёшь и НЕ предлагаешь бизнес.
Перед тобой до 10 самых свежих публичных постов людей, которых Неония УЖЕ отобрала
как подходящих по целевой аудитории. Не нужно заново требовать от каждого поста бизнес-тему:
человек может сегодня писать о семье, прогулке, мысли дня, путешествии или обычной жизни.

Твоя задача — выбрать только те публикации, где можно естественно и доброжелательно откликнуться,
чтобы человеку было приятно, что его действительно заметили и прочитали.

Правила:
- 10 просмотренных постов НЕ означают 10 комментариев;
- выбери от 0 до 3 публикаций за один проход;
- комментарий 1–2 естественных предложения;
- он пишется от лица владельца кабинета, не от имени Неоны;
- не упоминать Агентство W, ИИ, партнёрство, доход, встречу или бизнес-предложение;
- не льстить чрезмерно и не писать шаблонное «Отличный пост!»;
- опираться на конкретную мысль или деталь, реально присутствующую в тексте/описании;
- бытовой или простой пост — нормальный повод для тёплого касания;
- вопрос НЕ обязателен; задавай его только если человеку естественно захочется ответить;
- если у публикации нет достаточного доступного содержания и пришлось бы фантазировать — пропусти её;
- не использовать фамилию человека в комментарии.

Верни ТОЛЬКО JSON-массив, максимум 3 объекта:
[{"post_id":123,"owner_id":456,"reason":"...","comment":"..."}]
Если естественных касаний нет, верни [].
""".strip()
        request = "10 САМЫХ СВЕЖИХ ПОСТОВ ИЗ ПУЛА НЕОНИИ:\n" + json.dumps(
            public_view,
            ensure_ascii=False,
            indent=2,
        )
        parsed = _extract_json_array(ask_openai_fn(system, request))

    by_key = {
        (int(item["owner_id"]), int(item["post_id"])): item
        for item in eligible
    }
    recommendations: list[dict[str, Any]] = []
    used: set[tuple[int, int]] = set()

    for choice in parsed:
        try:
            key = (int(choice.get("owner_id") or 0), int(choice.get("post_id") or 0))
        except (TypeError, ValueError):
            continue
        source = by_key.get(key)
        if not source or key in used:
            continue
        comment = re.sub(r"\s+", " ", str(choice.get("comment") or "")).strip()
        if not comment:
            continue
        recommendations.append({
            **source,
            "business_signal": str(source.get("fit_summary") or "").strip(),
            "reason": re.sub(r"\s+", " ", str(choice.get("reason") or "")).strip(),
            "comment": comment,
        })
        used.add(key)
        if len(recommendations) >= 3:
            break

    return {
        **feed,
        "eligible": len(eligible),
        "recommendations": recommendations,
        "message": (
            f"Проверено кандидатов: {int(feed.get('checked_candidates') or 0)}; "
            f"свежих постов в верхней десятке: {len(items)}; "
            f"Неона выбрала: {len(recommendations)}."
        ),
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
    known_contact = assignment.get("daily_position") is None or fit.startswith(KNOWN_CONTACT_LABEL)

    if ask_openai_fn:
        if known_contact:
            system = """
Ты — Неона, секретарь-референт владельца кабинета Агентства W.
Подготовь короткое первое сообщение знакомому владельца в VK.
Текст сначала увидит и при необходимости поправит сам владелец.
Правила:
- обращаться только по имени, если оно надёжно известно;
- 1–3 коротких предложения;
- в первых двух предложениях понятно представить Неону;
- учитывать заметку владельца о знакомстве, но не выдумывать степень близости;
- одна понятная польза Агентства W, без презентации списком;
- ровно один простой вопрос, и он должен быть последним предложением;
- не давать ссылку на сообщество в первом сообщении;
- не обещать доход, результат или гарантированных партнёров.
Верни только готовый текст.
""".strip()
        else:
            system = """
Ты — Неона, секретарь-референт владельца кабинета Агентства W.
Подготовь короткое первое сообщение холодному контакту VK, которого Неония отобрала по публичным данным.
Текст сначала увидит и при необходимости поправит сам владелец.
Правила:
- обращаться только по имени, если оно надёжно известно;
- 1–3 коротких предложения;
- не говорить и не намекать, что человека анализировали или оценивали;
- не выдумывать факты о человеке;
- раскрыть только одну понятную пользу Агентства W простым человеческим языком;
- ровно один простой вопрос, и он должен быть последним предложением;
- не давать ссылку на сообщество в первом сообщении;
- без давления, срочности, обещаний дохода или результата.
Верни только готовый текст.
""".strip()
        request = (
            f"Имя: {first_name or 'неизвестно'}\n"
            f"Режим: {'знакомый владельца' if known_contact else 'холодный контакт'}\n"
            f"Контекст/заметка: {fit or 'данных мало'}"
        )
        message = str(ask_openai_fn(system, request) or "").strip()
    else:
        greeting = f"{first_name}, здравствуйте!" if first_name else "Здравствуйте!"
        if known_contact:
            message = (
                f"{greeting} Я Неона, секретарь-референт в Агентстве W. "
                "Мы помогаем снять часть повседневной рутины с помощью ИИ-команды. "
                "Вам было бы интересно посмотреть, что из этого могло бы пригодиться именно вам?"
            )
        else:
            message = (
                f"{greeting} Я Неона, секретарь-референт в Агентстве W. "
                "Мы помогаем освобождать время от повторяющейся работы с помощью ИИ-команды. "
                "Вам было бы интересно посмотреть, как это работает?"
            )

    now = datetime.now(UTC).isoformat()
    _sb_patch(
        "agency_vk_assignments",
        {"id": f"eq.{int(assignment_id)}"},
        {
            "status": "prepared",
            "invitation_text": message,
            # Первое сообщение больше не заставляет человека идти в сообщество.
            "invitation_link": None,
            "prepared_at": now,
            "updated_at": now,
        },
    )
    return {
        "assignment_id": int(assignment_id),
        "vk_user_id": int(candidate["vk_user_id"]),
        "name": " ".join(x for x in [str(candidate.get("first_name") or "").strip(), str(candidate.get("last_name") or "").strip()] if x),
        "profile_url": vk_profile_url(candidate),
        "invitation_text": message,
        "invitation_link": None,
        "known_contact": known_contact,
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
            "daily_position": "not.is.null",
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

# ---------------------------------------------------------------------------
# VK live comment dialogue: published comment -> reply -> Neona continuation
# ---------------------------------------------------------------------------


def _vk_owner_user_id(owner_telegram_id: int) -> int:
    """Returns the VK user id connected to this Agency W owner."""
    rows = _sb_get(
        "agency_vk_oauth_tokens",
        {
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "select": "vk_user_id",
            "limit": 1,
        },
    )
    if not rows or not rows[0].get("vk_user_id"):
        raise VKScoutError("Не найден VK ID владельца. Переподключите VK Scout.")
    return int(rows[0]["vk_user_id"])




def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None

def _compact_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _comment_material(comment: dict[str, Any]) -> str:
    """Human-readable inbound comment content, including simple non-text signals."""
    text = _compact_ws(comment.get("text"))
    if text:
        return text

    sticker_id = comment.get("sticker_id")
    if sticker_id:
        return f"[стикер VK #{sticker_id}]"

    attachments = comment.get("attachments") or []
    kinds: list[str] = []
    for attachment in attachments:
        if isinstance(attachment, dict):
            kind = _compact_ws(attachment.get("type"))
            if kind:
                kinds.append(kind)
    if kinds:
        return "[вложение: " + ", ".join(kinds[:4]) + "]"
    return "[ответ без текста]"


def _flatten_vk_comments(response: Any) -> list[dict[str, Any]]:
    """Flattens top-level wall comments and the thread items returned with them."""
    if not isinstance(response, dict):
        return []
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        try:
            cid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0 or cid in seen:
            return
        seen.add(cid)
        result.append(item)
        thread = item.get("thread") if isinstance(item.get("thread"), dict) else {}
        for child in thread.get("items") or []:
            add(child)

    for item in response.get("items") or []:
        add(item)
    return result


def _fetch_vk_post_comments(
    owner_telegram_id: int,
    post_owner_id: int,
    post_id: int,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    response = _vk_user_api(
        int(owner_telegram_id),
        "wall.getComments",
        owner_id=int(post_owner_id),
        post_id=int(post_id),
        need_likes=1,
        count=max(1, min(100, int(count))),
        sort="desc",
        preview_length=0,
        extended=0,
        thread_items_count=10,
    )
    return _flatten_vk_comments(response)


def _fetch_vk_post_comments_static(
    post_owner_id: int,
    post_id: int,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    """Second attempt with the already configured static VK_ACCESS_TOKEN.

    In Agency W this token is the existing VK integration token stored in
    Streamlit/Render. We test it before asking the owner for any new VK key.
    """
    response = _vk_api(
        "wall.getComments",
        owner_id=int(post_owner_id),
        post_id=int(post_id),
        need_likes=1,
        count=max(1, min(100, int(count))),
        sort="desc",
        preview_length=0,
        extended=0,
        thread_items_count=10,
    )
    return _flatten_vk_comments(response)


def _is_profile_type_method_error(exc: Exception, method: str = "wall.getComments") -> bool:
    text = str(exc or "").casefold()
    return method.casefold() in text and ("1051" in text or "current profile type" in text)


def _fetch_vk_notifications(
    owner_telegram_id: int,
    *,
    start_time: int,
    count: int = 100,
) -> dict[str, Any]:
    """Read the owner's VK bell notifications with the already connected user token.

    This is a fallback for VK ID profile types where wall.getComments returns 1051.
    VK API 5.199 documents notifications.get as a user-token method.
    """
    response = _vk_user_api(
        int(owner_telegram_id),
        "notifications.get",
        count=max(1, min(100, int(count))),
        filters="comments,likes",
        start_time=max(0, int(start_time)),
    )
    return response if isinstance(response, dict) else {}


def _iter_nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_dicts(child)


def _notification_post_ids(item: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for obj in _iter_nested_dicts(item):
        if "post_id" in obj:
            try:
                result.add(int(obj.get("post_id") or 0))
            except (TypeError, ValueError):
                pass
    result.discard(0)
    return result


def _notification_feedback(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("feedback")
    return value if isinstance(value, dict) else {}


def _notification_candidate_match(
    item: dict[str, Any],
    *,
    candidate_vk_user_id: int,
    post_id: int,
) -> bool:
    feedback = _notification_feedback(item)
    try:
        from_id = int(feedback.get("from_id") or 0)
    except (TypeError, ValueError):
        from_id = 0
    if from_id != int(candidate_vk_user_id):
        return False

    ntype = _compact_ws(item.get("type")).casefold()
    if not (ntype.startswith("reply_comment") or ntype.startswith("comment_") or ntype.startswith("like_comment")):
        return False

    post_ids = _notification_post_ids(item)
    if post_ids and int(post_id) not in post_ids:
        return False
    return True


def _synthetic_comment_id(owner_telegram_id: int, post_owner_id: int, post_id: int, text: str) -> int:
    """Stable negative placeholder used only when VK refuses wall.getComments for this profile type."""
    raw = f"{int(owner_telegram_id)}|{int(post_owner_id)}|{int(post_id)}|{_compact_ws(text)}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:7], "big") or 1
    return -value


def _json_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in out:
            out.append(number)
    return out


def _json_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def load_vk_comment_thread(
    owner_telegram_id: int,
    post_owner_id: int,
    post_id: int,
) -> dict[str, Any] | None:
    """Loads the latest tracked Agency W comment thread for a VK post."""
    rows = _sb_get(
        "agency_vk_comment_threads",
        {
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "post_owner_id": f"eq.{int(post_owner_id)}",
            "post_id": f"eq.{int(post_id)}",
            "select": "*",
            "order": "created_at.desc",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def load_vk_comment_thread_by_id(thread_id: int) -> dict[str, Any] | None:
    rows = _sb_get(
        "agency_vk_comment_threads",
        {"id": f"eq.{int(thread_id)}", "select": "*", "limit": 1},
    )
    return rows[0] if rows else None


def register_published_vk_comment(
    owner_telegram_id: int,
    *,
    post_owner_id: int,
    post_id: int,
    comment_text: str,
    post_url: str = "",
    post_text: str = "",
    first_name: str = "",
    source: str = "radar",
    assignment_id: int | None = None,
) -> dict[str, Any]:
    """Finds the manually published comment in VK and starts watching its thread.

    The owner first publishes the prepared text from their own VK account, then
    presses "Комментарий опубликован". We locate the real VK comment id instead
    of trusting a local checkbox, so later replies can be matched reliably.
    """
    owner_telegram_id = int(owner_telegram_id)
    post_owner_id = int(post_owner_id)
    post_id = int(post_id)
    wanted = _compact_ws(comment_text)
    if not wanted:
        raise VKScoutError("Текст комментария пустой.")

    owner_vk_user_id = _vk_owner_user_id(owner_telegram_id)
    candidates: list[dict[str, Any]] = []
    profile_type_fallback = False
    try:
        comments = _fetch_vk_post_comments(owner_telegram_id, post_owner_id, post_id, count=100)
    except Exception as exc:
        if _is_profile_type_method_error(exc):
            try:
                comments = _fetch_vk_post_comments_static(post_owner_id, post_id, count=100)
            except Exception as static_exc:
                comments = []
                profile_type_fallback = True
                # Keep the static-token diagnosis visible if registration must
                # fall back to a synthetic comment id.
                static_error = str(static_exc)[:900]
            else:
                static_error = ""
        else:
            raise

    for item in comments:
        try:
            from_id = int(item.get("from_id") or 0)
        except (TypeError, ValueError):
            from_id = 0
        if from_id != owner_vk_user_id:
            continue
        if _compact_ws(item.get("text")) != wanted:
            continue
        candidates.append(item)

    if candidates:
        published = max(candidates, key=lambda x: int(x.get("date") or 0))
        comment_id = int(published["id"])
        published_ts = int(published.get("date") or 0)
        published_at = (
            datetime.fromtimestamp(published_ts, UTC).isoformat()
            if published_ts > 0 else datetime.now(UTC).isoformat()
        )
    elif profile_type_fallback:
        # VK ID currently allows us to read the wall but may reject wall.getComments
        # with 1051. Register a stable placeholder and watch the user's bell
        # notifications instead. The first real inbound reply gives us its own
        # comment id, which is enough to answer directly in the same thread.
        comment_id = _synthetic_comment_id(owner_telegram_id, post_owner_id, post_id, wanted)
        published_at = datetime.now(UTC).isoformat()
    else:
        raise VKScoutError(
            "Не нашла этот комментарий в VK. Сначала опубликуйте именно показанный текст "
            "под выбранным постом, затем нажмите «Комментарий опубликован»."
        )
    now = datetime.now(UTC).isoformat()
    initial_history = [
        {
            "direction": "out",
            "kind": "comment",
            "comment_id": comment_id,
            "text": wanted,
            "at": published_at,
        }
    ]
    payload = {
        "owner_telegram_id": owner_telegram_id,
        "owner_vk_user_id": owner_vk_user_id,
        "candidate_vk_user_id": post_owner_id,
        "post_owner_id": post_owner_id,
        "post_id": post_id,
        "post_url": _compact_ws(post_url) or None,
        "post_text": str(post_text or "").strip() or None,
        "first_name": _compact_ws(first_name) or None,
        "source": _compact_ws(source) or "radar",
        "assignment_id": int(assignment_id) if assignment_id else None,
        "initial_comment_id": comment_id,
        "initial_comment_text": wanted,
        "initial_comment_published_at": published_at,
        "last_outbound_comment_id": comment_id,
        "last_outbound_text": wanted,
        "outbound_comment_ids": [comment_id],
        "processed_event_keys": [],
        "dialogue_history": initial_history,
        "status": "watching",
        "last_checked_at": now,
        "last_error": None,
        "updated_at": now,
    }
    rows = _sb_post(
        "agency_vk_comment_threads",
        payload,
        on_conflict="owner_telegram_id,post_owner_id,post_id,initial_comment_id",
    )
    thread = rows[0] if rows else payload

    if assignment_id:
        try:
            _sb_patch(
                "agency_vk_assignments",
                {"id": f"eq.{int(assignment_id)}"},
                {"updated_at": now},
            )
        except Exception:
            pass
    return thread


def _candidate_liked_comment(
    owner_telegram_id: int,
    post_owner_id: int,
    comment_id: int,
    candidate_vk_user_id: int,
) -> bool:
    """Checks classic VK like/reaction identity for a comment.

    VK API 5.199 exposes likes on wall comments, but does not reliably expose
    the semantic emoji reaction type here. We therefore treat this only as
    "a reaction/like was received", never inventing what the emoji meant.
    """
    try:
        response = _vk_user_api(
            int(owner_telegram_id),
            "likes.getList",
            type="comment",
            owner_id=int(post_owner_id),
            item_id=int(comment_id),
            filter="likes",
            count=1000,
        )
    except Exception:
        return False
    if not isinstance(response, dict):
        return False
    for item in response.get("items") or []:
        try:
            uid = int(item.get("id") if isinstance(item, dict) else item)
        except (TypeError, ValueError):
            continue
        if uid == int(candidate_vk_user_id):
            return True
    return False


def _reply_target_matches(comment: dict[str, Any], outbound_ids: set[int]) -> bool:
    try:
        direct = int(comment.get("reply_to_comment") or 0)
    except (TypeError, ValueError):
        direct = 0
    if direct and direct in outbound_ids:
        return True
    parents = comment.get("parents_stack") if isinstance(comment.get("parents_stack"), list) else []
    for value in parents:
        try:
            if int(value) in outbound_ids:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _neona_vk_thread_reply(
    thread: dict[str, Any],
    *,
    event_kind: str,
    inbound_text: str,
    ask_openai_fn: Callable[..., Any] | None,
) -> str:
    first_name = _compact_ws(thread.get("first_name"))
    post_text = str(thread.get("post_text") or "").strip()
    history = thread.get("dialogue_history") if isinstance(thread.get("dialogue_history"), list) else []

    if ask_openai_fn is None:
        if event_kind == "reaction":
            return "Вижу вашу реакцию 🙂 Что именно в этой мысли вам откликнулось?"
        return "Интересно, спасибо за ответ. А что в этой теме для вас сейчас самое важное?"

    system = """
Ты — Неона, секретарь-референт владельца кабинета Агентства W, но этот разговор идёт
в публичной ветке комментариев с ЛИЧНОГО VK-аккаунта владельца. Поэтому НЕ представляйся
Неоной и не говори от имени бота/ИИ. Продолжай разговор естественно от лица владельца.

Это уже начатый диалог. Нельзя знакомиться заново и нельзя повторять собеседнику его же фразу.
Сначала пойми смысл последней реплики и ответь именно на неё.

Стиль:
- 1–3 коротких человеческих предложения;
- простой язык, но мысль не примитивная;
- можно лёгкий уместный юмор;
- не использовать фамилию;
- не льстить и не писать шаблонные «Согласна», «Отлично», «Очень интересно»;
- не упоминать Агентство W, доход, регистрацию, партнёрство или встречу, пока сам человек
  не вывел разговор к соответствующей потребности;
- вопрос в конце только если он естественно двигает разговор дальше;
- если человек отвечает одним emoji/стикером — учитывай смысл осторожно, не выдумывай;
- если событие обозначено как реакция/лайк и точный тип реакции неизвестен, прямо НЕ называй
  конкретный emoji; можно мягко спросить, что именно откликнулось.

Верни только готовый ответ без пояснений и кавычек.
""".strip()
    request = (
        f"Имя собеседника: {first_name or 'неизвестно'}\n"
        f"Исходная публикация: {post_text or '[текст публикации недоступен]'}\n"
        "История ветки:\n"
        + json.dumps(history[-12:], ensure_ascii=False, indent=2)
        + "\n\n"
        + f"Новое событие: {event_kind}\n"
        + f"Содержание: {inbound_text}"
    )
    answer = _compact_ws(ask_openai_fn(system, request))
    if not answer:
        raise VKScoutError("Неона не вернула ответ для VK-диалога")
    return answer


def _send_vk_thread_reply(
    thread: dict[str, Any],
    *,
    reply_to_comment_id: int,
    message: str,
    event_key: str,
) -> int:
    """Posts one reply in the same VK comment thread."""
    guid = f"agencyw_{int(thread['id'])}_{re.sub(r'[^0-9A-Za-z_:-]+', '_', event_key)[:80]}"
    response = _vk_user_api(
        int(thread["owner_telegram_id"]),
        "wall.createComment",
        owner_id=int(thread["post_owner_id"]),
        post_id=int(thread["post_id"]),
        message=str(message or "").strip(),
        reply_to_comment=int(reply_to_comment_id),
        guid=guid,
    )
    if isinstance(response, dict):
        cid = response.get("comment_id") or response.get("id")
    else:
        cid = response
    try:
        comment_id = int(cid)
    except (TypeError, ValueError) as exc:
        raise VKScoutError("VK не вернул ID ответа Неоны") from exc
    if comment_id <= 0:
        raise VKScoutError("VK не вернул ID ответа Неоны")
    return comment_id


def _append_history(history: Any, *events: dict[str, Any]) -> list[dict[str, Any]]:
    out = list(history) if isinstance(history, list) else []
    out.extend(events)
    return out[-40:]


def _mark_assignment_dialogue(thread: dict[str, Any]) -> None:
    assignment_id = thread.get("assignment_id")
    if not assignment_id:
        return
    try:
        _sb_patch(
            "agency_vk_assignments",
            {"id": f"eq.{int(assignment_id)}"},
            {"status": "dialogue", "updated_at": datetime.now(UTC).isoformat()},
        )
    except Exception:
        pass


def _process_vk_thread_event(
    thread: dict[str, Any],
    *,
    event_kind: str,
    event_key: str,
    reply_to_comment_id: int,
    inbound_comment_id: int | None,
    inbound_text: str,
    inbound_at: str,
    ask_openai_fn: Callable[..., Any] | None,
    auto_send: bool,
) -> dict[str, Any]:
    """Generates Neona's next reply and sends it when allowed."""
    now = datetime.now(UTC).isoformat()
    history = _append_history(
        thread.get("dialogue_history"),
        {
            "direction": "in",
            "kind": event_kind,
            "comment_id": int(inbound_comment_id) if inbound_comment_id else None,
            "reply_to_comment": int(reply_to_comment_id),
            "text": inbound_text,
            "at": inbound_at or now,
        },
    )
    reply = _neona_vk_thread_reply(
        {**thread, "dialogue_history": history},
        event_kind=event_kind,
        inbound_text=inbound_text,
        ask_openai_fn=ask_openai_fn,
    )

    processed = _json_str_list(thread.get("processed_event_keys"))
    if event_key not in processed:
        processed.append(event_key)

    base_patch: dict[str, Any] = {
        "status": "reply_ready",
        "pending_event_kind": event_kind,
        "pending_event_key": event_key,
        "pending_reply_to_comment_id": int(reply_to_comment_id),
        "pending_inbound_text": inbound_text,
        "neona_reply_text": reply,
        "last_inbound_comment_id": int(inbound_comment_id) if inbound_comment_id else None,
        "last_inbound_text": inbound_text,
        "last_inbound_at": inbound_at or now,
        "dialogue_history": history,
        "processed_event_keys": processed,
        "last_checked_at": now,
        "last_error": None,
        "updated_at": now,
    }
    _sb_patch("agency_vk_comment_threads", {"id": f"eq.{int(thread['id'])}"}, base_patch)
    _mark_assignment_dialogue(thread)
    updated = {**thread, **base_patch}

    # Textual replies are continued automatically. A bare like/reaction is seen
    # and understood as a signal, but its exact emoji meaning is not exposed by
    # this VK API surface, so we prepare the reply and leave one-click approval.
    should_send = bool(auto_send and event_kind != "reaction")
    if not should_send:
        return updated

    try:
        sent_id = _send_vk_thread_reply(
            updated,
            reply_to_comment_id=int(reply_to_comment_id),
            message=reply,
            event_key=event_key,
        )
    except Exception as exc:
        err_patch = {
            "status": "reply_ready",
            "last_error": str(exc)[:1500],
            "last_checked_at": now,
            "updated_at": now,
        }
        _sb_patch("agency_vk_comment_threads", {"id": f"eq.{int(thread['id'])}"}, err_patch)
        return {**updated, **err_patch}

    outbound = _json_int_list(thread.get("outbound_comment_ids"))
    if sent_id not in outbound:
        outbound.append(sent_id)
    sent_history = _append_history(
        history,
        {
            "direction": "out",
            "kind": "comment",
            "comment_id": sent_id,
            "reply_to_comment": int(reply_to_comment_id),
            "text": reply,
            "at": now,
        },
    )
    sent_patch = {
        "status": "watching",
        "last_outbound_comment_id": sent_id,
        "last_outbound_text": reply,
        "outbound_comment_ids": outbound,
        "dialogue_history": sent_history,
        "pending_event_kind": None,
        "pending_event_key": None,
        "pending_reply_to_comment_id": None,
        "pending_inbound_text": None,
        "neona_reply_comment_id": sent_id,
        "replied_at": now,
        "last_error": None,
        "last_checked_at": now,
        "updated_at": now,
    }
    _sb_patch("agency_vk_comment_threads", {"id": f"eq.{int(thread['id'])}"}, sent_patch)
    return {**updated, **sent_patch}


def check_vk_comment_thread(
    thread_id: int,
    *,
    ask_openai_fn: Callable[..., Any] | None = None,
    auto_send: bool = True,
) -> dict[str, Any]:
    """Checks one tracked VK thread and lets Neona continue a real reply."""
    thread = load_vk_comment_thread_by_id(int(thread_id))
    if not thread:
        raise VKScoutError("VK-ветка комментария не найдена")
    if str(thread.get("status") or "") in {"paused", "closed"}:
        return thread
    # Do not generate a second answer while one is waiting for manual send.
    if str(thread.get("status") or "") == "reply_ready" and thread.get("pending_event_key"):
        return thread

    outbound_ids = set(_json_int_list(thread.get("outbound_comment_ids")))
    if not outbound_ids and thread.get("initial_comment_id"):
        outbound_ids.add(int(thread["initial_comment_id"]))
    processed = set(_json_str_list(thread.get("processed_event_keys")))
    notification_fallback = False
    static_token_error = ""
    try:
        comments = _fetch_vk_post_comments(
            int(thread["owner_telegram_id"]),
            int(thread["post_owner_id"]),
            int(thread["post_id"]),
            count=100,
        )
    except Exception as exc:
        if _is_profile_type_method_error(exc):
            # The connected VK ID token is profile-limited. Before trying any
            # other workaround, test the VK_ACCESS_TOKEN that Agency W already
            # has configured for the VK integration.
            try:
                comments = _fetch_vk_post_comments_static(
                    int(thread["post_owner_id"]),
                    int(thread["post_id"]),
                    count=100,
                )
            except Exception as static_exc:
                comments = []
                static_token_error = str(static_exc)[:1200]
                notification_fallback = True
        else:
            raise

    inbound: list[dict[str, Any]] = []
    candidate_id = int(thread["candidate_vk_user_id"])
    for item in comments:
        try:
            cid = int(item.get("id") or 0)
            from_id = int(item.get("from_id") or 0)
        except (TypeError, ValueError):
            continue
        event_key = f"comment:{cid}"
        if cid <= 0 or from_id != candidate_id or event_key in processed:
            continue
        if not _reply_target_matches(item, outbound_ids):
            continue
        inbound.append(item)

    if inbound:
        # Process the oldest unseen reply first to preserve conversational order.
        item = min(inbound, key=lambda x: int(x.get("date") or 0))
        cid = int(item["id"])
        try:
            reply_to = int(item.get("reply_to_comment") or 0)
        except (TypeError, ValueError):
            reply_to = 0
        if reply_to not in outbound_ids:
            reply_to = int(thread.get("last_outbound_comment_id") or thread.get("initial_comment_id") or 0)
        ts = int(item.get("date") or 0)
        inbound_at = datetime.fromtimestamp(ts, UTC).isoformat() if ts > 0 else datetime.now(UTC).isoformat()
        return _process_vk_thread_event(
            thread,
            event_kind="comment",
            event_key=f"comment:{cid}",
            reply_to_comment_id=cid,  # Neona replies directly to the person's new comment.
            inbound_comment_id=cid,
            inbound_text=_comment_material(item),
            inbound_at=inbound_at,
            ask_openai_fn=ask_openai_fn,
            auto_send=auto_send,
        )

    if notification_fallback:
        published = _parse_dt(thread.get("initial_comment_published_at")) or _parse_dt(thread.get("created_at")) or datetime.now(UTC)
        start_time = int((published - timedelta(minutes=15)).timestamp())
        try:
            notifications = _fetch_vk_notifications(
                int(thread["owner_telegram_id"]),
                start_time=start_time,
                count=100,
            )
        except Exception as notification_exc:
            if static_token_error:
                raise VKScoutError(
                    "Существующий VK_ACCESS_TOKEN тоже не смог прочитать wall.getComments: "
                    + static_token_error
                    + "; VK ID notifications.get: "
                    + str(notification_exc)[:900]
                ) from notification_exc
            raise
        candidates = []
        for note in notifications.get("items") or []:
            if not isinstance(note, dict):
                continue
            if not _notification_candidate_match(
                note,
                candidate_vk_user_id=candidate_id,
                post_id=int(thread["post_id"]),
            ):
                continue
            feedback = _notification_feedback(note)
            try:
                fid = int(feedback.get("id") or 0)
            except (TypeError, ValueError):
                fid = 0
            ntype = _compact_ws(note.get("type")).casefold()
            ts = int(note.get("date") or 0)
            event_key = f"notification:{ntype}:{fid}:{ts}"
            if event_key in processed:
                continue
            candidates.append((ts, ntype, fid, feedback, event_key))

        if candidates:
            ts, ntype, fid, feedback, event_key = min(candidates, key=lambda x: x[0] or 0)
            inbound_at = datetime.fromtimestamp(ts, UTC).isoformat() if ts > 0 else datetime.now(UTC).isoformat()
            inbound_text = _compact_ws(feedback.get("text"))
            if ntype.startswith("like_comment"):
                return _process_vk_thread_event(
                    thread,
                    event_kind="reaction",
                    event_key=event_key,
                    reply_to_comment_id=(fid if fid > 0 else int(thread.get("last_outbound_comment_id") or 0)),
                    inbound_comment_id=None,
                    inbound_text="[человек поставил реакцию/лайк на комментарий; точный тип реакции VK API не сообщил]",
                    inbound_at=inbound_at,
                    ask_openai_fn=ask_openai_fn,
                    auto_send=False,
                )
            if fid > 0:
                return _process_vk_thread_event(
                    thread,
                    event_kind="comment",
                    event_key=event_key,
                    reply_to_comment_id=fid,
                    inbound_comment_id=fid,
                    inbound_text=inbound_text or "[ответ без текста]",
                    inbound_at=inbound_at,
                    ask_openai_fn=ask_openai_fn,
                    auto_send=auto_send,
                )

    # No text reply: notice one classic like/reaction on the latest Neona comment.
    last_outbound = int(thread.get("last_outbound_comment_id") or thread.get("initial_comment_id") or 0)
    if last_outbound > 0:
        reaction_key = f"reaction:{last_outbound}:{candidate_id}"
        if reaction_key not in processed and _candidate_liked_comment(
            int(thread["owner_telegram_id"]),
            int(thread["post_owner_id"]),
            last_outbound,
            candidate_id,
        ):
            return _process_vk_thread_event(
                thread,
                event_kind="reaction",
                event_key=reaction_key,
                reply_to_comment_id=last_outbound,
                inbound_comment_id=None,
                inbound_text="[человек поставил реакцию/лайк; точный тип реакции VK API не сообщил]",
                inbound_at=datetime.now(UTC).isoformat(),
                ask_openai_fn=ask_openai_fn,
                auto_send=False,
            )

    now = datetime.now(UTC).isoformat()
    _sb_patch(
        "agency_vk_comment_threads",
        {"id": f"eq.{int(thread_id)}"},
        {"last_checked_at": now, "last_error": None, "updated_at": now},
    )
    return {**thread, "last_checked_at": now, "last_error": None}


def send_pending_vk_comment_reply(thread_id: int) -> dict[str, Any]:
    """Sends a prepared Neona reply that could not/should not be auto-posted."""
    thread = load_vk_comment_thread_by_id(int(thread_id))
    if not thread:
        raise VKScoutError("VK-ветка комментария не найдена")
    message = _compact_ws(thread.get("neona_reply_text"))
    reply_to = int(thread.get("pending_reply_to_comment_id") or 0)
    event_key = _compact_ws(thread.get("pending_event_key"))
    if not message or reply_to <= 0 or not event_key:
        raise VKScoutError("Нет подготовленного ответа Неоны для отправки")

    sent_id = _send_vk_thread_reply(
        thread,
        reply_to_comment_id=reply_to,
        message=message,
        event_key=event_key,
    )
    now = datetime.now(UTC).isoformat()
    outbound = _json_int_list(thread.get("outbound_comment_ids"))
    if sent_id not in outbound:
        outbound.append(sent_id)
    history = _append_history(
        thread.get("dialogue_history"),
        {
            "direction": "out",
            "kind": "comment",
            "comment_id": sent_id,
            "reply_to_comment": reply_to,
            "text": message,
            "at": now,
        },
    )
    patch = {
        "status": "watching",
        "last_outbound_comment_id": sent_id,
        "last_outbound_text": message,
        "outbound_comment_ids": outbound,
        "dialogue_history": history,
        "pending_event_kind": None,
        "pending_event_key": None,
        "pending_reply_to_comment_id": None,
        "pending_inbound_text": None,
        "neona_reply_comment_id": sent_id,
        "replied_at": now,
        "last_error": None,
        "last_checked_at": now,
        "updated_at": now,
    }
    _sb_patch("agency_vk_comment_threads", {"id": f"eq.{int(thread_id)}"}, patch)
    return {**thread, **patch}


def watch_vk_comment_threads(
    *,
    ask_openai_fn: Callable[..., Any] | None = None,
    auto_send: bool = True,
    limit: int = 100,
) -> dict[str, int]:
    """Background pass over active VK comment threads."""
    rows = _sb_get(
        "agency_vk_comment_threads",
        {
            "status": "in.(watching,error)",
            "select": "*",
            "order": "last_checked_at.asc.nullsfirst,created_at.asc",
            "limit": max(1, min(500, int(limit))),
        },
    )
    stats = {"threads": len(rows), "checked": 0, "replies": 0, "ready": 0, "errors": 0}
    for row in rows:
        try:
            before_out = int(row.get("last_outbound_comment_id") or 0)
            before_in = int(row.get("last_inbound_comment_id") or 0)
            updated = check_vk_comment_thread(
                int(row["id"]),
                ask_openai_fn=ask_openai_fn,
                auto_send=auto_send,
            )
            stats["checked"] += 1
            if str(updated.get("status") or "") == "reply_ready":
                stats["ready"] += 1
            after_out = int(updated.get("last_outbound_comment_id") or 0)
            after_in = int(updated.get("last_inbound_comment_id") or 0)
            if after_in != before_in or after_out != before_out:
                stats["replies"] += 1
        except Exception as exc:
            stats["errors"] += 1
            try:
                now = datetime.now(UTC).isoformat()
                _sb_patch(
                    "agency_vk_comment_threads",
                    {"id": f"eq.{int(row['id'])}"},
                    {"status": "error", "last_error": str(exc)[:1500], "last_checked_at": now, "updated_at": now},
                )
            except Exception:
                pass
    return stats
