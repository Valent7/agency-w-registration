"""Compact wallet UI shared by all Content Studio tools."""
from __future__ import annotations

import os
import streamlit as st

from content_factory_billing import (
    BillingError,
    billing_enforced,
    create_stripe_checkout,
    format_eur,
    wallet_summary,
)

TOPUPS = {
    "€5": 500,
    "€10": 1000,
    "€20": 2000,
    "€50": 5000,
    "€100": 10000,
}


def _secret(name: str) -> str:
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return str(value or os.getenv(name) or "").strip()


def render_wallet_panel(owner_id: int, project: dict | None, store) -> None:
    if not billing_enforced():
        return
    if not isinstance(project, dict) or store is None:
        return

    # Always re-read before rendering: a Stripe webhook may have credited the
    # project since Streamlit loaded the page.
    try:
        fresh = store.get_project(owner_id, project["id"]) or project
    except Exception:
        fresh = project
    summary = wallet_summary(fresh)

    with st.container(border=True):
        col1, col2, col3, col4 = st.columns([1.4, 1.2, 1.2, 1.6], gap="medium")
        col1.metric("💶 Баланс", format_eur(summary["balance_cents"]))
        col2.metric("🔒 В резерве", format_eur(summary["reserved_cents"]))
        col3.metric("✅ Доступно", format_eur(summary["available_cents"]))
        with col4:
            st.caption("Оплата видео и музыки")
            if st.button(
                "💳 Пополнить баланс",
                type="primary",
                width="stretch",
                key=f"wallet_{owner_id}_{project['id']}_topup_open",
            ):
                st.session_state[f"wallet_{owner_id}_{project['id']}_open"] = True

        payment_state = str(st.query_params.get("wallet_payment") or "")
        if payment_state == "success":
            st.success("Платёж принят. Баланс обновится после подтверждения платёжным сервисом.")
        elif payment_state == "cancel":
            st.info("Оплата отменена. Деньги не списаны.")

        if st.session_state.get(f"wallet_{owner_id}_{project['id']}_open"):
            left, right = st.columns([2, 1], gap="large")
            with left:
                amount_label = st.radio(
                    "Сколько добавить на баланс",
                    list(TOPUPS),
                    horizontal=True,
                    index=1,
                    key=f"wallet_{owner_id}_{project['id']}_amount",
                )
                st.caption(
                    "Баланс расходуется только на фактически созданные платные материалы. "
                    "Перед генерацией сумма резервируется; при техническом сбое резерв возвращается."
                )
            with right:
                st.write("")
                if st.button(
                    "Перейти к оплате",
                    width="stretch",
                    key=f"wallet_{owner_id}_{project['id']}_checkout",
                ):
                    try:
                        url = create_stripe_checkout(
                            owner_id,
                            project["id"],
                            TOPUPS[amount_label],
                            secret_key=_secret("STRIPE_SECRET_KEY"),
                            return_url=_secret("AGENCY_W_APP_URL") or "https://agency-w.streamlit.app/",
                        )
                        st.session_state[f"wallet_{owner_id}_{project['id']}_checkout_url"] = url
                    except BillingError as exc:
                        st.error(str(exc))
                    except Exception:
                        st.error("Не удалось открыть оплату. Повторите попытку.")

                checkout_url = st.session_state.get(f"wallet_{owner_id}_{project['id']}_checkout_url")
                if checkout_url:
                    st.link_button(
                        "🔐 Открыть безопасную оплату",
                        checkout_url,
                        type="primary",
                        width="stretch",
                    )

        entries = list(reversed(summary["entries"][-8:]))
        if entries:
            with st.expander("История баланса", expanded=False):
                for item in entries:
                    kind = item.get("type")
                    amount = int(item.get("amount_cents") or 0)
                    if kind == "topup":
                        prefix = "➕"
                    elif kind == "charge":
                        prefix = "➖"
                    elif kind == "reserve":
                        prefix = "🔒"
                    else:
                        prefix = "↩️"
                    amount_text = format_eur(abs(amount)) if amount else "—"
                    st.caption(
                        f"{prefix} {item.get('label') or 'Операция'} · {amount_text}"
                        + (f" · {item.get('created_at')[:16]}" if item.get("created_at") else "")
                    )
