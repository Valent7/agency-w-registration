import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

st.set_page_config(
    page_title="Агентство W",
    page_icon="🏛️",
    layout="centered",
)

# Получаем код пригласившего из ссылки вида:
# https://agency-w.streamlit.app/?ref=W12345
referral_code = st.query_params.get("ref", "").strip()
def ask_openai(system_prompt, user_message):
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        return "Ключ OpenAI не найден в настройках приложения."

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-5-mini",
                "instructions": system_prompt,
                "input": user_message,
                "store": False,
            },
            timeout=90,
        )

        response.raise_for_status()
        data = response.json()

        text_parts = []

        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_parts.append(content.get("text", ""))

        answer = "\n".join(text_parts).strip()

        if not answer:
            return "ИИ не сформировал ответ. Попробуйте ещё раз."

        return answer

    except requests.exceptions.RequestException:
        return (
            "Не удалось связаться с ИИ. "
            "Проверьте подключение и повторите попытку."
        )
def save_member_to_supabase(telegram_data, referral_code):
    telegram_id = int(telegram_data["id"])
    member_code = f"W{telegram_id}"

    payload = {
        "telegram_id": telegram_id,
        "first_name": telegram_data.get("first_name", "Пользователь"),
        "username": telegram_data.get("username") or None,
        "member_code": member_code,
        "referrer_code": referral_code or None,
    }

    response = requests.post(
        f"{st.secrets['SUPABASE_URL']}/rest/v1/agency_members",
        headers={
            "apikey": st.secrets["SUPABASE_SECRET_KEY"],
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=payload,
        timeout=10,
    )

    if response.status_code == 409:
        return member_code, False

    response.raise_for_status()
    return member_code, True

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

                /* Мобильная версия */
        @media (max-width: 768px) {

            .block-container {
                padding-top: 1rem;
                padding-left: 1rem;
                padding-right: 1rem;
                max-width: 100%;
            }

            .main-title {
                font-size: clamp(1.7rem, 8vw, 2.3rem);
                line-height: 1.15;
                white-space: nowrap;
                margin-bottom: 0.5rem;
            }

            .subtitle {
                font-size: 1rem;
                line-height: 1.4;
                margin-bottom: 1.2rem;
            }

            .registration-card {
                padding: 1.5rem 1rem;
                border-radius: 1.25rem;
                min-height: auto !important;
                height: auto !important;
                justify-content: flex-start !important;
            }

            .registration-card h2 {
                font-size: 2rem;
                line-height: 1.15;
                margin-bottom: 1rem;
            }

            .registration-card p {
                font-size: 1rem;
                line-height: 1.5;
            }
div[data-testid="stAlert"] {
    padding: 0.9rem 1rem !important;
}

div[data-testid="stAlert"] p {
    color: #f5f5f5 !important;
    font-size: 0.95rem !important;
    line-height: 1.4 !important;
}
            div[data-testid="stCode"] pre {
                white-space: pre-wrap !important;
                overflow-wrap: anywhere;
                word-break: break-all;
                font-size: 0.85rem;
            }
        }

        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        #MainMenu,
        footer {
            display: none !important;
        }
                /* Убираем нижний значок и плашку Streamlit */
        div[class*="viewerBadge"],
        div[class*="ViewerBadge"],
        [data-testid="stViewerBadge"] {
            display: none !important;
        }

        /* Делаем обычный текст читаемым на тёмном фоне */
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] strong {
            color: #e7e0d4 !important;
        }

        /* Текст цветных уведомлений оставляем светлым */
        div[data-testid="stAlert"] p {
            color: #f5f5f5 !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

if not st.query_params.get("hash"):
    st.markdown(
        '<div class="main-title">🏛 Агентство W</div>',
        unsafe_allow_html=True,
    )

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

    if referral_code:
        st.success(f"Приглашение партнёра принято: {referral_code}")
    else:
        st.info("Вы открыли сайт без персональной партнёрской ссылки.")
# Защищённый вход через Telegram
import hashlib
import hmac
import html
import time
from urllib.parse import urlencode

telegram_keys = (
    "id",
    "first_name",
    "last_name",
    "username",
    "photo_url",
    "auth_date",
)

telegram_data = {
    key: str(st.query_params.get(key))
    for key in telegram_keys
    if st.query_params.get(key) is not None
}

received_hash = str(st.query_params.get("hash", ""))


def telegram_auth_is_valid(data, received_hash):
    if not data or not received_hash:
        return False

    try:
        auth_date = int(data["auth_date"])
    except (KeyError, TypeError, ValueError):
        return False

    # Не принимаем устаревшие данные авторизации
    if abs(time.time() - auth_date) > 86400:
        return False

    data_check_string = "\n".join(
        f"{key}={data[key]}" for key in sorted(data)
    )

    secret_key = hashlib.sha256(
        st.secrets["TELEGRAM_BOT_TOKEN"].encode("utf-8")
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated_hash, received_hash)


if received_hash:
    if telegram_auth_is_valid(telegram_data, received_hash):
        first_name = telegram_data.get("first_name", "Пользователь")
        telegram_id = telegram_data.get("id", "")
        member_code, created = save_member_to_supabase(telegram_data, referral_code)

        
        partner_link = f"https://agency-w.streamlit.app/?ref={member_code}"
        st.markdown(f"## Добро пожаловать, {first_name}!")
        st.markdown("### 🟢 Мы строим своё будущее")

        main_section = st.segmented_control(
            "Главное меню",
            ["☀️ День", "🤖 Агенты", "👥 Команда", "👤 Профиль"],
            default="☀️ День",
            required=True,
            label_visibility="collapsed",
            width="stretch",
            key="main_section",
        )

        if main_section == "☀️ День":
            st.markdown("### ☀️ Мой день")

            daily_moods = [
                "Сегодня важно сделать один конкретный шаг, который приблизит вас к цели.",
                "Не стремитесь сделать всё сразу. Главное — двигаться вперёд каждый день.",
                "Ваше будущее создаётся решениями, которые вы принимаете сегодня.",
                "Спокойствие, ясность и последовательность сильнее суеты.",
                "Сегодня хороший день, чтобы завершить то, что давно откладывалось.",
                "Большой результат начинается с одного простого действия.",
                "Сосредоточьтесь не на трудностях, а на следующем возможном шаге.",
                "Каждый новый человек — это новая история, а не просто контакт.",
                "Сегодня выбирайте действия, которые создают результат, а не занятость.",
                "Уверенность появляется не до действия, а после первых сделанных шагов.",
                "Не сравнивайте своё начало с чужим результатом. Продолжайте свой путь.",
                "Сегодня достаточно стать немного сильнее, чем вы были вчера.",
                "Возможности замечает тот, кто готов действовать.",
                "Ваш опыт — это ваша сила. Технологии помогают её масштабировать.",
            ]
    
            berlin_today = datetime.now(
                    ZoneInfo("Europe/Berlin")
            ).date()
    
            mood_index = berlin_today.toordinal() % len(daily_moods)
            mood_of_the_day = daily_moods[mood_index]
    
            st.info(f"Настрой дня: {mood_of_the_day}")  

            with st.container(border=True):
                st.markdown("**📅 Встречи**")
                st.caption("Сегодняшние встречи появятся здесь.")

            with st.container(border=True):
                st.markdown("**✅ Задачи на сегодня**")
                st.caption("Главные задачи дня появятся здесь.")

            with st.container(border=True):
                st.markdown("**📊 Итоги**")
                st.caption("Здесь будут итоги недели и месяца.")

        elif main_section == "🤖 Агенты":
            st.markdown("### 🤖 Агенты")

            selected_agent = st.selectbox(
                "Выберите агента",
                ["Стагирит", "Неония", "Неона", "Неола"],
                key="selected_agent",
            )

            agent_descriptions = {
                "Стагирит": (
                    "Главный координатор и заместитель директора. "
                    "Распределяет задачи между агентами и контролирует результат."
                ),
                "Неония": (
                    "Анализирует проекты, сегментирует людей "
                    "и находит подходящих кандидатов."
                ),
                "Неона": (
                    "Ведёт диалог, отвечает по контексту "
                    "и подводит человека к осознанной встрече."
                ),
                "Неола": (
                    "Проводит онбординг, помогает новичку начать работу "
                    "и сопровождает его после регистрации."
                ),
            }

            with st.container(border=True):
                st.markdown(f"#### {selected_agent}")
                st.write(agent_descriptions[selected_agent])
                if selected_agent == "Неония":
                    st.caption(
                        "Рабочая бета-версия: Неония анализирует проект, "
                        "целевую аудиторию и конкретного человека."
                    )

                    with st.form("neonia_analysis_form"):
                        project_description = st.text_area(
                            "Опишите проект или предложение",
                            placeholder=(
                                "Что вы предлагаете людям, какую проблему "
                                "решаете и какой результат они получают?"
                            ),
                            height=150,
                        )

                        person_description = st.text_area(
                            "Опишите человека или вставьте информацию о нём",
                            placeholder=(
                                "Интересы, профессия, сообщения, описание профиля. "
                                "Это поле можно пока оставить пустым."
                            ),
                            height=150,
                        )

                        neonia_submitted = st.form_submit_button(
                            "🔎 Провести анализ"
                        )

                        if neonia_submitted:
                            if not project_description.strip():
                                st.warning(
                                    "Сначала кратко опишите проект или предложение."
                                )
                            else:
                                neonia_prompt = """
    Ты — Неония, ИИ-аналитик Агентства W.
    
    Твоя задача:
    — понять суть проекта и его реальную ценность для людей;
    — определить подходящие сегменты целевой аудитории;
    — оценить конкретного человека, если информация о нём предоставлена;
    — не навязывать предложение и не манипулировать;
    — находить естественные точки соприкосновения;
    — готовить материал для передачи Неоне, которая продолжит диалог.
    
    Отвечай простым русским языком.
    
    Структура ответа:
    
    1. Суть предложения.
    2. Какие проблемы людей оно может решать.
    3. Подходящие сегменты аудитории.
    4. Анализ человека и степень соответствия — если данные есть.
    5. Что может его заинтересовать.
    6. Возможные сомнения или возражения.
    7. Лучший первый вопрос для начала разговора.
    8. Что передать Неоне для продолжения диалога.
    
    Не придумывай факты о человеке.
    Чётко отделяй известную информацию от предположений.
    """
    
                                neonia_request = f"""
    ПРОЕКТ ИЛИ ПРЕДЛОЖЕНИЕ:
    
    {project_description}
    
    ИНФОРМАЦИЯ О ЧЕЛОВЕКЕ:
    
    {person_description or "Информация пока не предоставлена."}
    """
    
                                with st.spinner("Неония проводит анализ..."):
                                    neonia_answer = ask_openai(
                                        neonia_prompt,
                                        neonia_request,
                                    )
    
                                st.markdown("#### 📋 Результат Неонии")
                                st.write(neonia_answer)
    
                elif selected_agent == "Неона":
                    st.info(
                        "Неону подключаем следующим шагом сегодня. "
                        "Она будет вести диалог от первого сообщения до встречи."
                    )

                else:
                    st.caption(
                        "Подключение этого агента будет следующим этапом."
                    )

        elif main_section == "👥 Команда":
            st.markdown("### 👥 Команда")

            with st.container(border=True):
                st.markdown("**Лично приглашённые партнёры**")
                st.caption("Список партнёров появится после подключения базы.")

            with st.container(border=True):
                st.markdown("**Структура и активность**")
                st.caption("Здесь будут новые регистрации и результаты команды.")

        elif main_section == "👤 Профиль":
            inviter_text = referral_code if referral_code else "не указан"

            st.markdown("### 👤 Профиль")

            with st.container(border=True):
                st.markdown(f"**Имя:** {first_name}")
                st.markdown(f"**Партнёрский код:** `{member_code}`")
                st.markdown(f"**Пригласитель:** `{inviter_text}`")
                st.markdown("**Статус:** 🟢 Активен")

            st.markdown("**Персональная партнёрская ссылка:**")
            st.code(partner_link, language=None)
    else:
        st.error(
            "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз."
        )
else:
    bot_username = st.secrets["TELEGRAM_BOT_USERNAME"]

    auth_url = "https://agency-w.streamlit.app/"
    if referral_code:
        auth_url += "?" + urlencode({"ref": referral_code})

    st.html(
        f"""
        <div style="display:flex; justify-content:center; margin:0.5rem 0 1rem;">
            <script async
                src="https://telegram.org/js/telegram-widget.js?22"
                data-telegram-login="{html.escape(bot_username, quote=True)}"
                data-size="large"
                data-radius="10"
                data-lang="ru"
                data-auth-url="{html.escape(auth_url, quote=True)}">
            </script>
        </div>
        """,
        unsafe_allow_javascript=True,
    )

    st.caption("Подтвердите вход в безопасном окне Telegram.")

if not st.query_params.get("hash"):
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
