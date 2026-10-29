"""Durable, owner-scoped storage for the content factory.

Supabase is the shared production store. SQLite is a local development option;
local files need an explicitly configured public media origin for social APIs.
No Streamlit imports or credentials are required for ordinary local queue work.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any
from urllib.parse import quote, urlparse
import uuid

from cryptography.fernet import Fernet, InvalidToken
import requests


class StoreError(RuntimeError):
    """A storage/configuration error with no credential-bearing details."""


_JOB_COLUMNS = {
    "id", "owner_id", "project_id", "dedupe_key", "platform", "format", "state",
    "scheduled_at", "available_at", "lease_owner", "lease_until", "attempts",
    "created_at", "updated_at",
}
_IMMUTABLE = {"id", "owner_id", "project_id", "dedupe_key", "created_at"}
_PROJECT_COLUMNS = {"id", "owner_id", "automation_enabled", "created_at", "updated_at"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value: str | datetime) -> str:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        raise StoreError("Нужна дата и время с часовым поясом.") from None


def _owner(value: Any) -> int:
    try:
        result = int(value)
        if isinstance(value, bool) or result <= 0:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise StoreError("Не определён владелец проекта.") from None


def _id(value: Any = None) -> str:
    try:
        return str(uuid.UUID(str(value))) if value else str(uuid.uuid4())
    except (ValueError, TypeError, AttributeError):
        raise StoreError("Некорректный идентификатор.") from None


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise StoreError("Данные не могут быть сохранены в JSON.") from None


def _unpack(row: dict | sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    fields = dict(row)
    data = fields.pop("data", {}) or {}
    if isinstance(data, str):
        data = json.loads(data)
    return {**data, **fields}


def _project_row(owner_id: int, data: dict, previous: dict | None) -> dict:
    if ("owner_id" in data and _owner(data["owner_id"]) != _owner(owner_id)) or (
        "created_at" in data and data["created_at"] != (previous or {}).get("created_at")
    ):
        raise StoreError("Нельзя изменить владельца или дату создания проекта.")
    merged = {**(previous or {}), **data}
    timestamp = _now()
    return {
        "id": _id(merged.get("id")), "owner_id": _owner(owner_id),
        "automation_enabled": bool(merged.get("automation_enabled", False)),
        "created_at": (previous or {}).get("created_at", timestamp), "updated_at": timestamp,
        "data": {key: value for key, value in merged.items() if key not in _PROJECT_COLUMNS},
    }


def _job_row(owner_id: int, project_id: str, data: dict) -> dict:
    timestamp = _now()
    dedupe_key = str(data.get("dedupe_key") or "").strip()
    if not dedupe_key or len(dedupe_key) > 500:
        raise StoreError("Нужен ключ защиты от повторной публикации.")
    if data.get("owner_id", owner_id) != owner_id or data.get("project_id", project_id) != project_id:
        raise StoreError("Нельзя изменить владельца или проект задания.")
    row = {
        "id": _id(data.get("id")), "owner_id": _owner(owner_id), "project_id": _id(project_id),
        "dedupe_key": dedupe_key, "platform": str(data.get("platform") or ""),
        "format": str(data.get("format") or "post"), "state": str(data.get("state") or "queued"),
        "scheduled_at": _time(data.get("scheduled_at") or timestamp),
        "available_at": _time(data.get("available_at") or timestamp),
        "lease_owner": None, "lease_until": None, "attempts": 0,
        "created_at": timestamp, "updated_at": timestamp,
    }
    row["data"] = {
        "topic": "", "content": {}, "assets": [], "checkpoint": {}, "error": "",
        "external_id": "", "permalink": "",
        **{key: value for key, value in data.items() if key not in _JOB_COLUMNS},
    }
    return row


def _updated_job(previous: dict, changes: dict) -> dict:
    if _IMMUTABLE.intersection(changes):
        raise StoreError("Идентификаторы и владелец задания неизменяемы.")
    merged = {**previous, **changes, "updated_at": _now()}
    for key in ("scheduled_at", "available_at", "lease_until"):
        if merged.get(key) is not None:
            merged[key] = _time(merged[key])
    row = {key: value for key, value in merged.items() if key in _JOB_COLUMNS}
    row["data"] = {key: value for key, value in merged.items() if key not in _JOB_COLUMNS}
    return row



def _validated_stalled_video_resume(job: dict, now: str, stale_seconds: int = 180) -> str:
    """Allow an owner to resume only a safely checkpointed stalled master video.

    Every unfinished scene must already have a provider task id. This guarantees
    that resuming polls existing paid orders instead of creating new scenes.
    """
    if not isinstance(job, dict) or job.get("state") != "queued" or job.get("studio_master") is not True:
        raise StoreError("Продолжить можно только зависшее создание мастер-видео.")
    production = job.get("production")
    if not isinstance(production, dict):
        raise StoreError("Не найдены сохранённые данные видеопроизводства.")
    if production.get("creation_uncertain") or production.get("starting_index") is not None:
        raise StoreError("Нельзя безопасно продолжить: прошлый платный заказ не подтверждён.")
    if production.get("failed_indices"):
        raise StoreError("Одна из сцен завершилась ошибкой; нужен обычный повтор после проверки.")
    tasks, clips = production.get("tasks"), production.get("clips")
    if not isinstance(tasks, list) or not tasks or not isinstance(clips, list) or len(tasks) != len(clips):
        raise StoreError("Не удалось проверить сохранённые сцены.")
    for task, clip in zip(tasks, clips):
        if clip:
            continue
        if not isinstance(task, str) or not task.strip():
            raise StoreError("Продолжение остановлено: не все оплаченные сцены имеют сохранённый номер заказа.")
    now_dt = datetime.fromisoformat(_time(now))
    updated_dt = datetime.fromisoformat(_time(job.get("updated_at")))
    if (now_dt - updated_dt).total_seconds() < stale_seconds:
        raise StoreError("Задача ещё не считается зависшей. Дайте фоновой проверке завершиться.")
    return now_dt.isoformat()


def _safe_connection(row: dict | sqlite3.Row) -> dict:
    result = dict(row)
    result.pop("token_encrypted", None)
    if isinstance(result.get("extra"), str):
        result["extra"] = json.loads(result["extra"])
    result["has_token"] = True
    return result


def _extra(value: dict | None) -> dict:
    """Connection metadata must never become an alternative secret store."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StoreError("Метаданные подключения должны быть объектом.")
    def inspect(item):
        if isinstance(item, dict):
            for key, nested in item.items():
                lowered = str(key).lower()
                if lowered not in {"token_expires_at", "token_expires_in"} and any(
                    marker in lowered for marker in ("token", "secret", "password", "authorization", "api_key")
                ):
                    raise StoreError("Секреты нужно передавать в защищённое поле подключения.")
                inspect(nested)
        elif isinstance(item, list):
            for nested in item:
                inspect(nested)
    inspect(value)
    _json(value)
    return value


class _StoreBase:
    def _verified_asset_bytes(self, asset, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= 100 * 1024 * 1024:
            raise StoreError("Исходный медиафайл отсутствует или слишком велик.")
        if type(asset.get("size")) is not int or len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != asset.get("sha256"):
            raise StoreError("Исходный медиафайл изменился. Создание видео остановлено.")
        return data

    def _cipher(self) -> Fernet:
        try:
            if not self.fernet_key:
                raise ValueError
            return Fernet(self.fernet_key)
        except (TypeError, ValueError):
            raise StoreError("Нужен действительный локальный CONTENT_FACTORY_FERNET_KEY для подключений площадок.") from None

    def _encrypt(self, token: str) -> str:
        if not str(token or "").strip():
            raise StoreError("Не указан токен площадки.")
        return self._cipher().encrypt(str(token).strip().encode()).decode()

    def _decrypt(self, token: str) -> str:
        try:
            return self._cipher().decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeError, AttributeError):
            raise StoreError("Не удалось открыть подключение: проверьте ключ шифрования контент-завода.") from None

    def _asset_path(self, owner_id: int, asset: dict) -> str:
        path = PurePosixPath(str(asset.get("storage_path") or ""))
        if path.is_absolute() or len(path.parts) < 4 or ".." in path.parts or path.parts[0] != str(_owner(owner_id)):
            raise StoreError("Медиа не принадлежат владельцу проекта.")
        return str(path)

    def _asset(self, owner_id: int, project_id: str, job_id: str, filename: str, data: bytes, mime_type: str) -> dict:
        owner_id = _owner(owner_id)
        project_id, job_id = _id(project_id), _id(job_id)
        job = self.get_job(owner_id, job_id)
        if not job or job["project_id"] != project_id:
            raise StoreError("Задание для сохранения медиа не найдено.")
        if not isinstance(data, bytes) or not data or len(data) > 100 * 1024 * 1024:
            raise StoreError("Нужен непустой медиафайл размером до 100 МБ.")
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "video/mp4", "audio/mpeg", "audio/mp4", "audio/wav"}:
            raise StoreError("Неподдерживаемый тип медиа.")
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(filename)).name)[:120] or "media"
        digest = hashlib.sha256(data).hexdigest()
        return {
            "storage_path": f"{owner_id}/{project_id}/{job_id}/{digest[:16]}-{filename}",
            "filename": filename, "mime_type": mime_type, "sha256": digest,
            "size": len(data), "kind": mime_type.split("/")[0],
        }

    def _reservation(self, reservation_id: str, day: str, limit: int) -> tuple[str, str, int]:
        try:
            reservation_id = str(reservation_id).strip()
            if not reservation_id or len(reservation_id) > 300 or not 1 <= int(limit) <= 1000:
                raise ValueError
            return reservation_id, date.fromisoformat(day).isoformat(), int(limit)
        except (TypeError, ValueError):
            raise StoreError("Некорректные параметры дневного лимита генерации.") from None

    def _refresh_payload(self, owner_id: int, project_id: str, platform: str, previous: dict, renewed: dict) -> tuple[dict, dict]:
        if not isinstance(previous, dict) or not isinstance(renewed, dict):
            raise StoreError("Не удалось проверить исходное подключение площадки.")
        identity = {"owner_id": _owner(owner_id), "project_id": _id(project_id), "platform": platform}
        if any(previous.get(key) != value for key, value in identity.items()):
            raise StoreError("Подключение не принадлежит выбранному проекту.")
        for key in ("id", "owner_id", "project_id", "platform", "account_id"):
            if renewed.get(key, previous.get(key)) != previous.get(key):
                raise StoreError("Продление доступа не может изменить аккаунт площадки.")
        filters = {**identity, "id": _id(previous.get("id")), "account_id": str(previous.get("account_id") or ""), "updated_at": _time(previous.get("updated_at"))}
        payload = {"token_encrypted": self._encrypt(renewed.get("access_token")), "extra": _extra(renewed.get("extra", previous.get("extra"))), "updated_at": _now()}
        return filters, payload


class SQLiteStore(_StoreBase):
    """One SQLite database; connections are short-lived and claims atomic."""

    def __init__(self, path: str | Path, fernet_key: str | bytes | None = None):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet_key = fernet_key
        self.media_dir = self.path.parent / (self.path.stem + "-media")
        self.media_base_url = os.environ.get("CONTENT_FACTORY_MEDIA_BASE_URL", "").strip().rstrip("/")
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS content_factory_projects (
                    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, automation_enabled INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(id,owner_id));
                CREATE TABLE IF NOT EXISTS content_factory_connections (
                    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, project_id TEXT NOT NULL, platform TEXT NOT NULL,
                    account_id TEXT NOT NULL, display_name TEXT NOT NULL, token_encrypted TEXT NOT NULL,
                    extra TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_id,project_id,platform),
                    FOREIGN KEY(project_id,owner_id) REFERENCES content_factory_projects(id,owner_id));
                CREATE TABLE IF NOT EXISTS content_factory_jobs (
                    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, project_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    platform TEXT NOT NULL, format TEXT NOT NULL, state TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL, available_at TEXT NOT NULL, lease_owner TEXT, lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_id,project_id,dedupe_key),
                    FOREIGN KEY(project_id,owner_id) REFERENCES content_factory_projects(id,owner_id));
                CREATE INDEX IF NOT EXISTS content_factory_jobs_due ON content_factory_jobs(state,available_at,lease_until);
                CREATE TABLE IF NOT EXISTS content_factory_generation_reservations (
                    owner_id INTEGER NOT NULL, project_id TEXT NOT NULL, reservation_id TEXT NOT NULL,
                    day TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id,project_id,reservation_id),
                    FOREIGN KEY(project_id,owner_id) REFERENCES content_factory_projects(id,owner_id));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        except sqlite3.Error:
            raise StoreError("Не удалось выполнить операцию с локальной базой контент-завода.") from None
        finally:
            db.close()

    def _insert(self, db, table: str, row: dict, prefix: str = "INSERT"):
        fields = list(row)
        values = [_json(row[key]) if key in {"data", "extra"} else row[key] for key in fields]
        db.execute(f"{prefix} INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)

    def save_project(self, owner_id: int, data: dict) -> dict:
        owner_id = _owner(owner_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = _unpack(db.execute("SELECT * FROM content_factory_projects WHERE owner_id=? AND id=?", (owner_id, _id(data["id"]))).fetchone()) if data.get("id") else None
            row = _project_row(owner_id, data, previous)
            if previous:
                db.execute("UPDATE content_factory_projects SET data=?,automation_enabled=?,updated_at=? WHERE owner_id=? AND id=?", (_json(row["data"]), row["automation_enabled"], row["updated_at"], owner_id, row["id"]))
            else:
                self._insert(db, "content_factory_projects", row)
            return _unpack(row)

    def get_project(self, owner_id: int, project_id: str) -> dict | None:
        with self._db() as db:
            result = _unpack(db.execute("SELECT * FROM content_factory_projects WHERE owner_id=? AND id=?", (_owner(owner_id), _id(project_id))).fetchone())
            if result:
                result["automation_enabled"] = bool(result["automation_enabled"])
            return result

    def list_projects(self, owner_id: int) -> list[dict]:
        with self._db() as db:
            return [dict(_unpack(row), automation_enabled=bool(row["automation_enabled"])) for row in db.execute("SELECT * FROM content_factory_projects WHERE owner_id=? ORDER BY created_at", (_owner(owner_id),))]

    def list_active_projects(self) -> list[dict]:
        with self._db() as db:
            return [dict(_unpack(row), automation_enabled=True) for row in db.execute("SELECT * FROM content_factory_projects WHERE automation_enabled=1 ORDER BY created_at")]

    def _require_project(self, owner_id: int, project_id: str) -> dict:
        result = self.get_project(owner_id, project_id)
        if not result:
            raise StoreError("Проект не найден у этого владельца.")
        return result

    def save_connection(self, owner_id: int, project_id: str, platform: str, account_id: str, display_name: str, access_token: str, extra: dict | None = None) -> dict:
        owner_id, project_id = _owner(owner_id), _id(project_id)
        self._require_project(owner_id, project_id)
        encrypted = self._encrypt(access_token)
        timestamp = _now()
        with self._db() as db:
            previous = db.execute("SELECT id,created_at FROM content_factory_connections WHERE owner_id=? AND project_id=? AND platform=?", (owner_id, project_id, platform)).fetchone()
            row = {"id": previous["id"] if previous else _id(), "owner_id": owner_id, "project_id": project_id, "platform": str(platform), "account_id": str(account_id), "display_name": str(display_name), "token_encrypted": encrypted, "extra": _extra(extra), "created_at": previous["created_at"] if previous else timestamp, "updated_at": timestamp}
            if previous:
                db.execute("UPDATE content_factory_connections SET account_id=?,display_name=?,token_encrypted=?,extra=?,updated_at=? WHERE id=? AND owner_id=?", (row["account_id"], row["display_name"], encrypted, _json(row["extra"]), timestamp, previous["id"], owner_id))
            else:
                self._insert(db, "content_factory_connections", row)
            return _safe_connection(row)

    def list_connections(self, owner_id: int, project_id: str) -> list[dict]:
        with self._db() as db:
            return [_safe_connection(row) for row in db.execute("SELECT * FROM content_factory_connections WHERE owner_id=? AND project_id=? ORDER BY platform", (_owner(owner_id), _id(project_id)))]

    def get_connection(self, owner_id: int, project_id: str, platform: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM content_factory_connections WHERE owner_id=? AND project_id=? AND platform=?", (_owner(owner_id), _id(project_id), platform)).fetchone()
            return {**_safe_connection(row), "access_token": self._decrypt(row["token_encrypted"])} if row else None

    def delete_connection(self, owner_id: int, project_id: str, platform: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM content_factory_connections WHERE owner_id=? AND project_id=? AND platform=?", (_owner(owner_id), _id(project_id), platform))

    def refresh_connection_if_unchanged(self, owner_id: int, project_id: str, platform: str, previous: dict, renewed: dict) -> bool:
        filters, payload = self._refresh_payload(owner_id, project_id, platform, previous, renewed)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE content_factory_connections SET token_encrypted=?,extra=?,updated_at=? "
                "WHERE owner_id=? AND project_id=? AND platform=? AND id=? AND account_id=? AND updated_at=?",
                (payload["token_encrypted"], _json(payload["extra"]), payload["updated_at"], filters["owner_id"], filters["project_id"], filters["platform"], filters["id"], filters["account_id"], filters["updated_at"]),
            )
            return result.rowcount == 1

    def enqueue_job(self, owner_id: int, project_id: str, data: dict) -> dict:
        owner_id, project_id = _owner(owner_id), _id(project_id)
        self._require_project(owner_id, project_id)
        row = _job_row(owner_id, project_id, data)
        with self._db() as db:
            self._insert(db, "content_factory_jobs", row, "INSERT OR IGNORE")
            result = db.execute("SELECT * FROM content_factory_jobs WHERE owner_id=? AND project_id=? AND dedupe_key=?", (owner_id, project_id, row["dedupe_key"])).fetchone()
            if not result:
                raise StoreError("Не удалось создать уникальное задание.")
            return _unpack(result)

    def list_jobs(self, owner_id: int, project_id: str, limit: int = 100) -> list[dict]:
        with self._db() as db:
            return [_unpack(row) for row in db.execute("SELECT * FROM content_factory_jobs WHERE owner_id=? AND project_id=? ORDER BY scheduled_at DESC,created_at DESC LIMIT ?", (_owner(owner_id), _id(project_id), max(1, min(1000, int(limit)))))]

    def get_job(self, owner_id: int, job_id: str) -> dict | None:
        with self._db() as db:
            return _unpack(db.execute("SELECT * FROM content_factory_jobs WHERE owner_id=? AND id=?", (_owner(owner_id), _id(job_id))).fetchone())

    def update_job(self, owner_id: int, job_id: str, changes: dict, lease_owner: str | None = None, expected_updated_at: str | None = None) -> dict:
        owner_id, job_id = _owner(owner_id), _id(job_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = _unpack(db.execute("SELECT * FROM content_factory_jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone())
            if not previous:
                raise StoreError("Задание не найдено у этого владельца.")
            if expected_updated_at is not None and _time(previous["updated_at"]) != _time(expected_updated_at):
                raise StoreError("Материал уже изменён. Обновите список перед следующим действием.")
            if lease_owner is not None:
                if previous.get("lease_owner") != lease_owner or not previous.get("lease_until") or previous["lease_until"] <= _now():
                    raise StoreError("Право на обработку задания истекло или передано другому процессу.")
            elif previous.get("lease_until") and previous["lease_until"] > _now():
                raise StoreError("Задание сейчас обрабатывается. Дождитесь завершения шага.")
            row = _updated_job(previous, changes)
            keys = [key for key in row if key not in _IMMUTABLE]
            values = [_json(row[key]) if key == "data" else row[key] for key in keys]
            db.execute(f"UPDATE content_factory_jobs SET {','.join(key+'=?' for key in keys)} WHERE owner_id=? AND id=?", [*values, owner_id, job_id])
            return _unpack(row)

    def resume_stalled_video_job(self, owner_id: int, job_id: str, now: str, stale_seconds: int = 180) -> dict:
        owner_id, job_id = _owner(owner_id), _id(job_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM content_factory_jobs WHERE owner_id=? AND id=?",
                (owner_id, job_id),
            ).fetchone()
            job = _unpack(row)
            if not job:
                raise StoreError("Задача создания видео не найдена.")
            resume_at = _validated_stalled_video_resume(job, now, stale_seconds)
            changed = db.execute(
                "UPDATE content_factory_jobs SET lease_owner=NULL, lease_until=NULL, available_at=?, updated_at=? WHERE owner_id=? AND id=? AND updated_at=? AND state='queued'",
                (resume_at, resume_at, owner_id, job_id, job["updated_at"]),
            ).rowcount
            if changed != 1:
                raise StoreError("Состояние видео уже изменилось. Обновите страницу.")
            return _unpack(
                db.execute(
                    "SELECT * FROM content_factory_jobs WHERE owner_id=? AND id=?",
                    (owner_id, job_id),
                ).fetchone()
            )

    def claim_job(self, worker_id: str, now: str, lease_seconds: int = 900, owner_id: int | None = None, project_id: str | None = None) -> dict | None:
        now = _time(now)
        if not worker_id or len(worker_id) > 200:
            raise StoreError("Не определён процесс обработки.")
        lease_until = _time(datetime.fromisoformat(now) + timedelta(seconds=max(1, min(7200, int(lease_seconds)))))
        filters = ["state IN ('queued','scheduled')", "available_at<=?", "(lease_until IS NULL OR lease_until<=?)"]
        values: list = [now, now]
        if owner_id is not None:
            filters.append("owner_id=?")
            values.append(_owner(owner_id))
        if project_id is not None:
            filters.append("project_id=?")
            values.append(_id(project_id))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM content_factory_jobs WHERE " + " AND ".join(filters) + " ORDER BY available_at,created_at LIMIT 1", values).fetchone()
            if not row:
                return None
            db.execute("UPDATE content_factory_jobs SET lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", (worker_id, lease_until, now, row["id"]))
            return _unpack(db.execute("SELECT * FROM content_factory_jobs WHERE id=?", (row["id"],)).fetchone())

    def reserve_generation(self, owner_id: int, project_id: str, reservation_id: str, day: str, limit: int) -> bool:
        owner_id, project_id = _owner(owner_id), _id(project_id)
        self._require_project(owner_id, project_id)
        reservation_id, day, limit = self._reservation(reservation_id, day, limit)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM content_factory_generation_reservations WHERE owner_id=? AND project_id=? AND reservation_id=?", (owner_id, project_id, reservation_id)).fetchone():
                return True
            used = db.execute("SELECT COUNT(*) FROM content_factory_generation_reservations WHERE owner_id=? AND project_id=? AND day=?", (owner_id, project_id, day)).fetchone()[0]
            if used >= limit:
                return False
            db.execute("INSERT INTO content_factory_generation_reservations VALUES (?,?,?,?,?)", (owner_id, project_id, reservation_id, day, _now()))
            return True

    def put_asset(self, owner_id: int, project_id: str, job_id: str, filename: str, data: bytes, mime_type: str) -> dict:
        asset = self._asset(owner_id, project_id, job_id, filename, data, mime_type)
        path = self.media_dir / asset["storage_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return asset

    def resolve_assets(self, owner_id: int, assets: list[dict]) -> list[dict]:
        result = []
        for asset in assets:
            storage_path = self._asset_path(owner_id, asset)
            path = (self.media_dir / storage_path).resolve()
            if not path.is_relative_to(self.media_dir.resolve()) or not path.is_file():
                raise StoreError("Локальный медиафайл не найден.")
            result.append({**asset, "path": str(path), "url": f"{self.media_base_url}/{quote(storage_path, safe='/')}" if self.media_base_url else ""})
        return result

    def read_asset(self, owner_id: int, asset: dict) -> bytes:
        resolved = self.resolve_assets(owner_id, [asset])[0]
        path = Path(resolved["path"])
        if not 0 < path.stat().st_size <= 100 * 1024 * 1024:
            raise StoreError("Исходный медиафайл слишком велик.")
        return self._verified_asset_bytes(asset, path.read_bytes())


class SupabaseStore(_StoreBase):
    """Service-role client. Browser sessions never receive this credential."""

    def __init__(self, url: str, key: str, fernet_key: str | bytes | None = None, bucket: str = "content-factory-media", session=None):
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not key:
            raise StoreError("Нужны HTTPS SUPABASE_URL и серверный ключ Supabase.")
        self.url, self.key, self.fernet_key = url.rstrip("/"), key, fernet_key
        self.bucket = str(bucket)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.bucket):
            raise StoreError("Некорректное имя хранилища медиа.")
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, *, prefer: str | None = None, raw: bool = False, headers: dict | None = None, **kwargs):
        auth = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if prefer:
            auth["Prefer"] = prefer
        try:
            response = self.session.request(method, self.url + path, headers={**auth, **(headers or {})}, timeout=60, **kwargs)
        except requests.RequestException:
            raise StoreError("Нет соединения с Supabase. Проверьте доступ к проекту.") from None
        if not response.ok:
            raise StoreError(f"Supabase отклонил операцию (HTTP {response.status_code}). Проверьте серверный ключ и миграцию content_factory_setup.sql.")
        if raw:
            return response
        try:
            return response.json() if response.content else []
        except (ValueError, TypeError):
            raise StoreError("Supabase вернул некорректный ответ.") from None

    def _rows(self, table: str, params: dict) -> list[dict]:
        result = self._request("GET", "/rest/v1/" + table, params=params)
        if not isinstance(result, list):
            raise StoreError("Supabase вернул некорректный список.")
        return result

    def _require_project(self, owner_id: int, project_id: str) -> dict:
        result = self.get_project(owner_id, project_id)
        if not result:
            raise StoreError("Проект не найден у этого владельца.")
        return result

    def save_project(self, owner_id: int, data: dict) -> dict:
        owner_id = _owner(owner_id)
        previous = self.get_project(owner_id, data["id"]) if data.get("id") else None
        row = _project_row(owner_id, data, previous)
        if previous:
            result = self._request("PATCH", "/rest/v1/content_factory_projects", params={"owner_id": f"eq.{owner_id}", "id": f"eq.{row['id']}"}, json={key: value for key, value in row.items() if key not in {"id", "owner_id", "created_at"}}, prefer="return=representation")
        else:
            result = self._request("POST", "/rest/v1/content_factory_projects", json=row, prefer="return=representation")
        if not result:
            raise StoreError("Не удалось сохранить проект.")
        return _unpack(result[0])

    def get_project(self, owner_id: int, project_id: str) -> dict | None:
        rows = self._rows("content_factory_projects", {"owner_id": f"eq.{_owner(owner_id)}", "id": f"eq.{_id(project_id)}", "limit": 1})
        return _unpack(rows[0]) if rows else None

    def list_projects(self, owner_id: int) -> list[dict]:
        return [_unpack(row) for row in self._rows("content_factory_projects", {"owner_id": f"eq.{_owner(owner_id)}", "order": "created_at.asc"})]

    def list_active_projects(self) -> list[dict]:
        return [_unpack(row) for row in self._rows("content_factory_projects", {"automation_enabled": "eq.true", "order": "created_at.asc"})]

    def _owner_instagram_connection(self, owner_id: int, project_id: str, *, with_token: bool = False) -> dict | None:
        """Expose the Agency-wide Instagram owner login to any of their projects."""
        rows = self._rows(
            "agency_instagram_connections",
            {
                "owner_telegram_id": f"eq.{_owner(owner_id)}",
                "status": "eq.connected",
                "select": (
                    "owner_telegram_id,instagram_account_id,instagram_username,"
                    "access_token_encrypted,token_expires_at,status,connected_at,updated_at"
                ),
                "limit": 1,
            },
        )
        if not rows:
            return None
        row = rows[0]
        account_id = str(row.get("instagram_account_id") or "").strip()
        if not account_id:
            return None
        result = {
            "id": f"owner-instagram-{_owner(owner_id)}",
            "owner_id": _owner(owner_id),
            "project_id": _id(project_id),
            "platform": "instagram",
            "account_id": account_id,
            "display_name": str(row.get("instagram_username") or account_id),
            "extra": {
                "owner_level": True,
                "direct_enabled": True,
                "token_expires_at": row.get("token_expires_at"),
                "connected_at": row.get("connected_at"),
            },
            "created_at": row.get("connected_at") or row.get("updated_at") or _now(),
            "updated_at": row.get("updated_at") or row.get("connected_at") or _now(),
            "has_token": True,
        }
        if with_token:
            encrypted = str(row.get("access_token_encrypted") or "").strip()
            if not encrypted:
                return None
            # agency_instagram_connections is encrypted by the Instagram
            # webhook service with the shared FERNET_KEY. Do not assume the
            # Content Factory's optional CONTENT_FACTORY_FERNET_KEY is equal.
            instagram_key = str(os.environ.get("FERNET_KEY") or "").strip()
            if not instagram_key:
                raise StoreError("Не настроен общий ключ Instagram-подключений.")
            try:
                result["access_token"] = Fernet(
                    instagram_key.encode("utf-8")
                ).decrypt(encrypted.encode("utf-8")).decode("utf-8")
            except (InvalidToken, ValueError, TypeError):
                raise StoreError(
                    "Instagram-подключение не удалось открыть. Переподключите аккаунт."
                ) from None
        return result

    def save_connection(self, owner_id: int, project_id: str, platform: str, account_id: str, display_name: str, access_token: str, extra: dict | None = None) -> dict:
        owner_id, project_id = _owner(owner_id), _id(project_id)
        self._require_project(owner_id, project_id)
        row = {"owner_id": owner_id, "project_id": project_id, "platform": str(platform), "account_id": str(account_id), "display_name": str(display_name), "token_encrypted": self._encrypt(access_token), "extra": _extra(extra), "updated_at": _now()}
        result = self._request("POST", "/rest/v1/content_factory_connections", params={"on_conflict": "owner_id,project_id,platform"}, json=row, prefer="resolution=merge-duplicates,return=representation")
        if not result:
            raise StoreError("Не удалось сохранить подключение.")
        return _safe_connection(result[0])

    def list_connections(self, owner_id: int, project_id: str) -> list[dict]:
        fields = "id,owner_id,project_id,platform,account_id,display_name,extra,created_at,updated_at"
        result = [_safe_connection(row) for row in self._rows(
            "content_factory_connections",
            {
                "owner_id": f"eq.{_owner(owner_id)}",
                "project_id": f"eq.{_id(project_id)}",
                "select": fields,
                "order": "platform.asc",
            },
        )]
        # The owner-level Instagram login is canonical. A legacy project-only
        # Instagram row remains only as fallback until the owner reconnects.
        try:
            owner_instagram = self._owner_instagram_connection(owner_id, project_id)
        except Exception:
            owner_instagram = None
        if owner_instagram:
            result = [item for item in result if item.get("platform") != "instagram"]
            result.append(owner_instagram)
        return sorted(result, key=lambda item: str(item.get("platform") or ""))

    def get_connection(self, owner_id: int, project_id: str, platform: str) -> dict | None:
        if platform == "instagram":
            try:
                owner_instagram = self._owner_instagram_connection(
                    owner_id, project_id, with_token=True
                )
            except Exception:
                owner_instagram = None
            if owner_instagram:
                return owner_instagram
        rows = self._rows(
            "content_factory_connections",
            {
                "owner_id": f"eq.{_owner(owner_id)}",
                "project_id": f"eq.{_id(project_id)}",
                "platform": f"eq.{platform}",
                "limit": 1,
            },
        )
        return (
            {**_safe_connection(rows[0]), "access_token": self._decrypt(rows[0]["token_encrypted"])}
            if rows else None
        )

    def delete_connection(self, owner_id: int, project_id: str, platform: str) -> None:
        self._request("DELETE", "/rest/v1/content_factory_connections", params={"owner_id": f"eq.{_owner(owner_id)}", "project_id": f"eq.{_id(project_id)}", "platform": f"eq.{platform}"}, raw=True)

    def refresh_connection_if_unchanged(self, owner_id: int, project_id: str, platform: str, previous: dict, renewed: dict) -> bool:
        filters, payload = self._refresh_payload(owner_id, project_id, platform, previous, renewed)
        result = self._request("PATCH", "/rest/v1/content_factory_connections", params={key: "eq." + str(value) for key, value in filters.items()}, json=payload, prefer="return=representation")
        return bool(result)

    def enqueue_job(self, owner_id: int, project_id: str, data: dict) -> dict:
        owner_id, project_id = _owner(owner_id), _id(project_id)
        self._require_project(owner_id, project_id)
        row = _job_row(owner_id, project_id, data)
        result = self._request("POST", "/rest/v1/content_factory_jobs", params={"on_conflict": "owner_id,project_id,dedupe_key"}, json=row, prefer="resolution=ignore-duplicates,return=representation")
        if result:
            return _unpack(result[0])
        rows = self._rows("content_factory_jobs", {"owner_id": f"eq.{owner_id}", "project_id": f"eq.{project_id}", "dedupe_key": f"eq.{row['dedupe_key']}", "limit": 1})
        if not rows:
            raise StoreError("Не удалось создать уникальное задание.")
        return _unpack(rows[0])

    def list_jobs(self, owner_id: int, project_id: str, limit: int = 100) -> list[dict]:
        return [_unpack(row) for row in self._rows("content_factory_jobs", {"owner_id": f"eq.{_owner(owner_id)}", "project_id": f"eq.{_id(project_id)}", "order": "scheduled_at.desc,created_at.desc", "limit": max(1, min(1000, int(limit)))})]

    def get_job(self, owner_id: int, job_id: str) -> dict | None:
        rows = self._rows("content_factory_jobs", {"owner_id": f"eq.{_owner(owner_id)}", "id": f"eq.{_id(job_id)}", "limit": 1})
        return _unpack(rows[0]) if rows else None

    def update_job(self, owner_id: int, job_id: str, changes: dict, lease_owner: str | None = None, expected_updated_at: str | None = None) -> dict:
        previous = self.get_job(owner_id, job_id)
        if not previous:
            raise StoreError("Задание не найдено у этого владельца.")
        params = {"owner_id": f"eq.{_owner(owner_id)}", "id": f"eq.{_id(job_id)}"}
        expected = expected_updated_at if expected_updated_at is not None else previous.get("updated_at")
        if expected is not None:
            if previous.get("updated_at") and _time(previous["updated_at"]) != _time(expected):
                raise StoreError("Материал уже изменён. Обновите список перед следующим действием.")
            params["updated_at"] = "eq." + _time(expected)
        if lease_owner is not None:
            params["lease_owner"] = "eq." + str(lease_owner)
            params["lease_until"] = "gt." + _now()
        else:
            params["or"] = f"(lease_until.is.null,lease_until.lte.{_now()})"
        row = _updated_job(previous, changes)
        result = self._request("PATCH", "/rest/v1/content_factory_jobs", params=params, json={key: value for key, value in row.items() if key not in _IMMUTABLE}, prefer="return=representation")
        if not result:
            raise StoreError("Материал изменён или уже обрабатывается. Обновите список.")
        return _unpack(result[0])

    def resume_stalled_video_job(self, owner_id: int, job_id: str, now: str, stale_seconds: int = 180) -> dict:
        owner_id, job_id = _owner(owner_id), _id(job_id)
        job = self.get_job(owner_id, job_id)
        if not job:
            raise StoreError("Задача создания видео не найдена.")
        resume_at = _validated_stalled_video_resume(job, now, stale_seconds)
        result = self._request(
            "PATCH",
            "/rest/v1/content_factory_jobs",
            params={
                "owner_id": f"eq.{owner_id}",
                "id": f"eq.{job_id}",
                "state": "eq.queued",
                "updated_at": "eq." + _time(job["updated_at"]),
            },
            json={
                "lease_owner": None,
                "lease_until": None,
                "available_at": resume_at,
                "updated_at": resume_at,
            },
            prefer="return=representation",
        )
        if not result:
            raise StoreError("Состояние видео уже изменилось. Обновите страницу.")
        return _unpack(result[0])

    def claim_job(self, worker_id: str, now: str, lease_seconds: int = 900, owner_id: int | None = None, project_id: str | None = None) -> dict | None:
        if not worker_id or len(worker_id) > 200:
            raise StoreError("Не определён процесс обработки.")
        result = self._request("POST", "/rest/v1/rpc/content_factory_claim_job", json={"p_worker_id": worker_id, "p_now": _time(now), "p_lease_seconds": max(1, min(7200, int(lease_seconds))), "p_owner_id": _owner(owner_id) if owner_id is not None else None, "p_project_id": _id(project_id) if project_id is not None else None})
        return _unpack(result[0]) if result else None

    def reserve_generation(self, owner_id: int, project_id: str, reservation_id: str, day: str, limit: int) -> bool:
        reservation_id, day, limit = self._reservation(reservation_id, day, limit)
        result = self._request("POST", "/rest/v1/rpc/content_factory_reserve_generation", json={"p_owner_id": _owner(owner_id), "p_project_id": _id(project_id), "p_reservation_id": reservation_id, "p_day": day, "p_limit": limit})
        if not isinstance(result, bool):
            raise StoreError("Не удалось проверить дневной лимит генерации.")
        return result

    def put_asset(self, owner_id: int, project_id: str, job_id: str, filename: str, data: bytes, mime_type: str) -> dict:
        asset = self._asset(owner_id, project_id, job_id, filename, data, mime_type)
        self._request("POST", f"/storage/v1/object/{self.bucket}/{quote(asset['storage_path'], safe='/')}", headers={"Content-Type": mime_type, "x-upsert": "true"}, data=data, raw=True)
        return asset

    def read_asset(self, owner_id: int, asset: dict) -> bytes:
        path = self._asset_path(owner_id, asset)
        response = self._request("GET", f"/storage/v1/object/{self.bucket}/{quote(path, safe='/')}", raw=True, stream=True, allow_redirects=False)
        try:
            output = bytearray()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                output.extend(chunk)
                if len(output) > 100 * 1024 * 1024:
                    raise StoreError("Исходный медиафайл слишком велик.")
            return self._verified_asset_bytes(asset, bytes(output))
        except requests.RequestException:
            raise StoreError("Не удалось прочитать исходный медиафайл из хранилища.") from None
        finally:
            response.close()

    def resolve_assets(self, owner_id: int, assets: list[dict]) -> list[dict]:
        result = []
        for asset in assets:
            path = self._asset_path(owner_id, asset)
            payload = self._request("POST", f"/storage/v1/object/sign/{self.bucket}/{quote(path, safe='/')}", json={"expiresIn": 3600})
            signed = str(payload.get("signedURL") or payload.get("signedUrl") or "") if isinstance(payload, dict) else ""
            if not signed:
                raise StoreError("Не удалось создать временный адрес медиа.")
            if signed.startswith("/"):
                signed = self.url + ("/storage/v1" if not signed.startswith("/storage/v1/") else "") + signed
            parsed = urlparse(signed)
            if parsed.scheme != "https" or parsed.hostname != urlparse(self.url).hostname:
                raise StoreError("Хранилище вернуло неподдерживаемый адрес медиа.")
            result.append({**asset, "url": signed})
        return result
