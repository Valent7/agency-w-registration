from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import streamlit as st


st.set_page_config(page_title="Agent Reach W", page_icon="👁️", layout="wide")

SOURCE_MAP = {
    "Интернет": "web",
    "YouTube": "youtube",
    "GitHub": "github",
}


def gate() -> bool:
    required = str(os.environ.get("AGENT_REACH_ACCESS_CODE") or "").strip()
    if not required:
        return True
    entered = st.text_input("Код доступа", type="password", placeholder="Введите код")
    if entered != required:
        st.info("Введите код доступа к тестовой версии.")
        return False
    return True


def run_reach(args: list[str], timeout: int = 120) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(
        ["agent-reach", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    payload = None
    for line in reversed(lines):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        detail = (proc.stderr or stdout or "Agent Reach не вернул ответ.").strip()
        raise RuntimeError(detail[-1600:])
    if not payload.get("ok"):
        err = payload.get("error") or {}
        message = err.get("message") or "Agent Reach не выполнил запрос."
        hint = err.get("hint")
        raise RuntimeError(message + (f"\n{hint}" if hint else ""))
    return payload


def items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("items")
    return value if isinstance(value, list) else []


def excerpt(value: Any, limit: int = 900) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_items(rows: list[dict[str, Any]], label: str) -> None:
    st.subheader(label)
    if not rows:
        st.caption("Ничего не найдено.")
        return
    for i, row in enumerate(rows, start=1):
        title = row.get("title") or row.get("url") or f"Результат {i}"
        st.markdown(f"**{i}. {title}**")
        meta = " · ".join(
            str(x) for x in (row.get("author"), row.get("published_at")) if x
        )
        if meta:
            st.caption(meta)
        if row.get("text"):
            st.write(excerpt(row.get("text")))
        if row.get("url"):
            st.link_button("Открыть источник", str(row["url"]))
        st.divider()


def make_report(query: str, grouped: dict[str, list[dict[str, Any]]]) -> str:
    out = [f"# Agent Reach — разведка", "", f"Запрос: {query}", ""]
    for source, rows in grouped.items():
        out += [f"## {source}", ""]
        for row in rows:
            title = row.get("title") or row.get("url") or "Источник"
            out.append(f"- **{title}**")
            if row.get("url"):
                out.append(f"  - {row['url']}")
            if row.get("author"):
                out.append(f"  - Автор: {row['author']}")
            if row.get("text"):
                out.append(f"  - {excerpt(row['text'], 1200)}")
        out.append("")
    return "\n".join(out)


st.markdown("# 👁️ Agent Reach")
st.caption("Интернет-разведчик Агентства W: поиск, чтение сайтов, YouTube и GitHub.")

if not gate():
    st.stop()

tab_search, tab_read, tab_youtube = st.tabs(
    ["🔎 Разведка", "📄 Прочитать ссылку", "▶️ YouTube субтитры"]
)

with tab_search:
    st.markdown("### Что хотим узнать?")
    query = st.text_area(
        "Запрос",
        placeholder="Например: что предпринимателей больше всего раздражает в рутинной работе и что они хотят передать ИИ?",
        height=100,
    )
    sources = st.multiselect(
        "Где искать",
        list(SOURCE_MAP),
        default=["Интернет", "YouTube", "GitHub"],
    )
    limit = st.select_slider("Результатов с каждой площадки", options=[3, 5, 7, 10], value=5)

    if st.button("🔎 Найти сигналы", type="primary", use_container_width=True):
        if not query.strip():
            st.error("Напишите, что нужно найти.")
        elif not sources:
            st.error("Выберите хотя бы одну площадку.")
        else:
            grouped: dict[str, list[dict[str, Any]]] = {}
            with st.status("Agent Reach исследует интернет…", expanded=True) as status:
                for source in sources:
                    channel = SOURCE_MAP[source]
                    st.write(f"Ищем: {source}")
                    try:
                        payload = run_reach(
                            [channel, "search", query.strip(), "--limit", str(limit), "--timeout", "90"],
                            timeout=105,
                        )
                        grouped[source] = items(payload)
                    except Exception as exc:
                        grouped[source] = []
                        st.warning(f"{source}: {exc}")
                status.update(label="Разведка завершена", state="complete")
            st.session_state["reach_query"] = query.strip()
            st.session_state["reach_results"] = grouped

    if st.session_state.get("reach_results"):
        grouped = st.session_state["reach_results"]
        for source, rows in grouped.items():
            render_items(rows, source)
        report = make_report(st.session_state.get("reach_query", ""), grouped)
        st.download_button(
            "⬇️ Скачать отчёт",
            data=report.encode("utf-8"),
            file_name="agent-reach-report.md",
            mime="text/markdown",
            use_container_width=True,
        )

with tab_read:
    st.markdown("### Прочитать страницу без лишнего мусора")
    target = st.text_input(
        "Ссылка",
        placeholder="https://example.com/article",
        key="read_target",
    )
    if st.button("📄 Прочитать", type="primary", use_container_width=True, key="read_btn"):
        if not target.strip():
            st.error("Вставьте ссылку.")
        else:
            try:
                with st.spinner("Agent Reach читает страницу…"):
                    payload = run_reach(["read", target.strip(), "--timeout", "90"], timeout=105)
                rows = items(payload)
                if not rows:
                    st.warning("Текст не получен.")
                else:
                    row = rows[0]
                    st.subheader(row.get("title") or "Результат")
                    if row.get("url"):
                        st.link_button("Открыть оригинал", str(row["url"]))
                    st.text_area("Текст", str(row.get("text") or ""), height=500)
            except Exception as exc:
                st.error(str(exc))

with tab_youtube:
    st.markdown("### Получить субтитры YouTube без просмотра ролика")
    yt_url = st.text_input(
        "Ссылка на YouTube",
        placeholder="https://www.youtube.com/watch?v=...",
        key="yt_url",
    )
    langs = st.text_input(
        "Языки субтитров",
        value="ru,en,en-orig",
        help="Agent Reach возьмёт первый доступный язык из списка.",
    )
    if st.button("▶️ Получить субтитры", type="primary", use_container_width=True, key="yt_btn"):
        if not yt_url.strip():
            st.error("Вставьте ссылку на YouTube.")
        else:
            try:
                with st.spinner("Получаем субтитры…"):
                    payload = run_reach(
                        ["youtube", "subtitle", yt_url.strip(), "--lang", langs.strip(), "--timeout", "120"],
                        timeout=135,
                    )
                rows = items(payload)
                if not rows:
                    st.warning("Субтитры не найдены.")
                else:
                    row = rows[0]
                    st.subheader(row.get("title") or "Субтитры")
                    text_value = str(row.get("text") or "")
                    st.text_area("Текст ролика", text_value, height=500)
                    st.download_button(
                        "⬇️ Скачать текст",
                        data=text_value.encode("utf-8"),
                        file_name="youtube-subtitles.txt",
                        mime="text/plain",
                        use_container_width=True,
                    )
            except Exception as exc:
                st.error(str(exc))

st.divider()
st.caption(
    "Сейчас подключены открытые источники. X, Reddit, Instagram и Facebook добавим после проверки базовой версии и настройки безопасной авторизации."
)
