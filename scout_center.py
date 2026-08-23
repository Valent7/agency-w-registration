from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
SCOUT_MODEL = "gpt-5-mini"
SCOUT_REPORT_TABLE = "agency_competitor_reports"

MISSION_FALLBACK = """
МИССИЯ АГЕНТСТВА W
Агентство W возвращает человеку время. ИИ забирает рутину и организует работу
виртуальной команды. Человек остаётся Директором: ставит цели, принимает решения,
строит отношения и создаёт своё настоящее.

Главный принцип: Не человек обслуживает программу. Программа служит человеку.
Тест миссии любой новой идеи:
1. Возвращает ли она человеку время?
2. Убирает ли лишнее ручное действие?
3. Улучшает ли реальный результат, а не просто добавляет функцию?
4. Помогает ли агентам работать как одной команде?
5. Остаётся ли человек Директором и сохраняет ключевые решения?
""".strip()


def _read_optional_text(filename: str, max_chars: int = 18000) -> str:
    path = APP_DIR / filename
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def _agency_context() -> str:
    parts = []
    core = _read_optional_text("AGENCY_W_CORE.md")
    competitive = _read_optional_text("AGENCY_W_COMPETITIVE_MAP_v1_20260823.md", 26000)
    person_card = _read_optional_text("AGENCY_W_PERSON_CARD_2_0_SPEC_20260823.md", 9000)

    parts.append(core or MISSION_FALLBACK)
    if competitive:
        parts.append("\nКАРТА КОНКУРЕНТНОГО РАЗВИТИЯ W:\n" + competitive)
    if person_card:
        parts.append("\nПРИНЦИП ПАМЯТИ ОТНОШЕНИЙ W:\n" + person_card)
    return "\n\n".join(parts)


def _headers() -> dict[str, str]:
    api_key = str(st.secrets.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_text_and_sources(data: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    text_parts: list[str] = []
    all_annotations: list[dict[str, Any]] = []

    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text = str(content.get("text") or "")
            offset = sum(len(part) for part in text_parts)
            if text_parts:
                offset += len(text_parts) - 1  # newlines inserted when joining
            text_parts.append(text)
            for ann in content.get("annotations", []) or []:
                if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                    continue
                shifted = dict(ann)
                try:
                    shifted["start_index"] = int(ann.get("start_index", 0)) + offset
                    shifted["end_index"] = int(ann.get("end_index", 0)) + offset
                except (TypeError, ValueError):
                    continue
                all_annotations.append(shifted)

    text = "\n".join(text_parts).strip()
    if not text:
        raise RuntimeError("Разведчик не сформировал отчёт.")

    unique: list[dict[str, str]] = []
    url_to_number: dict[str, int] = {}
    for ann in all_annotations:
        url = str(ann.get("url") or "").strip()
        if not url:
            continue
        if url not in url_to_number:
            url_to_number[url] = len(unique) + 1
            unique.append({
                "url": url,
                "title": str(ann.get("title") or url).strip(),
            })

    inserts: list[tuple[int, str]] = []
    seen_insert: set[tuple[int, int]] = set()
    for ann in all_annotations:
        url = str(ann.get("url") or "").strip()
        if not url or url not in url_to_number:
            continue
        try:
            end = int(ann.get("end_index", 0))
        except (TypeError, ValueError):
            continue
        number = url_to_number[url]
        marker_key = (end, number)
        if marker_key in seen_insert:
            continue
        seen_insert.add(marker_key)
        inserts.append((end, f" [S{number}]"))

    decorated = text
    for end, marker in sorted(inserts, key=lambda x: x[0], reverse=True):
        if 0 <= end <= len(decorated):
            decorated = decorated[:end] + marker + decorated[end:]

    return decorated, unique


def run_competitor_research(competitor_name: str, website: str = "") -> dict[str, Any]:
    competitor_name = str(competitor_name or "").strip()
    website = str(website or "").strip()
    if not competitor_name:
        raise ValueError("Введите название конкурента.")

    today = datetime.now().astimezone().date().isoformat()
    agency_context = _agency_context()

    system_prompt = f"""
Ты — Разведчик W, аналитик конкурентной разведки Агентства W.
Ты НЕ занимаешься промышленным шпионажем, взломом, сбором закрытых данных или
атаками на конкурентов. Ты работаешь только с открытыми источниками в интернете.

Твоя формула: НАЙТИ → ПРОВЕРИТЬ → СРАВНИТЬ → ПРЕДУПРЕДИТЬ → НАУЧИТЬ.

КОНТЕКСТ АГЕНТСТВА W:
{agency_context}

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. Сначала ищи официальные источники: официальный сайт, документацию, help center,
   changelog, pricing, release notes. Независимые источники используй как дополнение.
2. Не выдавай маркетинговое обещание за доказанную техническую возможность.
3. Если функции не удалось подтвердить, пиши: «не подтверждено в открытых источниках».
4. Не утверждай, что у конкурента чего-то НЕТ, если ты просто не нашёл это публично.
   Формулировка: «не найдено подтверждение в проверенных открытых источниках».
5. Не унижай конкурента и не пиши рекламных сравнений «мы лучше» без основания.
6. Сравнивай W только с тем, что дано во внутреннем контексте выше.
7. Каждая важная фактическая характеристика конкурента должна опираться на источник.
8. Если сведения противоречат друг другу, покажи противоречие и не выбирай удобную версию.
9. Отделяй: ПОДТВЕРЖДЕНО / МАРКЕТИНГОВОЕ ЗАЯВЛЕНИЕ / НЕ ПОДТВЕРЖДЕНО.
10. Никаких автоматических действий наружу. Только анализ и рекомендация Директору.

ФОРМАТ ОТЧЁТА — строго эти разделы:
# Разведка: <название>
**Проверено:** {today}

## 1. Кто это и какую задачу решает
Коротко, простым русским языком.

## 2. Подтверждённые возможности
Для каждого пункта укажи статус в начале: [ПОДТВЕРЖДЕНО] или [МАРКЕТИНГОВОЕ ЗАЯВЛЕНИЕ].
Не больше 8 действительно важных пунктов.

## 3. Где конкурент сейчас сильнее или зрелее W
Только конкретные зоны. Если сравнение нельзя подтвердить — так и напиши.

## 4. Где W отличается или имеет своё преимущество
Не рекламный лозунг, а архитектурное отличие по внутреннему контексту W.

## 5. Чему стоит научиться
До 5 идей. Для каждой: что взять как ПРИНЦИП, а не что скопировать буквально.

## 6. Что сознательно не копируем
То, что противоречит миссии W или создаёт лишнюю сложность/давление на человека.

## 7. Тест миссии W
Пять вопросов миссии. Для каждого: ДА / НЕТ / НЕЯСНО + одно предложение.

## 8. Факты для Неоны
2–4 спокойных проверенных факта, которые Неона сможет использовать, если человек скажет
«у меня уже есть <конкурент>». Не спорить и не обесценивать выбор человека.

## 9. Сигнал Стагириту
Одно короткое управленческое резюме: что отслеживать дальше и почему.

## 10. Рекомендация Директору
Ровно один итоговый статус из четырёх:
**РЕКОМЕНДУЮ В ДОРОЖНУЮ КАРТУ** / **СТОИТ ПРОТЕСТИРОВАТЬ** / **НАБЛЮДАТЬ** / **НЕ РЕКОМЕНДУЮ**.
После статуса — 2–4 предложения обоснования.
После раздела 10 отчёт ЗАКАНЧИВАЕТСЯ. Не предлагай дополнительные услуги, новые исследования,
чек-листы, отзывы или фразы «если хотите, могу...».

Пиши компактно. Не перечисляй всё подряд. Ценность отчёта — в проверенных выводах.
""".strip()

    site_hint = f"\nОфициальный сайт, указанный Директором: {website}" if website else ""
    user_prompt = (
        f"Проведи конкурентную разведку по компании/продукту: {competitor_name}."
        f"{site_hint}\n"
        "Проверь актуальную информацию в интернете на момент исследования. "
        "Если указан сайт, сначала используй его как ориентир, но подтверждай конкретные функции "
        "официальной документацией или help-страницами, когда это возможно."
    )

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers=_headers(),
        json={
            "model": SCOUT_MODEL,
            "instructions": system_prompt,
            "input": user_prompt,
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": "medium",
                }
            ],
            "store": False,
        },
        timeout=240,
    )
    response.raise_for_status()
    data = response.json()
    report, sources = _extract_text_and_sources(data)

    # Разведчик завершает отчёт рекомендацией Директору, без сервисных предложений в финале.
    report = re.split(
        r"\n(?:Если хотите|Если нужно|Могу также|Могу дополнительно)[,:]?",
        report,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].rstrip()

    verdict_match = re.search(
        r"\*\*(РЕКОМЕНДУЮ В ДОРОЖНУЮ КАРТУ|СТОИТ ПРОТЕСТИРОВАТЬ|НАБЛЮДАТЬ|НЕ РЕКОМЕНДУЮ)\*\*",
        report,
        flags=re.IGNORECASE,
    )
    verdict = verdict_match.group(1).upper() if verdict_match else "НАБЛЮДАТЬ"

    return {
        "competitor_name": competitor_name,
        "website": website,
        "report": report,
        "sources": sources,
        "verdict": verdict,
        "checked_at": datetime.now().astimezone().isoformat(),
    }


def _storage_headers() -> dict[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").strip()
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("Supabase не настроен для истории Разведчика.")
    # В Агентстве SUPABASE_SECRET_KEY используется как серверный apikey.
    # Не передаём его как Bearer: новые Supabase secret keys не являются JWT.
    return {
        "apikey": key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def save_report(owner_telegram_id: int, result: dict[str, Any]) -> tuple[bool, str]:
    base_url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    if not base_url:
        return False, "Supabase не настроен."

    try:
        response = requests.post(
            f"{base_url}/rest/v1/{SCOUT_REPORT_TABLE}",
            headers=_storage_headers(),
            json={
                "owner_telegram_id": int(owner_telegram_id),
                "competitor_name": str(result.get("competitor_name") or "")[:220],
                "website": str(result.get("website") or "")[:1200] or None,
                "report_markdown": str(result.get("report") or ""),
                "sources": result.get("sources") or [],
                "verdict": str(result.get("verdict") or "НАБЛЮДАТЬ")[:120],
                "checked_at": str(result.get("checked_at") or datetime.now().astimezone().isoformat()),
            },
            timeout=30,
        )
        if response.status_code == 404:
            return False, "Таблица истории Разведчика ещё не создана."
        if not response.ok:
            details = str(response.text or "").strip()[:700]
            return False, (
                f"Supabase вернул HTTP {response.status_code}. "
                + (details or "Подробности не переданы.")
            )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def load_reports(owner_telegram_id: int, limit: int = 8) -> list[dict[str, Any]]:
    base_url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    if not base_url:
        return []
    try:
        response = requests.get(
            f"{base_url}/rest/v1/{SCOUT_REPORT_TABLE}",
            headers=_storage_headers(),
            params={
                "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
                "select": "competitor_name,website,report_markdown,sources,verdict,checked_at,created_at",
                "order": "checked_at.desc",
                "limit": str(max(1, min(int(limit), 20))),
            },
            timeout=30,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        rows = response.json()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _source_label(source: dict[str, str]) -> str:
    url = str(source.get("url") or "").strip()
    title = str(source.get("title") or "").strip()
    domain = urlparse(url).netloc.replace("www.", "") if url else ""
    if title and domain and domain.lower() not in title.lower():
        return f"{title} — {domain}"
    return title or domain or url


def _render_sources(sources: list[dict[str, str]]) -> None:
    if not sources:
        st.caption("Источники не извлечены из ответа. Такой отчёт нельзя считать полностью проверенным.")
        return
    with st.expander(f"🔗 Источники ({len(sources)})"):
        for index, source in enumerate(sources, start=1):
            url = str(source.get("url") or "").strip()
            label = _source_label(source)
            if url:
                st.markdown(f"**S{index}.** [{label}]({url})")


def _render_report(result: dict[str, Any], key_prefix: str) -> None:
    st.markdown(str(result.get("report") or "Отчёт пуст."))
    _render_sources(result.get("sources") or [])
    report_name = re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "_", str(result.get("competitor_name") or "competitor")).strip("_")
    st.download_button(
        "⬇️ Скачать отчёт .md",
        data=str(result.get("report") or ""),
        file_name=f"SCOUT_{report_name}_{datetime.now().date().isoformat()}.md",
        mime="text/markdown",
        key=f"{key_prefix}_download",
    )


def render_scout_center(owner_telegram_id: int, owner_name: str) -> None:
    st.markdown("### 🛰️ Разведчик W")
    st.caption(
        "Анализирует только открытые источники: находит, проверяет, сравнивает и предлагает, "
        "чему W стоит научиться. Ничего сам не публикует и не меняет в Агентстве."
    )

    with st.container(border=True):
        st.markdown("#### 🔎 Исследовать конкурента")
        competitor_name = st.text_input(
            "Название компании или продукта",
            placeholder="Например: Sintra AI",
            key="scout_competitor_name",
        )
        website = st.text_input(
            "Официальный сайт — необязательно",
            placeholder="Например: https://sintra.ai",
            key="scout_competitor_website",
        )
        st.caption(
            "Один запуск использует веб-поиск OpenAI. Разведчик отдаёт приоритет официальным "
            "документам и помечает неподтверждённые заявления."
        )
        if st.button("🛰️ Провести разведку", type="primary", key="scout_run"):
            if not competitor_name.strip():
                st.warning("Введите название конкурента.")
            else:
                with st.spinner("Разведчик проверяет открытые источники и сравнивает их с W..."):
                    try:
                        result = run_competitor_research(competitor_name, website)
                        st.session_state["scout_last_report"] = result
                        saved, save_error = save_report(owner_telegram_id, result)
                        st.session_state["scout_last_saved"] = saved
                        st.session_state["scout_last_save_error"] = save_error
                    except requests.exceptions.HTTPError as exc:
                        details = ""
                        if exc.response is not None:
                            details = exc.response.text[:700]
                        st.error("Не удалось выполнить веб-разведку. " + (details or str(exc)))
                    except Exception as exc:
                        st.error(f"Не удалось выполнить разведку: {exc}")

    last = st.session_state.get("scout_last_report")
    if isinstance(last, dict):
        st.divider()
        st.markdown("### 📌 Последний отчёт")
        _render_report(last, "scout_last")
        if st.session_state.get("scout_last_saved"):
            st.success("Отчёт сохранён в истории Разведчика.")
        else:
            save_error = str(st.session_state.get("scout_last_save_error") or "").strip()
            st.warning("Отчёт получен, но пока не сохранён в истории.")
            if save_error:
                with st.expander("Техническая причина"):
                    st.code(save_error)
            if st.button(
                "💾 Сохранить последний отчёт в историю",
                type="primary",
                key="scout_retry_save",
            ):
                saved, retry_error = save_report(owner_telegram_id, last)
                st.session_state["scout_last_saved"] = saved
                st.session_state["scout_last_save_error"] = retry_error
                if saved:
                    st.success("Отчёт сохранён в истории Разведчика.")
                    st.rerun()
                else:
                    st.error("Сохранить отчёт пока не удалось. Откройте «Техническая причина».")

    recent = load_reports(owner_telegram_id)
    if recent:
        st.divider()
        st.markdown("### 🗂️ История разведки")
        for index, row in enumerate(recent):
            checked = str(row.get("checked_at") or row.get("created_at") or "")
            date_label = checked[:10] if checked else "без даты"
            title = str(row.get("competitor_name") or "Конкурент")
            verdict = str(row.get("verdict") or "")
            with st.expander(f"{date_label} · {title} · {verdict}"):
                _render_report(
                    {
                        "competitor_name": title,
                        "report": row.get("report_markdown") or "",
                        "sources": row.get("sources") if isinstance(row.get("sources"), list) else [],
                    },
                    f"scout_history_{index}",
                )

    st.caption(
        "Разведчик W v1.0 работает только по команде Директора. Автоматический еженедельный "
        "мониторинг и передача изменений Стагириту — следующий этап после тестирования качества."
    )
