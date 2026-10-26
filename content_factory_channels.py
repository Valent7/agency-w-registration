"""Resumable publishing adapters. Credentials remain in the calling owner service.

No requests are made on import. A persisted ``publish_started`` checkpoint is a
barrier: ambiguous public writes are never automatically repeated.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import tempfile
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests


class PublishError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class PublishPending(RuntimeError):
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class PublishUncertain(RuntimeError):
    pass


def _public_url(value: str) -> str:
    """Reject non-public URL literals; providers fetch Graph assets themselves."""
    try:
        parts = urlsplit(str(value or ""))
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError as exc:
        raise PublishError("Некорректный адрес медиа.") from exc
    if (
        parts.scheme != "https" or not host or parts.username or parts.password
        or port not in (None, 443) or parts.fragment
        or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host or host.isdigit() or host.startswith("0x")
        or not parts.path
    ):
        raise PublishError("Медиа должны иметь публичный HTTPS-адрес.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject legacy abbreviated/octal IPv4 spellings accepted by some clients.
        if re.fullmatch(r"[0-9.]+", host):
            raise PublishError("Некорректный адрес медиа.")
    else:
        if not address.is_global:
            raise PublishError("Локальные и приватные адреса медиа запрещены.")
    return str(value)


def _save(state: dict, update: dict, persist: Callable[[dict], None]) -> None:
    candidate = {**state, **deepcopy(update)}
    try:
        persist(deepcopy(candidate))
    except Exception as exc:
        raise PublishError("Не удалось сохранить этап публикации.", retryable=True) from exc
    state.clear()
    state.update(candidate)


def _response_json(response, *, public_write: bool = False) -> dict | list:
    code = response.status_code
    if public_write and (code >= 500 or code == 408):
        raise PublishUncertain("Сервис не подтвердил результат отправки. Требуется сверка публикации.")
    if code >= 300:
        raise PublishError(
            f"Сервис отклонил запрос (HTTP {int(code)}).",
            retryable=code in (408, 425, 429) or code >= 500,
        )
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        if public_write:
            raise PublishUncertain("Результат отправки не распознан. Требуется сверка публикации.") from exc
        raise PublishError("Сервис вернул некорректный ответ.", retryable=True) from exc
    if not isinstance(data, (dict, list)):
        if public_write:
            raise PublishUncertain("Результат отправки не распознан. Требуется сверка публикации.")
        raise PublishError("Сервис вернул некорректный ответ.", retryable=True)
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        error_code = error.get("code", error.get("error_code")) if isinstance(error, dict) else None
        retryable = error_code in (1, 2, 4, 6, 9, 17, 29, 32, 613)
        raise PublishError("Сервис отклонил публикацию. Проверьте права подключения и ограничения аккаунта.", retryable)
    return data


def _request(session, method: str, url: str, *, public_write: bool = False, **kwargs):
    try:
        response = getattr(session, method)(url, timeout=(15, 120), allow_redirects=False, **kwargs)
    except requests.RequestException:
        if public_write:
            raise PublishUncertain("Связь прервалась при отправке. Требуется сверка публикации.") from None
        raise PublishError("Нет подтверждённого ответа сервиса.", retryable=True) from None
    return _response_json(response, public_write=public_write)


def _identifier(data, key: str = "id") -> str:
    value = str(data.get(key) or "") if isinstance(data, dict) else ""
    if not value or not re.fullmatch(r"-?\d+(?:_\d+)?", value):
        raise PublishError("Сервис не вернул идентификатор материала.", retryable=True)
    return value


def _write_once(session, url, *, state, persist, data, result_fn, headers=None):
    _save(state, {"publish_started": True}, persist)
    try:
        response = _request(session, "post", url, data=data, headers=headers, public_write=True)
    except PublishError:
        # An explicit provider rejection is known not to have published anything.
        _save(state, {"publish_started": False}, persist)
        raise
    try:
        result = result_fn(response)
    except Exception as exc:
        raise PublishUncertain("Сервис принял запрос, но результат требует сверки.") from exc
    try:
        _save(state, {"result": result, "publish_started": False}, persist)
    except PublishError as exc:
        raise PublishUncertain("Публикация отправлена, но её результат не сохранён. Требуется сверка.") from exc
    return result


def _ready(session, base, container_id, platform, headers):
    field = "status_code" if platform == "instagram" else "status"
    data = _request(session, "get", f"{base}/{container_id}", headers=headers,
                    params={"fields": field + ",error_message" if platform == "threads" else field})
    status = str(data.get(field) or "").upper() if isinstance(data, dict) else ""
    if status == "FINISHED":
        return
    if status == "PUBLISHED":
        raise PublishUncertain("Контейнер уже опубликован. Требуется сверка результата.")
    if status in ("ERROR", "EXPIRED"):
        raise PublishError("Соцсеть не подготовила медиа: контейнер отклонён или истёк.")
    if status not in ("IN_PROGRESS", ""):
        raise PublishError("Соцсеть вернула неизвестный статус подготовки медиа.")
    raise PublishPending("Соцсеть обрабатывает материал.")


def _graph(platform, connection, content, assets, state, persist, session):
    extra = connection.get("extra") or {}
    version = str(extra.get("api_version") or ("v23.0" if platform == "instagram" else "v1.0"))
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise PublishError("Некорректная версия API соцсети.")
    host = "graph.instagram.com" if platform == "instagram" else "graph.threads.net"
    base = f"https://{host}/{version}"
    account = str(connection["account_id"])
    headers = {"Authorization": "Bearer " + connection["access_token"]}
    endpoint = "media" if platform == "instagram" else "threads"
    text = content["text"]
    fmt = content["format"]
    caption_key = "caption" if platform == "instagram" else "text"

    def media_payload(asset, *, child=False):
        kind = "VIDEO" if asset["mime_type"].startswith("video/") else "IMAGE"
        payload = {"video_url" if kind == "VIDEO" else "image_url": asset["url"]}
        if platform == "threads" or kind == "VIDEO":
            payload["media_type"] = "REELS" if platform == "instagram" and not child else kind
        if child:
            payload["is_carousel_item"] = "true"
        return payload

    if not state.get("container_id"):
        if fmt == "carousel":
            children = list(state.get("children") or [])
            for index, asset in enumerate(assets):
                if index >= len(children):
                    response = _request(session, "post", f"{base}/{account}/{endpoint}",
                                        headers=headers, data=media_payload(asset, child=True))
                    children.append(_identifier(response))
                    _save(state, {"children": children}, persist)
                _ready(session, base, children[index], platform, headers)
            payload = {"media_type": "CAROUSEL", "children": ",".join(children), caption_key: text}
        elif assets:
            payload = {**media_payload(assets[0]), caption_key: text}
        else:
            payload = {"media_type": "TEXT", "text": text}
        response = _request(session, "post", f"{base}/{account}/{endpoint}", headers=headers, data=payload)
        _save(state, {"container_id": _identifier(response)}, persist)

    container_id = str(state["container_id"])
    _ready(session, base, container_id, platform, headers)
    publish_endpoint = "media_publish" if platform == "instagram" else "threads_publish"

    def completed(response):
        external_id = _identifier(response)
        # The provider permalink is fetched separately; failure must not turn a
        # successful write into a retry of that write.
        return {"external_id": external_id, "permalink": ""}

    result = _write_once(session, f"{base}/{account}/{publish_endpoint}", state=state, persist=persist,
                         data={"creation_id": container_id}, headers=headers, result_fn=completed)
    try:
        data = _request(session, "get", f"{base}/{result['external_id']}",
                        headers=headers, params={"fields": "permalink"})
        permalink = str(data.get("permalink") or "") if isinstance(data, dict) else ""
        if permalink:
            _public_url(permalink)
            result = {**result, "permalink": permalink}
            _save(state, {"result": result}, persist)
    except (PublishError, PublishPending, PublishUncertain):
        pass
    return result


@contextmanager
def _asset_file(asset, session):
    maximum = 300 * 1024 * 1024 if asset["mime_type"].startswith("video/") else 20 * 1024 * 1024
    path = asset.get("path")
    if path:
        try:
            source = Path(path)
            if not source.is_file() or not 0 < source.stat().st_size <= maximum:
                raise PublishError("Файл медиа отсутствует или превышает допустимый размер.")
            with source.open("rb") as handle:
                yield source.name, handle
        except OSError as exc:
            raise PublishError("Не удалось прочитать файл медиа.") from exc
        return
    url = _public_url(asset.get("url", ""))
    response = None
    try:
        # Never forward credentials to a media URL, and validate every redirect.
        for _ in range(4):
            response = session.get(url, timeout=(15, 120), stream=True, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                next_url = _public_url(urljoin(url, response.headers.get("Location", "")))
                response.close()
                response = None
                url = next_url
                continue
            if response.status_code != 200:
                raise PublishError("Не удалось загрузить медиа для публикации.", retryable=True)
            break
        else:
            raise PublishError("Слишком много перенаправлений адреса медиа.")
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as handle:
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise PublishError("Медиа превышает допустимый размер.")
                handle.write(chunk)
            if size == 0:
                raise PublishError("Файл медиа пуст.")
            handle.seek(0)
            suffix = ".mp4" if asset["mime_type"].startswith("video/") else ".jpg"
            yield "content" + suffix, handle
    except requests.RequestException:
        raise PublishError("Не удалось загрузить медиа для публикации.", retryable=True) from None
    finally:
        if response is not None:
            response.close()


def _vk(connection, content, assets, state, persist, session):
    extra = connection.get("extra") or {}
    try:
        account = int(connection["account_id"])
        group = abs(int(extra.get("group_id") or 0))
    except (TypeError, ValueError) as exc:
        raise PublishError("Некорректная площадка ВК.") from exc
    owner = -group if group else account
    if not owner:
        raise PublishError("Не указана площадка ВК.")
    group = abs(owner) if owner < 0 else 0
    version = str(extra.get("api_version") or "5.199")
    if not re.fullmatch(r"\d+\.\d+", version):
        raise PublishError("Некорректная версия API ВК.")
    common = {"access_token": connection["access_token"], "v": version}
    target = {"group_id": group} if group else {}

    def call(method, **params):
        data = _request(session, "post", f"https://api.vk.com/method/{method}",
                        data={**common, **params})
        if not isinstance(data, dict) or "response" not in data:
            raise PublishError("ВК не вернул результат запроса.", retryable=True)
        return data["response"]

    attachments = list(state.get("vk_attachments") or [])
    for index, asset in enumerate(assets):
        if index < len(attachments):
            continue
        video = asset["mime_type"].startswith("video/")
        info = call("video.save", name=content.get("title") or "Видео", **target) if video else call(
            "photos.getWallUploadServer", **target)
        if not isinstance(info, dict):
            raise PublishError("ВК не подготовил загрузку медиа.")
        upload_url = _public_url(str(info.get("upload_url") or ""))
        with _asset_file(asset, session) as (filename, handle):
            uploaded = _request(session, "post", upload_url,
                                files={"video_file" if video else "photo": (filename, handle, asset["mime_type"])})
        if video:
            if not isinstance(uploaded, dict):
                raise PublishError("ВК не подтвердил загрузку видео.")
            media_owner = uploaded.get("owner_id")
            media_id = uploaded.get("video_id")
            attachment = "video" + _identifier({"id": media_owner}) + "_" + _identifier({"id": media_id})
            access_key = str(uploaded.get("access_key") or info.get("access_key") or "")
        else:
            if not isinstance(uploaded, dict) or not all(key in uploaded for key in ("photo", "server", "hash")):
                raise PublishError("ВК не подтвердил загрузку изображения.")
            saved = call("photos.saveWallPhoto", photo=uploaded["photo"], server=uploaded["server"],
                         hash=uploaded["hash"], **target)
            if not isinstance(saved, list) or len(saved) != 1:
                raise PublishError("ВК не сохранил изображение.")
            attachment = "photo" + _identifier(saved[0], "owner_id") + "_" + _identifier(saved[0])
            access_key = str(saved[0].get("access_key") or "")
        if access_key:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", access_key):
                raise PublishError("ВК вернул некорректный идентификатор медиа.")
            attachment += "_" + access_key
        attachments.append(attachment)
        _save(state, {"vk_attachments": attachments}, persist)

    payload = {**common, "owner_id": owner, "message": content["text"],
               "attachments": ",".join(attachments), "guid": state["fingerprint"][:16]}
    if group:
        payload["from_group"] = 1

    def completed(response):
        external_id = _identifier(response.get("response"), "post_id")
        return {"external_id": f"{owner}_{external_id}", "permalink": f"https://vk.com/wall{owner}_{external_id}"}

    return _write_once(session, "https://api.vk.com/method/wall.post", state=state, persist=persist,
                       data=payload, result_fn=completed)


def publish(platform: str, connection: dict, content: dict, assets: list[dict], checkpoint: dict,
            save_checkpoint: Callable[[dict], None], session=None) -> dict:
    """Publish one immutable owner-scoped job; callers atomically claim jobs.

    ``save_checkpoint`` must durably replace this job's checkpoint before it
    returns. Pending processing can be retried. ``PublishUncertain`` must be
    reconciled by the operator and must never cause an automatic resend.
    """
    if platform not in ("instagram", "threads", "vk"):
        raise PublishError("Площадка не поддерживается.")
    if not isinstance(connection, dict) or not str(connection.get("access_token") or "").strip():
        raise PublishError("Площадка не подключена или отсутствует доступ для публикации.")
    account = str(connection.get("account_id") or "")
    if not re.fullmatch(r"-?\d+", account) or int(account) == 0 or (platform != "vk" and int(account) < 0):
        raise PublishError("Некорректный идентификатор аккаунта.")
    if not isinstance(content, dict) or content.get("format") not in ("post", "carousel", "reel"):
        raise PublishError("Неподдерживаемый формат материала.")
    text = str(content.get("text") or "").strip()
    limit = {"instagram": 2200, "threads": 500, "vk": 16384}[platform]
    if len(text) > limit:
        raise PublishError(f"Текст для этой площадки превышает {limit} символов.")
    if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
        raise PublishError("Некорректный список медиа.")
    assets = [dict(asset) for asset in assets]
    fmt = content["format"]
    if fmt == "carousel" and not 2 <= len(assets) <= (20 if platform == "threads" else 10):
        raise PublishError("Для карусели требуется от 2 до допустимого числа медиа.")
    if fmt != "carousel" and len(assets) > 1:
        raise PublishError("Для нескольких медиа выберите карусель.")
    if fmt == "reel" and (len(assets) != 1 or assets[0].get("mime_type") != "video/mp4"):
        raise PublishError("Для ролика требуется один MP4-файл.")
    if platform == "instagram" and not assets:
        raise PublishError("Для Instagram требуется изображение, карусель или ролик.")
    if not text and not assets:
        raise PublishError("Материал пуст.")
    for asset in assets:
        mime = str(asset.get("mime_type") or "")
        if mime not in ("image/jpeg", "image/png", "video/mp4"):
            raise PublishError("Неподдерживаемый тип медиа.")
        if platform == "instagram" and mime == "image/png":
            raise PublishError("Для Instagram подготовьте изображение JPEG.")
        if fmt == "post" and mime == "video/mp4":
            raise PublishError("Для видео выберите формат ролика.")
        if platform in ("instagram", "threads") or not asset.get("path"):
            _public_url(asset.get("url", ""))
    content = {**content, "text": text}
    identity = {"platform": platform, "account": account,
                "extra": {key: value for key, value in (connection.get("extra") or {}).items() if key in {"group_id", "api_version"}},
                "content": content, "assets": [
                    {"path": str(item.get("path") or ""), "url": str(item.get("url") or "").split("?", 1)[0],
                     "mime_type": item["mime_type"]} for item in assets]}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    state = deepcopy(checkpoint or {})
    if state.get("fingerprint") not in (None, fingerprint):
        raise PublishError("Этапы публикации принадлежат другой версии материала или площадке.")
    if state.get("result"):
        return dict(state["result"])
    if state.get("publish_started"):
        raise PublishUncertain("Отправка уже начиналась. Требуется сверка перед повторной публикацией.")
    if not state.get("fingerprint"):
        _save(state, {"fingerprint": fingerprint}, save_checkpoint)
    transport = session if session is not None else requests
    if platform == "vk":
        return _vk(connection, content, assets, state, save_checkpoint, transport)
    return _graph(platform, connection, content, assets, state, save_checkpoint, transport)
