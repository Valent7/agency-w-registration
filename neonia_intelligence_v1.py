from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st


TARGET_STATUSES = {
    "green": ("🟢", "Сильный признак"),
    "yellow": ("🟡", "Дополнительный / требует контекста"),
    "red": ("🔴", "Слабое соответствие"),
    "gray": ("⚪", "Недостаточно данных"),
}

RISK_STATUSES = {
    "green": ("🟢", "Существенных рисков не выявлено"),
    "yellow": ("🟡", "Требует проверки"),
    "red": ("🔴", "Высокий риск"),
    "black": ("⛔", "Официальное предупреждение / подтверждённые нарушения"),
    "gray": ("⚪", "Недостаточно данных"),
}


def extract_json_object(answer: str) -> dict:
    text = str(answer or "").strip()
    if not text:
        raise ValueError("Пустой ответ ИИ")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("ИИ не вернул JSON-объект")

    return json.loads(text[start : end + 1])


def _clean_items(items, max_items=8):
    if not isinstance(items, list):
        return []
    result = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if text:
                result.append({"text": text[:220], "reason": reason[:320]})
        else:
            text = str(item).strip()
            if text:
                result.append({"text": text[:220], "reason": ""})
    return result


def normalize_target_profile(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def text(key, limit=1800):
        return str(raw.get(key) or "").strip()[:limit]

    profile = {
        "project_name": text("project_name", 120) or "Проект владельца",
        "portrait": text("portrait", 2200),
        "who_is_this": text("who_is_this", 1500),
        "current_situation": text("current_situation", 1500),
        "goals": _clean_items(raw.get("goals"), 8),
        "pains": _clean_items(raw.get("pains"), 8),
        "fears": _clean_items(raw.get("fears"), 8),
        "motivators": _clean_items(raw.get("motivators"), 8),
        "telegram_signals": _clean_items(raw.get("telegram_signals"), 12),
        "weak_fit": _clean_items(raw.get("weak_fit"), 8),
        "do_not_assume": [
            str(x).strip()[:260]
            for x in (raw.get("do_not_assume") or [])[:10]
            if str(x).strip()
        ],
        "saved_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
    }

    if not profile["portrait"]:
        profile["portrait"] = (
            "Недостаточно данных, чтобы представить человека достаточно точно. "
            "Добавьте материалы проекта."
        )
    return profile


def analyze_owner_project_target_profile(
    ask_openai_fn,
    project_links: str,
    project_files,
    owner_note: str,
):
    prompt = r"""
Ты — Неония, специалист по поиску целевой аудитории Агентства W.

ВНУТРЕННЕ изучи проект владельца, но НЕ показывай пользователю анализ проекта,
структуру компании, CEO, основателей, администраторов каналов, юридических
владельцев и прочую справочную информацию, если она не помогает понять
потенциального клиента/партнёра.

ТВОЙ ЕДИНСТВЕННЫЙ РЕЗУЛЬТАТ — ЖИВОЙ ПОРТРЕТ ЦЕЛЕВОЙ АУДИТОРИИ.

Проект может быть любым. Нельзя использовать фиксированную ЦА Neonexa.
Для каждого проекта заново пойми:
- какую реальную пользу получает человек;
- какую проблему или желание закрывает предложение;
- в какой жизненной/деловой ситуации возникает потребность;
- что человек хочет изменить;
- что его раздражает или тормозит;
- чего он опасается;
- какие слова, выгоды и возможности способны привлечь его внимание;
- какие ПОВЕДЕНЧЕСКИЕ признаки можно реально увидеть в Telegram.

Портрет должен быть настолько конкретным и образным, чтобы владелец мог
представить этого человека: чем он живёт, чего хочет, что его беспокоит,
почему он может остановиться и прочитать предложение.

НЕ ДЕЛАЙ демографических предположений по полу, возрасту, национальности,
фотографии, религии, здоровью или политическим взглядам.
Не придумывай факты.

Очень важно:
- интерес к ИИ, криптовалюте, Web3, TON или Telegram сам по себе НЕ означает,
  что человек подходит;
- разработчик ботов, криптокошельков или программист НЕ становится ЦА только
  из-за технологической тематики;
- отличай интерес к теме от реальной потребности в предложении проекта;
- признаки для Telegram должны быть наблюдаемыми, а не фантазиями о человеке.

Верни ТОЛЬКО JSON без Markdown:
{
  "project_name": "короткое название проекта",
  "portrait": "яркий цельный портрет идеального человека, 5-8 предложений",
  "who_is_this": "кто он в деловом/жизненном смысле, без демографических догадок",
  "current_situation": "что сейчас происходит в его работе/бизнесе/поиске возможностей",
  "goals": [
    {"text": "чего хочет", "reason": "почему проект может быть ему полезен"}
  ],
  "pains": [
    {"text": "что болит или раздражает", "reason": "как это связано с проектом"}
  ],
  "fears": [
    {"text": "сомнение или страх", "reason": "что важно учитывать в первом контакте"}
  ],
  "motivators": [
    {"text": "что способно зацепить внимание", "reason": "почему"}
  ],
  "telegram_signals": [
    {"text": "наблюдаемый сигнал в Telegram", "reason": "почему он указывает на соответствие ЦА"}
  ],
  "weak_fit": [
    {"text": "кому проект скорее не подходит", "reason": "почему"}
  ],
  "do_not_assume": [
    "что нельзя считать достаточным признаком ЦА"
  ]
}
""".strip()

    file_names = ", ".join(file.name for file in (project_files or [])) or "Файлы не загружены."
    request = (
        f"Ссылки на проект:\n{project_links.strip() or 'Ссылки не указаны.'}\n\n"
        f"Загруженные материалы:\n{file_names}\n\n"
        f"Комментарий владельца:\n{owner_note.strip() or 'Комментарий не указан.'}"
    )

    answer = ask_openai_fn(
        prompt,
        request,
        uploaded_files=project_files,
        use_web_search=bool(project_links.strip()),
    )
    if str(answer).startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)
    return normalize_target_profile(extract_json_object(answer))


def target_profile_for_analysis(profile: dict) -> str:
    """Машиночитаемый портрет, по которому Неония сравнивает реальные контакты."""
    profile = profile if isinstance(profile, dict) else {}
    payload = {
        "project_name": profile.get("project_name"),
        "portrait": profile.get("portrait"),
        "who_is_this": profile.get("who_is_this"),
        "current_situation": profile.get("current_situation"),
        "goals": [x.get("text") for x in profile.get("goals", [])],
        "pains": [x.get("text") for x in profile.get("pains", [])],
        "motivators": [x.get("text") for x in profile.get("motivators", [])],
        "observable_telegram_signals": [
            x.get("text") for x in profile.get("telegram_signals", [])
        ],
        "weak_fit": [x.get("text") for x in profile.get("weak_fit", [])],
        "do_not_assume": profile.get("do_not_assume", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_target_profile(profile: dict):
    """Пользователь видит только портрет ЦА. Внутренний анализ проекта скрыт."""
    profile = profile if isinstance(profile, dict) else {}
    st.markdown(f"## 🎯 Портрет вашей ЦА")
    st.caption(profile.get("project_name") or "Проект")

    if profile.get("portrait"):
        st.success(profile["portrait"])

    if profile.get("who_is_this"):
        st.markdown("### 👤 Кто этот человек")
        st.write(profile["who_is_this"])

    if profile.get("current_situation"):
        st.markdown("### 🌿 Что сейчас происходит в его жизни или бизнесе")
        st.write(profile["current_situation"])

    sections = [
        ("goals", "🎯 Чего он хочет"),
        ("pains", "😣 Что его беспокоит"),
        ("fears", "⚠️ Чего он опасается"),
        ("motivators", "✨ Что способно остановить его взгляд"),
    ]
    for key, title in sections:
        items = profile.get(key) or []
        if not items:
            continue
        st.markdown(f"### {title}")
        for item in items:
            txt = item.get("text", "") if isinstance(item, dict) else str(item)
            reason = item.get("reason", "") if isinstance(item, dict) else ""
            st.write(f"• **{txt}**")
            if reason:
                st.caption(reason)

    signals = profile.get("telegram_signals") or []
    if signals:
        with st.expander("🔍 Как Неония узнает такого человека в Telegram"):
            for item in signals:
                st.write(f"• **{item.get('text', '')}**")
                if item.get("reason"):
                    st.caption(item["reason"])

    weak = profile.get("weak_fit") or []
    if weak:
        with st.expander("🚫 Кому проект, скорее всего, не подходит"):
            for item in weak:
                st.write(f"• {item.get('text', '')}")
                if item.get("reason"):
                    st.caption(item["reason"])

    if profile.get("do_not_assume"):
        with st.expander("🧭 Что Неония НЕ будет принимать за признак ЦА"):
            for item in profile["do_not_assume"]:
                st.write(f"• {item}")

    st.info("💡 Вот такого человека Неония будет искать среди ваших контактов.")



def normalize_project_risk(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    level = str(raw.get("risk_level") or "gray").lower().strip()
    if level not in RISK_STATUSES:
        level = "gray"

    checks = []
    for item in (raw.get("checks") or [])[:10]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "gray").lower().strip()
        if status not in RISK_STATUSES:
            status = "gray"
        checks.append({
            "status": status,
            "label": str(item.get("label") or "Проверка")[:120],
            "finding": str(item.get("finding") or "")[:700],
            "source_url": str(item.get("source_url") or "").strip()[:1200],
            "source_title": str(item.get("source_title") or "").strip()[:220],
        })

    return {
        "project_name": str(raw.get("project_name") or "Проект кандидата")[:160],
        "risk_level": level,
        "summary": str(raw.get("summary") or "")[:1000],
        "checks": checks,
        "checked_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
        "disclaimer": (
            "Это аналитическая оценка по найденным данным, а не юридическое или "
            "инвестиционное заключение. Отсутствие красных флагов не гарантирует безопасность."
        ),
    }


def analyze_candidate_project_risk(
    ask_openai_fn,
    project_name: str,
    project_url: str = "",
    evidence: str = "",
):
    prompt = r'''
Ты — Неония, аналитик рисков Агентства W.
Проверяешь ПРОЕКТ, который продвигает найденный кандидат.

Нельзя называть проект мошенничеством или фейком по впечатлению.
Сильные выводы допустимы только при проверяемых фактах.

ПРИОРИТЕТ ИСТОЧНИКОВ:
1) официальные финансовые регуляторы, государственные реестры, суды,
   правоохранительные органы;
2) официальный сайт проекта и юридические документы;
3) надёжные первичные источники.

ПРОВЕРЬ, если применимо:
- юридическое лицо и руководителей;
- лицензии/регистрации там, где они требуются;
- официальные предупреждения регуляторов;
- судебные/enforcement-материалы;
- обещания гарантированной или необычно высокой доходности;
- понятность источника выручки и экономической модели;
- наличие реального продукта/услуги и внешнего спроса;
- зависимость вознаграждений главным образом от рекрутирования новых участников;
- прозрачность условий, вывода средств и рисков.

Уровни:
green = существенных красных флагов по проверенным данным не выявлено;
yellow = требует проверки / есть вопросы / недостаточно подтверждений;
red = несколько существенных подтверждаемых красных флагов;
black = есть официальное предупреждение регулятора, решение суда или иное
        сильное официальное подтверждение нарушений;
gray = данных недостаточно для оценки.

ВАЖНО:
- MLM, криптовалюта, Web3 или ИИ сами по себе НЕ являются доказательством риска.
- Не используй слово «рекомендуемый».
- Для каждого существенного пункта дай конкретный URL источника.
- Если надёжного источника нет — source_url оставь пустым и поставь gray/yellow.

Верни ТОЛЬКО JSON:
{
  "project_name": "...",
  "risk_level": "green|yellow|red|black|gray",
  "summary": "короткий итог простыми словами",
  "checks": [
    {
      "status": "green|yellow|red|black|gray",
      "label": "что проверено",
      "finding": "конкретный факт или честно: не подтверждено",
      "source_url": "https://... или пусто",
      "source_title": "название источника"
    }
  ]
}
'''.strip()

    request = (
        f"Название проекта: {project_name.strip() or 'не указано'}\n"
        f"Известная ссылка: {project_url.strip() or 'не указана'}\n"
        f"Контекст из профиля/переписки кандидата: {evidence.strip() or 'нет'}"
    )
    answer = ask_openai_fn(prompt, request, use_web_search=True)
    if str(answer).startswith("Ошибка OpenAI:"):
        raise RuntimeError(answer)
    return normalize_project_risk(extract_json_object(answer))


def render_project_risk(risk: dict):
    risk = risk if isinstance(risk, dict) else {}
    level = risk.get("risk_level", "gray")
    emoji, label = RISK_STATUSES.get(level, RISK_STATUSES["gray"])
    st.markdown(f"**{emoji} Риск проекта: {label}**")
    if risk.get("summary"):
        st.write(risk["summary"])

    for item in risk.get("checks") or []:
        status = item.get("status", "gray")
        item_emoji, _ = RISK_STATUSES.get(status, RISK_STATUSES["gray"])
        st.write(f"{item_emoji} **{item.get('label', 'Проверка')}** — {item.get('finding', '')}")
        url = item.get("source_url") or ""
        title = item.get("source_title") or "Источник"
        if url.startswith("http://") or url.startswith("https://"):
            st.markdown(f"[Источник: {title}]({url})")
        else:
            st.caption("Надёжный источник не найден или не подтверждён.")

    st.caption(risk.get("disclaimer") or "")
