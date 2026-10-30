"""Three-step direct music workspace for Agency W."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import streamlit as st

MOODS = (
    "Спокойное",
    "Вдохновляющее",
    "Энергичное",
    "Торжественное",
    "Тёплое",
    "Современное",
)

DURATIONS = {
    "30 секунд": 30_000,
    "1 минута": 60_000,
    "2 минуты": 120_000,
}


def _secret(name):
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return str(value or os.getenv(name) or "").strip()


def _clean(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _prompt(description, kind, mood):
    description = _clean(description, 1200)
    if not description:
        return ""
    parts = [description]
    if mood:
        parts.append("Настроение: " + mood.lower())
    if kind == "Инструментальная":
        parts.append("Без вокала, только инструментальная музыка")
    else:
        parts.append("С вокалом")
    parts.append("Чистое современное звучание, выразительная мелодия, качественная аранжировка")
    return ". ".join(parts) + "."


def _music_jobs(owner_id, project, store):
    if not store or not project:
        return []
    try:
        jobs = store.list_jobs(owner_id, project["id"], limit=100)
    except Exception:
        return []
    return [
        job for job in jobs
        if isinstance(job, dict)
        and job.get("state") == "studio"
        and job.get("studio_tool") == "music"
        and job.get("studio_complete") is True
        and isinstance(job.get("assets"), list)
        and job["assets"]
    ]


def _title(description):
    value = _clean(description, 120)
    return value if value else "Новый музыкальный трек"


def _generate(owner_id, project, store, description, prompt, duration_ms, instrumental):
    from content_studio_catalog import save_studio_result
    from content_studio_music_provider import MusicProviderError, generate_music

    api_key = _secret("ELEVENLABS_API_KEY")
    if not api_key:
        raise MusicProviderError("Музыкальный двигатель ещё не подключён владельцем Агентства.")

    audio = generate_music(
        prompt,
        duration_ms=duration_ms,
        instrumental=instrumental,
        api_key=api_key,
    )
    job = save_studio_result(
        store,
        owner_id,
        project["id"],
        "music",
        _title(description),
        text=prompt,
        data=audio,
        mime_type="audio/mpeg",
        request_id="music-" + uuid.uuid4().hex,
    )
    if not isinstance(job, dict) or job.get("studio_complete") is not True:
        raise MusicProviderError("Музыка создана, но не удалось сохранить её в проекте. Повторите ещё раз.")
    st.session_state[f"studio_music_{owner_id}_selected_job"] = job["id"]
    return job


def _render_step_three(owner_id, project, store):
    st.markdown("### 3. Прослушайте и выберите")
    jobs = _music_jobs(owner_id, project, store)
    if not jobs:
        st.caption("Здесь появится созданная музыка.")
        return

    recent = jobs[:3]
    selected_id = st.session_state.get(f"studio_music_{owner_id}_selected_job")
    for index, job in enumerate(recent, 1):
        title = str((job.get("content") or {}).get("title") or f"Вариант {index}")
        with st.container(border=True):
            st.markdown(f"**{title}**")
            try:
                resolved = store.resolve_assets(owner_id, job["assets"][:1])[0]
                source = resolved.get("url") or resolved.get("path")
                if source:
                    st.audio(source, format=resolved.get("mime_type"))
            except Exception:
                st.caption("Предпросмотр временно недоступен, но трек сохранён.")
            label = "✓ Выбран для видео" if job["id"] == selected_id else "Использовать в видео"
            if st.button(
                label,
                type="primary" if job["id"] != selected_id else "secondary",
                width="stretch",
                key=f"studio_music_{owner_id}_{project['id']}_{job['id']}_select",
                disabled=job["id"] == selected_id,
            ):
                st.session_state[f"studio_music_{owner_id}_selected_job"] = job["id"]
                st.success("Музыка выбрана для видео.")
                st.rerun()


def render_music_tool(owner_id, project, store):
    st.markdown("## Музыка — 4 шага")
    st.caption("Рассказать желание → проверить смысл → утвердить → создать.")

    if not store or not project:
        st.info("Сначала выберите или сохраните проект Агентства W.")
        return

    key = f"studio_music_{owner_id}_{project['id']}"
    st.markdown("### 1. Расскажите, какую музыку Вы хотите")
    with st.form(key + "_intent_form"):
        description = st.text_area(
            "Описание музыки",
            placeholder=(
                "Например: хочу лёгкую музыку для ролика о свободном времени. "
                "В начале немного усталости, потом ощущение облегчения и воздуха. Без вокала и без пафоса."
            ),
            height=150,
            key=key + "_prompt",
            label_visibility="collapsed",
        )
        voice_wish = st.audio_input("🎙️ Или наговорите желание", key=key + "_voice_wish")
        type_col, mood_col, duration_col = st.columns(3, gap="large")
        with type_col:
            kind = st.radio(
                "Тип",
                ["Инструментальная", "С вокалом"],
                horizontal=True,
                key=key + "_kind",
            )
        with mood_col:
            mood = st.selectbox(
                "Настроение",
                MOODS,
                key=key + "_mood",
            )
        with duration_col:
            duration_label = st.selectbox(
                "Длительность",
                list(DURATIONS),
                index=1,
                key=key + "_duration",
            )
        understand = st.form_submit_button(
            "1. Понять мой замысел",
            type="primary",
            width="stretch",
        )
        st.caption("Музыкальный сервис на этом шаге не запускается.")

    extra = {
        "music_type": kind,
        "mood": mood,
        "duration_ms": DURATIONS[duration_label],
        "instrumental": kind == "Инструментальная",
    }
    if understand:
        from content_studio_intent_ui import begin_intent
        begin_intent(
            key,
            typed_text=description,
            audio=voice_wish,
            kind="music",
            project=project,
            extra=extra,
        )

    from content_studio_intent_ui import render_intent_review
    production = render_intent_review(
        key,
        kind="music",
        project=project,
        extra=extra,
    )

    from content_factory_billing import (
        BillingError,
        billing_enforced,
        format_eur,
        quote_music,
        release_usage,
        reserve_usage,
        settle_usage,
    )

    billing_quote = None
    if isinstance(production, dict) and billing_enforced():
        try:
            billing_quote = quote_music(
                DURATIONS[duration_label],
                api_key=_secret("ELEVENLABS_API_KEY"),
            )
            st.caption("Стоимость создания: " + format_eur(billing_quote["amount_cents"]))
        except BillingError as exc:
            st.caption("Стоимость сейчас не рассчитана: " + str(exc))

    if isinstance(production, dict):
        st.markdown("### 4. Создать музыку")
        create = st.button(
            "✨ Создать музыку по утверждённому замыслу",
            type="primary",
            width="stretch",
            key=key + "_create",
        )
        if create:
            billing_ref = "music:" + uuid.uuid4().hex
            reserved = False
            now = datetime.now(timezone.utc).isoformat()
            try:
                if billing_enforced():
                    if not billing_quote:
                        raise BillingError("Стоимость музыки не рассчитана. Создание остановлено, чтобы не списать неверную сумму.")
                    reserve_usage(
                        store,
                        owner_id,
                        project["id"],
                        billing_quote["amount_cents"],
                        reference=billing_ref,
                        kind="music",
                        label=billing_quote["label"],
                        now=now,
                    )
                    reserved = True
                prompt = str(production.get("production_prompt") or "").strip()
                if not prompt:
                    raise MusicProviderError("Профессиональный промт не подготовлен.")
                with st.spinner("Создаю музыку по утверждённому замыслу…"):
                    _generate(
                        owner_id,
                        project,
                        store,
                        description or str((production.get("approved_intent") or {}).get("main_idea") or "Музыкальный трек"),
                        prompt,
                        DURATIONS[duration_label],
                        kind == "Инструментальная",
                    )
                if reserved:
                    settle_usage(
                        store,
                        owner_id,
                        project["id"],
                        reference=billing_ref,
                        actual_cents=billing_quote["amount_cents"],
                        now=datetime.now(timezone.utc).isoformat(),
                    )
                st.success(
                    "Музыка готова."
                    + ((" Списано " + format_eur(billing_quote["amount_cents"]) + ".") if reserved else "")
                )
                st.rerun()
            except Exception as error:
                if reserved:
                    try:
                        release_usage(
                            store,
                            owner_id,
                            project["id"],
                            reference=billing_ref,
                            now=datetime.now(timezone.utc).isoformat(),
                        )
                    except Exception:
                        pass
                from content_studio_music_provider import MusicProviderError
                if isinstance(error, BillingError):
                    st.error(str(error))
                elif isinstance(error, MusicProviderError):
                    st.error(str(error))
                else:
                    st.error("Не удалось создать музыку. Повторная генерация автоматически не запускалась.")

    _render_step_three(owner_id, project, store)

    with st.expander("Техническое подключение — только для владельца", expanded=False):
        if _secret("ELEVENLABS_API_KEY"):
            st.success("Музыкальный двигатель подключён.")
        else:
            st.warning(
                "Нужно один раз добавить ELEVENLABS_API_KEY в Secrets приложения. "
                "После этого пользователи работают без технических ключей."
            )

