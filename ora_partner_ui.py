from __future__ import annotations

import io
import os
import re
from typing import Any

import requests
import streamlit as st
from pypdf import PdfReader

try:
    from docx import Document
except Exception:
    Document = None


def _ora_api_url() -> str:
    return str(
        os.environ.get("ORA_PARTNER_API_URL")
        or st.secrets.get("ORA_PARTNER_API_URL", "")
        or ""
    ).rstrip("/")


def _ora_secret() -> str:
    return str(
        os.environ.get("ORA_PARTNER_API_SECRET")
        or st.secrets.get("ORA_PARTNER_API_SECRET", "")
        or ""
    ).strip()


def _headers() -> dict[str, str]:
    secret = _ora_secret()
    if not secret:
        raise RuntimeError("Связь с Орой ещё не настроена.")
    return {
        "X-Ora-Partner-Secret": secret,
        "Content-Type": "application/json",
    }


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _ora_api_url()
    if not base:
        raise RuntimeError("Адрес Оры не настроен.")
    response = requests.post(
        base + path,
        headers=_headers(),
        json=payload,
        timeout=90,
    )
    if response.status_code == 403:
        raise RuntimeError(
            "Ора может принимать базу только для чата, где вы являетесь администратором."
        )
    response.raise_for_status()
    return response.json()


def _clean_text(value: str) -> str:
    text = re.sub(r"\r\n?", "\n", str(value or ""))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_pdf(raw: bytes) -> str:
    reader = PdfReader(io.BytesIO(raw))
    parts = []
    for page in reader.pages:
        value = page.extract_text() or ""
        if value.strip():
            parts.append(value)
    return _clean_text("\n\n".join(parts))


def _extract_docx(raw: bytes) -> str:
    if Document is None:
        raise RuntimeError("Поддержка DOCX ещё не загружена на сервер.")
    document = Document(io.BytesIO(raw))
    return _clean_text("\n".join(p.text for p in document.paragraphs if p.text.strip()))


def _extract_upload(uploaded) -> str:
    if uploaded is None:
        return ""
    raw = uploaded.getvalue()
    name = str(uploaded.name or "").lower()
    if name.endswith(".pdf"):
        return _extract_pdf(raw)
    if name.endswith(".docx"):
        return _extract_docx(raw)
    if name.endswith((".txt", ".md")):
        return _clean_text(raw.decode("utf-8", errors="replace"))
    raise RuntimeError("Поддерживаются PDF, DOCX, TXT и MD.")


def render_ora_partner_center(owner_telegram_id: int, owner_name: str = "") -> None:
    owner_id = int(owner_telegram_id)
    st.markdown("### 🕰 Ора — цифровой сотрудник чата")
    st.caption(
        "Подключите свой Telegram-чат и загрузите материалы проекта. "
        "Ора использует только базу этого чата и не смешивает проекты партнёров."
    )

    chat_id = st.text_input(
        "Telegram ID вашего чата",
        key=f"ora_chat_id_{owner_id}",
        placeholder="-1001234567890",
        help=(
            "Ора проверит, что вы администратор этого чата. "
            "Позже мы сделаем выбор чата из списка — без ручного ID."
        ),
    ).strip()

    if not chat_id:
        st.info("Сначала укажите чат, который должна вести Ора.")
        return

    if not re.fullmatch(r"-?\d+", chat_id):
        st.warning("ID Telegram-чата должен состоять из цифр.")
        return

    try:
        docs_data = _post(
            "/partner/knowledge/list",
            {"owner_telegram_id": owner_id, "chat_id": chat_id},
        )
        documents = list(docs_data.get("documents") or [])
        st.success(f"Чат подтверждён. В базе знаний: {len(documents)} материалов.")
    except Exception as exc:
        st.error(str(exc))
        return

    with st.expander("➕ Добавить материал в базу знаний", expanded=not bool(documents)):
        mode = st.radio(
            "Как добавить знания",
            ["Вставить текст", "Загрузить файл"],
            horizontal=True,
            key=f"ora_kb_mode_{owner_id}_{chat_id}",
        )
        title = st.text_input(
            "Название материала",
            key=f"ora_kb_title_{owner_id}_{chat_id}",
            placeholder="Например: Презентация проекта / FAQ / правила работы",
        )

        if mode == "Вставить текст":
            body = st.text_area(
                "Текст",
                height=240,
                key=f"ora_kb_text_{owner_id}_{chat_id}",
                placeholder="Вставьте сюда информацию, которую Ора должна знать.",
            )
            extracted = _clean_text(body)
        else:
            upload = st.file_uploader(
                "PDF, DOCX, TXT или MD",
                type=["pdf", "docx", "txt", "md"],
                key=f"ora_kb_file_{owner_id}_{chat_id}",
            )
            try:
                extracted = _extract_upload(upload)
                if upload is not None and extracted:
                    st.caption(
                        f"Материал прочитан: примерно {len(extracted):,} знаков."
                    )
            except Exception as exc:
                st.error(f"Не удалось прочитать файл: {exc}")
                extracted = ""

        if st.button(
            "📥 Передать Оре",
            type="primary",
            use_container_width=True,
            key=f"ora_kb_add_{owner_id}_{chat_id}",
        ):
            clean_title = str(title or "").strip()
            if not clean_title:
                st.warning("Напишите название материала.")
            elif not extracted:
                st.warning("Материал пуст.")
            else:
                try:
                    result = _post(
                        "/partner/knowledge/add",
                        {
                            "owner_telegram_id": owner_id,
                            "chat_id": chat_id,
                            "title": clean_title,
                            "text": extracted,
                            "source": (
                                "agency-w-file-upload"
                                if mode == "Загрузить файл"
                                else "agency-w-text"
                            ),
                        },
                    )
                    chunks = int(
                        ((result.get("document") or {}).get("chunks") or 0)
                    )
                    st.success(
                        f"Готово. Ора добавила материал в базу знаний"
                        + (f" ({chunks} фрагментов)." if chunks else ".")
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Не удалось передать материал Оре: {exc}")

    if documents:
        st.markdown("#### 📚 Что Ора уже знает об этом проекте")
        for item in documents:
            with st.container(border=True):
                st.markdown(f"**{item.get('title') or 'Материал'}**")
                details = []
                if item.get("chunks") is not None:
                    details.append(f"фрагментов: {item.get('chunks')}")
                if item.get("created_at"):
                    details.append(str(item.get("created_at"))[:16].replace("T", " "))
                if details:
                    st.caption(" · ".join(details))

    st.divider()
    st.markdown("#### 🧠 Память Оры")
    st.write(
        "Разговоры этого чата сохраняются отдельно в постоянной Postgres-памяти. "
        "Ора использует их, чтобы помнить контекст и не начинать беседу заново."
    )
