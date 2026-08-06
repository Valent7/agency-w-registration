import streamlit as st


def render_neonia_chats():
    """Показывает безопасный запуск поиска Telegram-групп и каналов."""

    st.markdown("### 🔎 Мои Telegram-чаты")

    telegram_ready = (
        "TELEGRAM_API_ID" in st.secrets
        and "TELEGRAM_API_HASH" in st.secrets
    )

    if not telegram_ready:
        st.error(
            "Не найдены TELEGRAM_API_ID и TELEGRAM_API_HASH "
            "в настройках приложения."
        )
        return {
            "telegram_connected": False,
            "find_chats": False,
        }

    telegram_connected = st.session_state.get(
        "neonia_telegram_connected",
        False,
    )

    if telegram_connected:
        st.write(
            "Telegram уже подключён. Неония может получить список "
            "доступных групп и каналов."
        )
    else:
        st.warning(
            "Сначала подключите Telegram при входе в кабинет."
        )

    st.caption(
        "На этом шаге загружаются только названия и служебные сведения "
        "о группах и каналах. Сообщения и участники не анализируются."
    )

    find_chats = st.button(
        "🔎 Найти мои чаты",
        key="neonia_find_chats",
        disabled=not telegram_connected,
        type="primary",
    )

    return {
        "telegram_connected": telegram_connected,
        "find_chats": find_chats,
    }
