from __future__ import annotations

"""
Agency W — Neonia Public Scout.

Назначение:
- искать потенциальных партнёров по ПУБЛИЧНЫМ сигналам потребности;
- режимы: open web и YouTube discovery;
- не обходить логины/закрытые группы/ограничения;
- не отправлять сообщения и не публиковать комментарии автоматически;
- выдавать доказательства, URL первоисточника и черновик следующего шага.

Этот модуль не заменяет VK/Telegram Scout. Он добавляет новые "глаза"
к единому мозгу Неонии.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

from neonia_candidate_policy import BUSINESS_GATE_RULES, apply_business_gate

UTC = timezone.utc

WEIGHTS = {
    "pain_strength": 25,
    "product_fit": 25,
    "timing": 20,
    "reachability": 15,
    "evidence_quality": 15,
}


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Не найдена переменная окружения {name}")
    return value


def _extract_text_and_sources(data: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    text_parts: list[str] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text_parts.append(str(content.get("text") or ""))
            for ann in content.get("annotations", []) or []:
                if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                    continue
                url = str(ann.get("url") or "").strip()
                if url and url not in seen:
                    seen.add(url)
                    sources.append({
                        "url": url,
                        "title": str(ann.get("title") or url).strip(),
                    })

    text = "\n".join(text_parts).strip()
    if not text:
        raise RuntimeError("Неония-разведчик не вернула результат.")
    return text, sources


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.I | re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _score_dimensions(dimensions: dict[str, Any]) -> int:
    total = 0.0
    for key, weight in WEIGHTS.items():
        try:
            value = float(dimensions.get(key, 0))
        except (TypeError, ValueError):
            value = 0.0
        value = max(0.0, min(5.0, value))
        total += value / 5.0 * weight
    return int(round(total))


def _public_profile_text(target_profile: dict[str, Any] | str) -> str:
    if isinstance(target_profile, dict):
        return json.dumps(target_profile, ensure_ascii=False, indent=2)
    return str(target_profile or "").strip()


def _parse_iso_date(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw[:10]
    try:
        return datetime.fromisoformat(raw).date()
    except Exception:
        return None


def _language_matches(candidate_language: str, allowed_languages: list[str]) -> bool:
    if not allowed_languages:
        return True
    value = str(candidate_language or "").strip().lower()
    aliases = {
        "русский": {"ru", "rus", "russian", "русский"},
        "немецкий": {"de", "deu", "german", "deutsch", "немецкий"},
        "английский": {"en", "eng", "english", "английский"},
    }
    allowed = set()
    for language in allowed_languages:
        allowed.update(aliases.get(str(language).strip().lower(), {str(language).strip().lower()}))
    return value in allowed


def discover_public_candidates(
    target_profile: dict[str, Any] | str,
    *,
    mode: str = "web",
    max_results: int = 8,
    language: str = "ru",
    allowed_languages: list[str] | None = None,
    max_age_days: int = 30,
    require_contactable: bool = True,
) -> dict[str, Any]:
    """
    mode:
      - web: открытый интернет;
      - youtube: только публичные YouTube-видео/каналы/обсуждения.

    Возвращает evidence-backed кандидатов. Ничего не отправляет наружу.
    """
    mode = str(mode or "web").strip().lower()
    if mode not in {"web", "youtube"}:
        raise ValueError("mode должен быть web или youtube")

    max_results = max(1, min(12, int(max_results)))
    max_age_days = max(1, min(365, int(max_age_days)))
    allowed_languages = allowed_languages or ["Русский"]
    api_key = _required_env("OPENAI_API_KEY")
    model = _env("NEONIA_PUBLIC_SCOUT_MODEL", "gpt-5-mini") or "gpt-5-mini"
    profile_text = _public_profile_text(target_profile)
    today = datetime.now(UTC).date().isoformat()

    youtube_rules = ""
    if mode == "youtube":
        youtube_rules = rf"""
ДОПОЛНИТЕЛЬНО ДЛЯ YOUTUBE:
- YouTube здесь — ПЛОЩАДКА ДЛЯ ВИДИМОСТИ, а не автоматический список партнёров.
- Ищи только видео не старше {max_age_days} дней.
- Язык видео/автора/обсуждения должен входить в: {", ".join(allowed_languages)}.
- ОБЯЗАТЕЛЬНО исключай ролик, если комментарии отключены, закрыты или это нельзя подтвердить.
- ОБЯЗАТЕЛЬНО исключай старые каналы без свежей активности.
- Не выдавай автора видео за потенциального партнёра только потому, что он говорит о MLM.
- Тренер/коуч/продавец курсов по рекрутингу — это обычно ПЛОЩАДКА/ЭКСПЕРТ, а не кандидат.
- comment_draft — содержательный комментарий к КОНКРЕТНОМУ видео, до 450 знаков:
  без ссылки, без "приходите ко мне", без массового рекламного шаблона,
  с одной полезной мыслью и, если естественно, одним вопросом.
- комментарий является ЧЕРНОВИКОМ и НЕ публикуется автоматически.
"""

    system_prompt = f"""
Ты — Неония-разведчик Агентства W.
Твоя задача — находить потенциальных партнёров не по внешнему сходству с ЦА,
а по конкретным ПУБЛИЧНЫМ сигналам актуальной деловой потребности.

Дата проверки: {today}

{BUSINESS_GATE_RULES}

ПРАВИЛА ИССЛЕДОВАНИЯ:
1. Используй только открытые публичные профессиональные/деловые сведения.
2. Не обходи логины, paywall, robots, закрытые группы и ограничения платформ.
3. Не используй чувствительные признаки и не делай выводы по фото, полу, возрасту,
   национальности, здоровью, религии, политике или личной жизни.
4. Поисковая выдача — только способ найти источник. Для TOP нужен открытый первоисточник.
5. Ищи прежде всего сигналы:
   - человек прямо ищет партнёров/лидеров/команду/клиентов;
   - жалуется на ручной рекрутинг, нехватку времени, слабый отклик;
   - описывает обходной ручной процесс;
   - ищет альтернативу инструменту/подходу;
   - есть свежий деловой триггер: запуск, расширение, набор команды.
6. Старое/не датированное свидетельство снижай по timing.
7. Если проблема уже решена, человек только продаёт похожий продукт или есть
   противоречащие факты — исключай либо явно указывай caution.
8. Не называй человека заинтересованным/готовым купить. Только:
   "потенциальный партнёр по публичным сигналам".
9. Лучше 3 сильных кандидата, чем 10 слабых.
10. ЖЁСТКИЙ ФИЛЬТР ЯЗЫКА: допустимы только {", ".join(allowed_languages)}.
11. ЖЁСТКИЙ ФИЛЬТР СВЕЖЕСТИ: публичный сигнал/активность не старше {max_age_days} дней.
12. ЖЁСТКИЙ ФИЛЬТР ДОСТУПНОСТИ: человек/площадка должны иметь реальный публичный путь взаимодействия.
13. Не путай "продаёт своё решение" с "ищет решение". Рекламный CTA автора сам по себе НЕ является болью.
14. Исключай тренеров, агентства, продавцов курсов и сервисов, если они только продают рекрутинг/лидогенерацию.
{youtube_rules}

ВЕРНИ ТОЛЬКО JSON-Массив. Для каждого:
{{
  "name": "...",
  "entity_url": "публичный профиль/сайт/канал",
  "source_url": "КОНКРЕТНЫЙ первоисточник сигнала",
  "source_title": "...",
  "signal_date": "YYYY-MM-DD",
  "activity_date": "YYYY-MM-DD",
  "candidate_language": "ru/de/en/другой код",
  "role_type": "network_marketer|mlm_leader|entrepreneur|trainer_vendor|content_creator|company_page|unknown",
  "intent_type": "seeking_partners|seeking_solution|complaining_about_problem|promoting_own_team|selling_own_solution|educational_content|unknown",
  "contactable": true,
  "comments_open": true,
  "result_kind": "person|venue",
  "pain_signal": "что конкретно публично показывает потребность",
  "why_fit": "почему совпадает с портретом ЦА",
  "why_now": "почему актуально сейчас",
  "business_relevant": true,
  "business_evidence": ["1-3 конкретных свидетельства"],
  "business_gate_reason": "...",
  "dimensions": {{
     "pain_strength": 0,
     "product_fit": 0,
     "timing": 0,
     "reachability": 0,
     "evidence_quality": 0
  }},
  "contact_route": "публичный релевантный путь контакта или пусто",
  "next_step": "один конкретный следующий шаг",
  "caution": "что проверить/не переоценить",
  "comment_draft": "для youtube — черновик комментария; для web пусто"
}}
""".strip()

    user_prompt = (
        "ПОРТРЕТ ЦЕЛЕВОЙ АУДИТОРИИ:\n"
        + profile_text
        + f"\n\nРежим поиска: {mode}. "
        + f"Найди не более {max_results} сильных результатов. "
        + f"Только языки: {', '.join(allowed_languages)}. "
        + f"Только активность/сигнал за последние {max_age_days} дней. "
        + "Каждый результат должен иметь открываемый первоисточник и реальный путь взаимодействия."
    )

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
            "store": False,
        },
        timeout=240,
    )
    response.raise_for_status()
    raw_text, cited_sources = _extract_text_and_sources(response.json())
    parsed = _extract_json_array(raw_text)

    results: list[dict[str, Any]] = []
    for item in parsed:
        dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        score = _score_dimensions(dimensions)
        score, _, evidence, gate_reason = apply_business_gate(item, score)

        source_url = str(item.get("source_url") or "").strip()
        entity_url = str(item.get("entity_url") or "").strip()
        if not source_url.startswith(("http://", "https://")):
            continue
        if not entity_url.startswith(("http://", "https://")):
            entity_url = source_url

        if mode == "youtube":
            low = source_url.lower()
            if "youtube.com/" not in low and "youtu.be/" not in low:
                continue

        business_relevant = bool(item.get("business_relevant") is True and evidence)
        if not business_relevant or score < 50:
            continue

        candidate_language = str(item.get("candidate_language") or "").strip().lower()
        if not _language_matches(candidate_language, allowed_languages):
            continue

        signal_date = _parse_iso_date(item.get("signal_date"))
        activity_date = _parse_iso_date(item.get("activity_date"))
        freshest_date = max(
            [d for d in (signal_date, activity_date) if d is not None],
            default=None,
        )
        if freshest_date is None:
            # Если Неония не может подтвердить свежесть — не выдаём в рабочий shortlist.
            continue
        age_days = (datetime.now(UTC).date() - freshest_date).days
        if age_days < 0 or age_days > max_age_days:
            continue

        contactable = bool(item.get("contactable") is True)
        contact_route = str(item.get("contact_route") or "").strip()
        if require_contactable and (not contactable or not contact_route):
            continue

        role_type = str(item.get("role_type") or "unknown").strip().lower()
        intent_type = str(item.get("intent_type") or "unknown").strip().lower()
        result_kind = str(item.get("result_kind") or ("venue" if mode == "youtube" else "person")).strip().lower()

        if mode == "youtube":
            # YouTube-режим выдаёт площадки для участия в разговоре, а не "готовых партнёров".
            if result_kind != "venue":
                continue
            if item.get("comments_open") is not True:
                continue
        else:
            # В web-режиме нужны люди, а не контент-продавцы или страницы компаний.
            if result_kind != "person":
                continue
            if role_type in {"trainer_vendor", "content_creator", "company_page"}:
                continue
            if intent_type in {"selling_own_solution", "educational_content", "unknown"}:
                continue

        results.append({
            "name": str(item.get("name") or "Без имени/названия")[:220],
            "entity_url": entity_url,
            "source_url": source_url,
            "source_title": str(item.get("source_title") or "")[:400],
            "signal_date": str(item.get("signal_date") or "")[:10],
            "activity_date": str(item.get("activity_date") or "")[:10],
            "candidate_language": candidate_language,
            "role_type": role_type,
            "intent_type": intent_type,
            "contactable": contactable,
            "comments_open": bool(item.get("comments_open") is True),
            "result_kind": result_kind,
            "age_days": age_days,
            "pain_signal": str(item.get("pain_signal") or "")[:1000],
            "why_fit": str(item.get("why_fit") or "")[:1000],
            "why_now": str(item.get("why_now") or "")[:800],
            "business_relevant": business_relevant,
            "business_evidence": evidence,
            "business_gate_reason": gate_reason,
            "dimensions": dimensions,
            "score": score,
            "contact_route": contact_route[:1200],
            "next_step": str(item.get("next_step") or "")[:700],
            "caution": str(item.get("caution") or "")[:700],
            "comment_draft": (
                str(item.get("comment_draft") or "")[:650]
                if mode == "youtube"
                else ""
            ),
            "source_type": mode,
            "checked_at": datetime.now(UTC).isoformat(),
        })

    results.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    return {
        "mode": mode,
        "candidates": results[:max_results],
        "web_citations": cited_sources,
        "checked_at": datetime.now(UTC).isoformat(),
        "note": "Черновики комментариев/контактов не отправлены автоматически.",
    }


def _call_text_model(system_prompt: str, user_prompt: str) -> str:
    api_key = _required_env("OPENAI_API_KEY")
    model = _env("NEONIA_PUBLIC_SCOUT_MODEL", "gpt-5-mini") or "gpt-5-mini"
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": str(system_prompt or ""),
            "input": str(user_prompt or ""),
            "store": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    text, _ = _extract_text_and_sources(response.json())
    return text.strip()


def draft_engagement_comment(
    candidate: dict[str, Any],
    target_profile: dict[str, Any] | str,
) -> str:
    """Готовит естественный публичный комментарий. Ничего не публикует."""
    source_type = str(candidate.get("source_type") or "web").lower()
    system_prompt = """
Ты — Неона, секретарь-референт Агентства W.
Подготовь ОДИН естественный публичный комментарий к найденной публикации.

Цель комментария — не продать и не рекламировать, а:
- показать, что автор действительно прочитан;
- добавить одну содержательную мысль;
- вызвать нормальный профессиональный разговор.

Правила:
- 1–3 коротких предложения;
- без ссылок, без спама, без обещаний дохода;
- не критиковать проект человека и не переманивать его напрямую;
- не притворяться знакомым;
- не выдумывать детали, которых нет в карточке;
- если уместно — закончить одним содержательным вопросом;
- не писать слова «ИИ-бот», «автоматический бот»;
- комментарий должен быть уникальным для данного публичного сигнала.
Верни только готовый комментарий без пояснений и кавычек.
""".strip()

    if source_type == "youtube":
        system_prompt += (
            "\nДля YouTube комментарий должен относиться именно к теме конкретного видео "
            "и быть не длиннее примерно 450 знаков."
        )
    else:
        system_prompt += (
            "\nДля LinkedIn/публичного поста тон профессиональный, человеческий, "
            "без канцелярита и без рекламного призыва."
        )

    request = {
        "target_profile": target_profile,
        "candidate": {
            "name": candidate.get("name"),
            "source_title": candidate.get("source_title"),
            "pain_signal": candidate.get("pain_signal"),
            "why_fit": candidate.get("why_fit"),
            "why_now": candidate.get("why_now"),
            "business_evidence": candidate.get("business_evidence"),
            "source_type": source_type,
        },
    }
    return _call_text_model(
        system_prompt,
        json.dumps(request, ensure_ascii=False, default=str),
    )[:650].strip()


def draft_first_message_for_public_lead(
    candidate: dict[str, Any],
    target_profile: dict[str, Any] | str,
    owner_name: str = "",
) -> str:
    """Готовит первое личное сообщение для найденного во внешнем источнике лида."""
    system_prompt = """
Ты — Неона, секретарь-референт владельца Агентства W.
Подготовь ОДНО первое личное сообщение человеку, найденному Неонией по публичному сигналу.

Правила:
- 2–4 коротких предложения;
- обращаться только по имени, если оно уверенно известно;
- честно опираться на конкретный публичный пост/сигнал;
- не утверждать, что человек заинтересован;
- не обещать доход, результат или лёгкие деньги;
- не критиковать его текущий проект;
- не писать массовую рекламную заготовку;
- цель первого сообщения — начать разговор и задать один лёгкий вопрос;
- если имя неясно, начать с «Здравствуйте!».
Верни только готовый текст без пояснений и кавычек.
""".strip()

    request = {
        "owner_name": owner_name,
        "target_profile": target_profile,
        "candidate": {
            "name": candidate.get("name"),
            "source_title": candidate.get("source_title"),
            "pain_signal": candidate.get("pain_signal"),
            "why_fit": candidate.get("why_fit"),
            "why_now": candidate.get("why_now"),
            "business_evidence": candidate.get("business_evidence"),
            "contact_route": candidate.get("contact_route"),
            "source_url": candidate.get("source_url"),
            "source_type": candidate.get("source_type"),
        },
    }
    return _call_text_model(
        system_prompt,
        json.dumps(request, ensure_ascii=False, default=str),
    )[:1200].strip()
