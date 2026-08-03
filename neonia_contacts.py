import streamlit as st


def render_neonia_contacts():
    """Показывает подключение Telegram и резервную загрузку файла."""

    st.markdown("### 👥 Контакты")
    st.write(
        "Подключите Telegram один раз. "
        "Неония сама подготовит доступные контакты к анализу."
    )

    telegram_ready = (
        "TELEGRAM_API_ID" in st.secrets
        and "TELEGRAM_API_HASH" in st.secrets
    )

    if telegram_ready:
        st.success("Агентство W готово к подключению Telegram.")
    else:
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
        st.success("Ваш Telegram подключён.")
    elif st.button(
        "🔗 Подключить мой Telegram",
        key="neonia_connect_telegram",
    ):
        st.session_state["neonia_connect_requested"] = True

    if (
        st.session_state.get("neonia_connect_requested", False)
        and not telegram_connected
    ):
        st.info(
            "Кнопка подключения готова. "
            "Следующим шагом добавим ввод номера и кода Telegram."
        )

    find_contacts = st.button(
        "🔍 Найти мои контакты",
        key="neonia_find_contacts",
        disabled=not telegram_connected,
    )

    if not telegram_connected:
        st.caption(
            "Поиск станет доступен после подключения Telegram."
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
