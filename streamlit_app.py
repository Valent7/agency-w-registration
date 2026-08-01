import streamlit as st

st.set_page_config(
    page_title="Агентство W",
    page_icon="🏛️",
    layout="centered",
)

# Получаем код пригласившего из ссылки вида:
# https://agency-w.streamlit.app/?ref=W12345
referral_code = st.query_params.get("ref", "").strip()

st.markdown(
    """
    <style>
        .stApp {
            background:
                radial-gradient(circle at top, #28231d 0%, #101116 45%, #08090d 100%);
        }

        .main-title {
            text-align: center;
            font-size: 3rem;
            font-weight: 800;
            color: #ffffff;
            margin-top: 2rem;
        }

        .subtitle {
            text-align: center;
            font-size: 1.25rem;
            color: #d8c9b0;
            margin-bottom: 2rem;
        }

        .registration-card {
            padding: 2rem;
            border: 1px solid rgba(224, 205, 171, 0.25);
            border-radius: 22px;
            background: rgba(255, 255, 255, 0.05);
            box-shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
        }

        .registration-card h2 {
            color: #ffffff;
            text-align: center;
        }

        .registration-card p {
            color: #e7e0d4;
            text-align: center;
            line-height: 1.6;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-title">🏛️ Агентство W</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">ИИ-агентство нового поколения</div>',
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="registration-card">
        <h2>Добро пожаловать</h2>
        <p>
            Здесь вы сможете зарегистрироваться, получить личный кабинет
            и подключить интеллектуальных помощников для развития своего проекта.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.write("")

if referral_code:
    st.success(f"Приглашение партнёра принято: {referral_code}")
else:
    st.info("Вы открыли сайт без персональной партнёрской ссылки.")

st.button(
    "Продолжить регистрацию через Telegram",
    use_container_width=True,
    disabled=True,
)

st.caption(
    "Подключение защищённого входа через Telegram будет активировано на следующем этапе."
)

st.divider()

st.markdown(
    """
    **После регистрации вы получите:**

    🤖 доступ к ИИ-помощникам;  
    🔗 собственную партнёрскую ссылку;  
    🗂️ личный рабочий кабинет;  
    📊 инструменты поиска, общения и сопровождения партнёров.
    """
)
