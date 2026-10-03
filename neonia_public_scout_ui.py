from __future__ import annotations

import streamlit as st

from neonia_public_scout import discover_public_candidates


def _render_candidate(item: dict, *, youtube: bool = False) -> None:
    score = int(item.get("score") or 0)
    name = str(item.get("name") or "Кандидат")
    with st.container(border=True):
        st.markdown(f"### {name} · {score}/100")
        if item.get("pain_signal"):
            st.write("**Публичный сигнал:**", item["pain_signal"])
        if item.get("why_fit"):
            st.write("**Почему подходит:**", item["why_fit"])
        if item.get("why_now"):
            st.write("**Почему сейчас:**", item["why_now"])

        evidence = item.get("business_evidence") or []
        if evidence:
            st.write("**Подтверждения:**")
            for value in evidence:
                st.write("•", value)

        if item.get("caution"):
            st.caption("Проверить: " + str(item["caution"]))

        source_url = str(item.get("source_url") or "")
        if source_url:
            st.markdown(f"[🔗 Открыть первоисточник]({source_url})")

        if youtube:
            draft = str(item.get("comment_draft") or "").strip()
            if draft:
                st.text_area(
                    "Черновик комментария Неонии — не опубликован",
                    value=draft,
                    height=130,
                    key=f"yt_draft_{abs(hash(source_url))}",
                )
                st.caption(
                    "Сначала прочитайте и при необходимости измените. "
                    "На этом этапе Неония ничего не публикует автоматически."
                )


def render_neonia_public_scout(target_profile: dict) -> None:
    st.markdown("### 🌐 Неония-разведчик")
    st.caption(
        "Ищет не просто людей, а подтверждённые публичные сигналы потребности. "
        "Первая версия работает по команде Директора, чтобы проверить качество и расходы."
    )

    web_tab, youtube_tab = st.tabs(["🌐 Интернет", "▶️ YouTube"])

    with web_tab:
        st.write(
            "Неония ищет открытые публикации, профили, сайты и обсуждения, "
            "где видна актуальная потребность, совпадающая с портретом ЦА."
        )
        if st.button("🔎 Найти потенциальных партнёров в интернете", type="primary", key="neonia_web_scout_run"):
            with st.spinner("Неония проверяет открытые источники..."):
                try:
                    result = discover_public_candidates(target_profile, mode="web", max_results=8)
                    st.session_state["neonia_web_scout_result"] = result
                except Exception as exc:
                    st.error(f"Веб-разведка не выполнена: {exc}")

        result = st.session_state.get("neonia_web_scout_result")
        if isinstance(result, dict):
            candidates = result.get("candidates") or []
            if not candidates:
                st.info("Сильных подтверждённых кандидатов в этом проходе не найдено.")
            for item in candidates:
                _render_candidate(item, youtube=False)

    with youtube_tab:
        st.write(
            "Неония ищет релевантные публичные видео/каналы и готовит "
            "уникальный комментарий по содержанию конкретного видео."
        )
        st.warning(
            "Первая версия только готовит комментарий. Публикация остаётся за человеком; "
            "автоматическую отправку подключаем отдельно после проверки качества."
        )
        if st.button("▶️ Найти разговоры на YouTube", type="primary", key="neonia_youtube_scout_run"):
            with st.spinner("Неония ищет подходящие YouTube-разговоры..."):
                try:
                    result = discover_public_candidates(target_profile, mode="youtube", max_results=6)
                    st.session_state["neonia_youtube_scout_result"] = result
                except Exception as exc:
                    st.error(f"YouTube-разведка не выполнена: {exc}")

        result = st.session_state.get("neonia_youtube_scout_result")
        if isinstance(result, dict):
            candidates = result.get("candidates") or []
            if not candidates:
                st.info("Подходящих YouTube-сигналов в этом проходе не найдено.")
            for item in candidates:
                _render_candidate(item, youtube=True)
