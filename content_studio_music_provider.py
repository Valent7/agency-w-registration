"""Direct music generation provider for Agency W.

Provider credentials are server-side only. The browser never receives them.
"""
from __future__ import annotations

import os
import requests

API_URL = "https://api.elevenlabs.io/v1/music"
DEFAULT_MODEL = "music_v2_5"
MAX_AUDIO_BYTES = 60 * 1024 * 1024


class MusicProviderError(RuntimeError):
    pass


def _clean_key(value):
    value = str(value or "").strip()
    if not value or len(value) > 5000:
        return ""
    return value


def music_api_key(explicit=None):
    return _clean_key(explicit) or _clean_key(os.getenv("ELEVENLABS_API_KEY"))


def is_configured(explicit=None):
    return bool(music_api_key(explicit))


def generate_music(prompt, *, duration_ms=60000, instrumental=True, api_key=None,
                   model_id=DEFAULT_MODEL, timeout=240):
    prompt = " ".join(str(prompt or "").split())
    if not 3 <= len(prompt) <= 4100:
        raise MusicProviderError("Опишите музыку чуть подробнее.")
    if type(duration_ms) is not int or not 3000 <= duration_ms <= 600000:
        raise MusicProviderError("Выберите допустимую длительность музыки.")
    key = music_api_key(api_key)
    if not key:
        raise MusicProviderError("Музыкальный генератор ещё не подключён владельцем Агентства.")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 100:
        raise MusicProviderError("Не удалось выбрать музыкальную модель.")

    payload = {
        "prompt": prompt,
        "music_length_ms": duration_ms,
        "model_id": model_id.strip(),
        "force_instrumental": bool(instrumental),
    }
    try:
        response = requests.post(
            API_URL,
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            params={"output_format": "mp3_44100_128"},
            json=payload,
            timeout=(15, timeout),
            allow_redirects=False,
        )
    except requests.Timeout:
        raise MusicProviderError("Создание музыки заняло слишком много времени. Повторите попытку.") from None
    except requests.RequestException:
        raise MusicProviderError("Музыкальный сервис временно недоступен. Повторите попытку.") from None

    try:
        if response.status_code in {401, 403}:
            raise MusicProviderError("Музыкальный генератор требует повторного подключения владельцем Агентства.")
        if response.status_code == 429:
            raise MusicProviderError("Лимит музыкального генератора временно исчерпан.")
        if not 200 <= response.status_code < 300:
            raise MusicProviderError("Музыкальный сервис не смог создать трек. Попробуйте изменить описание.")
        data = response.content
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
            raise MusicProviderError("Музыкальный сервис вернул неожиданный формат.")
        if not isinstance(data, bytes) or not 1000 <= len(data) <= MAX_AUDIO_BYTES:
            raise MusicProviderError("Полученный трек имеет неверный размер.")
        return data
    finally:
        response.close()
