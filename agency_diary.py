from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests
import streamlit as st


UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")

MORNING_LATIN = (
    "Quid hodie faciam, ut vita mea et vita aliorum meliores fiant?"
)
MORNING_RU = (
    "Что я сегодня сделаю, чтобы моя жизнь и жизнь окружающих стала лучше?"
)

EVENING_LATIN = (
    "Num vita mea et facta mea principiis moralibus congruerunt?"
)
EVENING_RU = (
    "Соответствовали ли моя жизнь и мои поступки принципам морали?"
)

# Одна латинская мысль держится всю календарную неделю (Пн–Вс).
# Авторов намеренно не указываем: это краткие традиционные латинские формулы.
WEEKLY_LATIN_THOUGHTS: tuple[tuple[str, str], ...] = (
    ("Age quod agis.", "Сосредоточься на том, что делаешь."),
    ("Per aspera ad astra.", "Через трудности — к звёздам."),
    ("Dum spiro, spero.", "Пока дышу — надеюсь."),
    ("Festina lente.", "Спеши медленно."),
    ("Gutta cavat lapidem.", "Капля точит камень."),
    ("Facta, non verba.", "Дела, а не слова."),
    ("Sapere aude.", "Осмелься быть мудрым."),
    ("Labor omnia vincit.", "Труд побеждает всё."),
    ("Fortes fortuna adiuvat.", "Смелым судьба помогает."),
    ("Amor vincit omnia.", "Любовь побеждает всё."),
    ("Ad meliora.", "К лучшему."),
    ("Esse quam videri.", "Быть, а не казаться."),
)


class DiaryStorageError(RuntimeError):
    pass


def _config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise DiaryStorageError(
            "Не найдены SUPABASE_URL или SUPABASE_SECRET_KEY."
        )
    return url, key


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _weekly_thought(day: date) -> tuple[str, str, str]:
    iso_year, iso_week, _ = day.isocalendar()
    index = (iso_week - 1) % len(WEEKLY_LATIN_THOUGHTS)
    latin, ru = WEEKLY_LATIN_THOUGHTS[index]
    return f"{iso_year}-W{iso_week:02d}", latin, ru


def _load_entry(
    owner_telegram_id: int,
    entry_date: date,
    reflection_type: str,
) -> dict[str, Any] | None:
    url, _ = _config()
    response = requests.get(
        f"{url}/rest/v1/agency_diary_entries",
        headers=_headers(),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "entry_date": f"eq.{entry_date.isoformat()}",
            "reflection_type": f"eq.{reflection_type}",
            "select": "*",
            "limit": 1,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise DiaryStorageError(
            "Не удалось загрузить запись дневника: "
            + response.text[:500]
        )
    rows = response.json()
    return rows[0] if isinstance(rows, list) and rows else None


def _save_entry(
    owner_telegram_id: int,
    entry_date: date,
    reflection_type: str,
    voice_transcript: str,
    entry_text: str,
) -> None:
    url, _ = _config()
    quote_week, quote_latin, quote_ru = _weekly_thought(entry_date)
    payload = {
        "owner_telegram_id": int(owner_telegram_id),
        "entry_date": entry_date.isoformat(),
        "reflection_type": reflection_type,
        "voice_transcript": str(voice_transcript or "").strip() or None,
        "entry_text": str(entry_text or "").strip(),
        "quote_week": quote_week,
        "quote_latin": quote_latin,
        "quote_translation": quote_ru,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    response = requests.post(
        f"{url}/rest/v1/agency_diary_entries"
        "?on_conflict=owner_telegram_id,entry_date,reflection_type",
        headers={
            **_headers("resolution=merge-duplicates,return=minimal"),
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise DiaryStorageError(
            "Не удалось сохранить запись дневника: "
            + response.text[:500]
        )


def _transcribe_audio(audio_file: Any) -> str:
    api_key = str(st.secrets.get("OPENAI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")

    audio_bytes = audio_file.getvalue()
    if not audio_bytes:
        raise RuntimeError("Аудиозапись пустая.")

    mime_type = str(getattr(audio_file, "type", "") or "audio/wav")
    file_name = str(getattr(audio_file, "name", "") or "reflection.wav")

    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (file_name, audio_bytes, mime_type)},
        data={
            "model": "gpt-4o-mini-transcribe",
            "language": "ru",
            "response_format": "json",
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            "Не удалось распознать голос: " + response.text[:500]
        )
    data = response.json()
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("Распознавание вернуло пустой текст.")
    return text


def _quote_block(latin: str, translation: str) -> None:
    st.markdown(
        f"""
        <div style="margin:0.25rem 0 1rem 0;">
          <div style="font-size:1.22rem;font-weight:700;line-height:1.45;">
            {latin}
          </div>
          <div style="font-size:0.93rem;opacity:0.76;line-height:1.45;margin-top:0.25rem;">
            {translation}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_reflection_page(
    owner_telegram_id: int,
    reflection_type: str,
) -> None:
    is_morning = reflection_type == "morning"
    title = "☀️ Утренние размышления" if is_morning else "🌙 Вечерние размышления"
    latin_prompt = MORNING_LATIN if is_morning else EVENING_LATIN
    ru_prompt = MORNING_RU if is_morning else EVENING_RU

    st.markdown(f"### {title}")

    selected_date = st.date_input(
        "Дата записи",
        value=datetime.now(BERLIN).date(),
        max_value=datetime.now(BERLIN).date(),
        key=f"diary_date_{reflection_type}_{owner_telegram_id}",
    )

    _, quote_latin, quote_ru = _weekly_thought(selected_date)

    st.caption("Вопрос дня")
    _quote_block(latin_prompt, ru_prompt)

    st.caption("Латинская мысль недели")
    _quote_block(quote_latin, quote_ru)

    entry = _load_entry(
        int(owner_telegram_id),
        selected_date,
        reflection_type,
    )

    voice_key = f"diary_voice_{owner_telegram_id}_{reflection_type}_{selected_date}"
    text_key = f"diary_text_{owner_telegram_id}_{reflection_type}_{selected_date}"
    transcript_key = (
        f"diary_transcript_{owner_telegram_id}_{reflection_type}_{selected_date}"
    )
    loaded_key = (
        f"diary_loaded_{owner_telegram_id}_{reflection_type}_{selected_date}"
    )

    if not st.session_state.get(loaded_key):
        st.session_state[text_key] = str((entry or {}).get("entry_text") or "")
        st.session_state[transcript_key] = str(
            (entry or {}).get("voice_transcript") or ""
        )
        st.session_state[loaded_key] = True

    st.markdown("#### 🎙 Сказать вслух")
    audio = st.audio_input(
        "Запишите размышления голосом",
        key=voice_key,
    )

    if audio is not None:
        if st.button(
            "📝 Расшифровать голос",
            type="primary",
            key=f"diary_transcribe_{owner_telegram_id}_{reflection_type}_{selected_date}",
        ):
            with st.spinner("Расшифровываю..."):
                try:
                    transcript = _transcribe_audio(audio)
                    previous = str(st.session_state.get(text_key) or "").strip()
                    combined = (
                        f"{previous}\n\n{transcript}".strip()
                        if previous
                        else transcript
                    )
                    st.session_state[transcript_key] = transcript
                    st.session_state[text_key] = combined
                    st.success(
                        "Готово. Текст можно исправить, дополнить или переписать."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    st.markdown("#### ✍️ Мои размышления")
    st.text_area(
        "Текст записи",
        key=text_key,
        height=260,
        placeholder=(
            "Говорите в микрофон или начинайте писать здесь. "
            "Текст можно свободно редактировать."
        ),
        label_visibility="collapsed",
    )

    cols = st.columns([1, 2])
    with cols[0]:
        if st.button(
            "💾 Сохранить",
            type="primary",
            key=f"diary_save_{owner_telegram_id}_{reflection_type}_{selected_date}",
            width="stretch",
        ):
            try:
                _save_entry(
                    int(owner_telegram_id),
                    selected_date,
                    reflection_type,
                    str(st.session_state.get(transcript_key) or ""),
                    str(st.session_state.get(text_key) or ""),
                )
                st.success("Запись сохранена.")
            except DiaryStorageError as exc:
                st.error(str(exc))

    if entry and str((entry or {}).get("entry_text") or "").strip():
        st.caption("Эта запись уже была сохранена ранее. Вы можете её редактировать.")


def render_agency_diary(owner_telegram_id: int) -> None:
    st.markdown("## 📖 Дневник")
    st.caption(
        "Две короткие точки дня: намерение утром и нравственная проверка вечером. "
        "Голос после распознавания не сохраняется — в дневнике остаётся только текст."
    )

    if "agency_diary_mode" not in st.session_state:
        st.session_state["agency_diary_mode"] = "morning"

    left, right = st.columns(2)
    with left:
        if st.button(
            "☀️ Утренние размышления",
            type="primary"
            if st.session_state["agency_diary_mode"] == "morning"
            else "secondary",
            width="stretch",
            key="open_diary_morning",
        ):
            st.session_state["agency_diary_mode"] = "morning"
            st.rerun()

    with right:
        if st.button(
            "🌙 Вечерние размышления",
            type="primary"
            if st.session_state["agency_diary_mode"] == "evening"
            else "secondary",
            width="stretch",
            key="open_diary_evening",
        ):
            st.session_state["agency_diary_mode"] = "evening"
            st.rerun()

    st.divider()

    _render_reflection_page(
        int(owner_telegram_id),
        str(st.session_state["agency_diary_mode"]),
    )
