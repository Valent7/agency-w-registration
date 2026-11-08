"""Draft partner development and reward model for Agency W. No live payouts."""
import streamlit as st


def render_w_partner_plan():
    st.markdown("### Партнёрская модель Агентства W")
    st.caption("Проект собственных правил W: не переносит тарифы и обязательства другой компании.")
    p, t, m, e = st.tabs(
        ["📦 Продукты и роли", "🌳 Структура", "🏅 Мотивация", "💶 Экономика"]
    )
    with p:
        st.markdown(
            "**Клиент** получает цифровые услуги. **Партнёр** знакомит с продуктом "
            "и помогает начать. **Наставник** обучает партнёров и отвечает за качество сопровождения."
        )
        st.markdown("**Направления продукта:** цифровые сотрудники, контент-завод, "
                    "настраиваемый помощник для Telegram-чата или сообщества.")
        st.markdown("**Принцип:** вознаграждения возможны только от подтверждённой "
                    "реальной выручки за полезные услуги, а не просто за привлечение людей.")
        st.warning("Цены, проценты комиссий, ранги и бонусы пока не утверждены для W.")

    with t:
        st.markdown("### Развитие и поддержка команды")
        st.markdown("Маршрут: **владелец W → наставники → партнёры → клиенты**. "
                    "Наставники сопровождают людей, а не только формируют структуру.")
        c1, c2, c3 = st.columns(3)
        with c1:
            mentors = st.number_input("Наставников", 0, step=1, key="w_plan_mentors")
        with c2:
            partners = st.number_input("Активных партнёров", 0, step=1, key="w_plan_partners")
        with c3:
            clients = st.number_input("Клиентов", 0, step=1, key="w_plan_clients")
        st.metric("Участников модели", mentors + partners + clients)
        st.caption("Числа вводятся вручную и пока не связаны с партнёрской базой.")
        if st.button("Открыть команду", key="w_plan_go_team"):
            st.session_state["agency_open_main_section"] = "👥 Команда"
            st.rerun()

    with m:
        st.markdown("### Ступени развития — по компетенциям и результату")
        levels = [
            ("Старт", "Умеет объяснить клиенту реальную пользу одного продукта."),
            ("Практик", "Проводит содержательные встречи и помогает начать работу."),
            ("Наставник", "Поддерживает партнёров и помогает исправлять рабочие ошибки."),
            ("Лидер", "Обеспечивает качество обслуживания и устойчивость команды."),
        ]
        for title, desc in levels:
            with st.expander(title):
                st.write(desc)
        st.info("Это предварительные уровни подготовки, а не оплачиваемые ранги.")

    with e:
        st.markdown("### Проверка экономической устойчивости")
        st.caption("Сценарный калькулятор, не прогноз дохода и не действующая схема выплат.")
        revenue_per_client = st.number_input(
            "Поступления с одного оплаченного клиента в месяц, €", 0.0, step=1.0,
            key="w_plan_revenue")
        paying_clients = st.number_input(
            "Оплачивающих клиентов", 0, step=1, key="w_plan_paying")
        service_cost = st.number_input(
            "Обслуживание одного клиента в месяц, €", 0.0, step=1.0,
            key="w_plan_cost")
        fixed = st.number_input(
            "Общие расходы и резерв в месяц, €", 0.0, step=1.0,
            key="w_plan_fixed")
        revenue = revenue_per_client * paying_clients
        costs = service_cost * paying_clients + fixed
        available = revenue - costs
        a, b, c = st.columns(3)
        a.metric("Выручка", f"{revenue:,.2f} €")
        b.metric("Затраты", f"{costs:,.2f} €")
        c.metric("Остаток до выплат", f"{available:,.2f} €")
        if available <= 0 and revenue > 0:
            st.error("В этом сценарии свободного бюджета на партнёрские выплаты нет.")
        st.markdown("**Не утверждены:** ставки, глубина командных комиссий, "
                    "бонусы, условия активности и правовые требования.")
