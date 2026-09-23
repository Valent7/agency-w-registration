"""
Индексатор и семантический поиск Базы знаний Неолы.

Что делает:
1) скачивает уже сохранённый приватный файл из Supabase Storage;
2) извлекает текст из PDF / TXT / MD / DOCX;
3) делит текст на смысловые фрагменты;
4) создаёт embeddings через OpenAI;
5) сохраняет фрагменты в public.neola_knowledge_chunks;
6) выполняет закрытый семантический поиск через RPC match_neola_knowledge.

Партнёры напрямую этот модуль не вызывают. Он работает серверным ключом Агентства W.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests
import streamlit as st
from pypdf import PdfReader


EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
KNOWLEDGE_BUCKET = "neola-knowledge"

CHUNK_TARGET_CHARS = 1800
CHUNK_OVERLAP_CHARS = 260
MAX_TEXT_CHARS = 4_000_000
EMBED_BATCH_SIZE = 64


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _supabase_base() -> str:
    return str(st.secrets["SUPABASE_URL"]).rstrip("/")


def _service_key() -> str:
    return str(st.secrets["SUPABASE_SECRET_KEY"])


def _openai_key() -> str:
    value = str(st.secrets.get("OPENAI_API_KEY") or "").strip()
    if not value:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")
    return value


def _service_headers(*, json_content: bool = True, prefer: str | None = None) -> dict:
    headers = {
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _rest_url(path: str) -> str:
    return f"{_supabase_base()}/rest/v1/{path.lstrip('/')}"


def _storage_url(bucket: str, path: str) -> str:
    safe_bucket = quote(str(bucket), safe="")
    safe_path = quote(str(path), safe="/")
    return f"{_supabase_base()}/storage/v1/object/{safe_bucket}/{safe_path}"


def _vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(v):.9g}" for v in values) + "]"


def _get_source(source_id: int) -> dict:
    response = requests.get(
        _rest_url("neola_knowledge_sources"),
        headers=_service_headers(),
        params={
            "id": f"eq.{int(source_id)}",
            "select": (
                "id,title,author,status,storage_bucket,storage_path,"
                "original_filename,mime_type,processing_status"
            ),
            "limit": "1",
        },
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise RuntimeError("Источник не найден в Базе знаний.")
    return rows[0]


def _update_source(source_id: int, **changes) -> None:
    payload = dict(changes)
    payload["updated_at"] = _now_iso()
    response = requests.patch(
        _rest_url("neola_knowledge_sources"),
        headers=_service_headers(prefer="return=minimal"),
        params={"id": f"eq.{int(source_id)}"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def _download_source_bytes(source: dict) -> bytes:
    bucket = str(source.get("storage_bucket") or KNOWLEDGE_BUCKET)
    path = str(source.get("storage_path") or "").strip()
    if not path:
        raise RuntimeError("У источника нет сохранённого файла.")

    response = requests.get(
        _storage_url(bucket, path),
        headers=_service_headers(json_content=False),
        timeout=120,
    )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("Supabase Storage вернул пустой файл.")
    return response.content


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_pdf(data: bytes) -> list[dict]:
    reader = PdfReader(io.BytesIO(data))
    sections = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            text = _clean_text(page.extract_text() or "")
        except Exception:
            text = ""
        if text:
            sections.append({
                "section_title": f"Страница {page_no}",
                "text": text,
                "page_start": page_no,
                "page_end": page_no,
            })
    if not sections:
        raise RuntimeError(
            "Из PDF не удалось извлечь текст. Возможно, это скан без текстового слоя."
        )
    return sections


def _extract_text_file(data: bytes) -> list[dict]:
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    text = _clean_text(text)
    if not text:
        raise RuntimeError("В файле не найден текст.")
    return [{"section_title": "", "text": text, "page_start": None, "page_end": None}]


def _extract_docx(data: bytes) -> list[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except Exception as exc:
        raise RuntimeError("Не удалось открыть DOCX.") from exc

    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", ns)]
        line = _clean_text("".join(parts))
        if line:
            paragraphs.append(line)

    text = _clean_text("\n\n".join(paragraphs))
    if not text:
        raise RuntimeError("В DOCX не найден текст.")
    return [{"section_title": "", "text": text, "page_start": None, "page_end": None}]


def _extract_sections(data: bytes, filename: str, mime_type: str = "") -> list[dict]:
    suffix = Path(str(filename or "")).suffix.lower()
    mime = str(mime_type or "").lower()

    if suffix == ".pdf" or "pdf" in mime:
        return _extract_pdf(data)
    if suffix == ".docx" or "wordprocessingml" in mime:
        return _extract_docx(data)
    if suffix in {".txt", ".md"} or mime.startswith("text/"):
        return _extract_text_file(data)

    raise RuntimeError(
        "Этот формат пока нельзя индексировать. Поддерживаются PDF, TXT, MD и DOCX."
    )


def _split_long_piece(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    parts = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            candidates = [
                text.rfind("\n\n", start, end),
                text.rfind(". ", start, end),
                text.rfind("! ", start, end),
                text.rfind("? ", start, end),
                text.rfind("; ", start, end),
            ]
            cut = max(candidates)
            if cut > start + max_chars // 2:
                end = cut + 1

        piece = text[start:end].strip()
        if piece:
            parts.append(piece)

        if end >= len(text):
            break

        start = max(end - CHUNK_OVERLAP_CHARS, start + 1)

    return parts


def _chunk_sections(sections: list[dict]) -> list[dict]:
    chunks = []
    running_index = 0

    for section in sections:
        title = str(section.get("section_title") or "").strip()
        text = _clean_text(section.get("text") or "")
        if not text:
            continue

        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        buffer = ""

        def flush():
            nonlocal buffer, running_index
            cleaned = _clean_text(buffer)
            if not cleaned:
                buffer = ""
                return
            chunks.append({
                "chunk_index": running_index,
                "section_title": title or None,
                "content": cleaned,
                "page_start": section.get("page_start"),
                "page_end": section.get("page_end"),
                "metadata": {"char_count": len(cleaned)},
            })
            running_index += 1
            buffer = ""

        for paragraph in paragraphs:
            pieces = _split_long_piece(paragraph, CHUNK_TARGET_CHARS)
            for piece in pieces:
                candidate = (buffer + "\n\n" + piece).strip() if buffer else piece
                if len(candidate) <= CHUNK_TARGET_CHARS:
                    buffer = candidate
                    continue

                previous = buffer
                flush()

                overlap = previous[-CHUNK_OVERLAP_CHARS:].strip() if previous else ""
                buffer = (overlap + "\n\n" + piece).strip() if overlap else piece

                if len(buffer) > CHUNK_TARGET_CHARS * 1.35:
                    flush()

        flush()

    if not chunks:
        raise RuntimeError("После обработки не осталось текстовых фрагментов.")
    return chunks


def _openai_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    response = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {_openai_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": EMBEDDING_MODEL,
            "input": texts,
            "dimensions": EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
        },
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()

    items = sorted(payload.get("data") or [], key=lambda item: int(item.get("index", 0)))
    embeddings = [item.get("embedding") for item in items]
    if len(embeddings) != len(texts) or any(not item for item in embeddings):
        raise RuntimeError("OpenAI вернул неполный набор embeddings.")
    return embeddings


def _delete_existing_chunks(source_id: int) -> None:
    response = requests.delete(
        _rest_url("neola_knowledge_chunks"),
        headers=_service_headers(prefer="return=minimal"),
        params={"source_id": f"eq.{int(source_id)}"},
        timeout=60,
    )
    response.raise_for_status()


def _insert_chunks(rows: list[dict]) -> None:
    if not rows:
        return
    response = requests.post(
        _rest_url("neola_knowledge_chunks"),
        headers=_service_headers(prefer="return=minimal"),
        json=rows,
        timeout=120,
    )
    response.raise_for_status()


def index_neola_knowledge_source(source_id: int) -> dict:
    """
    Полностью переиндексирует один источник.
    Старые фрагменты удаляются только после успешного извлечения текста
    и создания embeddings для всех новых фрагментов.
    """
    source_id = int(source_id)
    source = _get_source(source_id)

    if str(source.get("status") or "") != "Одобрен":
        raise RuntimeError("Индексировать можно только источник со статусом «Одобрен».")

    filename = str(source.get("original_filename") or "source.bin")
    mime_type = str(source.get("mime_type") or "")

    _update_source(source_id, processing_status="Индексируется…")

    try:
        raw = _download_source_bytes(source)
        sections = _extract_sections(raw, filename, mime_type)

        total_chars = sum(len(str(s.get("text") or "")) for s in sections)
        if total_chars > MAX_TEXT_CHARS:
            raise RuntimeError(
                f"Текст слишком большой для одного прохода ({total_chars:,} знаков)."
            )

        chunks = _chunk_sections(sections)

        all_embeddings = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start:start + EMBED_BATCH_SIZE]
            all_embeddings.extend(
                _openai_embeddings([item["content"] for item in batch])
            )

        prepared = []
        embedded_at = _now_iso()
        for chunk, embedding in zip(chunks, all_embeddings):
            prepared.append({
                "source_id": source_id,
                "chunk_index": int(chunk["chunk_index"]),
                "section_title": chunk.get("section_title"),
                "content": chunk["content"],
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "metadata": {
                    **(chunk.get("metadata") or {}),
                    "source_filename": filename,
                    "content_sha256": hashlib.sha256(
                        chunk["content"].encode("utf-8")
                    ).hexdigest(),
                },
                "embedding": _vector_literal(embedding),
                "embedding_model": EMBEDDING_MODEL,
                "embedded_at": embedded_at,
            })

        _delete_existing_chunks(source_id)

        for start in range(0, len(prepared), 80):
            _insert_chunks(prepared[start:start + 80])

        _update_source(
            source_id,
            processing_status=f"Индексировано · {len(prepared)} фрагментов",
            indexed_at=_now_iso(),
        )

        return {
            "ok": True,
            "source_id": source_id,
            "title": str(source.get("title") or filename),
            "chunks": len(prepared),
            "characters": total_chars,
            "model": EMBEDDING_MODEL,
        }

    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        try:
            _update_source(
                source_id,
                processing_status=("Ошибка индексации: " + message)[:900],
            )
        except Exception:
            pass
        raise


def search_neola_knowledge(
    query: str,
    *,
    match_count: int = 6,
    min_similarity: float = 0.20,
) -> list[dict]:
    clean_query = _clean_text(query)
    if not clean_query:
        return []

    embedding = _openai_embeddings([clean_query])[0]
    response = requests.post(
        _rest_url("rpc/match_neola_knowledge"),
        headers=_service_headers(),
        json={
            "query_embedding_text": _vector_literal(embedding),
            "match_count": max(1, min(int(match_count), 12)),
            "min_similarity": float(min_similarity),
        },
        timeout=60,
    )
    response.raise_for_status()
    rows = response.json()
    return rows if isinstance(rows, list) else []


def build_neola_knowledge_context(
    query: str,
    *,
    match_count: int = 6,
    min_similarity: float = 0.20,
    max_chars: int = 9000,
) -> str:
    rows = search_neola_knowledge(
        query,
        match_count=match_count,
        min_similarity=min_similarity,
    )
    if not rows:
        return ""

    blocks = []
    used = 0
    for row in rows:
        title = str(row.get("source_title") or "Источник")
        author = str(row.get("source_author") or "").strip()
        section = str(row.get("section_title") or "").strip()
        content = _clean_text(row.get("content") or "")
        similarity = float(row.get("similarity") or 0.0)

        header = title
        if author:
            header += f" — {author}"
        if section:
            header += f" · {section}"

        block = (
            f"[ИСТОЧНИК: {header}; релевантность {similarity:.3f}]\n"
            f"{content}"
        )
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)

    return "\n\n---\n\n".join(blocks)
