import streamlit as st


def render_neonia_contacts():
    """Показывает поиск Telegram-контактов и резервную загрузку файла."""

    st.markdown("### 👥 Контакты")

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
            "find_contacts": False,
            "file": None,
            "note": "",
            "submitted": False,
        }

    telegram_connected = st.session_state.get(
        "neonia_telegram_connected",
        False,
    )

    if telegram_connected:
        st.write(
            "Telegram уже подключён. Неония может получить "
            "доступные контакты для анализа."
        )
    else:
        st.warning(
            "Сначала подключите Telegram при входе в кабинет."
        )

    find_contacts = st.button(
        "🔍 Найти мои контакты",
        key="neonia_find_contacts",
        disabled=not telegram_connected,
    )

    contacts_file = None
    contacts_note = ""
    contacts_submitted = False

    with st.expander("Другой способ: загрузить файл"):
        with st.form("neonia_contacts_file_form"):
            contacts_file = st.file_uploader(
                "Загрузите список контактов",
                type=["csv", "xlsx", "txt", "vcf"],
                key="neonia_contacts_file",
            )

            contacts_note = st.text_area(
                "Комментарий к контактам — необязательно",
                key="neonia_contacts_note",
            )

            contacts_submitted = st.form_submit_button(
                "Подготовить файл к анализу"
            )

        if contacts_submitted:
            if contacts_file is None:
                st.warning("Сначала загрузите файл с контактами.")
            else:
                st.success("Файл с контактами загружен.")

    return {
        "telegram_connected": telegram_connected,
        "find_contacts": find_contacts,
        "file": contacts_file,
        "note": contacts_note,
        "submitted": contacts_submitted,
    }
