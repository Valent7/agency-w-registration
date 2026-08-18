from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
import streamlit as st

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def _config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise RuntimeError("Не найдены SUPABASE_URL или SUPABASE_SECRET_KEY.")
    return url, key


def _headers() -> dict[str, str]:
    _, key = _config()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _get(table: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
    url, _ = _config()
    response = requests.get(
        f"{url}/rest/v1/{table}",
        headers=_headers(),
        params=params,
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _local_range_to_utc(start_day: date, end_day_exclusive: date) -> tuple[str, str]:
    local_start = datetime.combine(start_day, time.min, tzinfo=BERLIN)
    local_end = datetime.combine(end_day_exclusive, time.min, tzinfo=BERLIN)
    return (
        local_start.astimezone(UTC).isoformat(),
        local_end.astimezone(UTC).isoformat(),
    )


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _period_label(start_day: date, end_day_exclusive: date) -> str:
    end_day = end_day_exclusive - timedelta(days=1)
    if start_day.month == end_day.month:
        return f"{start_day.day}–{end_day.day:02d}.{end_day.month:02d}.{end_day.year}"
    return (
        f"{start_day.day:02d}.{start_day.month:02d}–"
        f"{end_day.day:02d}.{end_day.month:02d}.{end_day.year}"
    )


def _meeting_stats(owner_id: int, start_day: date, end_day_exclusive: date) -> dict[str, int]:
    start_utc, end_utc = _local_range_to_utc(start_day, end_day_exclusive)
    rows = _get(
        "agency_meetings",
        [
            ("owner_telegram_id", f"eq.{int(owner_id)}"),
            ("start_at", f"gte.{start_utc}"),
            ("start_at", f"lt.{end_utc}"),
            ("select", "id,status,start_at"),
            ("limit", "1000"),
        ],
    )

    held = 0
    scheduled = 0
    cancelled = 0
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status == "состоялась":
            held += 1
        elif status in {"отменена", "перенесена"}:
            cancelled += 1
        else:
            scheduled += 1

    return {
        "held": held,
        "scheduled": scheduled,
        "cancelled": cancelled,
        "all": len(rows),
    }


def _task_stats(owner_id: int, start_day: date, end_day_exclusive: date) -> dict[str, int]:
    rows = _get(
        "agency_personal_tasks",
        [
            ("owner_telegram_id", f"eq.{int(owner_id)}"),
            ("task_date", f"gte.{start_day.isoformat()}"),
            ("task_date", f"lt.{end_day_exclusive.isoformat()}"),
            ("status", "neq.deleted"),
            ("select", "id,status,task_date"),
            ("limit", "1000"),
        ],
    )

    completed = sum(1 for row in rows if str(row.get("status") or "") == "completed")
    postponed = sum(1 for row in rows if str(row.get("status") or "") == "postponed")
    planned = sum(1 for row in rows if str(row.get("status") or "") == "planned")
    return {
        "completed": completed,
        "postponed": postponed,
        "planned": planned,
        "all": len(rows),
    }


def _structure_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 1000
    while True:
        batch = _get(
            "agency_members",
            [
                ("select", "telegram_id,member_code,referrer_code,created_at"),
                ("order", "created_at.asc"),
                ("limit", str(page)),
                ("offset", str(offset)),
            ],
        )
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
        if offset > 100000:
            break
    return rows


def _structure_stats(owner_id: int, start_day: date, end_day_exclusive: date) -> dict[str, int]:
    rows = _structure_rows()
    owner = next(
        (
            row
            for row in rows
            if str(row.get("telegram_id") or "") == str(int(owner_id))
        ),
        None,
    )
    if not owner:
        return {"total": 0, "new": 0}

    root_code = str(owner.get("member_code") or "").strip()
    if not root_code:
        return {"total": 0, "new": 0}

    children: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        referrer = str(row.get("referrer_code") or "").strip()
        if referrer:
            children.setdefault(referrer, []).append(row)

    descendants: list[dict[str, Any]] = []
    queue = [root_code]
    seen_codes: set[str] = set()
    seen_ids: set[str] = {str(int(owner_id))}

    while queue:
        parent_code = queue.pop(0)
        if parent_code in seen_codes:
            continue
        seen_codes.add(parent_code)

        for row in children.get(parent_code, []):
            telegram_id = str(row.get("telegram_id") or "").strip()
            if not telegram_id or telegram_id in seen_ids:
                continue
            seen_ids.add(telegram_id)
            descendants.append(row)
            member_code = str(row.get("member_code") or "").strip()
            if member_code:
                queue.append(member_code)

    local_start = datetime.combine(start_day, time.min, tzinfo=BERLIN)
    local_end = datetime.combine(end_day_exclusive, time.min, tzinfo=BERLIN)

    new_count = 0
    for row in descendants:
        created = _parse_datetime(row.get("created_at"))
        if not created:
            continue
        created_local = created.astimezone(BERLIN)
        if local_start <= created_local < local_end:
            new_count += 1

    return {"total": len(descendants), "new": new_count}


def _weekly_goal(owner_id: int, today: date) -> dict[str, Any] | None:
    """Мягко читает актуальную недельную цель Стагирита, если она есть."""
    try:
        rows = _get(
            "agency_stagirite_tasks",
            [
                ("owner_telegram_id", f"eq.{int(owner_id)}"),
                ("select", "id,assignment,status,result,created_at,updated_at"),
                ("order", "created_at.desc"),
                ("limit", "100"),
            ],
        )
    except Exception:
        return None

    for row in rows:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        goal = result.get("weekly_goal") if isinstance(result.get("weekly_goal"), dict) else {}
        if not goal:
            continue
        try:
            start_day = date.fromisoformat(str(goal.get("period_start") or ""))
            end_day = date.fromisoformat(str(goal.get("period_end") or ""))
        except ValueError:
            continue
        if start_day <= today <= end_day:
            progress = (
                result.get("progress_summary")
                if isinstance(result.get("progress_summary"), dict)
                else {}
            )
            return {
                "minimum": int(goal.get("minimum") or 3),
                "desired": int(goal.get("desired") or 5),
                "scheduled": int(progress.get("scheduled", 0) or 0),
                "start": start_day,
                "end": end_day,
            }
    return None


def _render_period(owner_id: int, start_day: date, end_day_exclusive: date, *, show_goal: bool) -> None:
    errors: list[str] = []

    try:
        meetings = _meeting_stats(owner_id, start_day, end_day_exclusive)
    except Exception:
        meetings = {"held": 0, "scheduled": 0, "cancelled": 0, "all": 0}
        errors.append("встречи")

    try:
        tasks = _task_stats(owner_id, start_day, end_day_exclusive)
    except Exception:
        tasks = {"completed": 0, "postponed": 0, "planned": 0, "all": 0}
        errors.append("задачи")

    try:
        structure = _structure_stats(owner_id, start_day, end_day_exclusive)
    except Exception:
        structure = {"total": 0, "new": 0}
        errors.append("структура")

    st.caption(f"Период: {_period_label(start_day, end_day_exclusive)}")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("🤝 Встречи состоялись", meetings["held"])
        st.caption(f"Ещё запланировано: {meetings['scheduled']}")
    with c2:
        st.metric("✅ Главные задачи", tasks["completed"])
        st.caption(f"Всего поставлено: {tasks['all']}")
    with c3:
        st.metric("👥 Новые в структуре", f"+{structure['new']}")
        st.caption(f"Всего в вашей структуре: {structure['total']}")

    if tasks["all"]:
        ratio = min(1.0, tasks["completed"] / max(1, tasks["all"]))
        st.progress(ratio, text=f"Выполнено главных задач: {tasks['completed']} из {tasks['all']}")

    if show_goal:
        goal = _weekly_goal(owner_id, datetime.now(BERLIN).date())
        if goal:
            minimum = goal["minimum"]
            desired = max(minimum, goal["desired"])
            scheduled = goal["scheduled"]
            denominator = max(1, desired)
            progress = min(1.0, scheduled / denominator)
            st.progress(
                progress,
                text=(
                    f"🎯 Недельная цель встреч: {scheduled} из {desired} "
                    f"(минимум {minimum})"
                ),
            )
            if scheduled >= desired:
                st.success("Недельная цель встреч выполнена полностью.")
            elif scheduled >= minimum:
                st.success("Минимум по встречам выполнен. Продолжаем к желаемому максимуму.")

    parts = [
        f"состоялось встреч — {meetings['held']}",
        f"выполнено главных задач — {tasks['completed']}",
        f"новых людей в структуре — {structure['new']}",
    ]
    st.info("Коротко: " + "; ".join(parts) + ".")

    if errors:
        st.caption(
            "Часть данных пока недоступна: " + ", ".join(errors) + ". "
            "Остальные показатели рассчитаны нормально."
        )


def render_agency_results(owner_telegram_id: int | str) -> None:
    owner_id = int(owner_telegram_id)
    today = datetime.now(BERLIN).date()

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)

    month_start = date(today.year, today.month, 1)
    if today.month == 12:
        month_end = date(today.year + 1, 1, 1)
    else:
        month_end = date(today.year, today.month + 1, 1)

    tabs = st.tabs(["Неделя", "Месяц"])
    with tabs[0]:
        _render_period(owner_id, week_start, week_end, show_goal=True)
    with tabs[1]:
        _render_period(owner_id, month_start, month_end, show_goal=False)

    if st.button(
        "🔄 Обновить итоги",
        key=f"agency_results_refresh_{owner_id}_{today.isoformat()}",
        use_container_width=True,
    ):
        st.rerun()
