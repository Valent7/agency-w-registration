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

from vk_quality_daily import ensure_quality_vk_assignments

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
    url = "https://api.openai.com/v1/responses"
    payload = {
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
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    retryable = {429, 500, 502, 503, 504}
    delays = (0, 2, 5, 10)
    last_error: Exception | None = None

    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=120,
            )
            if response.status_code in retryable:
                last_error = RuntimeError(
                    f"OpenAI временно недоступен: HTTP {response.status_code}"
                )
                _log(
                    f"OpenAI временный сбой HTTP {response.status_code}; "
                    f"повтор {attempt}/{len(delays)}"
                )
                continue
            response.raise_for_status()
            answer = _extract_response_text(response.json())
            if not answer:
                raise RuntimeError("OpenAI не вернул текст")
            return answer
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= len(delays):
                break
            _log(
                f"OpenAI запрос временно не выполнен ({type(exc).__name__}); "
                f"повтор {attempt}/{len(delays)}"
            )

    raise RuntimeError(
        "OpenAI временно недоступен. Неония не потеряла очередь кандидатов — "
        "повторите «Добрать кандидатов» через несколько секунд."
    ) from last_error


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _score_until_daily_five(
    owner_id: int,
    member_code: str,
    target_profile: dict[str, Any],
    *,
    daily_limit: int,
    min_score: int,
    analysis_limit: int,
    max_rounds: int = 25,
    priority_ids: list[int] | None = None,
) -> tuple[dict[str, Any], int, int | None]:
    """Keep scoring fresh VK profiles until today's 5/5 is complete or pool is exhausted."""
    total_analyzed = 0
    best_score: int | None = None

    prepared = ensure_quality_vk_assignments(
        owner_id,
        member_code,
        limit=daily_limit,
        min_score=min_score,
    )
    if prepared.get("complete"):
        return prepared, total_analyzed, best_score

    # score_vk_candidates excludes profiles already scored for this owner, so
    # every round advances through the fresh pool instead of repeating the same 30.
    for round_no in range(1, max(1, int(max_rounds)) + 1):
        score_result = score_vk_candidates(
            owner_id,
            target_profile,
            ask_openai_fn=_ask_openai,
            limit=analysis_limit,
            priority_ids=(priority_ids if round_no == 1 else None),
        )
        analyzed_now = int(score_result.get("analyzed") or 0)
        total_analyzed += analyzed_now

        try:
            round_best = int(score_result.get("best_score"))
        except (TypeError, ValueError):
            round_best = None
        if round_best is not None:
            best_score = round_best if best_score is None else max(best_score, round_best)

        prepared = ensure_quality_vk_assignments(
            owner_id,
            member_code,
            limit=daily_limit,
            min_score=min_score,
        )
        _log(
            f"{owner_id}: VK отбор раунд={round_no}; "
            f"оценено_в_раунде={analyzed_now}; всего_оценено={total_analyzed}; "
            f"готово={prepared.get('count') or 0}/{daily_limit}; "
            f"лучший_балл={round_best}; "
            f"диагностика={prepared.get('diagnostics') or {}}"
        )
        if prepared.get("complete"):
            break

        # No fresh rows left: score_vk_candidates switched to reanalysis.
        # Re-running the same pool would only spend money without finding new people.
        if analyzed_now <= 0 or str(score_result.get("mode") or "") == "повторный анализ":
            break

    return prepared, total_analyzed, best_score


def top_up_vk_owner_now(
    owner_id: int,
    member_code: str,
    *,
    daily_limit: int = 5,
    min_score: int = 50,
    analysis_limit: int = 30,
) -> dict[str, Any]:
    """Immediate manual top-up for one owner's VK daily five."""
    owner_id = int(owner_id)
    member_code = str(member_code or "").strip()

    profiles = _load_target_profiles([owner_id])
    target_profile = profiles.get(owner_id)
    if not target_profile:
        raise VKScoutError("Не найден сохранённый живой портрет ЦА.")

    sources = load_vk_sources(owner_id)
    if not sources:
        raise VKScoutError("Нет активных VK-источников.")

    service_access_token = _env("VK_SERVICE_TOKEN")
    legacy_access_token = _env("VK_ACCESS_TOKEN")
    fallback_access_token = service_access_token or legacy_access_token

    try:
        scout_access_token = get_valid_vk_scout_access_token(owner_id)
        os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
    except VKScoutOAuthError:
        if not fallback_access_token:
            raise VKScoutError("Нет рабочего VK-токена для добора.")
        os.environ["VK_SCOUT_ACCESS_TOKEN"] = fallback_access_token

    # Сначала используем уже сохранённый большой пул. Кнопка «Добрать»
    # не должна ждать VK API, если в базе уже есть тысячи профилей.
    prepared, analyzed_total, best_score = _score_until_daily_five(
        owner_id,
        member_code,
        target_profile,
        daily_limit=max(1, min(5, int(daily_limit))),
        min_score=max(0, min(100, int(min_score))),
        analysis_limit=max(5, min(100, int(analysis_limit))),
        max_rounds=2,
    )
    scan_result = {"candidates_found": 0}
    if not prepared.get("complete"):
        try:
            scan_result = scan_vk_sources(
                owner_id,
                per_source=200,
                max_sources=1,
                include_active_context=True,
            )
        except Exception as exc:
            _log(f"{owner_id}: обновление VK отложено; работаю из сохранённого пула: {exc}")
            scan_result = {"candidates_found": 0, "errors": [str(exc)]}
        if int(scan_result.get("candidates_found") or 0) > 0:
            prepared2, analyzed2, best2 = _score_until_daily_five(
                owner_id,
                member_code,
                target_profile,
                daily_limit=max(1, min(5, int(daily_limit))),
                min_score=max(0, min(100, int(min_score))),
                analysis_limit=max(5, min(100, int(analysis_limit))),
                max_rounds=3,
                priority_ids=[
                    int(x) for x in (scan_result.get("active_context_ids") or [])
                    if str(x).isdigit()
                ],
            )
            prepared = prepared2
            analyzed_total += analyzed2
            if best2 is not None:
                best_score = best2 if best_score is None else max(best_score, best2)
    return {
        "ok": True,
        "count": int(prepared.get("count") or len(prepared.get("assignments") or [])),
        "complete": bool(prepared.get("complete")),
        "created": int(prepared.get("created") or 0),
        "analyzed": int(analyzed_total),
        "best_score": best_score,
        "message": str(prepared.get("message") or "").strip(),
        "scan_found": int(scan_result.get("candidates_found") or 0),
    }


def run_once() -> dict[str, int]:
    """Один проход качественного VK-поиска.

    Источники владельца обновляются, Неония оценивает публичные профили по
    сохранённому портрету ЦА, и только прошедшие порог попадают в дневную пятёрку.
    Первый контакт по-прежнему не отправляется автоматически.
    """
    members = _load_members()
    member_ids: list[int] = []
    member_by_id: dict[int, dict[str, Any]] = {}

    from agency_access_control import is_agency_access_revoked
    for member in members:
        try:
            owner_id = int(member.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        if is_agency_access_revoked(owner_id):
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

    service_access_token = _env("VK_SERVICE_TOKEN")
    legacy_access_token = _env("VK_ACCESS_TOKEN")
    fallback_access_token = service_access_token or legacy_access_token
    service_mode = _env("VK_SCOUT_SERVICE_MODE").casefold() in {"1", "true", "yes", "on"}

    if service_mode and service_access_token:
        # Agency W deliberately abandoned the blocked legacy VK ID application.
        # In service mode never touch its stored refresh token again.
        scout_access_token = service_access_token
        os.environ["VK_SCOUT_ACCESS_TOKEN"] = service_access_token
        _log("VK Scout работает только через VK_SERVICE_TOKEN; legacy VK ID отключён")
    else:
        try:
            scout_access_token = get_valid_vk_scout_access_token(root_owner_id)
            os.environ["VK_SCOUT_ACCESS_TOKEN"] = scout_access_token
        except VKScoutOAuthError as exc:
            if fallback_access_token:
                scout_access_token = fallback_access_token
                os.environ["VK_SCOUT_ACCESS_TOKEN"] = fallback_access_token
                if service_access_token:
                    _log(
                        "VK Scout OAuth недоступен; использую VK_SERVICE_TOKEN приложения; "
                        f"причина: {exc}"
                    )
                else:
                    _log(
                        "VK Scout OAuth недоступен; использую резервный VK_ACCESS_TOKEN "
                        "из прежней Streamlit-схемы"
                    )
            else:
                _log(f"VK Scout OAuth: {exc}")
                scout_access_token = ""

    target_profiles = _load_target_profiles(member_ids)

    stats = {
        "members": len(member_ids),
        "active": 0,
        "source_owners": 0,
        "assignments_ready": 0,
        "analyzed": 0,
        "errors": 0,
    }

    daily_limit = _int_env("VK_SCOUT_DAILY_LIMIT", 5, 1, 5)
    analysis_limit = _int_env("VK_SCOUT_ANALYSIS_LIMIT", 30, 5, 100)
    min_score = _int_env("VK_SCOUT_MIN_SCORE", 50, 0, 100)

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

        target_profile = target_profiles.get(owner_id)
        if not target_profile:
            _log(f"{owner_id}: нет сохранённого живого портрета ЦА — VK-пятёрку не выдаю")
            continue

        try:
            # Выдача дневной пятёрки не зависит от доступности VK API:
            # сначала работаем с уже сохранённым пулом, и только при нехватке
            # обновляем источники. Это убирает 45-секундные таймауты из критического пути.
            prepared, analyzed_total, best_score = _score_until_daily_five(
                owner_id,
                member_code,
                target_profile,
                daily_limit=daily_limit,
                min_score=min_score,
                analysis_limit=analysis_limit,
                max_rounds=2,
            )
            stats["analyzed"] += analyzed_total
            scan_result = {"candidates_found": 0, "errors": []}

            if not prepared.get("complete"):
                try:
                    scan_result = scan_vk_sources(
                        owner_id,
                        per_source=200,
                        max_sources=1,
                        include_active_context=True,
                    )
                    for scan_error in scan_result.get("errors") or []:
                        _log(f"{owner_id}: VK источник: {scan_error}")
                except Exception as scan_exc:
                    _log(
                        f"{owner_id}: VK API не ответил; продолжаю из сохранённого пула: "
                        f"{scan_exc}"
                    )
                    scan_result = {"candidates_found": 0, "errors": [str(scan_exc)]}

                if int(scan_result.get("candidates_found") or 0) > 0:
                    prepared2, analyzed2, best2 = _score_until_daily_five(
                        owner_id,
                        member_code,
                        target_profile,
                        daily_limit=daily_limit,
                        min_score=min_score,
                        analysis_limit=analysis_limit,
                        max_rounds=17,
                        priority_ids=[
                            int(x) for x in (scan_result.get("active_context_ids") or [])
                            if str(x).isdigit()
                        ],
                    )
                    prepared = prepared2
                    analyzed_total += analyzed2
                    stats["analyzed"] += analyzed2
                    if best2 is not None:
                        best_score = best2 if best_score is None else max(best_score, best2)

            _log(
                f"{owner_id}: Неония проверила VK-пул; "
                f"найдено_свежих={scan_result.get('candidates_found') or 0}, "
                f"активных_с_контекстом={scan_result.get('active_context_count') or 0}, "
                f"оценено={analyzed_total}, "
                f"лучший_балл={best_score}"
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
                    scan_result = scan_vk_sources(
                        owner_id,
                        per_source=200,
                        max_sources=1,
                        include_active_context=True,
                    )
                    prepared, analyzed_total, _ = _score_until_daily_five(
                        owner_id,
                        member_code,
                        target_profile,
                        daily_limit=daily_limit,
                        min_score=min_score,
                        analysis_limit=analysis_limit,
                    )
                    stats["analyzed"] += analyzed_total
                except Exception as retry_exc:
                    if fallback_access_token:
                        try:
                            scout_access_token = fallback_access_token
                            os.environ["VK_SCOUT_ACCESS_TOKEN"] = fallback_access_token
                            scan_result = scan_vk_sources(
                                owner_id,
                                per_source=200,
                                max_sources=min(10, len(sources)),
                            )
                            prepared, analyzed_total, _ = _score_until_daily_five(
                                owner_id,
                                member_code,
                                target_profile,
                                daily_limit=daily_limit,
                                min_score=min_score,
                                analysis_limit=analysis_limit,
                            )
                            stats["analyzed"] += analyzed_total
                            _log(
                                f"{owner_id}: VK Scout восстановлен через резервный "
                                + ("VK_SERVICE_TOKEN" if service_access_token else "VK_ACCESS_TOKEN")
                            )
                        except Exception as legacy_exc:
                            stats["errors"] += 1
                            _log(
                                f"{owner_id}: резервный VK_ACCESS_TOKEN тоже не сработал: "
                                f"{legacy_exc}"
                            )
                            continue
                    else:
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
