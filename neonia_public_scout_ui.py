from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import streamlit as st

from neonia_public_scout import (
    discover_public_candidates,
    draft_engagement_comment,
    draft_first_message_for_public_lead,
)

UTC = timezone.utc


def _stable_key(value: str) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:14]


def _lead_queue_key(owner_id: int) -> str:
    return f"neona_public_leads_{int(owner_id)}"


def _rejected_key(owner_id: int) -> str:
    return f"neonia_public_scout_rejected_{int(owner_id)}"


def _draft_key(owner_id: int, source_url: str) -> str:
    return f"neonia_public_comment_{int(owner_id)}_{_stable_key(source_url)}"


def _add_public_lead(owner_id: int, item: dict, comment_draft: str = "") -> None:
    key = _lead_queue_key(owner_id)
    leads = st.session_state.get(key, [])
    if not isinstance(leads, list):
        leads = []

    source_url = str(item.get("source_url") or "")
    stable_id = _stable_key(source_url or str(item.get("entity_url") or item.get("name") or ""))

    payload = {
        **item,
        "stable_id": stable_id,
        "comment_draft": str(comment_draft or item.get("comment_draft") or "").strip(),
        "status": "Передан Неоне",
        "transferred_at": datetime.now(UTC).isoformat(),
    }

    replaced = False
    for idx, existing in enumerate(leads):
        if not isinstance(existing, dict):
            continue
        if str(existing.get("stable_id") or "") == stable_id:
            leads[idx] = {**existing, **payload}
            replaced = True
            break
    if not replaced:
        leads.append(payload)

    st.session_state[key] = leads


def _mark_rejected(owner_id: int, source_url: str) -> None:
    key = _rejected_key(owner_id)
    values = st.session_state.get(key, [])
    if not isinstance(values, list):
        values = []
    if source_url and source_url not in values:
        values.append(source_url)
    st.session_state[key] = values


def _render_candidate(
    item: dict,
    *,
    target_profile: dict,
    owner_id: int,
    youtube: bool = False,
) -> None:
    source_url = str(item.get("source_url") or "")
    rejected = set(st.session_state.get(_rejected_key(owner_id), []) or [])
    if source_url and source_url in rejected:
        return

    score = int(item.get("score") or 0)
    name = str(item.get("name") or "Кандидат")
    card_id = _stable_key(source_url or name)
    draft_state_key = _draft_key(owner_id, source_url)

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

        existing_draft = str(
            st.session_state.get(draft_state_key)
            or item.get("comment_draft")
            or ""
        ).strip()

        # 4 действия, которые Директору нужны сразу на карточке.
        c1, c2, c3, c4 = st.columns(4)

        if source_url:
            c1.link_button(
                "🔗 Открыть источник",
                source_url,
                use_container_width=True,
            )
        else:
            c1.button(
                "🔗 Открыть источник",
                disabled=True,
                key=f"public_source_disabled_{owner_id}_{card_id}",
                use_container_width=True,
            )

        if c2.button(
            "💬 Подготовить комментарий",
            key=f"public_comment_{owner_id}_{card_id}",
            use_container_width=True,
        ):
            with st.spinner("Неона пишет комментарий именно к этой публикации..."):
                try:
                    existing_draft = draft_engagement_comment(item, target_profile)
                    st.session_state[draft_state_key] = existing_draft
                except Exception as exc:
                    st.error(f"Комментарий не подготовлен: {exc}")

        if c3.button(
            "➡️ Передать Неоне",
            key=f"public_to_neona_{owner_id}_{card_id}",
            use_container_width=True,
        ):
            _add_public_lead(owner_id, item, existing_draft)
            st.success("✅ Контакт передан Неоне. Откройте агента «Неона».")

        if c4.button(
            "🚫 Не подходит",
            key=f"public_reject_{owner_id}_{card_id}",
            use_container_width=True,
        ):
            _mark_rejected(owner_id, source_url)
            st.info("Контакт убран из текущей подборки.")
            st.rerun()

        existing_draft = str(st.session_state.get(draft_state_key) or existing_draft).strip()
        if existing_draft:
            st.text_area(
                "Комментарий Неоны — пока не опубликован",
                value=existing_draft,
                height=130,
                key=f"public_comment_text_{owner_id}_{card_id}",
            )
            if youtube:
                st.caption(
                    "Это черновик для YouTube. Он не публикуется автоматически."
                )
            else:
                st.caption(
                    "Сначала откройте первоисточник и убедитесь, что комментарий "
                    "точно соответствует публикации. Отправка остаётся за вами."
                )


def render_neonia_public_scout(target_profile: dict, owner_id: int) -> None:
    owner_id = int(owner_id)

    st.markdown("### 🌐 Неония-разведчик")
    st.caption(
        "Ищет не просто людей, а подтверждённые публичные сигналы потребности. "
        "Каждая находка должна иметь первоисточник."
    )

    web_tab, youtube_tab = st.tabs(["🌐 Интернет", "▶️ YouTube"])

    with web_tab:
        st.write(
            "Неония ищет открытые публикации, профили, сайты и обсуждения, "
            "где видна актуальная потребность, совпадающая с портретом ЦА."
        )
        if st.button(
            "🔎 Найти потенциальных партнёров в интернете",
            type="primary",
            key=f"neonia_web_scout_run_{owner_id}",
        ):
            with st.spinner("Неония проверяет открытые источники..."):
                try:
                    result = discover_public_candidates(
                        target_profile,
                        mode="web",
                        max_results=8,
                    )
                    st.session_state[f"neonia_web_scout_result_{owner_id}"] = result
                except Exception as exc:
                    st.error(f"Веб-разведка не выполнена: {exc}")

        result = st.session_state.get(f"neonia_web_scout_result_{owner_id}")
        if isinstance(result, dict):
            candidates = result.get("candidates") or []
            if not candidates:
                st.info("Сильных подтверждённых кандидатов в этом проходе не найдено.")
            for item in candidates:
                _render_candidate(
                    item,
                    target_profile=target_profile,
                    owner_id=owner_id,
                    youtube=False,
                )

    with youtube_tab:
        st.write(
            "Неония ищет релевантные публичные видео/каналы и готовит "
            "уникальный комментарий по содержанию конкретного видео."
        )
        st.warning(
            "Комментарии пока только готовятся. Автоматической публикации нет."
        )
        if st.button(
            "▶️ Найти разговоры на YouTube",
            type="primary",
            key=f"neonia_youtube_scout_run_{owner_id}",
        ):
            with st.spinner("Неония ищет подходящие YouTube-разговоры..."):
                try:
                    result = discover_public_candidates(
                        target_profile,
                        mode="youtube",
                        max_results=6,
                    )
                    st.session_state[f"neonia_youtube_scout_result_{owner_id}"] = result
                except Exception as exc:
                    st.error(f"YouTube-разведка не выполнена: {exc}")

        result = st.session_state.get(f"neonia_youtube_scout_result_{owner_id}")
        if isinstance(result, dict):
            candidates = result.get("candidates") or []
            if not candidates:
                st.info("Подходящих YouTube-сигналов в этом проходе не найдено.")
            for item in candidates:
                _render_candidate(
                    item,
                    target_profile=target_profile,
                    owner_id=owner_id,
                    youtube=True,
                )


def render_neona_public_leads(
    owner_id: int,
    target_profile: dict | None = None,
    owner_name: str = "",
) -> None:
    """Показывает Неоне людей, которых Директор передал из веб-разведки."""
    owner_id = int(owner_id)
    leads = st.session_state.get(_lead_queue_key(owner_id), [])
    if not isinstance(leads, list) or not leads:
        return

    st.markdown("### 🌐 Передано Неоне из веб-разведки")
    st.caption(
        "Это люди из LinkedIn, YouTube и других открытых источников. "
        "Агентство пока не отправляет им сообщения автоматически."
    )

    keep: list[dict] = []

    for lead in leads:
        if not isinstance(lead, dict):
            continue

        stable_id = str(lead.get("stable_id") or _stable_key(str(lead.get("source_url") or "")))
        name = str(lead.get("name") or "Контакт")
        source_url = str(lead.get("source_url") or "")
        draft_key = f"neona_public_first_message_{owner_id}_{stable_id}"

        with st.container(border=True):
            st.markdown(f"#### {name}")
            if lead.get("pain_signal"):
                st.write("**Сигнал:**", lead["pain_signal"])
            if lead.get("why_fit"):
                st.write("**Почему Неония обратила внимание:**", lead["why_fit"])
            if source_url:
                st.link_button(
                    "🔗 Открыть первоисточник",
                    source_url,
                    use_container_width=True,
                )

            comment = str(lead.get("comment_draft") or "").strip()
            if comment:
                with st.expander("💬 Подготовленный публичный комментарий"):
                    st.write(comment)

            first_message = str(st.session_state.get(draft_key) or "").strip()
            a1, a2 = st.columns(2)

            if a1.button(
                "✍️ Подготовить первое сообщение",
                key=f"neona_public_message_{owner_id}_{stable_id}",
                use_container_width=True,
            ):
                if not target_profile:
                    st.warning("Сначала нужен сохранённый портрет ЦА.")
                else:
                    with st.spinner("Неона готовит персональное первое сообщение..."):
                        try:
                            first_message = draft_first_message_for_public_lead(
                                lead,
                                target_profile,
                                owner_name=owner_name,
                            )
                            st.session_state[draft_key] = first_message
                        except Exception as exc:
                            st.error(f"Сообщение не подготовлено: {exc}")

            remove_clicked = a2.button(
                "🗑 Убрать из Неоны",
                key=f"neona_public_remove_{owner_id}_{stable_id}",
                use_container_width=True,
            )

            first_message = str(st.session_state.get(draft_key) or first_message).strip()
            if first_message:
                st.text_area(
                    "Первое сообщение — проверьте перед отправкой",
                    value=first_message,
                    height=140,
                    key=f"neona_public_message_text_{owner_id}_{stable_id}",
                )
                st.caption(
                    "Отправьте его вручную через тот канал, где найден человек. "
                    "Автоматической отправки внешнему контакту здесь нет."
                )

            if not remove_clicked:
                keep.append(lead)

    st.session_state[_lead_queue_key(owner_id)] = keep
