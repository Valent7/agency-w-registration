"""Journal calendar and public verification in the existing marketing cabinet."""
from datetime import date

import requests
import streamlit as st

from journal_schedule import DAYS, WEEK, calendar_topics
from marketing_content_plan import create_content_plan
from marketing_journal import normalized_title, read_feed, verify_publication


def render_journal(store, prefix, revision, state, passport_version, passport):
    with st.expander("Журнал «Время жить»"):
        start = st.date_input("Начало редакционного календаря", date.today(), key=prefix + "_journal_date")
        days = st.radio("Период в днях", (7, 30), horizontal=True, key=prefix + "_journal_days")
        directions = state.get("journal_week", [row[0] for row in WEEK])
        with st.form(prefix + "_journal_week"):
            edited = [st.text_input(day, value=direction, max_chars=180, key=f"{prefix}_week_{index}")
                      for index, (day, direction) in enumerate(zip(DAYS, directions))]
            update_week = st.form_submit_button("Сохранить недельный график")
        if update_week:
            if not all(value.strip() for value in edited):
                st.warning("Заполните направления на все семь дней.")
            else:
                try:
                    store.save(revision, {**state, "journal_week": [value.strip() for value in edited]})
                except (requests.RequestException, RuntimeError):
                    st.warning("График не сохранён. Обновите страницу и повторите.")
                else:
                    st.rerun()

        plan_key = f"journal/{passport_version}/{start.isoformat()}/{days}"
        plans = state.get("content_plans", {})
        plan = plans.get(plan_key)
        if st.button("Создать редакционный календарь", key=prefix + "_journal_plan"):
            try:
                published = {normalized_title(entry["title"]) for entry in read_feed().values()}
                plan = create_content_plan(passport, passport_version, start, days, channel="journal")
                plan = calendar_topics(
                    [entry for entry in plan if normalized_title(entry["title"]) not in published],
                    directions,
                )
                store.save(revision, {**state, "content_plans": {**plans, plan_key: plan}})
            except (requests.RequestException, ValueError, RuntimeError):
                st.warning("Календарь не сохранён. Проверьте активные сегменты и доступность журнала.")
                return
            st.rerun()
        if plan:
            st.caption(f"Черновик календаря · версия паспорта {passport_version}")
            st.dataframe([{
                "Дата": item["date"], "Сегмент": item["segment"], "Формат": item["format"],
                "Направление": item.get("direction", ""),
                "Цель": item.get("goal", ""),
                "Рабочая тема": item["topic"], "Следующее действие": item["call_to_action"],
            } for item in plan], hide_index=True)

        slug = st.text_input("Идентификатор статьи из адреса страницы", key=prefix + "_journal_slug").strip()
        ledger = state.get("journal_publications", {})
        if st.button("Проверить размещение", disabled=not slug, key=prefix + "_journal_verify"):
            try:
                receipt = verify_publication(slug)
                store.save(revision, {**state, "journal_publications": {**ledger, slug: receipt}})
            except (requests.RequestException, ValueError, RuntimeError):
                st.warning("Размещение не подтверждено. Сверьте адрес и доступность статьи.")
                return
            st.rerun()
        for receipt in sorted(ledger.values(), key=lambda item: item["verified_at"], reverse=True):
            st.link_button(receipt["title"], receipt["url"])
            st.caption(f"Доступность подтверждена · {receipt['verified_at'][:16].replace('T', ' ')} UTC")


@st.cache_data(ttl=300, show_spinner=False)
def _editorial_run():
    from marketing_journal import latest_editorial_run

    try:
        return latest_editorial_run()
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None


def render_editorial_run_status():
    run = _editorial_run()
    if run is None:
        return
    if run["conclusion"] in {"failure", "cancelled", "timed_out", "action_required"}:
        st.error("Последний редакционный цикл не завершился успешно. Проверьте журнал выполнения.")
    st.link_button("Открыть последний запуск редакции", run["url"])
