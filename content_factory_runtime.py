"""Shared secure configuration for the UI and standalone worker."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


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
    )


def get_connect_url(owner_id, project_id, platform):
    from content_factory_oauth import make_connect_url
    return make_connect_url(owner_id, project_id, platform, _configuration())
