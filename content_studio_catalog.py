"""Owner-scoped history and storage for independent content studio tools.

Reading history never invokes a generation provider. Studio results have their
own inert state, so saving a text, image or voice cannot start publication.
"""
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
import re
import uuid

import requests

from agency_access_control import is_agency_access_revoked
from content_factory_core import iso, utc_now
from content_factory_store import StoreError


def _owner(value):
    if isinstance(value, bool) or not (type(value) is int or isinstance(value, str) and value.isdecimal()):
        raise ValueError("Не удалось определить владельца материалов.")
    owner = int(value)
    if owner <= 0 or is_agency_access_revoked(owner):
        raise ValueError("Материалы этого кабинета недоступны.")
    return owner


def list_manual_packages(owner_id, limit=100, *, offset=0):
    """Read one bounded page of existing manual packages, newest first.

    The existing server configuration is imported lazily; credentials and raw
    storage errors are never returned to a page. Missing storage is an empty
    history, just as in the existing manual factory's latest-package loader.
    """
    owner_id = _owner(owner_id)
    if type(limit) is not int or type(offset) is not int or limit < 1 or offset < 0:
        raise ValueError("Укажите допустимую страницу истории материалов.")
    limit = min(limit, 100)
    try:
        from content_factory_ui import _supabase_config, _supabase_headers
        url, key = _supabase_config()
        if not url or not key:
            return []
        response = requests.get(
            f"{url}/rest/v1/agency_content_packages",
            headers=_supabase_headers(key),
            params={"owner_telegram_id": f"eq.{owner_id}", "select": "package",
                    "order": "updated_at.desc,package_id.desc", "limit": limit, "offset": offset},
            timeout=20, allow_redirects=False,
        )
        try:
            if not 200 <= response.status_code < 300:
                return []
            rows = response.json()
        finally:
            response.close()
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows[:limit]:
            package = row.get("package") if isinstance(row, dict) else None
            if not isinstance(package, dict):
                continue
            candidate = package.get("owner_telegram_id")
            if isinstance(candidate, bool) or not (type(candidate) is int or isinstance(candidate, str) and candidate.isdecimal()):
                continue
            if int(candidate) == owner_id:
                result.append(package)
        return result
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, OSError):
        return []


def save_studio_result(store, owner_id, project_id, kind, prompt, *, text="", data=None,
                       mime_type=None, request_id=None):
    """Persist an already-created result without generating or publishing.

    The request ID is durable across ambiguous responses and repeated clicks.
    A completed result is returned unchanged. An upload failure leaves an
    incomplete, unpublished record with its text and a safe explanation.
    """
    owner_id = _owner(owner_id)
    if not isinstance(kind, str) or kind not in {"text", "image", "audio"}:
        raise ValueError("Выберите текст, изображение или озвучку.")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 6000 or "\x00" in prompt:
        raise ValueError("Опишите задачу: от 1 до 6000 символов.")
    prompt = prompt.strip()
    if not isinstance(text, str) or len(text) > 50000 or "\x00" in text:
        raise ValueError("Текст результата имеет неверный формат.")
    if kind == "text":
        if not text.strip() or data is not None or mime_type is not None:
            raise ValueError("Для сохранения текста нужен непустой текстовый результат.")
    else:
        allowed = {"image": {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"},
                   "audio": {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav"}}[kind]
        if not isinstance(mime_type, str) or mime_type not in allowed or not isinstance(data, bytes) or not 0 < len(data) <= 100 * 1024 * 1024:
            raise ValueError("Нужен готовый медиафайл поддерживаемого формата размером до 100 МБ.")
    request_id = uuid.uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
        raise ValueError("Не удалось определить запрос сохранения. Обновите страницу.")
    project = store.get_project(owner_id, project_id)
    if not project or project.get("owner_id") != owner_id:
        raise ValueError("Проект не найден в вашем кабинете.")
    now = utc_now()
    fingerprint = sha256(json.dumps([kind, prompt], ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
    content = {"title": prompt.splitlines()[0][:150], "text": text, "prompt": prompt}
    job = store.enqueue_job(owner_id, project["id"], {
        "platform": "studio", "format": kind, "state": "studio",
        "dedupe_key": "studio:" + request_id, "scheduled_at": iso(now), "available_at": iso(now),
        "studio_tool": kind, "studio_complete": False, "studio_request_id": request_id,
        "studio_request_fingerprint": fingerprint, "content": content, "assets": [],
        "topic": prompt, "checkpoint": {}, "error": "",
    })
    if job.get("state") != "studio" or job.get("platform") != "studio" or job.get("format") != kind or job.get("studio_request_fingerprint") != fingerprint:
        raise ValueError("Этот запрос уже сохранён с другими условиями. Создайте новый запрос.")
    if job.get("studio_complete") is True:
        return job

    # Reuse the existing owner-scoped optimistic update and lease, including
    # across two simultaneous browser sessions. Only its holder uploads media.
    writer = "studio-save-" + uuid.uuid4().hex
    try:
        job = store.update_job(owner_id, job["id"], {
            "lease_owner": writer, "lease_until": iso(now + timedelta(minutes=5)),
            "content": content, "error": "",
        }, expected_updated_at=job.get("updated_at"))
    except StoreError:
        latest = store.get_job(owner_id, job["id"])
        if latest is not None:
            return latest
        raise StoreError("Не удалось подтвердить сохранение результата. Обновите историю.") from None

    assets = []
    try:
        if kind != "text":
            assets = [store.put_asset(owner_id, project["id"], job["id"],
                                      "studio_result." + allowed[mime_type], data, mime_type)]
        return store.update_job(owner_id, job["id"], {
            "state": "studio", "content": content, "assets": assets,
            "studio_complete": True, "error": "", "lease_owner": None, "lease_until": None,
        }, lease_owner=writer)
    except Exception:
        # Never expose a credential-bearing SDK/storage exception to the page.
        try:
            return store.update_job(owner_id, job["id"], {
                "state": "studio", "content": content, "assets": assets,
                "studio_complete": False, "error": "Не удалось завершить сохранение файла. Результат ещё не готов; повторите сохранение.",
                "lease_owner": None, "lease_until": None,
            }, lease_owner=writer)
        except Exception:
            raise StoreError("Не удалось подтвердить сохранение результата. Обновите историю перед повтором.") from None
