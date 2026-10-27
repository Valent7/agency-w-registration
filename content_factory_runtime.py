"""Shared secure configuration for the UI and standalone worker."""
from __future__ import annotations

import base64
from hashlib import sha256
import hmac
import json
import os
import tomllib
from pathlib import Path
import uuid


def _configuration():
    # Read configuration for use, never log its contents or copy credentials.
    result = {}
    paths = [Path.home() / ".streamlit/secrets.toml", Path(__file__).resolve().parent / ".streamlit/secrets.toml"]
    override = os.environ.get("CONTENT_FACTORY_SECRETS_FILE")
    if override:
        paths.append(Path(override))
    for path in paths:
        if path.is_file():
            with path.open("rb") as file:
                result.update(tomllib.load(file))
    result.update(os.environ)
    # Existing Agency W installations already share this OAuth service with
    # Instagram onboarding. A dedicated factory service can still override it.
    if not result.get("CONTENT_FACTORY_SERVICE_URL"):
        result["CONTENT_FACTORY_SERVICE_URL"] = result.get("INSTAGRAM_OAUTH_SERVICE_URL", "")
    return result


def get_capabilities():
    cfg = _configuration()
    return {
        "storage": "supabase" if cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_SECRET_KEY") else "local",
        "ai_configured": bool(cfg.get("OPENAI_API_KEY")),
        "encryption_configured": bool(cfg.get("CONTENT_FACTORY_FERNET_KEY") or cfg.get("FERNET_KEY")),
        "live_video_configured": bool(cfg.get("RUNWAYML_API_SECRET")),
    }


def build_store():
    from content_factory_store import SQLiteStore, SupabaseStore
    cfg = _configuration()
    key = cfg.get("CONTENT_FACTORY_FERNET_KEY") or cfg.get("FERNET_KEY") or ""
    if cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_SECRET_KEY"):
        return SupabaseStore(str(cfg["SUPABASE_URL"]), str(cfg["SUPABASE_SECRET_KEY"]), str(key))
    root = Path(str(cfg.get("CONTENT_FACTORY_DATA_DIR") or "/workspace/.cloud-onboarding/content-factory"))
    root.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(str(root / "factory.sqlite3"), str(key))


def get_generator():
    from content_factory_generation import Generator
    cfg = _configuration()
    if not cfg.get("OPENAI_API_KEY"):
        raise ValueError("Для создания материалов подключите ИИ в настройках Агентства.")
    return Generator(
        str(cfg["OPENAI_API_KEY"]),
        text_model=str(cfg.get("CONTENT_FACTORY_TEXT_MODEL") or "gpt-5"),
        review_model=str(cfg.get("CONTENT_FACTORY_REVIEW_MODEL") or cfg.get("CONTENT_FACTORY_TEXT_MODEL") or "gpt-5"),
        image_model=str(cfg.get("CONTENT_FACTORY_IMAGE_MODEL") or "gpt-image-2.5-sunburst"),
        speech_model=str(cfg.get("CONTENT_FACTORY_SPEECH_MODEL") or "gpt-4o-mini-tts"),
        voice=str(cfg.get("CONTENT_FACTORY_VOICE") or "alloy"),
        image_quality=str(cfg.get("CONTENT_FACTORY_IMAGE_QUALITY") or "high"),
        live_video_client_resolver=resolve_live_video_client,
    )


def get_connect_url(owner_id, project_id, platform):
    from content_factory_oauth import make_connect_url
    return make_connect_url(owner_id, project_id, platform, _configuration())


_VIDEO_PROVIDER = "runway_video"
_PROVIDER_KEY_DOMAIN = b"agency-w/content-factory/provider-seal/v1\x00"


def _video_identity(owner_id, project_id):
    try:
        if isinstance(owner_id, bool) or int(owner_id) <= 0:
            raise ValueError
        return int(owner_id), str(uuid.UUID(str(project_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Не удалось определить проект для подключения видео.") from None


def _provider_store(cfg):
    """Use a separate encryption domain, never the social-connection cipher.

    Streamlit and the worker already share the Supabase server credential.
    Deriving this dedicated seal allows the existing Streamlit video secret to
    be handed to the worker without exposing it in the browser or project JSON.
    Provider payloads also bind the encrypted value to owner and project.
    """
    from content_factory_store import SQLiteStore, SupabaseStore
    secret = str(cfg.get("SUPABASE_SECRET_KEY") or "").strip()
    if not secret:
        raise ValueError("Защищённое подключение видео должен завершить администратор Агентства.")
    key = base64.urlsafe_b64encode(sha256(_PROVIDER_KEY_DOMAIN + secret.encode()).digest()).decode()
    if cfg.get("SUPABASE_URL"):
        return SupabaseStore(str(cfg["SUPABASE_URL"]), secret, key)
    root = Path(str(cfg.get("CONTENT_FACTORY_DATA_DIR") or "/workspace/.cloud-onboarding/content-factory"))
    return SQLiteStore(root / "factory.sqlite3", key)


def _saved_video_key(owner_id, project_id, cfg):
    owner_id, project_id = _video_identity(owner_id, project_id)
    connection = _provider_store(cfg).get_connection(owner_id, project_id, _VIDEO_PROVIDER)
    if not connection:
        return ""
    try:
        payload = json.loads(connection["access_token"])
        if (not isinstance(payload, dict) or payload.get("owner_id") != owner_id
                or payload.get("project_id") != project_id or payload.get("provider") != _VIDEO_PROVIDER
                or not isinstance(payload.get("key"), str) or not payload["key"].strip()):
            raise ValueError
        return payload["key"]
    except (TypeError, ValueError, KeyError):
        raise ValueError("Подключение видео не принадлежит выбранному проекту. Подключите его заново.") from None


def save_live_video_connection(owner_id, project_id, api_key):
    owner_id, project_id = _video_identity(owner_id, project_id)
    if not isinstance(api_key, str) or not 10 <= len(api_key.strip()) <= 4096 or "\x00" in api_key:
        raise ValueError("Введите действующий ключ видеосервиса.")
    cfg = _configuration()
    payload = json.dumps({"provider": _VIDEO_PROVIDER, "owner_id": owner_id,
                          "project_id": project_id, "key": api_key.strip()}, separators=(",", ":"))
    try:
        _provider_store(cfg).save_connection(owner_id, project_id, _VIDEO_PROVIDER,
                                            "runway", "Создание живого видео", payload)
    except Exception:
        raise ValueError("Не удалось сохранить подключение видео. Проверьте настройки защищённого хранилища.") from None


def sync_live_video_connection(owner_id, project_id, api_key=None):
    """Bridge an existing server secret once; never generate or publish here."""
    cfg = _configuration()
    key = api_key if api_key is not None else cfg.get("RUNWAYML_API_SECRET")
    if not isinstance(key, str) or not key.strip():
        return False
    key = key.strip()
    try:
        existing = _saved_video_key(owner_id, project_id, cfg)
    except Exception:
        existing = ""
    if existing and hmac.compare_digest(existing, key):
        return True
    save_live_video_connection(owner_id, project_id, key)
    return True


def get_live_video_capabilities(owner_id, project_id):
    cfg = _configuration()
    configured = bool(cfg.get("RUNWAYML_API_SECRET"))
    if not configured and cfg.get("SUPABASE_SECRET_KEY"):
        try:
            configured = bool(_saved_video_key(owner_id, project_id, cfg))
        except Exception:
            configured = False
    return {"live_video_configured": configured,
            "live_video_connection_storage": bool(cfg.get("SUPABASE_SECRET_KEY"))}


def resolve_live_video_client(project):
    """Resolve a trusted project's private provider access at generation time."""
    from content_factory_live_video import RunwayVideoClient
    cfg = _configuration()
    key = str(cfg.get("RUNWAYML_API_SECRET") or "").strip()
    if not key:
        try:
            key = _saved_video_key(project.get("owner_id"), project.get("id"), cfg)
        except Exception:
            key = ""
    if not key:
        raise ValueError("Живое видео ещё не подключено. Откройте подключение видеосервиса в настройках проекта.")
    return RunwayVideoClient(key, model=str(cfg.get("CONTENT_FACTORY_VIDEO_MODEL") or "gen4.5"))
