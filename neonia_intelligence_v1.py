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
    project_name = str(raw.get("project_name") or "Проект владельца").strip()[:120]
    summary = str(raw.get("project_summary") or "").strip()[:900]
    ideal = str(raw.get("ideal_audience") or "").strip()[:1400]

    profile = {
        "project_name": project_name,
        "project_summary": summary,
        "ideal_audience": ideal,
        "green": _clean_items(raw.get("green"), 8),
        "yellow": _clean_items(raw.get("yellow"), 8),
        "red": _clean_items(raw.get("red"), 8),
        "gray": _clean_items(raw.get("gray"), 5),
        "search_signals": _clean_items(raw.get("search_signals"), 10),
        "do_not_assume": [
            str(x).strip()[:220]
            for x in (raw.get("do_not_assume") or [])[:8]
            if str(x).strip()
        ],
        "saved_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
    }

    if not profile["ideal_audience"]:
        profile["ideal_audience"] = (
            "Недостаточно данных для уверенного портрета ЦА. "
            "Добавьте материалы проекта."
        )
    return profile


def analyze_owner_project_target_profile(
    ask_openai_fn,
    project_links: str,
    project_files,
    owner_note: str,
):
    prompt = r'''
Ты — Неония, аналитик Агентства W.

Твоя задача — НЕ написать длинный обзор проекта, а определить, КОГО ИСКАТЬ
для конкретного проекта владельца.

Проект может быть любым: сетевой бизнес, услуги, образование, туризм,
недвижимость, обычная компания и т.д. НИКОГДА не используй фиксированную ЦА
Neonexa для другого проекта.

Сначала пойми реальную ценность, продукт, модель и кому это может быть нужно.
Затем сформируй портрет ЦА по поведенческим и мотивационным признакам.
Тематические интересы (например ИИ, криптовалюта, Web3) сами по себе не делают
человека целевой аудиторией, если проект не требует именно этого.

Нельзя делать выводы по полу, национальности, фотографии или возрасту.
Нельзя придумывать факты.

Верни ТОЛЬКО JSON без Markdown:
{
  "project_name": "короткое название",
  "project_summary": "1-3 простых предложения — что предлагает проект",
  "ideal_audience": "один чёткий абзац: кто наш идеальный человек",
  "green": [
    {"text": "сильный признак ЦА", "reason": "почему это важно"}
  ],
  "yellow": [
    {"text": "дополнительный или неоднозначный признак", "reason": "почему недостаточно одного этого признака"}
  ],
  "red": [
    {"text": "признак слабого соответствия", "reason": "почему"}
  ],
  "gray": [
    {"text": "что нельзя определить без дополнительных данных", "reason": "каких данных не хватает"}
  ],
  "search_signals": [
    {"text": "конкретный сигнал для поиска в Telegram-контактах", "reason": "как он связан с ЦА"}
  ],
  "do_not_assume": [
    "ошибочный критерий, который нельзя считать достаточным основанием"
  ]
}

Пиши простым русским языком. 4-8 сильных зелёных признаков лучше, чем 20 общих.
'''.strip()

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
    """Короткий машиночитаемый паспорт, по которому Неония сравнивает контакты."""
    profile = profile if isinstance(profile, dict) else {}
    payload = {
        "project_name": profile.get("project_name"),
        "ideal_audience": profile.get("ideal_audience"),
        "strong_signals": [x.get("text") for x in profile.get("green", [])],
        "secondary_signals": [x.get("text") for x in profile.get("yellow", [])],
        "weak_fit_signals": [x.get("text") for x in profile.get("red", [])],
        "search_signals": [x.get("text") for x in profile.get("search_signals", [])],
        "do_not_assume": profile.get("do_not_assume", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_target_profile(profile: dict):
    profile = profile if isinstance(profile, dict) else {}
    st.markdown(f"### 🎯 Портрет ЦА: {profile.get('project_name') or 'Проект'}")
    if profile.get("project_summary"):
        st.caption(profile["project_summary"])

    if profile.get("ideal_audience"):
        st.success(f"**Итоговый портрет ЦА:** {profile['ideal_audience']}")

    for key in ("green", "yellow", "red", "gray"):
        emoji, label = TARGET_STATUSES[key]
        items = profile.get(key) or []
        if not items:
            continue
        st.markdown(f"**{emoji} {label}**")
        for item in items:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            reason = item.get("reason", "") if isinstance(item, dict) else ""
            st.write(f"{emoji} {text}")
            if reason:
                st.caption(reason)

    if profile.get("search_signals"):
        with st.expander("🔍 По каким признакам Неония будет искать людей"):
            for item in profile["search_signals"]:
                st.write(f"• {item.get('text', '')}")
                if item.get("reason"):
                    st.caption(item["reason"])

    if profile.get("do_not_assume"):
        with st.expander("🚫 Что НЕ считать достаточным признаком ЦА"):
            for item in profile["do_not_assume"]:
                st.write(f"• {item}")


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
