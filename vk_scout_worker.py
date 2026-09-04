from __future__ import annotations

"""Agency W — background worker for VK Partner Scout.

Работает отдельно от Streamlit UI и ничего не рассылает автоматически.
Цикл:
1) читает активных партнёров Агентства W;
2) берёт сохранённый живой портрет ЦА из agency_workspace_states;
3) обновляет общий пул кандидатов из настроенных VK-сообществ;
4) просит Неонию оценить кандидатов;
5) резервирует до 5 VK-кандидатов на текущий день.

Первое сообщение по-прежнему только готовится в интерфейсе и отправляется
самим партнёром после просмотра.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken

from vk_scout_oauth import (
    VKScoutOAuthError,
    force_refresh_vk_scout_access_token,
    get_valid_vk_scout_access_token,
)

from vk_scout import (
    VKScoutError,
    ensure_daily_vk_assignments,
    load_today_vk_assignments,
    load_vk_sources,
    release_expired_vk_assignments,
    scan_vk_sources,
    score_vk_candidates,
)

UTC = timezone.utc


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Не найдена переменная окружения {name}")
    return value


def _supabase_config() -> tuple[str, str]:
    url = _required_env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SECRET_KEY") or _env("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise RuntimeError(
            "Не найдена SUPABASE_SECRET_KEY или SUPABASE_SERVICE_ROLE_KEY"
        )
    return url, key


def _sb_headers() -> dict[str, str]:
    _, key = _supabase_config()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _sb_get(table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url, _ = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/{table}",
        headers=_sb_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[VK Scout] {stamp} | {message}", flush=True)


def _load_members() -> list[dict[str, Any]]:
    return _sb_get(
        "agency_members",
        {
            "select": "telegram_id,first_name,username,member_code,referrer_code",
            "order": "telegram_id.asc",
            "limit": 10000,
        },
    )


def _load_confirmed_ids(member_ids: list[int]) -> set[int]:
    """Возвращает партнёров с подтверждённым доступом.

    Корневой/старый владелец обычно имеет legacy_active. Если таблица активаций
    временно недоступна, worker не блокирует всех: вернёт исходный список.
    """
    if not member_ids:
        return set()

    confirmed: set[int] = set()
    try:
        for start in range(0, len(member_ids), 200):
            chunk = member_ids[start:start + 200]
            rows = _sb_get(
                "partner_activations",
                {
                    "telegram_id": "in.(" + ",".join(str(x) for x in chunk) + ")",
                    "status": "in.(confirmed,legacy_active)",
                    "select": "telegram_id,status",
                    "limit": 1000,
                },
            )
            for row in rows:
                try:
                    confirmed.add(int(row.get("telegram_id")))
                except (TypeError, ValueError):
                    pass
    except requests.HTTPError as exc:
        _log(
            "Не удалось прочитать partner_activations; "
            f"временно не фильтрую по активации: {exc}"
        )
        return set(member_ids)

    return confirmed


def _workspace_cipher() -> Fernet:
    key = _required_env("FERNET_KEY")
    return Fernet(key.encode("utf-8"))


def _decrypt_workspace_state(encrypted_state: str) -> dict[str, Any]:
    if not encrypted_state:
        return {}
    try:
        raw = _workspace_cipher().decrypt(encrypted_state.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}


def _load_target_profiles(member_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Читает сохранённые живые портреты ЦА из зашифрованного workspace."""
    result: dict[int, dict[str, Any]] = {}
    if not member_ids:
        return result

    for start in range(0, len(member_ids), 100):
        chunk = member_ids[start:start + 100]
        rows = _sb_get(
            "agency_workspace_states",
            {
                "telegram_id": "in.(" + ",".join(str(x) for x in chunk) + ")",
                "select": "telegram_id,encrypted_state",
                "limit": 1000,
            },
        )
        for row in rows:
            try:
                owner_id = int(row.get("telegram_id"))
            except (TypeError, ValueError):
                continue

            state = _decrypt_workspace_state(
                str(row.get("encrypted_state") or "")
            )
            passport = state.get("passport")
            if not isinstance(passport, dict):
                continue

            profile = passport.get("profile")
            if not isinstance(profile, dict):
                continue

            # Повторяем правило текущего интерфейса: старый смешанный вариант ЦА
            # не используем, нужен именно новый «живой портрет».
            is_live_profile = bool(
                profile.get("portrait")
                and (profile.get("who_is_this") or profile.get("current_situation"))
            )
            if not is_live_profile:
                continue

            analysis = passport.get("analysis")
            result[owner_id] = (
                analysis if isinstance(analysis, dict) and analysis else profile
            )

    return result


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
    model = _env("VK_SCOUT_OPENAI_MODEL", "gpt-5-mini") or "gpt-5-mini"

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": str(system_prompt or ""),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": str(user_message or ""),
                        }
                    ],
                }
            ],
            "store": False,
        },
        timeout=180,
    )
    response.raise_for_status()
    answer = _extract_response_text(response.json())
    if not answer:
        raise RuntimeError("OpenAI не вернул текст")
    return answer


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def run_once() -> dict[str, int]:
    """Один безопасный проход VK Scout."""
    members = _load_members()
    member_ids: list[int] = []
    member_by_id: dict[int, dict[str, Any]] = {}

    for member in members:
        try:
            owner_id = int(member.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        member_code = str(member.get("member_code") or "").strip()
        if not member_code:
            continue
        member_ids.append(owner_id)
        member_by_id[owner_id] = member

    confirmed_ids = _load_confirmed_ids(member_ids)
    # Корневой владелец (без referrer_code) считается активным даже если
    # историческая запись partner_activations ещё не была создана.
    confirmed_ids.update(
        owner_id
        for owner_id, member in member_by_id.items()
        if not str(member.get("referrer_code") or "").strip()
    )
    target_profiles = _load_target_profiles(member_ids)

    # One VK user authorization is enough for the shared background scanner.
    # Use the root Agency W owner (member without referrer_code).
    root_owner_id = next((
        owner_id for owner_id, member in member_by_id.items()
        if not str(member.get("referrer_code") or "").strip()
    ), None)
    if root_owner_id is None:
        raise RuntimeError("Не найден корневой владелец Агентства W для VK Scout OAuth")

    try:
        scout_access_token = get_valid_vk_scout_access_token(root_owner_id)
        # vk_scout._config() prefers VK_SCOUT_ACCESS_TOKEN. This process-local
        # value is refreshed from encrypted Supabase storage when needed.
        os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
    except VKScoutOAuthError as exc:
        _log(f"VK Scout OAuth: {exc}")
        scout_access_token = ""

    stats = {
        "members": len(member_ids),
        "active": 0,
        "profiles": len(target_profiles),
        "source_owners": 0,
        "sources_scanned": 0,
        "candidates_saved": 0,
        "owners_scored": 0,
        "assignments_ready": 0,
        "errors": 0,
    }

    # Сначала обновляем общий пул VK-кандидатов. Источники могут быть настроены
    # у одного или нескольких владельцев; найденные публичные профили сохраняются
    # в общий пул, а оценка ЦА затем делается отдельно для каждого партнёра.
    per_source = _int_env("VK_SCOUT_PER_SOURCE", 200, 1, 1000)
    max_sources = _int_env("VK_SCOUT_MAX_SOURCES", 10, 1, 50)

    for owner_id in member_ids:
        if owner_id not in confirmed_ids:
            continue
        if not scout_access_token:
            continue
        try:
            sources = load_vk_sources(owner_id)
        except Exception as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: не удалось прочитать источники VK: {exc}")
            continue
        if not sources:
            continue

        stats["source_owners"] += 1
        try:
            scan = scan_vk_sources(
                owner_id,
                per_source=per_source,
                max_sources=max_sources,
            )

            # VK ID can bind the freshly issued user access token to the IP
            # that completed OAuth (Streamlit). If the scanner runs on Render,
            # VK may answer with error 5: "access_token was given to another
            # ip address". In that case refresh the rotating pair FROM Render
            # and retry the source scan once.
            scan_errors = [str(item or "") for item in (scan.get("errors") or [])]
            ip_bound_error = any(
                "another ip address" in item.lower()
                or "другому ip" in item.lower()
                for item in scan_errors
            )
            if ip_bound_error:
                _log(
                    f"{owner_id}: VK привязал access token к другому IP; "
                    "обновляю токен из Render и повторяю сканирование один раз"
                )
                scout_access_token = force_refresh_vk_scout_access_token(root_owner_id)
                os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
                scan = scan_vk_sources(
                    owner_id,
                    per_source=per_source,
                    max_sources=max_sources,
                )

            stats["sources_scanned"] += int(scan.get("sources_checked") or 0)
            stats["candidates_saved"] += int(scan.get("candidates_saved") or 0)
            for error in scan.get("errors") or []:
                _log(f"{owner_id}: источник VK: {error}")
        except Exception as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: ошибка сканирования VK: {exc}")

    try:
        release_expired_vk_assignments()
    except Exception as exc:
        stats["errors"] += 1
        _log(f"Не удалось освободить просроченные резервы: {exc}")

    analyze_limit = _int_env("VK_SCOUT_ANALYZE_LIMIT", 30, 1, 100)
    daily_limit = _int_env("VK_SCOUT_DAILY_LIMIT", 5, 1, 5)
    min_score = _int_env("VK_SCOUT_MIN_SCORE", 60, 0, 100)

    for owner_id in member_ids:
        if owner_id not in confirmed_ids:
            continue
        stats["active"] += 1

        profile = target_profiles.get(owner_id)
        if not profile:
            continue

        member = member_by_id[owner_id]
        member_code = str(member.get("member_code") or "").strip()

        try:
            existing = load_today_vk_assignments(owner_id)
            if len(existing) >= daily_limit:
                stats["assignments_ready"] += 1
                continue

            scored = score_vk_candidates(
                owner_id,
                profile,
                ask_openai_fn=_ask_openai,
                limit=analyze_limit,
            )
            if scored.get("ok"):
                stats["owners_scored"] += 1

            prepared = ensure_daily_vk_assignments(
                owner_id,
                member_code,
                limit=daily_limit,
                min_score=min_score,
            )
            if prepared.get("complete"):
                stats["assignments_ready"] += 1

            name = str(member.get("first_name") or owner_id).strip()
            _log(
                f"{name} ({owner_id}): {prepared.get('message') or 'VK-пятёрка обновлена'}"
            )

        except (VKScoutError, requests.RequestException, RuntimeError) as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: {exc}")
        except Exception as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: непредвиденная ошибка: {exc}")

    _log(
        "цикл завершён | "
        + ", ".join(f"{key}={value}" for key, value in stats.items())
    )
    return stats


def worker_forever(poll_seconds: int | None = None) -> None:
    """Запускает VK Scout сразу, затем повторяет цикл с заданным интервалом."""
    if poll_seconds is None:
        poll_seconds = _int_env(
            "VK_SCOUT_POLL_SECONDS",
            21600,   # 6 часов
            300,     # не чаще 5 минут
            86400,   # не реже 1 раза в сутки
        )

    _log(f"worker запущен; интервал {int(poll_seconds)} сек.")

    while True:
        try:
            run_once()
        except Exception as exc:
            # VK Scout никогда не должен уронить Неону/весь Render worker.
            _log(f"цикл не выполнен: {exc}")

        time.sleep(max(300, int(poll_seconds)))


if __name__ == "__main__":
    worker_forever()
