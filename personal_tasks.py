from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import requests
import streamlit as st

BERLIN = ZoneInfo("Europe/Berlin")

def _config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise RuntimeError("Не найдены SUPABASE_URL или SUPABASE_SECRET_KEY.")
    return url, key

def _headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers

def _get(table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url, _ = _config()
    r = requests.get(f"{url}/rest/v1/{table}", headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []

def _post(table: str, payload: dict[str, Any]) -> None:
    url, _ = _config()
    r = requests.post(
        f"{url}/rest/v1/{table}",
        headers=_headers("return=minimal"),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()

def _patch(table: str, filters: dict[str, str], payload: dict[str, Any]) -> None:
    url, _ = _config()
    r = requests.patch(
        f"{url}/rest/v1/{table}",
        headers=_headers("return=minimal"),
        params=filters,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()

def _today_iso() -> str:
    return datetime.now(BERLIN).date().isoformat()

def _tomorrow_iso() -> str:
    return (datetime.now(BERLIN).date() + timedelta(days=1)).isoformat()

def _load_day_tasks(owner_telegram_id: int, task_date: str) -> list[dict[str, Any]]:
    return _get(
        "agency_personal_tasks",
        {
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "task_date": f"eq.{task_date}",
            "status": "neq.deleted",
            "select": "*",
            "order": "position.asc,created_at.asc",
            "limit": 10,
        },
    )

def _next_free_position(tasks: list[dict[str, Any]]) -> int:
    used = {int(item.get("position")) for item in tasks if item.get("position") is not None}
    for position in (1, 2, 3):
        if position not in used:
            return position
    return 3

def render_personal_tasks(owner_telegram_id: int | str) -> None:
    owner_telegram_id = int(owner_telegram_id)
    today = _today_iso()

    st.markdown("**✅ МОИ 3 ГЛАВНЫЕ ЗАДАЧИ**")
    tasks = _load_day_tasks(owner_telegram_id, today)
    active_for_day = [item for item in tasks if item.get("status") != "deleted"]

    if not active_for_day:
        st.caption("Выберите до трёх действительно важных дел на сегодня. Задачи формулируете только вы.")

    for item in active_for_day:
        task_id = item.get("id")
        text = str(item.get("task_text") or "").strip()
        position = int(item.get("position") or 1)
        status = str(item.get("status") or "planned")

        with st.container(border=True):
            if status == "completed":
                st.markdown(f"**{position}. ✅ {text}**")
                st.caption("Статус: выполнено")
            elif status == "postponed":
                st.markdown(f"**{position}. ➡️ {text}**")
                st.caption(f"Статус: перенесено на {item.get('postponed_to') or 'завтра'}")
            else:
                st.markdown(f"**{position}. {text}**")
                st.caption("Статус: в работе")

            if status == "planned":
                c1, c2, c3 = st.columns(3)

                with c1:
                    if st.button("✅ Выполнено", key=f"task_complete_{task_id}", use_container_width=True):
                        now = datetime.now(BERLIN).isoformat()
                        _patch(
                            "agency_personal_tasks",
                            {"id": f"eq.{task_id}"},
                            {"status": "completed", "completed_at": now, "updated_at": now},
                        )
                        st.rerun()

                with c2:
                    if st.button("➡️ Перенести", key=f"task_postpone_{task_id}", use_container_width=True):
                        tomorrow = _tomorrow_iso()
                        _patch(
                            "agency_personal_tasks",
                            {"id": f"eq.{task_id}"},
                            {"status": "postponed", "postponed_to": tomorrow, "updated_at": datetime.now(BERLIN).isoformat()},
                        )
                        tomorrow_tasks = _load_day_tasks(owner_telegram_id, tomorrow)
                        _post(
                            "agency_personal_tasks",
                            {
                                "owner_telegram_id": owner_telegram_id,
                                "task_date": tomorrow,
                                "position": _next_free_position(tomorrow_tasks),
                                "task_text": text,
                                "status": "planned",
                                "source_task_id": task_id,
                            },
                        )
                        st.rerun()

                with c3:
                    with st.popover("✏️ Редактировать", use_container_width=True):
                        edit_text = st.text_area(
                            "Текст задачи",
                            value=text,
                            key=f"task_edit_text_{task_id}",
                            height=100,
                        )
                        if st.button("💾 Сохранить", key=f"task_edit_save_{task_id}", use_container_width=True):
                            clean = edit_text.strip()
                            if not clean:
                                st.warning("Задача не может быть пустой.")
                            else:
                                _patch(
                                    "agency_personal_tasks",
                                    {"id": f"eq.{task_id}"},
                                    {"task_text": clean, "updated_at": datetime.now(BERLIN).isoformat()},
                                )
                                st.rerun()

                        if st.button("🗑 Удалить", key=f"task_delete_{task_id}", use_container_width=True):
                            _patch(
                                "agency_personal_tasks",
                                {"id": f"eq.{task_id}"},
                                {"status": "deleted", "updated_at": datetime.now(BERLIN).isoformat()},
                            )
                            st.rerun()

    if len(active_for_day) < 3:
        st.divider()
        with st.form(f"personal_task_add_{owner_telegram_id}_{today}"):
            new_task = st.text_input(
                f"Задача №{len(active_for_day) + 1}",
                placeholder="Напишите главное дело на сегодня",
            )
            submitted = st.form_submit_button("➕ Добавить задачу", use_container_width=True)

        if submitted:
            clean = new_task.strip()
            if not clean:
                st.warning("Напишите задачу.")
            else:
                _post(
                    "agency_personal_tasks",
                    {
                        "owner_telegram_id": owner_telegram_id,
                        "task_date": today,
                        "position": _next_free_position(active_for_day),
                        "task_text": clean,
                        "status": "planned",
                    },
                )
                st.rerun()
    else:
        st.caption("На сегодня выбраны все 3 главные задачи.")
