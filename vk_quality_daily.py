from __future__ import annotations

"""Quality gate for Agency W daily VK candidates.

The legacy VK queue can discover people quickly, but a daily working card is
allowed only after Neonia has scored that same profile against the owner's
saved target-audience portrait. This module does not send messages.
"""

from datetime import date, datetime, timezone
from typing import Any

from vk_scout import (
    ACTIVE_ASSIGNMENT_STATUSES,
    _owner_processed_vk_ids,
    _partner_vk_ids,
    _sb_get,
    _sb_patch,
    _sb_post,
    _used_vk_ids,
    load_today_vk_assignments,
    load_vk_sources,
)

UTC = timezone.utc


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _qualified_scores(owner_id: int, min_score: int) -> list[dict[str, Any]]:
    return _sb_get(
        "agency_vk_candidate_scores",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "score": f"gte.{int(min_score)}",
            "select": "vk_user_id,score,fit_summary,positive_signals,weak_fit_signals,analyzed_at",
            "order": "score.desc,analyzed_at.desc",
            "limit": 5000,
        },
    )


def _candidate_rows(ids: list[int]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 200):
        chunk = ids[start : start + 200]
        if not chunk:
            continue
        rows = _sb_get(
            "agency_vk_candidates",
            {
                "vk_user_id": "in.(" + ",".join(str(x) for x in chunk) + ")",
                "can_write_private_message": "eq.true",
                "select": "*",
                "limit": len(chunk),
            },
        )
        for row in rows:
            uid = _int(row.get("vk_user_id"))
            if uid > 0:
                result[uid] = row
    return result


def _today_raw(owner_id: int) -> list[dict[str, Any]]:
    return _sb_get(
        "agency_vk_assignments",
        {
            "owner_telegram_id": f"eq.{int(owner_id)}",
            "assignment_date": f"eq.{date.today().isoformat()}",
            "daily_position": "not.is.null",
            "select": "*",
            "order": "daily_position.asc,id.asc",
            "limit": 100,
        },
    )


def ensure_quality_vk_assignments(
    owner_id: int,
    member_code: str,
    *,
    limit: int = 5,
    min_score: int = 50,
) -> dict[str, Any]:
    """Keep up to five daily VK cards, all with Neonia score >= min_score."""
    owner_id = int(owner_id)
    limit = max(1, min(5, int(limit)))
    min_score = max(0, min(100, int(min_score)))

    sources = load_vk_sources(owner_id)
    source_ids = {
        abs(_int(source.get("community_id")))
        for source in sources
        if abs(_int(source.get("community_id"))) > 0
    }
    if not source_ids:
        return {
            "ok": True,
            "assignments": [],
            "complete": False,
            "created": 0,
            "message": "Нет активных VK-источников для качественного отбора.",
        }

    score_rows = _qualified_scores(owner_id, min_score)
    score_by_id: dict[int, dict[str, Any]] = {}
    ordered_ids: list[int] = []
    for row in score_rows:
        uid = _int(row.get("vk_user_id"))
        if uid <= 0 or uid in score_by_id:
            continue
        score_by_id[uid] = row
        ordered_ids.append(uid)

    candidates = _candidate_rows(ordered_ids)
    qualified_ids = {
        uid
        for uid, candidate in candidates.items()
        if abs(_int(candidate.get("source_community_id"))) in source_ids
    }

    # Old sequential cards are not allowed to occupy today's working slots
    # unless they have actually passed Neonia's quality gate.
    now_iso = datetime.now(UTC).isoformat()
    for row in _today_raw(owner_id):
        assignment_id = _int(row.get("id"))
        uid = _int(row.get("vk_user_id"))
        if assignment_id <= 0:
            continue
        if uid in qualified_ids:
            _sb_patch(
                "agency_vk_assignments",
                {"id": f"eq.{assignment_id}"},
                {
                    "score": _int(score_by_id[uid].get("score")),
                    "updated_at": now_iso,
                },
            )
            continue
        _sb_patch(
            "agency_vk_assignments",
            {"id": f"eq.{assignment_id}"},
            {
                "daily_position": None,
                "status": "not_fit",
                "released_at": now_iso,
                "updated_at": now_iso,
            },
        )

    active_today = load_today_vk_assignments(owner_id)
    active_ids = {_int(row.get("vk_user_id")) for row in active_today}
    used_positions = {
        _int(row.get("daily_position"))
        for row in active_today
        if _int(row.get("daily_position")) > 0
    }
    free_positions = [p for p in range(1, limit + 1) if p not in used_positions]

    processed = _owner_processed_vk_ids(owner_id)
    global_used = _used_vk_ids()
    partner_ids = _partner_vk_ids()
    created = 0

    for uid in ordered_ids:
        if not free_positions:
            break
        if uid not in qualified_ids or uid in active_ids:
            continue
        if uid in processed or uid in global_used or uid in partner_ids:
            continue

        candidate = candidates.get(uid) or {}
        score_row = score_by_id.get(uid) or {}
        position = free_positions[0]
        payload = {
            "vk_user_id": uid,
            "owner_telegram_id": owner_id,
            "owner_member_code": str(member_code or "").strip() or None,
            "assignment_date": date.today().isoformat(),
            "daily_position": position,
            "status": "reserved",
            "score": _int(score_row.get("score")),
            "fit_summary": str(score_row.get("fit_summary") or "").strip() or None,
            "reserved_at": now_iso,
            "reservation_until": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        try:
            rows = _sb_post("agency_vk_assignments", payload)
        except Exception:
            continue
        created_row = rows[0] if rows else payload

        # One VK person must never be assigned to two Agency W partners.
        active = _sb_get(
            "agency_vk_assignments",
            {
                "vk_user_id": f"eq.{uid}",
                "status": "in.(" + ",".join(ACTIVE_ASSIGNMENT_STATUSES) + ")",
                "select": "id,owner_telegram_id",
                "order": "id.asc",
                "limit": 20,
            },
        )
        winner = active[0] if active else created_row
        if _int(winner.get("owner_telegram_id")) != owner_id:
            created_id = _int(created_row.get("id"))
            if created_id > 0:
                _sb_patch(
                    "agency_vk_assignments",
                    {"id": f"eq.{created_id}"},
                    {
                        "status": "released",
                        "daily_position": None,
                        "released_at": now_iso,
                        "updated_at": now_iso,
                    },
                )
            global_used.add(uid)
            continue

        created += 1
        active_ids.add(uid)
        processed.add(uid)
        global_used.add(uid)
        free_positions.pop(0)

    final_rows = load_today_vk_assignments(owner_id)
    count = min(len(final_rows), limit)
    return {
        "ok": True,
        "assignments": final_rows[:limit],
        "created": created,
        "complete": count >= limit,
        "qualified_pool": len(qualified_ids),
        "message": (
            f"VK-пятёрка Неонии: {count}/{limit}. "
            f"Все показанные кандидаты имеют оценку ЦА не ниже {min_score}."
        ),
    }
