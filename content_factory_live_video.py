"""Small, non-retrying Runway client for durable live-video production.

The caller owns task checkpoints and scheduling. This module never polls in a
loop, sleeps, publishes, or starts another paid task after an uncertain result.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import math
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps
from requests.sessions import Session as _RequestsSession


BASE_URL = "https://api.dev.runwayml.com/v1"
API_VERSION = "2024-11-06"
MAX_CLIP_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DATA_URI_BYTES = 5 * 1024 * 1024
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


class LiveVideoError(RuntimeError):
    """A bounded, credential-free error that is safe for the factory UI."""

    def __init__(self, message, *, retryable=False, code=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = code


class LiveVideoUncertain(LiveVideoError):
    """The paid creation might have been accepted; do not repeat its POST."""

    creation_uncertain = True


class LiveVideoPending(Exception):
    """The caller should release its lease and read known task IDs later."""

    def __init__(self, message="Видео ещё создаётся.", retry_after=30):
        super().__init__(message)
        self.retry_after = max(15, min(int(retry_after), 3600))


def _task_id(value):
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise LiveVideoError("Не удалось определить задачу создания видео.", code="invalid_task_id")
    return value


def _source_image(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise LiveVideoError("Для живой сцены нужно изображение размером до 20 МБ.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 25_000_000:
                raise LiveVideoError("Исходное изображение сцены слишком большое.")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
        # Runway produces a vertical 720×1280 scene. Keep the source within the
        # provider's data-URI limit without exposing an external image URL.
        image.thumbnail((1280, 1536), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        image.close()
        result = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise LiveVideoError("Не удалось прочитать исходное изображение сцены.") from error
    if len(result.encode("ascii")) > MAX_DATA_URI_BYTES:
        raise LiveVideoError("Изображение сцены превышает ограничение видеосервиса.")
    return result


def _public_url(value):
    """Validate every download and redirect target, including DNS answers."""
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise LiveVideoError("Видеосервис вернул недопустимый адрес видео.", code="invalid_video_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise LiveVideoError("Видеосервис вернул недопустимый адрес видео.", code="invalid_video_url") from None
    if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise LiveVideoError("Видеосервис вернул небезопасный адрес видео.", code="invalid_video_url")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host or host.endswith(".")
                or host.lower().endswith((".localhost", ".local", ".internal"))):
            raise LiveVideoError("Видеосервис вернул небезопасный адрес видео.", code="invalid_video_url") from None
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        except (OSError, ValueError):
            raise LiveVideoError("Не удалось проверить адрес готового видео.", retryable=True, code="video_dns_unavailable") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise LiveVideoError("Видеосервис вернул небезопасный адрес видео.", code="invalid_video_url")
    return value


def _json_response(response, *, creation=False):
    try:
        payload = response.json()
    except (ValueError, TypeError):
        exception = LiveVideoUncertain if creation else LiveVideoError
        raise exception("Видеосервис вернул непонятный ответ. Проверьте сохранённую задачу перед повтором.",
                        retryable=not creation, code="invalid_response") from None
    if not isinstance(payload, dict):
        exception = LiveVideoUncertain if creation else LiveVideoError
        raise exception("Видеосервис вернул ответ в неверном формате.", retryable=not creation, code="invalid_response")
    return payload


def _known_failure(status, *, reading=False):
    if status in (401, 403):
        return LiveVideoError("Не удалось подтвердить доступ к созданию живого видео. Проверьте подключение видеосервиса.", code="video_access_denied")
    if status == 402:
        return LiveVideoError("Недостаточно средств в видеосервисе для создания сцены.", code="video_credits_required")
    if status == 429:
        return LiveVideoError("Видеосервис временно ограничил запросы. Повторная платная задача не запускалась.",
                              retryable=reading, code="video_rate_limit")
    return LiveVideoError("Видеосервис отклонил запрос. Проверьте настройки живого видео.",
                          retryable=reading and status >= 500, code="video_request_rejected")


class RunwayVideoClient:
    def __init__(self, api_key, model="gen4.5", session=None, *, download_session=None):
        if (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 2048
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key.strip())):
            raise LiveVideoError("Подключите видеосервис для создания живых сцен.", code="video_key_missing")
        if model not in {"gen4.5", "gen4_turbo"}:
            raise LiveVideoError("Неизвестная настройка модели живого видео.", code="invalid_video_model")
        self.api_key = api_key.strip()
        self.model = model
        self.session = session if session is not None else requests.Session()
        if isinstance(self.session, _RequestsSession):
            self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        # Provider credentials are never attached to requests for output files.
        self.download_session = download_session if download_session is not None else requests.Session()
        self.download_session.trust_env = False
        self.download_session.auth = None
        if isinstance(getattr(self.download_session, "headers", None), dict):
            self.download_session.headers.pop("Authorization", None)

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key, "X-Runway-Version": API_VERSION,
                "Content-Type": "application/json"}

    def preflight(self):
        try:
            response = self.session.get(BASE_URL + "/organization", headers=self._headers(),
                                        timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise LiveVideoError("Не удалось проверить подключение видеосервиса.", retryable=True,
                                 code="video_preflight_unavailable") from None
        try:
            if response.status_code in (404, 405):
                return {"key_valid": None, "credit_balance": None, "supported": False}
            if not 200 <= response.status_code < 300:
                raise _known_failure(response.status_code, reading=True)
            payload = _json_response(response)
            value = payload.get("creditBalance")
            balance = None
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0):
                balance = value
            return {"key_valid": True, "credit_balance": balance, "supported": True}
        finally:
            response.close()

    def create_scene(self, image_bytes, prompt, duration):
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise LiveVideoError("Опишите действие для живой сцены.")
        prompt = prompt.strip()
        try:
            valid_prompt = len(prompt.encode("utf-16-le")) // 2 <= 1000
        except UnicodeEncodeError:
            valid_prompt = False
        if not valid_prompt:
            raise LiveVideoError("Описание движения сцены слишком длинное.")
        allowed_duration = range(2, 11) if self.model == "gen4.5" else (5, 10)
        if type(duration) is not int or duration not in allowed_duration:
            raise LiveVideoError("Неверная длительность живой сцены.")
        payload = {"model": self.model, "promptImage": _source_image(image_bytes),
                   "promptText": prompt, "ratio": "720:1280", "duration": duration}
        try:
            response = self.session.post(BASE_URL + "/image_to_video", headers=self._headers(), json=payload,
                                         timeout=(10, 120), allow_redirects=False)
        except requests.RequestException:
            raise LiveVideoUncertain("Связь прервалась при заказе сцены. Видеосервис мог принять её; повторный заказ остановлен.",
                                     code="video_creation_uncertain") from None
        try:
            if not 200 <= response.status_code < 300:
                if 400 <= response.status_code < 500:
                    raise _known_failure(response.status_code)
                raise LiveVideoUncertain("Не получено подтверждение заказа сцены. Проверьте задачу перед повтором.",
                                         code="video_creation_uncertain")
            payload = _json_response(response, creation=True)
            try:
                return _task_id(payload.get("id"))
            except LiveVideoError:
                raise LiveVideoUncertain("Видеосервис не вернул номер заказанной сцены. Повторный заказ остановлен.",
                                         code="video_creation_uncertain") from None
        finally:
            response.close()

    def task_status(self, task_id):
        task_id = _task_id(task_id)
        try:
            response = self.session.get(BASE_URL + "/tasks/" + task_id, headers=self._headers(),
                                        timeout=(10, 45), allow_redirects=False)
        except requests.RequestException:
            raise LiveVideoError("Не удалось прочитать состояние живой сцены. Её повторный заказ не выполнялся.",
                                 retryable=True, code="video_status_unavailable") from None
        try:
            if not 200 <= response.status_code < 300:
                if response.status_code == 404:
                    raise LiveVideoError("Заказанная сцена пока не видна в видеосервисе. Повторный заказ не выполнялся.",
                                         retryable=True, code="video_task_not_visible")
                raise _known_failure(response.status_code, reading=True)
            payload = _json_response(response)
            status = str(payload.get("status") or "").upper()
            if status in {"PENDING", "THROTTLED", "RUNNING"}:
                return {"status": "pending"}
            if status in {"FAILED", "CANCELLED"}:
                return {"status": "failed", "error": "Видеосервис не смог завершить создание сцены."}
            if status == "SUCCEEDED":
                output = payload.get("output")
                if not isinstance(output, list) or not output or not isinstance(output[0], str):
                    raise LiveVideoError("Видеосервис пока не предоставил файл готовой сцены.", retryable=True,
                                         code="video_output_unavailable")
                return {"status": "succeeded", "url": _public_url(output[0])}
            raise LiveVideoError("Видеосервис вернул неизвестное состояние сцены.", retryable=True,
                                 code="video_status_unknown")
        finally:
            response.close()

    def download(self, url):
        current = _public_url(url)
        for redirect in range(4):
            try:
                response = self.download_session.get(current, headers={"Accept": "video/mp4", "Authorization": None},
                                                     timeout=(10, 90), stream=True, allow_redirects=False)
            except requests.RequestException:
                raise LiveVideoError("Не удалось загрузить готовую сцену. Повторный заказ видео не выполнялся.",
                                     retryable=True, code="video_download_unavailable") from None
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not isinstance(location, str) or not location or redirect == 3:
                        raise LiveVideoError("Не удалось безопасно перейти к файлу сцены.", code="video_redirect_rejected")
                    current = _public_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    raise LiveVideoError("Файл живой сцены пока недоступен. Повторный заказ не выполнялся.",
                                         retryable=True, code="video_download_unavailable")
                length = response.headers.get("Content-Length")
                if isinstance(length, str) and length.isdecimal() and int(length) > MAX_CLIP_BYTES:
                    raise LiveVideoError("Готовая сцена превышает допустимый размер.", code="video_too_large")
                data, total = [], 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_CLIP_BYTES:
                        raise LiveVideoError("Готовая сцена превышает допустимый размер.", code="video_too_large")
                    data.append(chunk)
                if not total:
                    raise LiveVideoError("Видеосервис вернул пустой файл сцены.", retryable=True, code="video_empty")
                return b"".join(data)
            except requests.RequestException:
                raise LiveVideoError("Загрузка сцены прервалась. Повторный заказ видео не выполнялся.",
                                     retryable=True, code="video_download_unavailable") from None
            finally:
                response.close()
        raise LiveVideoError("Не удалось безопасно загрузить живую сцену.", code="video_redirect_rejected")
