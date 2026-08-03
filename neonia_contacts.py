import streamlit as st


def render_neonia_contacts():
    """Показывает раздел загрузки контактов для Неонии."""

    with st.form("neonia_contacts_form"):
        contacts_file = st.file_uploader(
            "📇 Загрузите список контактов",
            type=["csv", "xlsx", "txt", "vcf"],
            help="Подойдут CSV, Excel, TXT или файл контактов VCF.",
            key="neonia_contacts_file",
        )

        contacts_note = st.text_area(
            "Комментарий к контактам — необязательно",
            placeholder=(
                "Например: контакты из Telegram, "
                "старые знакомые, клиенты или партнёры."
            ),
            key="neonia_contacts_note",
        )

        contacts_submitted = st.form_submit_button(
            "👥 Подготовить контакты к анализу"
        )

    if contacts_submitted:
        if contacts_file is None:
            st.warning("Сначала загрузите файл с контактами.")
        else:
            st.success(
                "Список контактов загружен. "
                "Следующим шагом подключим анализ по параметрам ЦА."
            )

    return {
        "file": contacts_file,
        "note": contacts_note,
        "submitted": contacts_submitted,
    }
