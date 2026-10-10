"""Серверный контроллер бюджета пилота Неоны. Не включён в production.

Правило: если нет подтверждённой атомарной резервации в Supabase,
дорогой вызов модели НЕ выполняется. Один budget row на ВСЕХ участников.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import requests


class PilotBudgetBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    request_id: str
    reserved_micro_usd: int


def _server() -> tuple[str, str]:
    url = str(os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise PilotBudgetBlocked("Нет серверного подключения к лимиту пилота")
    return url, key


def _rpc(name: str, body: dict) -> bool:
    url, key = _server()
    try:
        response = requests.post(
            f"{url}/rest/v1/rpc/{name}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body, timeout=12,
        )
        response.raise_for_status()
        return response.json() is True
    except (ValueError, requests.RequestException) as exc:
        raise PilotBudgetBlocked("Не удалось подтвердить бюджетную операцию") from exc


def reserve(owner_id: int, *, worst_case_usd: str, pilot_key: str = "neona_v4") -> Reservation:
    """Резервирует максимум расходов до API вызова; НЕ использует оценку после вызова."""
    value = Decimal(str(worst_case_usd))
    if not value.is_finite() or value <= 0:
        raise PilotBudgetBlocked("Некорректная максимальная стоимость запроса")
    amount = int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    request_id = str(uuid.uuid4())
    if not _rpc("neona_pilot_reserve", {
        "p_pilot_key": pilot_key, "p_request_id": request_id,
        "p_owner_id": int(owner_id), "p_amount": amount,
    }):
        raise PilotBudgetBlocked("Лимит пилота исчерпан или пилот отключён")
    return Reservation(request_id, amount)


def settle(reservation: Reservation, *, actual_usd: str) -> None:
    value = Decimal(str(actual_usd))
    if not value.is_finite() or value < 0:
        raise PilotBudgetBlocked("Недостоверная стоимость вызова")
    actual = int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if not _rpc("neona_pilot_settle", {
        "p_request_id": reservation.request_id, "p_actual_micro_usd": actual,
    }):
        # Не освобождать резервацию: при недостоверном usage счётчик
        # обязан завышать расход, а не открывать дополнительный бюджет.
        raise PilotBudgetBlocked("Нельзя подтвердить фактический расход")


def release_if_not_sent(reservation: Reservation) -> None:
    """Только если API вызов гарантированно НЕ был отправлен."""
    if not _rpc("neona_pilot_release", {"p_request_id": reservation.request_id}):
        raise PilotBudgetBlocked("Резервация остаётся занятой")
