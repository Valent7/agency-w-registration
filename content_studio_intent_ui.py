"""Reusable Streamlit UI for the universal intent gate."""
from __future__ import annotations

import hashlib
import json

import streamlit as st

from content_studio_intent import (
    IntentError,
    compile_production_prompt,
    human_summary,
    interpret_intent,
    transcribe_audio,
)


def _fingerprint(raw: str, kind: str, extra: dict | None) -> str:
    payload = json.dumps(
        {"raw": str(raw or ""), "kind": str(kind or ""), "extra": extra if isinstance(extra, dict) else {}},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audio_bytes(audio) -> tuple[bytes, str, str]:
    if audio is None:
        return b"", "", ""
    try:
        data = audio.getvalue()
    except Exception:
        data = b""
    name = str(getattr(audio, "name", "") or "wish.wav")
    mime = str(getattr(audio, "type", "") or "audio/wav")
    return data, name, mime


def begin_intent(key: str, *, typed_text: str, audio, kind: str, project: dict,
                 extra: dict | None = None) -> dict | None:
    """Interpret the current text/voice input once and persist it in session."""
    typed = str(typed_text or "").strip()
    audio_data, audio_name, audio_mime = _audio_bytes(audio)
    try:
        transcript = ""
        if audio_data:
            with st.spinner("Слушаю и выделяю смысл…"):
                transcript = transcribe_audio(audio_data, filename=audio_name, mime_type=audio_mime)
        pieces = []
        if typed:
            pieces.append(typed)
        if transcript:
            pieces.append("Голосовое описание:\n" + transcript)
        raw = "\n\n".join(pieces).strip()
        if not raw:
            raise IntentError("Напишите или наговорите, что Вы хотите получить.")
        with st.spinner("Выделяю главное из Вашего желания…"):
            intent = interpret_intent(raw, kind, project, extra=extra)
        fp = _fingerprint(raw, kind, extra)
        st.session_state[key + "_intent"] = intent
        st.session_state[key + "_intent_raw"] = raw
        st.session_state[key + "_intent_fp"] = fp
        st.session_state.pop(key + "_production", None)
        st.session_state.pop(key + "_approved", None)
        st.session_state.pop(key + "_correction", None)
        return intent
    except IntentError as exc:
        st.error(str(exc))
    except Exception:
        st.error("Не удалось разобрать замысел. Генерация не запускалась.")
    return None


def render_intent_review(key: str, *, kind: str, project: dict, extra: dict | None = None) -> dict | None:
    """Show human meaning first; compile provider prompt only after approval."""
    intent = st.session_state.get(key + "_intent")
    raw = str(st.session_state.get(key + "_intent_raw") or "")
    if not isinstance(intent, dict) or not raw:
        return None

    st.markdown("### 2. Проверьте, правильно ли я Вас поняла")
    with st.container(border=True):
        for label, value in human_summary(intent):
            st.markdown(f"**{label}:** {value}")
        if intent.get("unclear"):
            st.warning("Есть моменты, которые система считает неоднозначными. Если смысл всё равно передан верно — можно утвердить. Иначе напишите поправку ниже.")

    correction = st.text_area(
        "Что я поняла неправильно или что нужно уточнить?",
        placeholder="Например: главное не бизнес, а ощущение свободного вечера; героиня должна быть женщиной около 60 лет.",
        max_chars=3000,
        key=key + "_correction",
    )
    fix_col, approve_col = st.columns([1, 1], gap="medium")
    if fix_col.button("✏️ Учесть мою поправку", width="stretch", key=key + "_reinterpret"):
        if not correction.strip():
            st.info("Напишите, что именно нужно изменить в понимании.")
        else:
            try:
                revised_raw = raw + "\n\nУточнение владельца:\n" + correction.strip()
                with st.spinner("Уточняю смысл…"):
                    revised = interpret_intent(revised_raw, kind, project, extra=extra)
                st.session_state[key + "_intent"] = revised
                st.session_state[key + "_intent_raw"] = revised_raw
                st.session_state[key + "_intent_fp"] = _fingerprint(revised_raw, kind, extra)
                st.session_state.pop(key + "_production", None)
                st.session_state.pop(key + "_approved", None)
                st.session_state[key + "_correction"] = ""
                st.rerun()
            except IntentError as exc:
                st.error(str(exc))
            except Exception:
                st.error("Не удалось учесть поправку. Генерация не запускалась.")

    if approve_col.button("✅ Да, именно это", type="primary", width="stretch", key=key + "_approve_intent"):
        try:
            with st.spinner("Готовлю профессиональный промт для производства…"):
                production = compile_production_prompt(intent, project, extra=extra)
            st.session_state[key + "_production"] = production
            st.session_state[key + "_approved"] = st.session_state.get(key + "_intent_fp")
            st.rerun()
        except IntentError as exc:
            st.error(str(exc))
        except Exception:
            st.error("Не удалось подготовить производственный промт. Генерация не запускалась.")

    production = st.session_state.get(key + "_production")
    approved = st.session_state.get(key + "_approved")
    current_fp = st.session_state.get(key + "_intent_fp")
    if not isinstance(production, dict) or not approved or approved != current_fp:
        return None

    st.success("Замысел утверждён. Профессиональный промт готов.")
    with st.expander("Посмотреть производственный промт", expanded=False):
        st.write(production.get("production_prompt") or "")
        if production.get("production_plan"):
            st.markdown("**План производства**")
            for item in production["production_plan"]:
                st.write("• " + str(item))
        if production.get("quality_criteria"):
            st.markdown("**Что будем проверять в результате**")
            for item in production["quality_criteria"]:
                st.write("• " + str(item))
    return production


def clear_intent(key: str) -> None:
    for suffix in ("_intent", "_intent_raw", "_intent_fp", "_production", "_approved", "_correction"):
        st.session_state.pop(key + suffix, None)
