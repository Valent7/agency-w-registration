"""Partner wallet and provider-cost settlement for Agency W Content Factory.

Money is kept as integer euro cents. Provider usage is reserved before paid
generation and settled only after a successful result. Stripe redirects never
credit the wallet; only a verified server webhook may do that.
"""
from __future__ import annotations

import copy
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import hashlib
import hmac
import json
import os
import time
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit, urlencode

import requests

from content_factory_store import StoreError


class BillingError(RuntimeError):
    pass


MAX_ENTRIES = 200
MAX_RESERVATIONS = 100
MIN_TOPUP_CENTS = 500
MAX_TOPUP_CENTS = 50_000


def _cents(value) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise BillingError("Некорректная сумма.") from None
    if isinstance(value, bool) or result < 0 or result > 100_000_000:
        raise BillingError("Некорректная сумма.")
    return result


def _wallet(project: dict | None) -> dict:
    raw = (project or {}).get("billing_wallet")
    if not isinstance(raw, dict):
        raw = {}
    reservations = raw.get("reservations")
    if not isinstance(reservations, dict):
        reservations = {}
    entries = raw.get("entries")
    if not isinstance(entries, list):
        entries = []
    balance = _cents(raw.get("balance_cents", 0))
    reserved = 0
    clean_reservations = {}
    for key, value in list(reservations.items())[-MAX_RESERVATIONS:]:
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            continue
        try:
            amount = _cents(value.get("amount_cents", 0))
        except BillingError:
            continue
        status = str(value.get("status") or "")
        if status == "reserved":
            reserved += amount
        clean_reservations[key] = {
            "amount_cents": amount,
            "status": status if status in {"reserved", "settled", "released"} else "released",
            "kind": str(value.get("kind") or "")[:40],
            "label": str(value.get("label") or "")[:200],
            "created_at": str(value.get("created_at") or "")[:80],
            "settled_cents": _cents(value.get("settled_cents", 0)),
        }
    clean_entries = []
    for value in entries[-MAX_ENTRIES:]:
        if not isinstance(value, dict):
            continue
        clean_entries.append({
            "id": str(value.get("id") or "")[:240],
            "type": str(value.get("type") or "")[:40],
            "amount_cents": int(value.get("amount_cents") or 0),
            "kind": str(value.get("kind") or "")[:40],
            "label": str(value.get("label") or "")[:200],
            "created_at": str(value.get("created_at") or "")[:80],
        })
    return {
        "version": 1,
        "currency": "EUR",
        "balance_cents": balance,
        "reserved_cents": reserved,
        "reservations": clean_reservations,
        "entries": clean_entries,
    }


def billing_enforced() -> bool:
    return bool(
        str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
        and str(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    )


def wallet_summary(project: dict | None) -> dict:
    wallet = _wallet(project)
    return {
        **wallet,
        "available_cents": max(0, wallet["balance_cents"] - wallet["reserved_cents"]),
    }


def format_eur(cents: int) -> str:
    cents = _cents(cents)
    return f"€{cents / 100:.2f}".replace(".", ",")


def _entry(wallet: dict, *, entry_id: str, entry_type: str, amount_cents: int, kind: str, label: str, now: str):
    wallet["entries"].append({
        "id": str(entry_id)[:240],
        "type": str(entry_type)[:40],
        "amount_cents": int(amount_cents),
        "kind": str(kind)[:40],
        "label": str(label)[:200],
        "created_at": str(now)[:80],
    })
    wallet["entries"] = wallet["entries"][-MAX_ENTRIES:]


def _mutate(store, owner_id: int, project_id: str, callback):
    last_error = None
    for _ in range(5):
        project = store.get_project(owner_id, project_id)
        if not isinstance(project, dict):
            raise BillingError("Проект для оплаты не найден.")
        wallet = _wallet(project)
        result = callback(wallet, project)
        try:
            updated = store.update_project_if_current(
                owner_id,
                project_id,
                {"billing_wallet": wallet},
                project["updated_at"],
            )
            return result, updated
        except StoreError as exc:
            last_error = exc
            if "уже изменились" not in str(exc):
                raise BillingError(str(exc)) from None
    raise BillingError("Баланс одновременно изменился в другом процессе. Повторите действие.") from last_error


def credit_topup(store, owner_id: int, project_id: str, amount_cents: int, *, payment_id: str, now: str):
    amount_cents = _cents(amount_cents)
    payment_id = str(payment_id or "").strip()
    if not payment_id or amount_cents < MIN_TOPUP_CENTS:
        raise BillingError("Некорректное пополнение.")

    def apply(wallet, _project):
        entry_id = "topup:" + payment_id
        if any(item.get("id") == entry_id for item in wallet["entries"]):
            return wallet_summary({"billing_wallet": wallet})
        wallet["balance_cents"] += amount_cents
        _entry(
            wallet,
            entry_id=entry_id,
            entry_type="topup",
            amount_cents=amount_cents,
            kind="wallet",
            label="Пополнение баланса",
            now=now,
        )
        return wallet_summary({"billing_wallet": wallet})

    return _mutate(store, owner_id, project_id, apply)[0]


def reserve_usage(store, owner_id: int, project_id: str, amount_cents: int, *, reference: str, kind: str, label: str, now: str):
    amount_cents = _cents(amount_cents)
    reference = str(reference or "").strip()
    if not reference or amount_cents <= 0:
        raise BillingError("Не удалось рассчитать стоимость операции.")

    def apply(wallet, _project):
        existing = wallet["reservations"].get(reference)
        if existing and existing.get("status") in {"reserved", "settled"}:
            if int(existing.get("amount_cents") or 0) != amount_cents:
                raise BillingError("Стоимость уже зарезервированной операции изменилась.")
            return copy.deepcopy(existing)
        available = wallet["balance_cents"] - wallet["reserved_cents"]
        if available < amount_cents:
            raise BillingError(
                f"Недостаточно средств. Нужно {format_eur(amount_cents)}, "
                f"доступно {format_eur(max(0, available))}. Пополните баланс."
            )
        wallet["reservations"][reference] = {
            "amount_cents": amount_cents,
            "status": "reserved",
            "kind": str(kind)[:40],
            "label": str(label)[:200],
            "created_at": str(now)[:80],
            "settled_cents": 0,
        }
        wallet["reserved_cents"] += amount_cents
        _entry(
            wallet,
            entry_id="reserve:" + reference,
            entry_type="reserve",
            amount_cents=0,
            kind=kind,
            label=label,
            now=now,
        )
        return copy.deepcopy(wallet["reservations"][reference])

    return _mutate(store, owner_id, project_id, apply)[0]


def settle_usage(store, owner_id: int, project_id: str, *, reference: str, actual_cents: int | None = None, now: str):
    reference = str(reference or "").strip()

    def apply(wallet, _project):
        reservation = wallet["reservations"].get(reference)
        if not reservation:
            raise BillingError("Резерв оплаты этой операции не найден.")
        if reservation.get("status") == "settled":
            return copy.deepcopy(reservation)
        if reservation.get("status") != "reserved":
            raise BillingError("Резерв оплаты уже закрыт.")
        reserved = _cents(reservation.get("amount_cents", 0))
        actual = reserved if actual_cents is None else _cents(actual_cents)
        if actual > reserved:
            raise BillingError("Фактическая стоимость выше зарезервированной. Автоматическое списание остановлено.")
        wallet["balance_cents"] -= actual
        wallet["reserved_cents"] = max(0, wallet["reserved_cents"] - reserved)
        reservation["status"] = "settled"
        reservation["settled_cents"] = actual
        _entry(
            wallet,
            entry_id="charge:" + reference,
            entry_type="charge",
            amount_cents=-actual,
            kind=reservation.get("kind", ""),
            label=reservation.get("label", ""),
            now=now,
        )
        return copy.deepcopy(reservation)

    return _mutate(store, owner_id, project_id, apply)[0]


def release_usage(store, owner_id: int, project_id: str, *, reference: str, now: str):
    reference = str(reference or "").strip()

    def apply(wallet, _project):
        reservation = wallet["reservations"].get(reference)
        if not reservation or reservation.get("status") == "released":
            return copy.deepcopy(reservation) if reservation else None
        if reservation.get("status") == "settled":
            return copy.deepcopy(reservation)
        amount = _cents(reservation.get("amount_cents", 0))
        wallet["reserved_cents"] = max(0, wallet["reserved_cents"] - amount)
        reservation["status"] = "released"
        _entry(
            wallet,
            entry_id="release:" + reference,
            entry_type="release",
            amount_cents=0,
            kind=reservation.get("kind", ""),
            label=reservation.get("label", ""),
            now=now,
        )
        return copy.deepcopy(reservation)

    return _mutate(store, owner_id, project_id, apply)[0]


@lru_cache(maxsize=1)
def usd_to_eur_rate() -> Decimal:
    configured = str(os.getenv("AGENCY_W_USD_TO_EUR") or "").strip()
    if configured:
        try:
            rate = Decimal(configured)
            if Decimal("0.5") <= rate <= Decimal("1.5"):
                return rate
        except InvalidOperation:
            pass
    try:
        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=8,
            allow_redirects=False,
        )
        response.raise_for_status()
        rate = Decimal(str((response.json().get("rates") or {}).get("EUR")))
        if Decimal("0.5") <= rate <= Decimal("1.5"):
            return rate
    except Exception:
        pass
    raise BillingError("Не удалось получить курс USD/EUR. Платная генерация временно остановлена, чтобы не списать неверную сумму.")


def usd_to_eur_cents(usd) -> int:
    try:
        value = Decimal(str(usd))
    except InvalidOperation:
        raise BillingError("Не удалось рассчитать стоимость.") from None
    if value < 0:
        raise BillingError("Не удалось рассчитать стоимость.")
    cents = (value * usd_to_eur_rate() * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(1, int(cents)) if value > 0 else 0


def quote_runway_credits(credits) -> dict:
    try:
        credits_value = Decimal(str(credits))
    except InvalidOperation:
        raise BillingError("Не удалось рассчитать стоимость Runway.") from None
    if credits_value <= 0:
        raise BillingError("Не удалось рассчитать стоимость Runway.")
    usd = credits_value * Decimal("0.01")
    return {
        "provider": "runway",
        "provider_units": float(credits_value),
        "provider_usd": float(usd),
        "amount_cents": usd_to_eur_cents(usd),
        "label": f"Видео Runway · {credits_value.normalize()} кредитов",
    }


def _music_price_usd_per_minute(api_key: str | None = None) -> Decimal:
    configured = str(os.getenv("AGENCY_W_MUSIC_USD_PER_MINUTE") or "").strip()
    if configured:
        try:
            value = Decimal(configured)
            if Decimal("0.01") <= value <= Decimal("10"):
                return value
        except InvalidOperation:
            pass
    key = str(api_key or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    tier = ""
    if key:
        try:
            response = requests.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": key},
                timeout=10,
                allow_redirects=False,
            )
            if response.ok:
                tier = str(response.json().get("tier") or "").strip().lower()
        except requests.RequestException:
            pass
    # Public Eleven Music extra-minute rates, with the most conservative
    # fallback when a workspace tier is not identifiable.
    return {
        "starter": Decimal("0.18"),
        "creator": Decimal("0.16"),
        "pro": Decimal("0.15"),
        "scale": Decimal("0.15"),
        "business": Decimal("0.15"),
    }.get(tier, Decimal("0.33"))


def quote_music(duration_ms: int, *, api_key: str | None = None) -> dict:
    if type(duration_ms) is not int or not 3000 <= duration_ms <= 600000:
        raise BillingError("Не удалось рассчитать стоимость музыки.")
    minutes = Decimal(duration_ms) / Decimal(60000)
    usd_per_minute = _music_price_usd_per_minute(api_key)
    usd = minutes * usd_per_minute
    return {
        "provider": "elevenlabs_music",
        "provider_units": float(minutes),
        "provider_usd": float(usd),
        "amount_cents": usd_to_eur_cents(usd),
        "label": f"Музыка · {duration_ms // 1000} сек.",
    }


def _return_url(base_url: str, status: str) -> str:
    parts = urlsplit(str(base_url or "https://agency-w.streamlit.app/"))
    query = urlencode({"wallet_payment": status})
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path or "/", query, ""))


def create_stripe_checkout(owner_id: int, project_id: str, amount_cents: int, *, secret_key: str | None = None, return_url: str | None = None) -> str:
    amount_cents = _cents(amount_cents)
    if not MIN_TOPUP_CENTS <= amount_cents <= MAX_TOPUP_CENTS:
        raise BillingError("Выберите пополнение от €5 до €500.")
    secret = str(secret_key or os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        raise BillingError("Онлайн-пополнение ещё не подключено владельцем Агентства.")
    base = str(return_url or os.getenv("AGENCY_W_APP_URL") or "https://agency-w.streamlit.app/").strip()
    data = {
        "mode": "payment",
        "success_url": _return_url(base, "success"),
        "cancel_url": _return_url(base, "cancel"),
        "client_reference_id": str(int(owner_id)),
        "line_items[0][price_data][currency]": "eur",
        "line_items[0][price_data][unit_amount]": str(amount_cents),
        "line_items[0][price_data][product_data][name]": "Пополнение баланса Агентства W",
        "line_items[0][quantity]": "1",
        "metadata[owner_id]": str(int(owner_id)),
        "metadata[project_id]": str(project_id),
        "metadata[purpose]": "agency_w_wallet_topup",
        "locale": "auto",
        "submit_type": "pay",
    }
    try:
        response = requests.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(secret, ""),
            data=data,
            timeout=20,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise BillingError("Платёжная страница временно недоступна. Повторите попытку.") from None
    try:
        if not response.ok:
            raise BillingError("Не удалось открыть безопасную страницу оплаты.")
        url = str(response.json().get("url") or "").strip()
        if not url.startswith("https://checkout.stripe.com/"):
            raise BillingError("Платёжный сервис вернул некорректную ссылку.")
        return url
    finally:
        response.close()


def verify_stripe_event(payload: bytes, signature_header: str, webhook_secret: str | None = None, *, tolerance: int = 300) -> dict:
    secret = str(webhook_secret or os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise BillingError("Stripe webhook не настроен.")
    if not isinstance(payload, bytes) or len(payload) > 1_048_576:
        raise BillingError("Некорректное платёжное уведомление.")
    timestamp = None
    signatures = []
    for part in str(signature_header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures or abs(int(time.time()) - timestamp) > tolerance:
        raise BillingError("Платёжное уведомление просрочено или не подписано.")
    expected = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, value) for value in signatures):
        raise BillingError("Неверная подпись платёжного уведомления.")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BillingError("Некорректное платёжное уведомление.") from None
    if not isinstance(event, dict) or not str(event.get("id") or "").startswith("evt_"):
        raise BillingError("Некорректное платёжное уведомление.")
    return event


def apply_stripe_event(store, event: dict, *, now: str) -> bool:
    event_type = str(event.get("type") or "")
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return False
    obj = ((event.get("data") or {}).get("object") or {})
    if not isinstance(obj, dict):
        return False
    if str(obj.get("payment_status") or "") != "paid":
        return False
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    if metadata.get("purpose") != "agency_w_wallet_topup":
        return False
    owner_id = str(metadata.get("owner_id") or "")
    project_id = str(metadata.get("project_id") or "")
    session_id = str(obj.get("id") or "")
    currency = str(obj.get("currency") or "").lower()
    amount_total = obj.get("amount_total")
    if not owner_id.isdigit() or not project_id or not session_id.startswith("cs_") or currency != "eur":
        raise BillingError("Платёж не удалось связать с балансом Агентства.")
    amount_cents = _cents(amount_total)
    if amount_cents < MIN_TOPUP_CENTS:
        raise BillingError("Сумма пополнения ниже допустимой.")
    credit_topup(
        store,
        int(owner_id),
        project_id,
        amount_cents,
        payment_id=session_id,
        now=now,
    )
    return True
