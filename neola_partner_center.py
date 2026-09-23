import base64
import hashlib
import json
import re
from urllib.parse import quote
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from neola_cabinet_map import NEOLA_MISSION, neola_cabinet_knowledge, neola_first_greeting


BERLIN_TZ = ZoneInfo("Europe/Berlin")
UTC_TZ = ZoneInfo("UTC")
MIN_LODGES = 5


def _now_iso():
    return datetime.now(UTC_TZ).isoformat()


def _supabase_headers(prefer=None):
    headers = {
        "apikey": st.secrets["SUPABASE_SECRET_KEY"],
        "Authorization": f"Bearer {st.secrets['SUPABASE_SECRET_KEY']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _supabase_url(path):
    return f"{st.secrets['SUPABASE_URL']}/rest/v1/{path.lstrip('/')}"


def _get_json(path, params=None, timeout=15):
    response = requests.get(
        _supabase_url(path),
        headers=_supabase_headers(),
        params=params or {},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _post_json(path, payload, prefer="return=representation", timeout=15):
    response = requests.post(
        _supabase_url(path),
        headers=_supabase_headers(prefer),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.text.strip():
        return []
    return response.json()


def _patch_json(path, params, payload, prefer="return=representation", timeout=15):
    response = requests.patch(
        _supabase_url(path),
        headers=_supabase_headers(prefer),
        params=params,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.text.strip():
        return []
    return response.json()


# ---------------------------------------------------------------------------
# Закрытая директорская очередь знаний Неолы
# ---------------------------------------------------------------------------

NEOLA_STUDY_STATUSES = (
    "Новый",
    "Ищем источник",
    "Источник найден",
    "Ответ подготовлен",
    "Ответ передан человеку",
)


def create_neola_study_question(
    partner_telegram_id,
    question,
    *,
    partner_name="",
    topic="",
    context="",
):
    """
    Внутренняя функция для Неолы.

    Создаёт карточку вопроса, который требует дополнительного изучения.
    Партнёру существование этой карточки НЕ сообщается. Карточку видит
    только корневой владелец Агентства W в директорском интерфейсе.

    На этом этапе функция подготовлена как безопасная точка интеграции.
    Автоматический вызов из диалога Неолы подключается отдельным шагом.
    """
    question_text = str(question or "").strip()
    if not question_text:
        return None

    payload = {
        "partner_telegram_id": int(partner_telegram_id),
        "partner_name": str(partner_name or "").strip()[:200],
        "question": question_text[:8000],
        "topic": str(topic or "").strip()[:300],
        "context": str(context or "").strip()[:8000],
        "status": "Новый",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    rows = _post_json(
        "neola_study_questions",
        payload,
        prefer="return=representation",
    )
    return rows[0] if rows else None


def _load_neola_study_questions(viewer_telegram_id, limit=300):
    """
    Возвращает карточки ТОЛЬКО корневому владельцу Агентства W.
    Даже если функцию ошибочно вызвать из партнёрского интерфейса,
    данные партнёру не выдаются.
    """
    if not _is_agency_owner(viewer_telegram_id):
        return []

    try:
        return _get_json(
            "neola_study_questions",
            params={
                "select": (
                    "id,partner_telegram_id,partner_name,question,topic,context,"
                    "status,source_title,source_reference,source_url,source_notes,"
                    "prepared_answer,director_note,created_at,updated_at,answered_at"
                ),
                "order": "created_at.desc",
                "limit": str(int(limit)),
            },
        )
    except requests.HTTPError as exc:
        # До применения SQL-миграции раздел остаётся безопасно пустым.
        if exc.response is not None and exc.response.status_code in {400, 404}:
            return []
        raise


def _update_neola_study_question(viewer_telegram_id, question_id, changes):
    """Редактировать карточку может только корневой владелец Агентства W."""
    if not _is_agency_owner(viewer_telegram_id):
        raise RuntimeError("Раздел «Вопросы на изучение» доступен только владельцу Агентства W.")

    allowed = {
        "status",
        "topic",
        "source_title",
        "source_reference",
        "source_url",
        "source_notes",
        "prepared_answer",
        "director_note",
        "answered_at",
    }
    payload = {
        key: value
        for key, value in dict(changes or {}).items()
        if key in allowed
    }
    if "status" in payload and payload["status"] not in NEOLA_STUDY_STATUSES:
        raise RuntimeError("Неизвестный статус карточки.")
    payload["updated_at"] = _now_iso()

    rows = _patch_json(
        "neola_study_questions",
        {"id": f"eq.{int(question_id)}"},
        payload,
        prefer="return=representation",
    )
    return rows[0] if rows else None


def _study_question_partner_label(row):
    name = str(row.get("partner_name") or "").strip()
    partner_id = row.get("partner_telegram_id")
    if name and partner_id:
        return f"{name} · Telegram {partner_id}"
    if name:
        return name
    if partner_id:
        return f"Telegram {partner_id}"
    return "Партнёр"


def _study_question_date_label(value):
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC_TZ)
        return parsed.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return raw


def render_neola_study_questions(viewer_telegram_id):
    """
    Закрытый кабинет роста знаний Неолы.
    Функция сама проверяет роль владельца и ничего не показывает партнёрам.
    """
    if not _is_agency_owner(viewer_telegram_id):
        return

    st.markdown("### 📚 Вопросы на изучение")
    st.caption(
        "Закрытый раздел Директора. Партнёры его не видят. "
        "Здесь остаются вопросы, для которых Неоле нужен более точный и надёжный ответ."
    )

    try:
        rows = _load_neola_study_questions(viewer_telegram_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить вопросы Неолы: {exc}")
        return

    counts = {status: 0 for status in NEOLA_STUDY_STATUSES}
    for row in rows:
        status = str(row.get("status") or "Новый")
        if status in counts:
            counts[status] += 1

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Новые", counts["Новый"])
    c2.metric("Ищем источник", counts["Ищем источник"])
    c3.metric(
        "Готовится ответ",
        counts["Источник найден"] + counts["Ответ подготовлен"],
    )
    c4.metric("Передано человеку", counts["Ответ передан человеку"])

    filter_value = st.segmented_control(
        "Статус",
        ["Все", *NEOLA_STUDY_STATUSES],
        default="Все",
        key=f"neola_study_filter_{viewer_telegram_id}",
    )

    filtered = [
        row
        for row in rows
        if filter_value == "Все"
        or str(row.get("status") or "Новый") == filter_value
    ]

    if not filtered:
        st.info(
            "В этой категории вопросов пока нет. "
            "Когда Неола зафиксирует вопрос на изучение, он появится здесь."
        )
        return

    for row in filtered:
        question_id = int(row.get("id") or 0)
        if not question_id:
            continue

        status = str(row.get("status") or "Новый")
        topic = str(row.get("topic") or "").strip()
        question = str(row.get("question") or "").strip()
        created_label = _study_question_date_label(row.get("created_at"))
        partner_label = _study_question_partner_label(row)

        title = f"{status} · {topic or 'Тема не определена'} · {partner_label}"
        with st.expander(title, expanded=status == "Новый"):
            st.markdown("**Вопрос партнёра**")
            st.write(question or "—")
            st.caption(f"Получен: {created_label}")

            context = str(row.get("context") or "").strip()
            if context:
                with st.expander("Контекст диалога", expanded=False):
                    st.write(context)

            status_options = list(NEOLA_STUDY_STATUSES)
            status_index = (
                status_options.index(status)
                if status in status_options
                else 0
            )
            selected_status = st.selectbox(
                "Статус работы",
                status_options,
                index=status_index,
                key=f"neola_study_status_{question_id}",
            )
            topic_value = st.text_input(
                "Тема",
                value=topic,
                key=f"neola_study_topic_{question_id}",
                placeholder="Например: устойчивость, отказ, доминанта, смысл",
            )

            st.markdown("**Источник ответа**")
            source_title = st.text_input(
                "Автор / название",
                value=str(row.get("source_title") or ""),
                key=f"neola_study_source_title_{question_id}",
                placeholder="Например: Виктор Франкл — «Сказать жизни „Да!“»",
            )
            source_reference = st.text_input(
                "Глава / статья / страницы / DOI",
                value=str(row.get("source_reference") or ""),
                key=f"neola_study_source_ref_{question_id}",
                placeholder="Точное место в источнике, если известно",
            )
            source_url = st.text_input(
                "Ссылка на источник",
                value=str(row.get("source_url") or ""),
                key=f"neola_study_source_url_{question_id}",
                placeholder="https://…",
            )
            source_notes = st.text_area(
                "Что именно подтверждает источник",
                value=str(row.get("source_notes") or ""),
                key=f"neola_study_source_notes_{question_id}",
                height=100,
            )

            prepared_answer = st.text_area(
                "Подготовленный ответ Неолы",
                value=str(row.get("prepared_answer") or ""),
                key=f"neola_study_answer_{question_id}",
                height=150,
                placeholder=(
                    "Ответ, который Неола сможет озвучить партнёру после проверки."
                ),
            )
            director_note = st.text_area(
                "Заметка Директора",
                value=str(row.get("director_note") or ""),
                key=f"neola_study_director_note_{question_id}",
                height=90,
            )

            if st.button(
                "💾 Сохранить карточку",
                type="primary",
                key=f"neola_study_save_{question_id}",
            ):
                answered_at = row.get("answered_at")
                if (
                    selected_status == "Ответ передан человеку"
                    and not answered_at
                ):
                    answered_at = _now_iso()

                try:
                    _update_neola_study_question(
                        viewer_telegram_id,
                        question_id,
                        {
                            "status": selected_status,
                            "topic": topic_value.strip(),
                            "source_title": source_title.strip(),
                            "source_reference": source_reference.strip(),
                            "source_url": source_url.strip(),
                            "source_notes": source_notes.strip(),
                            "prepared_answer": prepared_answer.strip(),
                            "director_note": director_note.strip(),
                            "answered_at": answered_at,
                        },
                    )
                    st.success("Карточка сохранена.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Не удалось сохранить карточку: {exc}")




# ---------------------------------------------------------------------------
# Закрытая база знаний Неолы
# ---------------------------------------------------------------------------

NEOLA_KNOWLEDGE_BUCKET = "neola-knowledge"
NEOLA_KNOWLEDGE_SOURCE_TYPES = (
    "Внутренний документ Агентства W",
    "Книга / первоисточник",
    "Научная статья",
    "Научный обзор / метаанализ",
    "Философский труд",
    "Практический материал",
    "Другое",
)
NEOLA_KNOWLEDGE_TRUST_LEVELS = (
    "Внутренний стандарт Агентства W",
    "Первоисточник автора",
    "Рецензируемое научное исследование",
    "Научный обзор / метаанализ",
    "Философский источник",
    "Требует дополнительной проверки",
)
NEOLA_KNOWLEDGE_STATUSES = ("Одобрен", "Черновик", "Архив")


def _storage_object_url(bucket, path):
    base = str(st.secrets["SUPABASE_URL"]).rstrip("/")
    safe_bucket = quote(str(bucket), safe="")
    safe_path = quote(str(path), safe="/")
    return f"{base}/storage/v1/object/{safe_bucket}/{safe_path}"


def _upload_neola_knowledge_file(viewer_telegram_id, uploaded_file):
    """Сохраняет оригинал источника в приватный Supabase Storage."""
    if not _is_agency_owner(viewer_telegram_id):
        raise RuntimeError("База знаний доступна только владельцу Агентства W.")

    file_bytes = uploaded_file.getvalue()
    if not file_bytes:
        raise RuntimeError("Файл пуст.")
    if len(file_bytes) > 50 * 1024 * 1024:
        raise RuntimeError("Файл слишком большой. Максимум 50 МБ.")

    original_name = str(getattr(uploaded_file, "name", "source.bin") or "source.bin")

    # Supabase Storage object keys should stay ASCII-safe.  Keep the human
    # filename (including Cyrillic) only as metadata and use a stable ASCII
    # object name for the private Storage path.
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "bin"
    extension = re.sub(r"[^0-9a-z]+", "", extension)[:12] or "bin"
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    stamp = datetime.now(UTC_TZ).strftime("%Y%m%dT%H%M%S%fZ")
    storage_path = f"sources/{stamp}_{content_hash[:16]}.{extension}"
    mime_type = str(getattr(uploaded_file, "type", "") or "application/octet-stream")

    response = requests.post(
        _storage_object_url(NEOLA_KNOWLEDGE_BUCKET, storage_path),
        headers={
            "apikey": st.secrets["SUPABASE_SECRET_KEY"],
            "Authorization": f"Bearer {st.secrets['SUPABASE_SECRET_KEY']}",
            "Content-Type": mime_type,
            "x-upsert": "false",
        },
        data=file_bytes,
        timeout=120,
    )
    response.raise_for_status()
    return {
        "storage_bucket": NEOLA_KNOWLEDGE_BUCKET,
        "storage_path": storage_path,
        "original_filename": original_name[:255],
        "mime_type": mime_type[:150],
        "file_size_bytes": len(file_bytes),
        "content_sha256": content_hash,
    }


def _find_neola_knowledge_source_by_hash(viewer_telegram_id, content_sha256):
    if not _is_agency_owner(viewer_telegram_id) or not content_sha256:
        return None
    rows = _get_json(
        "neola_knowledge_sources",
        params={
            "content_sha256": f"eq.{content_sha256}",
            "select": "id,title,author,original_filename,status",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def create_neola_knowledge_source(
    viewer_telegram_id,
    *,
    title,
    author="",
    source_type="Книга / первоисточник",
    knowledge_area="",
    publication_year=None,
    trust_level="Первоисточник автора",
    purpose="",
    notes="",
    status="Одобрен",
    uploaded_file=None,
):
    """Создаёт утверждённый Директором источник знаний Неолы."""
    if not _is_agency_owner(viewer_telegram_id):
        raise RuntimeError("Добавлять знания Неоле может только владелец Агентства W.")

    clean_title = str(title or "").strip()
    if not clean_title:
        raise RuntimeError("Укажите название источника.")
    if source_type not in NEOLA_KNOWLEDGE_SOURCE_TYPES:
        raise RuntimeError("Неизвестный тип источника.")
    if trust_level not in NEOLA_KNOWLEDGE_TRUST_LEVELS:
        raise RuntimeError("Неизвестный уровень доверия.")
    if status not in NEOLA_KNOWLEDGE_STATUSES:
        raise RuntimeError("Неизвестный статус источника.")

    file_meta = {}
    if uploaded_file is not None:
        raw = uploaded_file.getvalue()
        content_hash = hashlib.sha256(raw).hexdigest() if raw else ""
        duplicate = _find_neola_knowledge_source_by_hash(
            viewer_telegram_id, content_hash
        ) if content_hash else None
        if duplicate:
            raise RuntimeError(
                "Этот файл уже есть в базе знаний: "
                + str(duplicate.get("title") or duplicate.get("original_filename") or "источник")
            )
        file_meta = _upload_neola_knowledge_file(
            viewer_telegram_id, uploaded_file
        )

    year_value = None
    try:
        if publication_year not in {None, "", 0}:
            year_value = int(publication_year)
    except (TypeError, ValueError):
        year_value = None

    payload = {
        "title": clean_title[:500],
        "author": str(author or "").strip()[:300] or None,
        "source_type": source_type,
        "knowledge_area": str(knowledge_area or "").strip()[:300] or None,
        "publication_year": year_value,
        "trust_level": trust_level,
        "purpose": str(purpose or "").strip()[:4000] or None,
        "notes": str(notes or "").strip()[:8000] or None,
        "status": status,
        "processing_status": (
            "Файл сохранён — ожидает индексации"
            if file_meta else "Источник без файла — ожидает наполнения"
        ),
        "added_by": int(viewer_telegram_id),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        **file_meta,
    }
    rows = _post_json(
        "neola_knowledge_sources",
        payload,
        prefer="return=representation",
        timeout=30,
    )
    return rows[0] if rows else None


def _load_neola_knowledge_sources(viewer_telegram_id, limit=500):
    if not _is_agency_owner(viewer_telegram_id):
        return []
    try:
        return _get_json(
            "neola_knowledge_sources",
            params={
                "select": (
                    "id,title,author,source_type,knowledge_area,publication_year,"
                    "trust_level,purpose,notes,status,processing_status,"
                    "storage_bucket,storage_path,original_filename,mime_type,"
                    "file_size_bytes,content_sha256,added_by,created_at,updated_at,indexed_at"
                ),
                "order": "created_at.desc",
                "limit": str(int(limit)),
            },
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {400, 404}:
            return []
        raise


def _update_neola_knowledge_source(viewer_telegram_id, source_id, changes):
    if not _is_agency_owner(viewer_telegram_id):
        raise RuntimeError("База знаний доступна только владельцу Агентства W.")
    allowed = {
        "title", "author", "source_type", "knowledge_area",
        "publication_year", "trust_level", "purpose", "notes", "status",
    }
    payload = {
        key: value
        for key, value in dict(changes or {}).items()
        if key in allowed
    }
    if payload.get("source_type") and payload["source_type"] not in NEOLA_KNOWLEDGE_SOURCE_TYPES:
        raise RuntimeError("Неизвестный тип источника.")
    if payload.get("trust_level") and payload["trust_level"] not in NEOLA_KNOWLEDGE_TRUST_LEVELS:
        raise RuntimeError("Неизвестный уровень доверия.")
    if payload.get("status") and payload["status"] not in NEOLA_KNOWLEDGE_STATUSES:
        raise RuntimeError("Неизвестный статус источника.")
    payload["updated_at"] = _now_iso()
    rows = _patch_json(
        "neola_knowledge_sources",
        {"id": f"eq.{int(source_id)}"},
        payload,
        prefer="return=representation",
    )
    return rows[0] if rows else None


def _knowledge_file_size_label(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def render_neola_knowledge_base(viewer_telegram_id):
    """Закрытая библиотека знаний Неолы. Партнёры её не видят."""
    if not _is_agency_owner(viewer_telegram_id):
        return

    st.markdown("### 🧠 База знаний Неолы")
    st.caption(
        "Закрытая библиотека Директора. Здесь хранятся только те источники, "
        "которые вы разрешили Неоле использовать. Партнёры эту библиотеку не видят."
    )

    try:
        rows = _load_neola_knowledge_sources(viewer_telegram_id)
    except Exception as exc:
        st.error(f"Не удалось загрузить базу знаний Неолы: {exc}")
        return

    approved = sum(1 for row in rows if row.get("status") == "Одобрен")
    scientific = sum(
        1 for row in rows
        if row.get("source_type") in {"Научная статья", "Научный обзор / метаанализ"}
    )
    with_files = sum(1 for row in rows if row.get("storage_path"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Источников", len(rows))
    c2.metric("Одобрено", approved)
    c3.metric("Научные", scientific)
    c4.metric("Файлы сохранены", with_files)

    with st.expander("➕ Добавить источник", expanded=not bool(rows)):
        with st.form(f"neola_knowledge_add_{int(viewer_telegram_id)}", clear_on_submit=False):
            title = st.text_input(
                "Название *",
                placeholder="Например: Сказать жизни «Да!»",
            )
            author = st.text_input(
                "Автор",
                placeholder="Например: Виктор Франкл",
            )
            col_a, col_b = st.columns(2)
            source_type = col_a.selectbox(
                "Тип источника",
                NEOLA_KNOWLEDGE_SOURCE_TYPES,
                index=1,
            )
            trust_level = col_b.selectbox(
                "Уровень доверия / статус знания",
                NEOLA_KNOWLEDGE_TRUST_LEVELS,
                index=1,
            )
            col_c, col_d = st.columns(2)
            knowledge_area = col_c.text_input(
                "Область знаний",
                placeholder="Смысл, устойчивость, внимание, наставничество…",
            )
            publication_year = col_d.number_input(
                "Год издания / публикации",
                min_value=0,
                max_value=2200,
                value=0,
                step=1,
                help="Можно оставить 0, если год неважен или неизвестен.",
            )
            purpose = st.text_area(
                "Для чего этот источник нужен Неоле",
                placeholder="Какие вопросы и ситуации он помогает разбирать.",
                height=90,
            )
            notes = st.text_area(
                "Заметка Директора",
                placeholder="Границы применения, важные оговорки, что считать особенно ценным.",
                height=90,
            )
            uploaded = st.file_uploader(
                "Файл источника",
                type=["pdf", "txt", "md", "docx"],
                help="PDF, DOCX, TXT или MD. До 50 МБ. Файл хранится в приватном хранилище.",
            )
            status = st.selectbox(
                "Статус",
                NEOLA_KNOWLEDGE_STATUSES,
                index=0,
            )
            submitted = st.form_submit_button(
                "📥 Добавить в базу знаний",
                type="primary",
            )

        if submitted:
            try:
                with st.spinner("Сохраняю источник в закрытой библиотеке Неолы..."):
                    create_neola_knowledge_source(
                        viewer_telegram_id,
                        title=title,
                        author=author,
                        source_type=source_type,
                        knowledge_area=knowledge_area,
                        publication_year=publication_year,
                        trust_level=trust_level,
                        purpose=purpose,
                        notes=notes,
                        status=status,
                        uploaded_file=uploaded,
                    )
                st.success("Источник добавлен в базу знаний Неолы.")
                st.rerun()
            except Exception as exc:
                st.error(f"Не удалось добавить источник: {exc}")

    if not rows:
        st.info(
            "База пока пуста. Начнём с Конституции Неолы, архитектуры Агентства W, "
            "затем добавим выбранные вами источники Франкла и Ухтомского."
        )
        return

    st.caption(
        "Сейчас источники надёжно сохраняются и каталогизируются. "
        "Интеллектуальный поиск по тексту и автоматическое цитирование подключим следующим этапом."
    )

    status_filter = st.segmented_control(
        "Показать",
        ["Все", *NEOLA_KNOWLEDGE_STATUSES],
        default="Все",
        key=f"neola_knowledge_status_filter_{viewer_telegram_id}",
    )
    filtered = [
        row for row in rows
        if status_filter == "Все" or row.get("status") == status_filter
    ]

    for row in filtered:
        source_id = int(row.get("id") or 0)
        if not source_id:
            continue
        title_value = str(row.get("title") or "Без названия")
        author_value = str(row.get("author") or "").strip()
        label = title_value + (f" · {author_value}" if author_value else "")
        with st.expander(label, expanded=False):
            st.caption(
                f"{row.get('source_type') or 'Источник'} · "
                f"{row.get('trust_level') or '—'} · "
                f"статус: {row.get('status') or '—'}"
            )
            if row.get("original_filename"):
                st.write(
                    f"**Файл:** {row.get('original_filename')} · "
                    f"{_knowledge_file_size_label(row.get('file_size_bytes'))}"
                )
            st.write(
                f"**Состояние обработки:** {row.get('processing_status') or '—'}"
            )
            if row.get("purpose"):
                st.write(f"**Для чего Неоле:** {row.get('purpose')}")
            if row.get("notes"):
                st.write(f"**Заметка:** {row.get('notes')}")

            source_status = str(row.get("status") or "Одобрен")
            status_index = (
                list(NEOLA_KNOWLEDGE_STATUSES).index(source_status)
                if source_status in NEOLA_KNOWLEDGE_STATUSES else 0
            )
            edited_status = st.selectbox(
                "Статус источника",
                NEOLA_KNOWLEDGE_STATUSES,
                index=status_index,
                key=f"neola_knowledge_edit_status_{source_id}",
            )
            edited_purpose = st.text_area(
                "Для чего Неоле",
                value=str(row.get("purpose") or ""),
                key=f"neola_knowledge_edit_purpose_{source_id}",
                height=80,
            )
            edited_notes = st.text_area(
                "Заметка Директора",
                value=str(row.get("notes") or ""),
                key=f"neola_knowledge_edit_notes_{source_id}",
                height=80,
            )
            if st.button(
                "💾 Сохранить изменения",
                key=f"neola_knowledge_save_{source_id}",
            ):
                try:
                    _update_neola_knowledge_source(
                        viewer_telegram_id,
                        source_id,
                        {
                            "status": edited_status,
                            "purpose": edited_purpose.strip(),
                            "notes": edited_notes.strip(),
                        },
                    )
                    st.success("Источник обновлён.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Не удалось сохранить изменения: {exc}")



def load_agency_members():
    return _get_json(
        "agency_members",
        params={
            "select": "telegram_id,first_name,username,member_code,referrer_code,created_at",
            "order": "created_at.desc",
            "limit": "10000",
        },
    )


def get_member_by_telegram_id(telegram_id):
    rows = _get_json(
        "agency_members",
        params={
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "telegram_id,first_name,username,member_code,referrer_code,created_at",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _agency_owner_telegram_id():
    """Возвращает единственного корневого владельца Агентства W.

    В текущей базе владельцем считаем самый первый созданный аккаунт Agency W.
    Это принципиально отличается от старого правила «нет referrer_code = владелец»,
    из-за которого любой человек с потерянным кодом пригласителя мог стать legacy_active.
    """
    try:
        rows = _get_json(
            "agency_members",
            params={
                "select": "telegram_id,created_at",
                "order": "created_at.asc",
                "limit": "1",
            },
        )
    except Exception:
        return None
    if not rows:
        return None
    try:
        return int(rows[0].get("telegram_id"))
    except (TypeError, ValueError):
        return None


def _is_agency_owner(telegram_id):
    owner_id = _agency_owner_telegram_id()
    if owner_id is None:
        return False
    try:
        return int(telegram_id) == int(owner_id)
    except (TypeError, ValueError):
        return False


def load_partner_activations(telegram_ids=None):
    params = {
        "select": (
            "telegram_id,neoxa_nickname,lodges_count,status,proof_filename,"
            "proof_mime,submitted_at,reviewed_at,reviewed_by,rejection_reason,"
            "onboarding_status,onboarding_step,last_action_at,attention_level"
        ),
        "limit": "10000",
    }
    if telegram_ids:
        ids = ",".join(str(int(item)) for item in telegram_ids)
        params["telegram_id"] = f"in.({ids})"
    try:
        return _get_json("partner_activations", params=params)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {400, 404}:
            return []
        raise


def get_partner_activation(telegram_id):
    rows = load_partner_activations([telegram_id])
    return rows[0] if rows else None


def ensure_partner_activation(telegram_id):
    member = get_member_by_telegram_id(telegram_id)
    if not member:
        return None

    is_owner = _is_agency_owner(telegram_id)
    existing = get_partner_activation(telegram_id)
    if existing:
        # Старое правило ошибочно давало legacy_active каждому, у кого потерян referrer_code.
        # Теперь legacy_active разрешён ТОЛЬКО корневому владельцу Агентства W.
        # Любой другой человек без подтверждённого скриншота возвращается в ожидание.
        if existing.get("status") == "legacy_active" and not is_owner:
            try:
                rows = _patch_json(
                    "partner_activations",
                    {"telegram_id": f"eq.{int(telegram_id)}"},
                    {
                        "status": "awaiting_proof",
                        "lodges_count": 0,
                        "reviewed_at": None,
                        "reviewed_by": None,
                        "attention_level": "none",
                        "last_action_at": _now_iso(),
                    },
                )
                return rows[0] if rows else get_partner_activation(telegram_id)
            except requests.HTTPError:
                return existing
        return existing

    # Только корневой владелец сохраняет служебный legacy-доступ.
    # Каждый новый/неподтверждённый партнёр обязан загрузить доказательство 5 лож.
    status = "legacy_active" if is_owner else "awaiting_proof"
    onboarding_status = "not_started"

    try:
        rows = _post_json(
            "partner_activations?on_conflict=telegram_id",
            {
                "telegram_id": int(telegram_id),
                "status": status,
                "lodges_count": 0,
                "onboarding_status": onboarding_status,
                "onboarding_step": 0,
                "attention_level": "none",
                "last_action_at": _now_iso(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return rows[0] if rows else get_partner_activation(telegram_id)
    except requests.HTTPError:
        return None


def activation_is_confirmed(activation):
    if not activation:
        return False
    return activation.get("status") in {"confirmed", "legacy_active"}


def activation_label(activation):
    if not activation:
        return "⚪ Статус не создан"
    status = activation.get("status")
    labels = {
        "awaiting_proof": "🟡 Ожидается подтверждение 5 лож",
        "proof_submitted": "⏳ Скриншот отправлен владельцу Агентства W",
        "confirmed": "🟢 5 лож подтверждены",
        "rejected": "🔴 Нужен новый скриншот",
        "legacy_active": "🟢 Активный партнёр",
    }
    return labels.get(status, f"⚪ {status or 'Не определён'}")


def _extract_response_text(data):
    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def analyze_neoxa_proof(image_bytes, mime_type):
    """ИИ только подсказывает, что видно на скриншоте. Решение принимает владелец Агентства W."""
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "низкая",
            "note": "OpenAI API key не найден — скриншот проверит наставник вручную.",
        }

    data_url = (
        f"data:{mime_type or 'image/png'};base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    body = {
        "model": "gpt-5-mini",
        "instructions": (
            "Ты проверяешь скриншот активации партнёра NeoXa/NeoNexa. "
            "Не подтверждай покупку окончательно — только извлеки видимые данные. "
            "Верни ТОЛЬКО JSON-объект: nickname (строка), lodges_count (целое число), "
            "looks_like_neoxa (true/false), confidence ('высокая'|'средняя'|'низкая'), "
            "note (кратко что видно или чего не хватает). Не додумывай." 
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Найди на скриншоте ник/идентификатор пользователя и "
                            "видимое количество приобретённых или активированных лож."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": data_url,
                        "detail": "high",
                    },
                ],
            }
        ],
        "store": False,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=90,
    )
    response.raise_for_status()
    text = _extract_response_text(response.json())
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise RuntimeError("Не удалось прочитать данные со скриншота.")
    data = json.loads(match.group(0))
    try:
        lodges = int(data.get("lodges_count") or 0)
    except (TypeError, ValueError):
        lodges = 0
    return {
        "nickname": str(data.get("nickname") or "").strip()[:120],
        "lodges_count": max(0, lodges),
        "looks_like_neoxa": bool(data.get("looks_like_neoxa", False)),
        "confidence": str(data.get("confidence") or "низкая")[:20],
        "note": str(data.get("note") or "")[:500],
    }


def submit_activation_proof(telegram_id, uploaded_file):
    """
    Финансовое доказательство сохраняется ПЕРВЫМ.
    Ошибка OpenAI не должна приводить к потере скриншота.
    """
    image_bytes = uploaded_file.getvalue()
    if not image_bytes:
        raise RuntimeError("Файл пуст.")
    if len(image_bytes) > 4 * 1024 * 1024:
        raise RuntimeError("Скриншот слишком большой. Максимум 4 МБ.")

    mime_type = uploaded_file.type or "image/png"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    submitted_at = _now_iso()

    # ШАГ 1. Надёжно сохраняем сам файл и переводим заявку в ожидание проверки.
    initial_payload = {
        "telegram_id": int(telegram_id),
        "status": "proof_submitted",
        "neoxa_nickname": None,
        "lodges_count": 0,
        "proof_filename": uploaded_file.name[:255],
        "proof_mime": mime_type[:100],
        "proof_image_base64": encoded,
        "proof_ai_result": {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "не проверено",
            "note": "Скриншот сохранён. ИИ-анализ ещё не выполнен.",
        },
        "submitted_at": submitted_at,
        "reviewed_at": None,
        "reviewed_by": None,
        "rejection_reason": None,
        "attention_level": "red",
        "last_action_at": submitted_at,
    }

    rows = _post_json(
        "partner_activations?on_conflict=telegram_id",
        initial_payload,
        prefer="resolution=merge-duplicates,return=representation",
        timeout=30,
    )
    saved_activation = rows[0] if rows else get_partner_activation(telegram_id)

    # ШАГ 2. ИИ — только помощник. Любая его ошибка НЕ отменяет сохранение.
    try:
        analysis = analyze_neoxa_proof(image_bytes, mime_type)
        ai_payload = {
            "neoxa_nickname": analysis["nickname"] or None,
            "lodges_count": int(analysis["lodges_count"]),
            "proof_ai_result": analysis,
            "last_action_at": _now_iso(),
        }
        patched = _patch_json(
            "partner_activations",
            {"telegram_id": f"eq.{int(telegram_id)}"},
            ai_payload,
        )
        if patched:
            saved_activation = patched[0]
        return saved_activation, analysis, None
    except Exception as exc:
        fallback = {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "не проверено",
            "note": (
                "Скриншот сохранён. Автоматический анализ временно недоступен; "
                "нужна ручная проверка владельцем Агентства W."
            ),
        }
        # Сохраняем отметку об ошибке анализа, но не сам текст исключения целиком.
        try:
            _patch_json(
                "partner_activations",
                {"telegram_id": f"eq.{int(telegram_id)}"},
                {
                    "proof_ai_result": fallback,
                    "last_action_at": _now_iso(),
                },
            )
        except Exception:
            pass
        return saved_activation, fallback, str(exc)



def load_activation_proof(telegram_id):
    try:
        rows = _get_json(
            "partner_activations",
            params={
                "telegram_id": f"eq.{int(telegram_id)}",
                "select": "proof_image_base64,proof_mime,proof_ai_result,proof_filename,submitted_at,reviewed_at,reviewed_by,lodges_count,status",
                "limit": "1",
            },
        )
    except requests.HTTPError:
        return None
    return rows[0] if rows else None


def review_activation(
    telegram_id,
    reviewer_telegram_id,
    approved,
    reason="",
    confirmed_lodges=None,
):
    if not _is_agency_owner(reviewer_telegram_id):
        raise RuntimeError(
            "Подтверждать или отклонять скриншот 5 лож может только владелец Агентства W."
        )

    activation = get_partner_activation(telegram_id)
    if not activation:
        raise RuntimeError("Заявка на активацию не найдена.")

    payload = {
        "status": "confirmed" if approved else "rejected",
        "reviewed_at": _now_iso(),
        "reviewed_by": int(reviewer_telegram_id),
        "rejection_reason": None if approved else (reason.strip() or "Нужен более ясный скриншот."),
        "attention_level": "none" if approved else "orange",
        "last_action_at": _now_iso(),
    }
    if approved:
        try:
            confirmed_lodges = int(confirmed_lodges)
        except (TypeError, ValueError):
            confirmed_lodges = int(activation.get("lodges_count") or 0)
        if confirmed_lodges < MIN_LODGES:
            raise RuntimeError("Для активации нужно подтвердить не меньше 5 лож.")
        payload["lodges_count"] = confirmed_lodges
        payload["onboarding_status"] = "not_started"
        payload["onboarding_step"] = 0

    rows = _patch_json(
        "partner_activations",
        {"telegram_id": f"eq.{int(telegram_id)}"},
        payload,
    )
    return rows[0] if rows else get_partner_activation(telegram_id)


def _member_maps(members):
    by_code = {}
    children = {}
    for member in members:
        code = str(member.get("member_code") or "").strip()
        if code:
            by_code[code] = member
        ref = str(member.get("referrer_code") or "").strip()
        if ref:
            children.setdefault(ref, []).append(member)
    for value in children.values():
        value.sort(key=lambda x: str(x.get("first_name") or "").lower())
    return by_code, children


def descendants_for_member(members, root_member_code):
    _, children = _member_maps(members)
    result = []
    queue = [(root_member_code, 1)]
    seen = set()
    while queue:
        parent_code, depth = queue.pop(0)
        for member in children.get(parent_code, []):
            code = str(member.get("member_code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            row = dict(member)
            row["depth"] = depth
            result.append(row)
            queue.append((code, depth + 1))
    return result


def direct_inviter_member(members, member):
    ref = str(member.get("referrer_code") or "").strip()
    if not ref:
        return None
    by_code, _ = _member_maps(members)
    return by_code.get(ref)


def _member_display_name(member):
    if not member:
        return ""
    name = str(member.get("first_name") or "").strip()
    username = str(member.get("username") or "").strip().lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"Telegram {member.get('telegram_id') or '—'}"


def _parse_member_created_at(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC_TZ)
    return parsed


def _member_joined_today(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    if not parsed:
        return False
    return parsed.astimezone(BERLIN_TZ).date() == datetime.now(BERLIN_TZ).date()


def _member_joined_label(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    if not parsed:
        return "дата не определена"
    return parsed.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")


def _created_sort_key(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    return parsed.timestamp() if parsed else 0


def _visible_members_for_viewer(members, current_member, current_member_code, is_root_owner):
    """Владелец видит весь реестр, обычный партнёр — только свою ветку вниз."""
    descendants = descendants_for_member(members, current_member_code)
    if not is_root_owner:
        return descendants

    depth_by_code = {
        str(item.get("member_code") or ""): int(item.get("depth") or 0)
        for item in descendants
    }
    current_id = int((current_member or {}).get("telegram_id") or 0)
    visible = []
    for member in members:
        member_id = int(member.get("telegram_id") or 0)
        if member_id == current_id:
            continue
        row = dict(member)
        code = str(row.get("member_code") or "")
        row["depth"] = depth_by_code.get(code)
        row["outside_owner_tree"] = code not in depth_by_code
        visible.append(row)
    return visible


def _compact_status(activation):
    if not activation:
        return "⚪ Зарегистрирован"
    status = activation.get("status")
    onboarding = activation.get("onboarding_status")
    if status == "proof_submitted":
        return "🔴 Ждёт подтверждения"
    if status == "rejected":
        return "🟠 Нужен скриншот"
    if status in {"confirmed", "legacy_active"}:
        if onboarding in {"in_progress", "started"}:
            return "🟡 Онбординг"
        if onboarding == "activated":
            return "🟢 Активирован"
        return "🟢 Активен"
    return "🟡 Ждёт 5 лож"


def _attention_label(activation):
    level = str((activation or {}).get("attention_level") or "none")
    return {
        "red": "🔴 Владелец",
        "orange": "🟠 Наставник",
        "yellow": "🟡 Неола",
        "none": "—",
    }.get(level, "—")


def render_my_activation(telegram_id):
    activation = ensure_partner_activation(telegram_id)
    st.markdown("#### 🔐 Моя активация Neonexa")
    st.write(activation_label(activation))

    if activation_is_confirmed(activation):
        lodges = int((activation or {}).get("lodges_count") or 0)
        if lodges:
            st.caption(f"Подтверждено лож: {lodges}")
        return activation

    if activation and activation.get("status") == "proof_submitted":
        st.info("Скриншот уже отправлен. Доступ откроется только после подтверждения владельцем Агентства W.")
        return activation

    if activation and activation.get("status") == "rejected":
        reason = str(activation.get("rejection_reason") or "Нужен новый скриншот.")
        st.warning(f"Владелец Агентства W попросил новый скриншот: {reason}")

    st.caption(
        "Загрузите скриншот Neonexa, на котором одновременно видны ваш ник и "
        "количество приобретённых/активированных лож (не меньше 5)."
    )
    uploaded = st.file_uploader(
        "Скриншот Neonexa",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"neola_activation_proof_{telegram_id}",
    )
    if uploaded is not None and st.button(
        "📷 Отправить на подтверждение",
        type="primary",
        key=f"neola_submit_proof_{telegram_id}",
    ):
        try:
            with st.spinner("Сначала сохраняю скриншот, затем пробую его распознать..."):
                _, analysis, ai_error = submit_activation_proof(telegram_id, uploaded)

            st.success(
                "Скриншот сохранён и отправлен на подтверждение. "
                "Теперь он не потеряется даже при ошибке ИИ."
            )
            if ai_error:
                st.warning(
                    "Автоматическое распознавание временно недоступно. "
                    "Владелец Агентства W проверит скриншот вручную."
                )
            else:
                st.caption(
                    f"Предварительно распознано: ник — "
                    f"{analysis['nickname'] or 'не найден'}, "
                    f"ложи — {analysis['lodges_count']}. "
                    "Окончательное решение принимает только владелец Агентства W."
                )
            st.rerun()
        except Exception as exc:
            st.error(
                "Не удалось сохранить скриншот. "
                f"Повторите попытку: {exc}"
            )
    return activation


def render_partner_center(current_telegram_id, current_member_code, current_name):
    st.markdown("### 🌳 Центр партнёров")

    with st.container(border=True):
        render_my_activation(current_telegram_id)

    try:
        members = load_agency_members()
    except Exception as exc:
        st.error(f"Не удалось загрузить структуру партнёров: {exc}")
        return

    current_member = next(
        (
            member for member in members
            if int(member.get("telegram_id") or 0) == int(current_telegram_id)
        ),
        None,
    )
    is_root_owner = bool(current_member and _is_agency_owner(current_telegram_id))

    visible = _visible_members_for_viewer(
        members,
        current_member,
        current_member_code,
        is_root_owner,
    )
    visible_ids = {int(item.get("telegram_id") or 0) for item in visible}
    ids = sorted(visible_ids)
    activations = load_partner_activations(ids) if ids else []
    activation_by_id = {int(item["telegram_id"]): item for item in activations}

    # Автоматически исправляем старые ошибочные legacy_active у всех, кроме владельца.
    owner_id = _agency_owner_telegram_id()
    for member_id, activation in list(activation_by_id.items()):
        if (
            activation.get("status") == "legacy_active"
            and owner_id is not None
            and int(member_id) != int(owner_id)
        ):
            repaired = ensure_partner_activation(member_id)
            if repaired:
                activation_by_id[int(member_id)] = repaired

    if is_root_owner:
        st.info(
            "👑 Режим владельца Агентства W: здесь видны все зарегистрированные "
            "люди во всём Агентстве, независимо от ветки."
        )
    else:
        st.caption(
            "🔐 Вы видите только свою структуру вниз: личных партнёров и все поколения под ними."
        )

    unresolved_inviter = []
    for member in visible:
        ref_code = str(member.get("referrer_code") or "").strip()
        inviter = direct_inviter_member(members, member)
        if not ref_code or inviter is None:
            unresolved_inviter.append(member)

    if is_root_owner and unresolved_inviter:
        st.warning(
            f"⚠️ У {len(unresolved_inviter)} человек пригласитель не определён или "
            "цепочка приглашения нарушена. Они всё равно показаны владельцу Агентства."
        )

    total = len(visible)
    today_count = sum(1 for member in visible if _member_joined_today(member))
    waiting_count = sum(
        1 for member in visible
        if (activation_by_id.get(int(member["telegram_id"]), {}).get("status")
            in {None, "awaiting_proof", "proof_submitted", "rejected"})
    )
    onboarding_count = sum(
        1 for activation in activations
        if activation.get("onboarding_status") in {"started", "in_progress"}
    )
    attention_count = sum(
        1 for activation in activations
        if activation.get("attention_level") in {"red", "orange"}
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Всего", total)
    c2.metric("Новые сегодня", today_count)
    c3.metric("Ждут подтверждения", waiting_count)
    c4.metric("Онбординг", onboarding_count)
    c5.metric("Требуют внимания", attention_count)

    if not visible:
        if is_root_owner:
            st.info("В Агентстве пока нет других зарегистрированных участников.")
        else:
            st.info("В вашей ветке пока нет зарегистрированных партнёров.")
        return

    filter_options = [
        "Все",
        "Новые сегодня",
        "Ждут подтверждения",
        "Онбординг",
        "Активные",
        "Требуют внимания",
    ]
    filter_value = st.segmented_control(
        "Фильтр",
        filter_options,
        default="Все",
        key=f"partner_filter_{current_telegram_id}",
    )
    query = st.text_input(
        "🔎 Имя, ник или пригласитель",
        key=f"partner_search_{current_telegram_id}",
        placeholder="Начните вводить имя, @username или имя пригласившего",
    ).strip().lower().lstrip("@")

    def matches(member):
        activation = activation_by_id.get(int(member["telegram_id"]))
        inviter = direct_inviter_member(members, member)
        if query:
            haystack = " ".join(
                [
                    str(member.get("first_name") or ""),
                    str(member.get("username") or ""),
                    str(member.get("member_code") or ""),
                    str(member.get("referrer_code") or ""),
                    _member_display_name(inviter),
                ]
            ).lower()
            if query not in haystack:
                return False
        status = (activation or {}).get("status")
        onboarding = (activation or {}).get("onboarding_status")
        attention = (activation or {}).get("attention_level")
        if filter_value == "Новые сегодня":
            return _member_joined_today(member)
        if filter_value == "Ждут подтверждения":
            return status in {None, "awaiting_proof", "proof_submitted", "rejected"}
        if filter_value == "Онбординг":
            return onboarding in {"started", "in_progress"}
        if filter_value == "Активные":
            return status in {"confirmed", "legacy_active"}
        if filter_value == "Требуют внимания":
            return attention in {"red", "orange"}
        return True

    filtered = sorted(
        [member for member in visible if matches(member)],
        key=_created_sort_key,
        reverse=True,
    )

    by_code, children = _member_maps(members)
    list_tab, tree_tab = st.tabs(["📋 Список", "🌳 Структура"])

    with list_tab:
        if not filtered:
            st.info("По выбранному фильтру партнёров нет.")
        for member in filtered[:500]:
            member_id = int(member["telegram_id"])
            activation = activation_by_id.get(member_id)
            name = str(member.get("first_name") or "Партнёр")
            username = str(member.get("username") or "").strip()
            depth = member.get("depth")
            lodges = int((activation or {}).get("lodges_count") or 0)
            step = int((activation or {}).get("onboarding_step") or 0)
            status_text = _compact_status(activation)
            attention_text = _attention_label(activation)
            inviter = direct_inviter_member(members, member)
            inviter_name = _member_display_name(inviter)
            ref_code = str(member.get("referrer_code") or "").strip()
            member_code = str(member.get("member_code") or "").strip()
            direct_children = len(children.get(member_code, []))

            if depth:
                line_text = f"линия {int(depth)}"
            elif is_root_owner:
                line_text = "вне основной цепочки"
            else:
                line_text = "линия не определена"

            with st.expander(
                f"{name} · {line_text} · {status_text}",
                expanded=False,
            ):
                if inviter_name:
                    st.markdown(f"**Пригласил:** {inviter_name}")
                else:
                    st.error("⚠️ Пригласитель не определён")

                cols = st.columns([1.4, 1, 1, 1])
                cols[0].write(f"**@{username}**" if username else "Без username")
                cols[1].write(f"**Ложи:** {lodges or '—'}")
                cols[2].write(f"**Неола:** {step}/7")
                cols[3].write(f"**Личных партнёров:** {direct_children}")

                st.caption(
                    f"В Агентстве с: {_member_joined_label(member)} · "
                    f"код: {member_code or '—'} · "
                    f"код пригласившего: {ref_code or '—'} · "
                    f"внимание: {attention_text}"
                )

                # Финансовое подтверждение видит и проверяет только владелец Агентства W.
                can_review_activation = is_root_owner

                # Финансовое доказательство НЕ исчезает после подтверждения.
                proof = load_activation_proof(member_id) if can_review_activation else None
                has_proof = bool(proof and proof.get("proof_image_base64"))

                if has_proof:
                    with st.expander("🧾 Подтверждение 5 лож Neonexa", expanded=False):
                        if (
                            is_root_owner
                            and str(member.get("referrer_code") or "") != str(current_member_code)
                        ):
                            st.caption(
                                "👑 Вы видите это подтверждение как владелец Агентства."
                            )

                        try:
                            raw = base64.b64decode(proof["proof_image_base64"])
                            st.image(
                                raw,
                                caption="Финансовое подтверждение Neonexa",
                                width=420,
                            )
                        except Exception:
                            st.warning(
                                "Скриншот сохранён, но не удалось показать предпросмотр."
                            )

                        submitted_at = proof.get("submitted_at") or "—"
                        reviewed_at = proof.get("reviewed_at") or "—"
                        reviewer = proof.get("reviewed_by") or "—"
                        proof_lodges = int(proof.get("lodges_count") or 0)
                        proof_status = str(proof.get("status") or "")

                        st.caption(
                            f"Файл: {proof.get('proof_filename') or '—'} · "
                            f"загружен: {submitted_at}"
                        )

                        if proof_status in {"confirmed", "legacy_active"}:
                            st.success(
                                f"✅ Подтверждено лож: {proof_lodges or '—'} · "
                                f"кем: {reviewer} · дата: {reviewed_at}"
                            )
                        elif proof_status == "proof_submitted":
                            st.warning("⏳ Скриншот ждёт подтверждения.")
                        elif proof_status == "rejected":
                            st.warning(
                                "↩️ Этот скриншот был отклонён. "
                                "Он сохранён в истории как доказательство проверки."
                            )

                        ai_result = (proof or {}).get("proof_ai_result") or {}
                        if ai_result:
                            st.info(
                                "Предварительный разбор ИИ: "
                                f"ник — {ai_result.get('nickname') or 'не найден'}; "
                                f"ложи — {ai_result.get('lodges_count') or 0}; "
                                f"уверенность — "
                                f"{ai_result.get('confidence') or 'не проверено'}."
                            )

                if (
                    can_review_activation
                    and (activation or {}).get("status") == "proof_submitted"
                ):
                    recognized_lodges = int((activation or {}).get("lodges_count") or 0)
                    confirmed_lodges = st.number_input(
                        "Сколько лож вы видите на скриншоте?",
                        min_value=0,
                        max_value=100000,
                        value=max(0, recognized_lodges),
                        step=1,
                        key=f"confirmed_lodges_{current_telegram_id}_{member_id}",
                        help=(
                            "ИИ только помогает прочитать скриншот. "
                            "Окончательное число подтверждает только владелец Агентства W."
                        ),
                    )
                    reason_key = f"reject_reason_{current_telegram_id}_{member_id}"
                    reject_reason = st.text_input(
                        "Если нужно отклонить — причина",
                        key=reason_key,
                        placeholder="Например: не виден ник или количество лож",
                    )
                    a, b = st.columns(2)
                    if a.button(
                        "✅ Подтвердить 5 лож",
                        type="primary",
                        key=f"confirm_lodges_{current_telegram_id}_{member_id}",
                        disabled=int(confirmed_lodges) < MIN_LODGES,
                    ):
                        try:
                            review_activation(
                                member_id,
                                current_telegram_id,
                                True,
                                confirmed_lodges=int(confirmed_lodges),
                            )
                            st.success(
                                "Партнёр активирован. "
                                "Скриншот остаётся в финансовом архиве."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    if b.button(
                        "❌ Попросить новый скриншот",
                        key=f"reject_lodges_{current_telegram_id}_{member_id}",
                    ):
                        try:
                            review_activation(
                                member_id,
                                current_telegram_id,
                                False,
                                reject_reason,
                            )
                            st.warning(
                                "Партнёру будет показана просьба загрузить "
                                "новый скриншот. Старый останется в истории "
                                "до следующей загрузки."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

    with tree_tab:
        connected = descendants_for_member(members, current_member_code)
        connected_ids = {int(item.get("telegram_id") or 0) for item in connected}

        def tree_label(child, depth):
            child_id = int(child.get("telegram_id") or 0)
            activation = activation_by_id.get(child_id)
            child_code = str(child.get("member_code") or "")
            direct_children = len(
                [item for item in children.get(child_code, [])
                 if int(item.get("telegram_id") or 0) in visible_ids]
            )
            inviter = direct_inviter_member(members, child)
            inviter_text = _member_display_name(inviter) or "⚠️ не определён"
            return (
                f"{'↳ ' * min(depth, 4)}{child.get('first_name') or 'Партнёр'} "
                f"· пригласил: {inviter_text} · {_compact_status(activation)} · "
                f"личных: {direct_children}"
            )

        def render_branch(parent_code, depth=0, max_depth=20):
            if depth >= max_depth:
                st.caption("… глубина скрыта")
                return
            for child in children.get(parent_code, []):
                child_id = int(child.get("telegram_id") or 0)
                if child_id not in visible_ids:
                    continue
                child_code = str(child.get("member_code") or "")
                direct_visible_children = [
                    item for item in children.get(child_code, [])
                    if int(item.get("telegram_id") or 0) in visible_ids
                ]
                label = tree_label(child, depth)
                if direct_visible_children:
                    with st.expander(label, expanded=False):
                        st.caption(f"В Агентстве с: {_member_joined_label(child)}")
                        render_branch(child_code, depth + 1, max_depth)
                else:
                    st.write(label)

        st.markdown(f"**{current_name}**")
        render_branch(str(current_member_code))

        if is_root_owner:
            disconnected_ids = visible_ids - connected_ids
            if disconnected_ids:
                visible_codes = {
                    str(item.get("member_code") or "")
                    for item in visible
                    if int(item.get("telegram_id") or 0) in disconnected_ids
                }
                disconnected_roots = []
                for member in visible:
                    member_id = int(member.get("telegram_id") or 0)
                    if member_id not in disconnected_ids:
                        continue
                    ref = str(member.get("referrer_code") or "").strip()
                    if not ref or ref not in visible_codes:
                        disconnected_roots.append(member)

                st.divider()
                st.markdown("**⚠️ Вне основной цепочки приглашений**")
                st.caption(
                    "Эти люди зарегистрированы в Агентстве, но их связь с основной "
                    "структурой Валентины не определяется. Владелец всё равно видит их."
                )

                shown_roots = set()
                for root_member in sorted(
                    disconnected_roots,
                    key=_created_sort_key,
                    reverse=True,
                ):
                    root_id = int(root_member.get("telegram_id") or 0)
                    if root_id in shown_roots:
                        continue
                    shown_roots.add(root_id)
                    root_code = str(root_member.get("member_code") or "")
                    direct_visible_children = [
                        item for item in children.get(root_code, [])
                        if int(item.get("telegram_id") or 0) in visible_ids
                    ]
                    label = tree_label(root_member, 0)
                    if direct_visible_children:
                        with st.expander(label, expanded=False):
                            st.caption(
                                f"В Агентстве с: {_member_joined_label(root_member)}"
                            )
                            render_branch(root_code, 1, 20)
                    else:
                        st.write(label)


def _save_neola_message(telegram_id, role, content):
    try:
        _post_json(
            "neola_messages",
            {
                "telegram_id": int(telegram_id),
                "role": role,
                "content": str(content)[:8000],
                "created_at": _now_iso(),
            },
            prefer="return=minimal",
        )
    except Exception:
        # Чат остаётся работоспособным в session_state даже до миграции БД.
        pass


def _load_neola_messages(telegram_id, limit=30):
    try:
        rows = _get_json(
            "neola_messages",
            params={
                "telegram_id": f"eq.{int(telegram_id)}",
                "select": "role,content,created_at",
                "order": "created_at.asc",
                "limit": str(int(limit)),
            },
        )
        return rows
    except Exception:
        return []


def _transcribe_audio(audio_file):
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден.")
    audio_bytes = audio_file.getvalue()
    mime = getattr(audio_file, "type", None) or "audio/wav"
    name = getattr(audio_file, "name", None) or "neola_voice.wav"
    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": "gpt-4o-mini-transcribe",
            "language": "ru",
            "response_format": "json",
        },
        files={"file": (name, audio_bytes, mime)},
        timeout=90,
    )
    response.raise_for_status()
    return str(response.json().get("text") or "").strip()


def _synthesize_speech(text):
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key or not text:
        return None
    response = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini-tts",
            "voice": "marin",
            "input": str(text)[:4000],
            "instructions": (
                "Говори по-русски. Тёплый, спокойный, уверенный женский голос "
                "наставника. Короткие ясные фразы, без торопливости."
            ),
            "response_format": "mp3",
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.content


def _neola_system_prompt(owner_name, ui_context, activation, member):
    onboarding_step = int((activation or {}).get("onboarding_step") or 0)
    cabinet_map = neola_cabinet_knowledge()
    return f"""
Ты — Неола, голосовой наставник партнёра в Агентстве W.
Партнёр: {owner_name}.
Текущий статус: {activation_label(activation)}.
Шаг онбординга: {onboarding_step}/7.
Текущий интерфейс: {ui_context}.
Код партнёра: {str((member or {}).get('member_code') or '')}.

Твоя главная миссия:
{NEOLA_MISSION}

Ты должна знать, где находятся разделы и кнопки, понимать порядок действий и вести человека по одному шагу.
Ты не читаешь курс и не заменяешь других агентов. Навигация нужна для того, чтобы обучать реальной работе, а не просто показывать меню.

ПРАВИЛА РАБОТЫ:
- Официальное название всегда: «Агентство W» — одна буква W. Никогда не говори и не пиши «Агентство WW».
- Говори просто, тепло и коротко. Обычно 1–3 коротких предложения.
- Один шаг = одно действие. После действия дождись ответа человека.
- Всегда учитывай текущий экран: {ui_context}.
- Если человек уже находится в нужном разделе, продолжай оттуда.
- Никогда не выдумывай кнопку, вкладку, статус или выполненное действие.
- Если реальный экран отличается от карты, доверься экрану пользователя: спроси, что на нём написано.
- Если человеку трудно, объясни проще; если просит повторить — повтори только последний шаг.
- Не выбирай кандидатов вместо владельца.
- Не редактируй и не оценивай тексты Неоны. Покажи, где владелец сам может их изменить.
- Не выполняй работу Неонии, Неоны, Стагирита или календаря вместо них.
- Telegram подключается один раз. Если он уже подключён, не предлагай подключать его снова.

АКТУАЛЬНАЯ КАРТА КАБИНЕТА:
{cabinet_map}

Если пользователь спрашивает «где это?», сначала найди точный маршрут в карте выше, но озвучь только один следующий шаг.
Если точного маршрута в карте нет, честно скажи, что не хочешь придумывать, и попроси назвать элементы текущего экрана.
""".strip()


def _ask_neola(owner_name, telegram_id, user_text, ui_context, activation, member, ask_openai_fn):
    history_key = f"neola_session_history_{telegram_id}"
    if history_key not in st.session_state:
        persisted = _load_neola_messages(telegram_id)
        st.session_state[history_key] = [
            {"role": item.get("role"), "content": item.get("content")}
            for item in persisted[-20:]
            if item.get("role") in {"user", "assistant"}
        ]
    history = st.session_state[history_key]
    history_text = "\n".join(
        f"{('Партнёр' if item['role'] == 'user' else 'Неола')}: {item['content']}"
        for item in history[-12:]
    )
    prompt = _neola_system_prompt(owner_name, ui_context, activation, member)
    request = (
        f"ПОСЛЕДНИЙ ДИАЛОГ:\n{history_text}\n\n"
        f"НОВОЕ СООБЩЕНИЕ ПАРТНЁРА:\n{user_text}\n\n"
        "Ответь как Неола. Не повторяй уже выполненные шаги."
    )
    answer = ask_openai_fn(prompt, request)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    st.session_state[history_key] = history[-30:]
    _save_neola_message(telegram_id, "user", user_text)
    _save_neola_message(telegram_id, "assistant", answer)
    try:
        _patch_json(
            "partner_activations",
            {"telegram_id": f"eq.{int(telegram_id)}"},
            {
                "last_action_at": _now_iso(),
                "onboarding_status": "in_progress",
                "attention_level": "yellow",
            },
            prefer="return=minimal",
        )
    except Exception:
        pass
    return answer


def _render_neola_conversation(telegram_id, owner_name, ui_context, ask_openai_fn, compact=False):
    activation = ensure_partner_activation(telegram_id)
    member = get_member_by_telegram_id(telegram_id)

    if not activation_is_confirmed(activation):
        st.warning("Неола и рабочий кабинет включатся только после подтверждения 5 лож владельцем Агентства W.")
        st.caption(activation_label(activation))
        return

    history_key = f"neola_session_history_{telegram_id}"
    if history_key not in st.session_state:
        persisted = _load_neola_messages(telegram_id)
        st.session_state[history_key] = [
            {"role": item.get("role"), "content": item.get("content")}
            for item in persisted[-20:]
            if item.get("role") in {"user", "assistant"}
        ]

    history = st.session_state[history_key]
    if not history:
        greeting = neola_first_greeting(owner_name)
        history.append({"role": "assistant", "content": greeting})
        _save_neola_message(telegram_id, "assistant", greeting)
        st.session_state[history_key] = history

    show_count = 6 if compact else 14
    for item in history[-show_count:]:
        role_name = "Вы" if item["role"] == "user" else "Неола"
        st.markdown(f"**{role_name}:** {item['content']}")

    st.caption(f"Сейчас Неола видит контекст: {ui_context}")

    voice_text = ""
    if hasattr(st, "audio_input"):
        audio = st.audio_input(
            "🎙 Скажите Неоле",
            key=f"neola_audio_{telegram_id}_{'compact' if compact else 'full'}",
        )
        if audio is not None:
            audio_hash = hash(audio.getvalue())
            processed_key = f"neola_processed_audio_{telegram_id}_{audio_hash}"
            if not st.session_state.get(processed_key):
                try:
                    with st.spinner("Неола слушает..."):
                        voice_text = _transcribe_audio(audio)
                    st.session_state[processed_key] = True
                except Exception as exc:
                    st.error(f"Не удалось распознать голос: {exc}")
    else:
        st.caption("В этой версии Streamlit запись с микрофона недоступна; используйте текст.")

    typed_text = st.text_input(
        "Сообщение Неоле",
        key=f"neola_text_{telegram_id}_{'compact' if compact else 'full'}",
        placeholder="Например: Я уже в Telegram. Как изменить сообщение?",
    )

    user_text = voice_text or typed_text.strip()
    send_clicked = False
    if voice_text:
        st.info(f"Вы сказали: {voice_text}")
        send_clicked = st.button(
            "Отправить распознанный вопрос",
            type="primary",
            key=f"neola_send_voice_{telegram_id}_{'compact' if compact else 'full'}",
        )
    else:
        send_clicked = st.button(
            "Спросить Неолу",
            type="primary",
            key=f"neola_send_text_{telegram_id}_{'compact' if compact else 'full'}",
            disabled=not bool(user_text),
        )

    if send_clicked and user_text:
        try:
            with st.spinner("Неола отвечает..."):
                answer = _ask_neola(
                    owner_name,
                    telegram_id,
                    user_text,
                    ui_context,
                    activation,
                    member,
                    ask_openai_fn,
                )
                speech = _synthesize_speech(answer)
            st.success(answer)
            if speech:
                st.audio(speech, format="audio/mp3", autoplay=True)
        except Exception as exc:
            st.error(f"Неола не смогла ответить: {exc}")


def render_neola_quick_assistant(telegram_id, owner_name, ui_context, ask_openai_fn):
    activation = ensure_partner_activation(telegram_id)
    label = "🎙 Неола рядом" if activation_is_confirmed(activation) else "🔒 Неола"

    if hasattr(st, "popover"):
        with st.popover(label, use_container_width=True):
            st.markdown("#### 🎙 Неола рядом")
            _render_neola_conversation(
                telegram_id,
                owner_name,
                ui_context,
                ask_openai_fn,
                compact=True,
            )
    else:
        with st.expander(label):
            _render_neola_conversation(
                telegram_id,
                owner_name,
                ui_context,
                ask_openai_fn,
                compact=True,
            )


def render_neola_agent(telegram_id, owner_name, ui_context, ask_openai_fn):
    st.caption(
        "Неола — голосовой наставник. Она ведёт по реальным действиям, знает "
        "вложенную навигацию Агентства W и оставляет текстовые шаги в чате."
    )
    activation = ensure_partner_activation(telegram_id)
    if not activation_is_confirmed(activation):
        with st.container(border=True):
            render_my_activation(telegram_id)
        return

    step = int((activation or {}).get("onboarding_step") or 0)

    if _is_agency_owner(telegram_id):
        mentor_tab, study_tab, knowledge_tab = st.tabs(
            ["🎙 Неола", "📚 Вопросы на изучение", "🧠 База знаний"]
        )
        with mentor_tab:
            st.progress(
                min(max(step / 7.0, 0.0), 1.0),
                text=f"Прогресс Неолы: {step}/7",
            )
            with st.container(border=True):
                _render_neola_conversation(
                    telegram_id,
                    owner_name,
                    ui_context,
                    ask_openai_fn,
                    compact=False,
                )
        with study_tab:
            render_neola_study_questions(telegram_id)
        with knowledge_tab:
            render_neola_knowledge_base(telegram_id)
        return

    # Обычный партнёр видит только наставника.
    # Директорская очередь вопросов для него не существует даже в интерфейсе.
    st.progress(min(max(step / 7.0, 0.0), 1.0), text=f"Прогресс Неолы: {step}/7")
    with st.container(border=True):
        _render_neola_conversation(
            telegram_id,
            owner_name,
            ui_context,
            ask_openai_fn,
            compact=False,
        )
