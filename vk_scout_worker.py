from __future__ import annotations

"""Agency W — background worker for VK Partner Scout.

Работает отдельно от Streamlit UI. Первый холодный контакт не рассылает автоматически;
для уже начатых VK-веток может автоматически продолжить текстовый ответ человека.
Цикл:
1) читает активных партнёров Агентства W;
2) берёт сохранённый живой портрет ЦА из agency_workspace_states;
3) обновляет общий пул кандидатов из настроенных VK-сообществ;
4) просит Неонию оценить кандидатов;
5) резервирует до 5 VK-кандидатов на текущий день;
6) отдельно и чаще проверяет опубликованные комментарии Неоны и подхватывает реальные ответы.

Первый комментарий по-прежнему публикуется владельцем вручную. После регистрации
комментария worker следит за веткой; текстовые ответы Неона продолжает автоматически.
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
    watch_vk_comment_threads,
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
    """Один проход последовательной VK-очереди.

    Неония больше не оценивает участников сообществ по ЦА и не сканирует
    все источники одновременно. Для каждого партнёра берётся текущий источник,
    выдаются следующие 5 новых людей, затем очередь продолжается с сохранённого
    места. Закончилось сообщество — функция сама переходит к следующему.
    """
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
    confirmed_ids.update(
        owner_id
        for owner_id, member in member_by_id.items()
        if not str(member.get("referrer_code") or "").strip()
    )

    root_owner_id = next(
        (
            owner_id
            for owner_id, member in member_by_id.items()
            if not str(member.get("referrer_code") or "").strip()
        ),
        None,
    )
    if root_owner_id is None:
        raise RuntimeError(
            "Не найден корневой владелец Агентства W для VK Scout OAuth"
        )

    try:
        scout_access_token = get_valid_vk_scout_access_token(root_owner_id)
        os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
    except VKScoutOAuthError as exc:
        _log(f"VK Scout OAuth: {exc}")
        scout_access_token = ""

    stats = {
        "members": len(member_ids),
        "active": 0,
        "source_owners": 0,
        "assignments_ready": 0,
        "errors": 0,
    }

    daily_limit = _int_env("VK_SCOUT_DAILY_LIMIT", 5, 1, 5)

    for owner_id in member_ids:
        if owner_id not in confirmed_ids:
            continue
        stats["active"] += 1

        if not scout_access_token:
            continue

        member = member_by_id[owner_id]
        member_code = str(member.get("member_code") or "").strip()

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
            prepared = ensure_daily_vk_assignments(
                owner_id,
                member_code,
                limit=daily_limit,
                min_score=0,
            )
        except VKScoutError as exc:
            text_error = str(exc).lower()
            if (
                "another ip address" in text_error
                or "другому ip" in text_error
            ):
                try:
                    scout_access_token = force_refresh_vk_scout_access_token(
                        root_owner_id
                    )
                    os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
                    prepared = ensure_daily_vk_assignments(
                        owner_id,
                        member_code,
                        limit=daily_limit,
                        min_score=0,
                    )
                except Exception as retry_exc:
                    stats["errors"] += 1
                    _log(f"{owner_id}: VK очередь после обновления токена: {retry_exc}")
                    continue
            else:
                stats["errors"] += 1
                _log(f"{owner_id}: {exc}")
                continue
        except (requests.RequestException, RuntimeError) as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: {exc}")
            continue
        except Exception as exc:
            stats["errors"] += 1
            _log(f"{owner_id}: непредвиденная ошибка: {exc}")
            continue

        if prepared.get("complete"):
            stats["assignments_ready"] += 1

        name = str(member.get("first_name") or owner_id).strip()
        _log(
            f"{name} ({owner_id}): "
            f"{prepared.get('message') or 'VK-очередь обновлена'}"
        )

    _log(
        "цикл завершён | "
        + ", ".join(f"{key}={value}" for key, value in stats.items())
    )
    return stats


def worker_forever(poll_seconds: int | None = None) -> None:
    """Runs heavy VK Scout periodically and checks live comment threads more often."""
    if poll_seconds is None:
        poll_seconds = _int_env(
            "VK_SCOUT_POLL_SECONDS",
            21600,   # полный поиск кандидатов раз в 6 часов
            900,
            86400,
        )

    comment_poll_seconds = _int_env(
        "VK_COMMENT_POLL_SECONDS",
        300,      # живые ответы проверяем каждые 5 минут
        300,
        3600,
    )

    _log(
        f"worker запущен; поиск кандидатов каждые {int(poll_seconds)} сек.; "
        f"VK-диалоги каждые {int(comment_poll_seconds)} сек."
    )
    next_scout_at = 0.0

    while True:
        now_mono = time.monotonic()
        if now_mono >= next_scout_at:
            try:
                run_once()
            except Exception as exc:
                # VK Scout never takes down Neona/the whole Render worker.
                _log(f"цикл поиска кандидатов не выполнен: {exc}")
            next_scout_at = time.monotonic() + max(900, int(poll_seconds))

        try:
            comment_stats = watch_vk_comment_threads(
                ask_openai_fn=_ask_openai,
                auto_send=True,
                limit=100,
            )
            if comment_stats.get("replies") or comment_stats.get("ready") or comment_stats.get("errors"):
                _log(
                    "VK-диалоги | "
                    + ", ".join(f"{key}={value}" for key, value in comment_stats.items())
                )
        except Exception as exc:
            _log(f"проверка VK-диалогов не выполнена: {exc}")

        time.sleep(max(300, int(comment_poll_seconds)))


if __name__ == "__main__":
    worker_forever()
