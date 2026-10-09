import hashlib
import json
import os
import re
import mimetypes
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from urllib.parse import quote, urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route


VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")
CONTACT_EMAIL = "polesski.immobilien@gmail.com"

# The Instagram account is mapped to the existing Agency W owner.
# Keep these values in Render Environment, not in GitHub.
INSTAGRAM_OWNER_ID = os.getenv("INSTAGRAM_OWNER_ID", "").strip()
INSTAGRAM_OWNER_NAME = os.getenv("INSTAGRAM_OWNER_NAME", "").strip()
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
INSTAGRAM_API_VERSION = os.getenv("INSTAGRAM_API_VERSION", "v23.0").strip() or "v23.0"
INSTAGRAM_OAUTH_SCOPES = (
    "instagram_business_basic,"
    "instagram_business_manage_messages,"
    "instagram_business_manage_comments"
)

# Multi-partner Instagram Business Login. These values are configured once
# by the Agency W operator in Render; partners never see them.
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "").strip()
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "").strip()
INSTAGRAM_OAUTH_REDIRECT_URI = os.getenv("INSTAGRAM_OAUTH_REDIRECT_URI", "").strip()
INSTAGRAM_AGENCY_RETURN_URL = os.getenv(
    "INSTAGRAM_AGENCY_RETURN_URL",
    "https://agency-w.streamlit.app/",
).strip()
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()

# Facebook Login for Business — Instagram Radar.
# The configuration ID is public; the app secret must stay only in Render Environment.
FACEBOOK_APP_ID = os.getenv("FACEBOOK_APP_ID", "1101869308929738").strip()
FACEBOOK_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET", "").strip()
FACEBOOK_LOGIN_CONFIG_ID = (
    os.getenv("FACEBOOK_CONFIG_ID")
    or os.getenv("FACEBOOK_LOGIN_CONFIG_ID")
    or "1017467271327083"
).strip()
FACEBOOK_OAUTH_REDIRECT_URI = (
    os.getenv("FACEBOOK_REDIRECT_URI")
    or os.getenv("FACEBOOK_OAUTH_REDIRECT_URI")
    or "https://instagram-webhook-zeuv.onrender.com/facebook/callback"
).strip()
FACEBOOK_AGENCY_REDIRECT_URI = os.getenv(
    "FACEBOOK_AGENCY_REDIRECT_URI",
    "https://agency-w.streamlit.app/",
).strip() or "https://agency-w.streamlit.app/"
FACEBOOK_API_VERSION = os.getenv(
    "FACEBOOK_API_VERSION",
    "v23.0",
).strip() or "v23.0"

# VK community integration. Keep secrets/tokens in Render Environment.
VK_ACCESS_TOKEN = os.getenv("VK_ACCESS_TOKEN", "").strip()
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
VK_CALLBACK_CONFIRMATION = os.getenv("VK_CALLBACK_CONFIRMATION", "").strip()
VK_CALLBACK_SECRET = os.getenv("VK_CALLBACK_SECRET", "").strip()
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199").strip() or "5.199"
VK_OWNER_ID = os.getenv("VK_OWNER_ID", INSTAGRAM_OWNER_ID).strip()
VK_OWNER_NAME = os.getenv("VK_OWNER_NAME", INSTAGRAM_OWNER_NAME).strip()


def _page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #1f2937; }}
    h1, h2 {{ color: #111827; }}
    a {{ color: #2563eb; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""
    return HTMLResponse(html)


async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "instagram-webhook"})


async def privacy(request: Request):
    body = f"""
<h1>Agency W — Privacy Policy</h1>
<p class=\"muted\">Last updated: August 28, 2026</p>

<p>Agency W may process information received through the Instagram API in order to provide messaging and communication features.</p>

<h2>Data we may process</h2>
<ul>
  <li>Instagram account identifiers and profile information made available by Instagram;</li>
  <li>messages, public comments and content voluntarily sent to Instagram accounts connected by Agency W partners;</li>
  <li>technical metadata required to receive, process, secure and troubleshoot messages or comments.</li>
</ul>

<h2>How we use data</h2>
<ul>
  <li>to receive and respond to Instagram messages and public comments;</li>
  <li>to maintain conversation context;</li>
  <li>to operate, secure and improve Agency W.</li>
</ul>

<p>We do not sell personal data.</p>
<p>Data is retained only as long as reasonably necessary to provide the service, maintain security, troubleshoot issues, or comply with applicable legal obligations.</p>

<h2>Service providers</h2>
<p>Agency W may use service providers such as Meta/Instagram, Render, Supabase and OpenAI to operate its technical infrastructure and provide the service.</p>

<h2>Your choices</h2>
<p>You may request access to or deletion of data associated with your use of Agency W by contacting <a href=\"mailto:{CONTACT_EMAIL}\">{CONTACT_EMAIL}</a>.</p>
<p>See also our <a href=\"/data-deletion\">Data Deletion Instructions</a> and <a href=\"/terms\">Terms of Service</a>.</p>
"""
    return _page("Agency W — Privacy Policy", body)


async def terms(request: Request):
    body = f"""
<h1>Agency W — Terms of Service</h1>
<p class=\"muted\">Last updated: August 28, 2026</p>
<p>Agency W provides software-assisted communication and workflow features, including integrations with Instagram.</p>
<p>Users are responsible for using the service lawfully and for complying with Meta and Instagram terms, policies and platform rules.</p>
<p>The service may be changed, suspended or discontinued when required for maintenance, security, legal compliance or platform compatibility.</p>
<p>For questions, contact <a href=\"mailto:{CONTACT_EMAIL}\">{CONTACT_EMAIL}</a>.</p>
"""
    return _page("Agency W — Terms of Service", body)


async def data_deletion(request: Request):
    body = f"""
<h1>Agency W — Data Deletion Instructions</h1>
<p>To request deletion of data associated with your Instagram interactions with Agency W:</p>
<ol>
  <li>Send an email to <a href=\"mailto:{CONTACT_EMAIL}\">{CONTACT_EMAIL}</a>.</li>
  <li>Use the subject <strong>Agency W data deletion request</strong>.</li>
  <li>Include the Instagram username or account identifier connected with the request so we can locate the relevant data.</li>
</ol>
<p>After we verify the request, we will delete data under our control that is no longer required for security, fraud prevention or applicable legal obligations.</p>
<p>Data controlled directly by Meta or Instagram must be managed through the relevant Meta or Instagram account and privacy settings.</p>
"""
    return _page("Agency W — Data Deletion Instructions", body)


async def instagram_webhook_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")

    return PlainTextResponse("Forbidden", status_code=403)


def _stable_message_id(value: str) -> int:
    """Convert an Instagram message id into a positive signed-bigint-safe value."""
    raw = str(value or "").encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _instagram_contact_id(sender_id: str) -> int:
    """Use negative Instagram IDs so they never collide with Telegram contact IDs."""
    return -abs(int(str(sender_id).strip()))


def _message_datetime(timestamp_value) -> datetime:
    try:
        value = float(timestamp_value)
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _iter_instagram_messages(payload: dict):
    """Yield real incoming text or audio messages from Instagram."""
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue

        entry_timestamp = entry.get("time")
        for event in entry.get("messaging") or []:
            if not isinstance(event, dict):
                continue

            sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
            recipient = (
                event.get("recipient")
                if isinstance(event.get("recipient"), dict)
                else {}
            )
            message = (
                event.get("message")
                if isinstance(event.get("message"), dict)
                else {}
            )

            sender_id = str(sender.get("id") or "").strip()
            recipient_id = str(recipient.get("id") or "").strip()
            message_id = str(message.get("mid") or "").strip()
            text = str(message.get("text") or "").strip()

            if not sender_id or not recipient_id:
                continue
            if sender_id == recipient_id:
                continue
            if bool(message.get("is_echo")) or bool(message.get("is_self")):
                continue
            if bool(message.get("is_deleted")):
                continue

            audio_url = ""
            attachments = message.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    attachment_type = str(attachment.get("type") or "").strip().lower()
                    payload_data = (
                        attachment.get("payload")
                        if isinstance(attachment.get("payload"), dict)
                        else {}
                    )
                    if attachment_type == "audio":
                        candidate_url = str(payload_data.get("url") or "").strip()
                        if candidate_url:
                            audio_url = candidate_url
                            break

            if not text and not audio_url:
                continue

            yield {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message_id": message_id,
                "text": text,
                "audio_url": audio_url,
                "timestamp": event.get("timestamp") or entry_timestamp,
            }


def _owner_name_for_russian(name: str) -> str:
    """Normalize the current Instagram owner's name for Russian dialog."""
    value = str(name or "").strip()
    if value.casefold() == "valentina":
        return "Валентина"
    return value


def _polish_instagram_reply(text: str, owner_name: str) -> str:
    """Remove awkward owner-name constructions from Instagram replies."""
    reply = str(text or "").strip()
    owner = _owner_name_for_russian(owner_name)

    # Current cabinet owner. Keep this explicit until a generic declension
    # helper is added for all Agency W owners.
    if owner.casefold() == "валентина":
        reply = re.sub(r"\bValentina\b", "Валентина", reply, flags=re.IGNORECASE)
        reply = re.sub(
            r"(?i)\bсекретар(?:ь|я)(?:[\s‑-]*референт)?\s+Валентина\b",
            "секретарь-референт Валентины",
            reply,
        )
        reply = re.sub(r"(?i)\bс\s+Валентина\b", "с Валентиной", reply)
        reply = re.sub(r"(?i)\bу\s+Валентина\b", "у Валентины", reply)
        reply = re.sub(r"(?i)\bдля\s+Валентина\b", "для Валентины", reply)

    reply = re.sub(r"\s{2,}", " ", reply).strip()
    return reply


def _audio_suffix(content_type: str, url: str) -> str:
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/aac": ".aac",
        "audio/webm": ".webm",
    }
    if content_type in mapping:
        return mapping[content_type]

    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return guessed

    lower_url = str(url or "").lower()
    for suffix in (".ogg", ".opus", ".mp3", ".m4a", ".mp4", ".wav", ".aac", ".webm"):
        if suffix in lower_url:
            return ".ogg" if suffix == ".opus" else suffix

    return ".m4a"


def _download_instagram_audio(audio_url: str) -> tuple[Path, str]:
    """Download the temporary Instagram CDN audio URL immediately."""
    response = requests.get(
        str(audio_url),
        timeout=60,
        allow_redirects=True,
        headers={"User-Agent": "Agency-W-Instagram-Webhook/1.0"},
    )
    response.raise_for_status()
    audio_bytes = response.content
    if not audio_bytes:
        raise RuntimeError("Instagram audio download returned an empty file.")

    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    suffix = _audio_suffix(content_type, audio_url)

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
        temporary.write(audio_bytes)
        path = Path(temporary.name)

    return path, (content_type or mimetypes.guess_type(path.name)[0] or "audio/m4a")


def _transcribe_audio_with_model(path: Path, mime_type: str, model: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")

    with path.open("rb") as audio_file:
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={
                "model": model,
                "language": "ru",
                "response_format": "json",
            },
            files={
                "file": (
                    path.name,
                    audio_file,
                    str(mime_type or "audio/m4a"),
                )
            },
            timeout=120,
        )
    if not response.ok:
        raise RuntimeError(
            f"OpenAI transcription HTTP {response.status_code}: "
            f"{response.text[:800]}"
        )
    transcript = str(response.json().get("text") or "").strip()
    if not transcript:
        raise RuntimeError("OpenAI transcription returned empty text.")
    return transcript


def _transcribe_instagram_audio(audio_url: str) -> str:
    path = None
    try:
        path, mime_type = _download_instagram_audio(audio_url)
        try:
            return _transcribe_audio_with_model(
                path,
                mime_type,
                "gpt-4o-mini-transcribe",
            )
        except Exception as primary_exc:
            print(
                "INSTAGRAM_AUDIO_TRANSCRIBE_FALLBACK:",
                f"{type(primary_exc).__name__}: {primary_exc}",
                flush=True,
            )
            return _transcribe_audio_with_model(
                path,
                mime_type,
                "whisper-1",
            )
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass




def _instagram_cipher() -> Fernet:
    if not FERNET_KEY:
        raise RuntimeError("Missing FERNET_KEY for Instagram connections")
    return Fernet(FERNET_KEY.encode("utf-8"))


def _encrypt_instagram_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return _instagram_cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_instagram_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        return _instagram_cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Instagram token could not be decrypted") from exc


def _decode_instagram_state(state: str) -> dict:
    state = str(state or "").strip()
    if not state:
        raise RuntimeError("Missing Instagram OAuth state")
    try:
        raw = _instagram_cipher().decrypt(state.encode("utf-8"), ttl=15 * 60)
        payload = json.loads(raw.decode("utf-8"))
    except InvalidToken as exc:
        raise RuntimeError("Instagram authorization link expired or is invalid") from exc
    except Exception as exc:
        raise RuntimeError("Invalid Instagram authorization state") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid Instagram authorization state")
    owner_id = str(payload.get("owner_id") or "").strip()
    if not owner_id.isdigit():
        raise RuntimeError("Instagram authorization has no valid Agency W owner")
    return payload


def _instagram_connection_by_account_id(instagram_account_id: str) -> dict | None:
    account_id = str(instagram_account_id or "").strip()
    if not account_id:
        return None
    try:
        rows = _sb_get(
            "agency_instagram_connections",
            {
                "instagram_account_id": f"eq.{account_id}",
                "status": "eq.connected",
                "select": "*",
                "limit": 1,
            },
        )
    except Exception as exc:
        print("INSTAGRAM_CONNECTION_LOOKUP_ERROR:", exc, flush=True)
        return None
    if not rows:
        return None
    row = dict(rows[0])
    row["access_token"] = _decrypt_instagram_secret(row.get("access_token_encrypted") or "")
    return row


def _fallback_instagram_connection(recipient_id: str) -> dict | None:
    """Keep the proven single-account setup working only until migration starts.

    Once at least one DB-backed Instagram connection exists, unknown recipient IDs
    are never routed to the Director. This prevents cross-partner dialog leakage.
    """
    if not (INSTAGRAM_OWNER_ID and INSTAGRAM_ACCESS_TOKEN):
        return None
    try:
        existing = _sb_get(
            "agency_instagram_connections",
            {"status": "eq.connected", "select": "id", "limit": 1},
        )
        if existing:
            return None
    except Exception:
        # Before the migration table is created, preserve today's working mode.
        pass
    return {
        "owner_telegram_id": int(INSTAGRAM_OWNER_ID),
        "owner_name": INSTAGRAM_OWNER_NAME,
        "instagram_account_id": str(recipient_id or "").strip(),
        "instagram_username": "",
        "access_token": INSTAGRAM_ACCESS_TOKEN,
        "status": "fallback_env",
    }


def _ensure_fresh_instagram_token(connection: dict) -> dict:
    """Refresh a long-lived Instagram token before it expires."""
    row = dict(connection or {})
    token = str(row.get("access_token") or "").strip()
    expires_text = str(row.get("token_expires_at") or "").strip()
    if not token or not expires_text or row.get("status") == "fallback_env":
        return row
    try:
        expires_at = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return row
    if expires_at - datetime.now(timezone.utc) > timedelta(days=7):
        return row
    try:
        response = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": token,
            },
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        refreshed = str(data.get("access_token") or token).strip()
        expires_in = int(data.get("expires_in") or 0)
        next_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            if expires_in
            else expires_at
        )
        _sb_patch(
            "agency_instagram_connections",
            {"owner_telegram_id": f"eq.{int(row['owner_telegram_id'])}"},
            {
                "access_token_encrypted": _encrypt_instagram_secret(refreshed),
                "token_expires_at": next_expiry.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        row["access_token"] = refreshed
        row["token_expires_at"] = next_expiry.isoformat()
    except Exception as exc:
        # Do not break a live dialog if the current token still works.
        print("INSTAGRAM_TOKEN_REFRESH_ERROR:", exc, flush=True)
    return row


def _resolve_instagram_connection(recipient_id: str) -> dict:
    connection = _instagram_connection_by_account_id(recipient_id)
    if connection:
        return _ensure_fresh_instagram_token(connection)
    fallback = _fallback_instagram_connection(recipient_id)
    if fallback:
        return fallback
    raise RuntimeError(
        f"No Agency W Instagram connection found for recipient account {recipient_id}"
    )


def _save_instagram_connection(
    *,
    owner_id: int,
    owner_name: str,
    instagram_account_id: str,
    instagram_username: str,
    account_type: str,
    access_token: str,
    expires_in: int | None,
) -> None:
    now = datetime.now(timezone.utc)
    expires_at = None
    if expires_in:
        expires_at = (now + timedelta(seconds=int(expires_in))).isoformat()

    payload = {
        "owner_telegram_id": int(owner_id),
        "owner_name": str(owner_name or "").strip() or None,
        "instagram_account_id": str(instagram_account_id or "").strip(),
        "instagram_username": str(instagram_username or "").strip() or None,
        "account_type": str(account_type or "").strip() or None,
        "access_token_encrypted": _encrypt_instagram_secret(access_token),
        "token_expires_at": expires_at,
        "status": "connected",
        "connected_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    _sb_post(
        "agency_instagram_connections",
        payload,
        merge=True,
        on_conflict="owner_telegram_id",
    )


def _subscribe_instagram_webhooks(
    instagram_account_id: str,
    access_token: str,
) -> dict:
    """Subscribe the connected professional account to Direct and comments."""
    account_id = str(instagram_account_id or "").strip()
    token = str(access_token or "").strip()
    if not account_id or not token:
        raise RuntimeError("Instagram webhook subscription requires account and token.")

    response = requests.post(
        f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/"
        f"{account_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {token}"},
        data={"subscribed_fields": "messages,comments"},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Instagram webhook subscription HTTP {response.status_code}: "
            f"{response.text[:800]}"
        )
    payload = response.json() if response.text.strip() else {}
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError(
            "Instagram did not confirm the messages/comments webhook subscription."
        )
    return payload


def _instagram_oauth_settings() -> dict:
    required = {
        "INSTAGRAM_APP_ID": INSTAGRAM_APP_ID,
        "INSTAGRAM_APP_SECRET": INSTAGRAM_APP_SECRET,
        "INSTAGRAM_OAUTH_REDIRECT_URI": INSTAGRAM_OAUTH_REDIRECT_URI,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing Instagram OAuth settings: " + ", ".join(missing))
    return required


async def instagram_oauth_config(request: Request):
    """Return only public OAuth values so Agency W can link directly to Instagram."""
    try:
        settings = _instagram_oauth_settings()
        return JSONResponse(
            {
                "client_id": settings["INSTAGRAM_APP_ID"],
                "redirect_uri": settings["INSTAGRAM_OAUTH_REDIRECT_URI"],
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )


async def instagram_connect(request: Request):
    try:
        settings = _instagram_oauth_settings()
        state = str(request.query_params.get("state") or "").strip()
        _decode_instagram_state(state)
        params = {
            "client_id": settings["INSTAGRAM_APP_ID"],
            "redirect_uri": settings["INSTAGRAM_OAUTH_REDIRECT_URI"],
            "response_type": "code",
            "scope": INSTAGRAM_OAUTH_SCOPES,
            "state": state,
            "force_reauth": "true",
        }
        return RedirectResponse(
            "https://www.instagram.com/oauth/authorize?" + urlencode(params),
            status_code=302,
        )
    except Exception as exc:
        return _page(
            "Agency W — Instagram",
            f"<h1>Не удалось начать подключение Instagram</h1><p>{str(exc)}</p>",
        )


async def instagram_oauth_callback(request: Request):
    error = str(request.query_params.get("error") or "").strip()
    error_description = str(request.query_params.get("error_description") or "").strip()
    if error:
        return _page(
            "Agency W — Instagram",
            "<h1>Подключение Instagram отменено</h1>"
            f"<p>{error_description or error}</p>",
        )

    try:
        settings = _instagram_oauth_settings()
        code = str(request.query_params.get("code") or "").strip()
        state = str(request.query_params.get("state") or "").strip()
        state_payload = _decode_instagram_state(state)
        if not code:
            raise RuntimeError("Instagram did not return an authorization code")

        token_response = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": settings["INSTAGRAM_APP_ID"],
                "client_secret": settings["INSTAGRAM_APP_SECRET"],
                "grant_type": "authorization_code",
                "redirect_uri": settings["INSTAGRAM_OAUTH_REDIRECT_URI"],
                "code": code,
            },
            timeout=30,
        )
        if not token_response.ok:
            raise RuntimeError(
                f"Instagram token exchange HTTP {token_response.status_code}: "
                f"{token_response.text[:800]}"
            )
        short_data = token_response.json()
        short_token = str(short_data.get("access_token") or "").strip()
        if not short_token:
            raise RuntimeError("Instagram did not return an access token")

        long_response = requests.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings["INSTAGRAM_APP_SECRET"],
                "access_token": short_token,
            },
            timeout=30,
        )
        if not long_response.ok:
            raise RuntimeError(
                f"Instagram long-lived token HTTP {long_response.status_code}: "
                f"{long_response.text[:800]}"
            )
        long_data = long_response.json()
        access_token = str(long_data.get("access_token") or short_token).strip()
        expires_in = long_data.get("expires_in")

        profile_response = requests.get(
            "https://graph.instagram.com/me",
            params={
                "fields": "id,username,account_type",
                "access_token": access_token,
            },
            timeout=30,
        )
        if not profile_response.ok:
            raise RuntimeError(
                f"Instagram profile HTTP {profile_response.status_code}: "
                f"{profile_response.text[:800]}"
            )
        profile = profile_response.json()
        instagram_account_id = str(profile.get("id") or short_data.get("user_id") or "").strip()
        if not instagram_account_id:
            raise RuntimeError("Instagram account id was not returned")

        owner_id = int(state_payload["owner_id"])
        owner_name = str(state_payload.get("owner_name") or "").strip()
        if not owner_name:
            try:
                member = _vk_member_by_telegram(owner_id) or {}
                owner_name = str(member.get("first_name") or "").strip()
            except Exception:
                owner_name = ""

        _save_instagram_connection(
            owner_id=owner_id,
            owner_name=owner_name,
            instagram_account_id=instagram_account_id,
            instagram_username=str(profile.get("username") or "").strip(),
            account_type=str(profile.get("account_type") or "").strip(),
            access_token=access_token,
            expires_in=int(expires_in) if str(expires_in or "").isdigit() else None,
        )

        comments_ready = True
        try:
            _subscribe_instagram_webhooks(
                instagram_account_id,
                access_token,
            )
        except Exception as subscription_exc:
            comments_ready = False
            print(
                "INSTAGRAM_WEBHOOK_SUBSCRIPTION_ERROR:",
                f"{type(subscription_exc).__name__}: {subscription_exc}",
                flush=True,
            )

        return_to = INSTAGRAM_AGENCY_RETURN_URL or "https://agency-w.streamlit.app/"
        sep = "&" if "?" in return_to else "?"
        return RedirectResponse(
            f"{return_to}{sep}instagram=connected&comments="
            + ("ready" if comments_ready else "subscription_warning"),
            status_code=302,
        )
    except Exception as exc:
        print("INSTAGRAM_OAUTH_CALLBACK_ERROR:", f"{type(exc).__name__}: {exc}", flush=True)
        return _page(
            "Agency W — Instagram",
            "<h1>Instagram не подключён</h1>"
            f"<p>{str(exc)}</p>"
            "<p>Вернитесь в Агентство W и попробуйте ещё раз.</p>",
        )



def _facebook_oauth_settings() -> dict:
    required = {
        "FACEBOOK_APP_ID": FACEBOOK_APP_ID,
        "FACEBOOK_APP_SECRET": FACEBOOK_APP_SECRET,
        "FACEBOOK_LOGIN_CONFIG_ID": FACEBOOK_LOGIN_CONFIG_ID,
        "FACEBOOK_OAUTH_REDIRECT_URI": FACEBOOK_OAUTH_REDIRECT_URI,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Facebook Radar settings: " + ", ".join(missing)
        )
    return required


def _facebook_graph_get(
    path: str,
    *,
    access_token: str,
    params: dict | None = None,
) -> dict:
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("Missing Facebook access token")
    clean_path = str(path or "").strip().lstrip("/")
    response = requests.get(
        f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{clean_path}",
        params={
            **(params or {}),
            "access_token": token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Facebook Graph HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )
    payload = response.json() if response.text.strip() else {}
    if not isinstance(payload, dict):
        raise RuntimeError("Facebook Graph returned an unexpected response.")
    if payload.get("error"):
        raise RuntimeError(f"Facebook Graph error: {payload['error']}")
    return payload


def _save_instagram_radar_connection(
    *,
    owner_id: int,
    owner_name: str,
    facebook_user_id: str,
    facebook_user_name: str,
    page_id: str,
    page_name: str,
    instagram_account_id: str,
    instagram_username: str,
    user_access_token: str,
    page_access_token: str,
    expires_in: int | None,
) -> None:
    now = datetime.now(timezone.utc)
    expires_at = None
    if expires_in:
        expires_at = (now + timedelta(seconds=int(expires_in))).isoformat()

    payload = {
        "owner_telegram_id": int(owner_id),
        "owner_name": str(owner_name or "").strip() or None,
        "facebook_user_id": str(facebook_user_id or "").strip() or None,
        "facebook_user_name": str(facebook_user_name or "").strip() or None,
        "facebook_page_id": str(page_id or "").strip(),
        "facebook_page_name": str(page_name or "").strip() or None,
        "instagram_account_id": str(instagram_account_id or "").strip(),
        "instagram_username": str(instagram_username or "").strip() or None,
        "user_access_token_encrypted": _encrypt_instagram_secret(user_access_token),
        "page_access_token_encrypted": _encrypt_instagram_secret(page_access_token),
        "token_expires_at": expires_at,
        "status": "connected",
        "connected_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    _sb_post(
        "agency_instagram_radar_connections",
        payload,
        merge=True,
        on_conflict="owner_telegram_id",
    )


async def facebook_connect(request: Request):
    """Start Facebook Login for Business for the Instagram Radar."""
    try:
        settings = _facebook_oauth_settings()
        state = str(request.query_params.get("state") or "").strip()
        state_payload = _decode_instagram_state(state)
        purpose = str(state_payload.get("purpose") or "").strip()
        if purpose != "agency_w_instagram_radar_connect":
            raise RuntimeError("Invalid Instagram Radar authorization state")

        params = {
            "client_id": settings["FACEBOOK_APP_ID"],
            "redirect_uri": settings["FACEBOOK_OAUTH_REDIRECT_URI"],
            "state": state,
            "config_id": settings["FACEBOOK_LOGIN_CONFIG_ID"],
            "response_type": "code",
            "override_default_response_type": "true",
            "auth_type": "rerequest",
        }
        return RedirectResponse(
            f"https://www.facebook.com/{FACEBOOK_API_VERSION}/dialog/oauth?"
            + urlencode(params),
            status_code=302,
        )
    except Exception as exc:
        return _page(
            "Agency W — Instagram Radar",
            "<h1>Не удалось начать подключение Instagram Radar</h1>"
            f"<p>{str(exc)}</p>",
        )


def _complete_facebook_radar_oauth(*, code: str, state: str, redirect_uri: str) -> dict:
    """Exchange one Facebook Login for Business code and persist the Radar connection."""
    settings = _facebook_oauth_settings()
    code = str(code or "").strip()
    state = str(state or "").strip()
    redirect_uri = str(redirect_uri or "").strip()
    state_payload = _decode_instagram_state(state)
    if str(state_payload.get("purpose") or "").strip() != (
        "agency_w_instagram_radar_connect"
    ):
        raise RuntimeError("Invalid Instagram Radar authorization state")
    if not code:
        raise RuntimeError("Facebook did not return an authorization code")
    if not redirect_uri:
        raise RuntimeError("Missing Facebook redirect URI")

    token_response = requests.get(
        f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token",
        params={
            "client_id": settings["FACEBOOK_APP_ID"],
            "client_secret": settings["FACEBOOK_APP_SECRET"],
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if not token_response.ok:
        raise RuntimeError(
            f"Facebook token exchange HTTP {token_response.status_code}: "
            f"{token_response.text[:1000]}"
        )
    token_data = token_response.json()
    short_token = str(token_data.get("access_token") or "").strip()
    if not short_token:
        raise RuntimeError("Facebook did not return an access token")

    access_token = short_token
    expires_in = token_data.get("expires_in")
    try:
        long_response = requests.get(
            f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings["FACEBOOK_APP_ID"],
                "client_secret": settings["FACEBOOK_APP_SECRET"],
                "fb_exchange_token": short_token,
            },
            timeout=30,
        )
        if long_response.ok:
            long_data = long_response.json()
            access_token = str(
                long_data.get("access_token") or short_token
            ).strip()
            expires_in = long_data.get("expires_in") or expires_in
    except Exception as exchange_exc:
        print(
            "FACEBOOK_LONG_TOKEN_EXCHANGE_WARNING:",
            f"{type(exchange_exc).__name__}: {exchange_exc}",
            flush=True,
        )

    me = _facebook_graph_get(
        "me",
        access_token=access_token,
        params={"fields": "id,name"},
    )

    pages = _facebook_graph_get(
        "me/accounts",
        access_token=access_token,
        params={
            "fields": (
                "id,name,access_token,"
                "instagram_business_account{id,username,name}"
            ),
            "limit": 100,
        },
    ).get("data") or []

    owner_id = int(state_payload["owner_id"])
    owner_name = str(state_payload.get("owner_name") or "").strip()

    preferred_username = ""
    try:
        rows = _sb_get(
            "agency_instagram_connections",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "status": "eq.connected",
                "select": "instagram_username",
                "limit": 1,
            },
        )
        if rows:
            preferred_username = str(
                rows[0].get("instagram_username") or ""
            ).strip().casefold()
    except Exception:
        preferred_username = ""

    candidates = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        ig = page.get("instagram_business_account")
        if not isinstance(ig, dict):
            continue
        ig_id = str(ig.get("id") or "").strip()
        page_token = str(page.get("access_token") or "").strip()
        if not ig_id or not page_token:
            continue
        candidates.append((page, ig))

    if not candidates:
        raise RuntimeError(
            "Facebook не вернул Страницу с привязанным профессиональным "
            "Instagram. Проверьте, что Instagram Business/Creator связан "
            "со Страницей Facebook и у вашего Facebook-профиля есть доступ к ней."
        )

    selected_page, selected_ig = candidates[0]
    if preferred_username:
        for page, ig in candidates:
            if str(ig.get("username") or "").strip().casefold() == preferred_username:
                selected_page, selected_ig = page, ig
                break

    ig_id = str(selected_ig.get("id") or "").strip()
    ig_username = str(selected_ig.get("username") or "").strip()

    if not ig_username:
        try:
            ig_profile = _facebook_graph_get(
                ig_id,
                access_token=str(selected_page.get("access_token") or ""),
                params={"fields": "id,username,name"},
            )
            ig_username = str(ig_profile.get("username") or "").strip()
        except Exception:
            ig_username = ""

    _save_instagram_radar_connection(
        owner_id=owner_id,
        owner_name=owner_name,
        facebook_user_id=str(me.get("id") or "").strip(),
        facebook_user_name=str(me.get("name") or "").strip(),
        page_id=str(selected_page.get("id") or "").strip(),
        page_name=str(selected_page.get("name") or "").strip(),
        instagram_account_id=ig_id,
        instagram_username=ig_username,
        user_access_token=access_token,
        page_access_token=str(selected_page.get("access_token") or "").strip(),
        expires_in=(
            int(expires_in)
            if str(expires_in or "").isdigit()
            else None
        ),
    )

    return {
        "ok": True,
        "owner_id": owner_id,
        "instagram_username": ig_username,
        "facebook_page_name": str(selected_page.get("name") or "").strip(),
    }


async def facebook_exchange(request: Request):
    """Server-to-server callback exchange used by Agency W Streamlit.

    The user's browser never has to visit the Render hostname.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad Request"}, status_code=400)

    try:
        code = str((payload or {}).get("code") or "").strip()
        state = str((payload or {}).get("state") or "").strip()
        redirect_uri = str((payload or {}).get("redirect_uri") or "").strip()
        if redirect_uri.rstrip("/") != FACEBOOK_AGENCY_REDIRECT_URI.rstrip("/"):
            raise RuntimeError("Invalid Agency W redirect URI")
        result = _complete_facebook_radar_oauth(
            code=code,
            state=state,
            redirect_uri=redirect_uri,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        print(
            "FACEBOOK_RADAR_EXCHANGE_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )


async def facebook_oauth_callback(request: Request):
    """Legacy browser callback kept for compatibility; new UI uses Agency W callback."""
    error = str(request.query_params.get("error") or "").strip()
    error_description = str(
        request.query_params.get("error_description") or ""
    ).strip()
    if error:
        return _page(
            "Agency W — Instagram Radar",
            "<h1>Подключение Instagram Radar отменено</h1>"
            f"<p>{error_description or error}</p>",
        )

    try:
        result = _complete_facebook_radar_oauth(
            code=str(request.query_params.get("code") or "").strip(),
            state=str(request.query_params.get("state") or "").strip(),
            redirect_uri=FACEBOOK_OAUTH_REDIRECT_URI,
        )
        return_to = INSTAGRAM_AGENCY_RETURN_URL or "https://agency-w.streamlit.app/"
        sep = "&" if "?" in return_to else "?"
        return RedirectResponse(
            f"{return_to}{sep}instagram_radar=connected",
            status_code=302,
        )
    except Exception as exc:
        print(
            "FACEBOOK_RADAR_CALLBACK_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return _page(
            "Agency W — Instagram Radar",
            "<h1>Instagram Radar не подключён</h1>"
            f"<p>{str(exc)}</p>"
            "<p>Вернитесь в Агентство W и попробуйте ещё раз.</p>",
        )


def _send_instagram_text(
    sender_account_id: str,
    recipient_id: str,
    text: str,
    access_token: str | None = None,
) -> dict:
    """Send one text reply using the token belonging to this Agency W owner."""
    token = str(access_token or INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Missing Instagram access token")

    sender_account_id = str(sender_account_id or "").strip()
    recipient_id = str(recipient_id or "").strip()
    text = str(text or "").strip()
    if not sender_account_id or not recipient_id or not text:
        raise RuntimeError("Instagram send requires sender account id, recipient id and text.")

    endpoint = (
        f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/"
        f"{sender_account_id}/messages"
    )
    body = json.dumps(
        {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = UrlRequest(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Agency-W-Instagram-Webhook/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(
            f"Instagram API HTTP {exc.code}: {detail[:1200]}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Instagram API connection error: {exc.reason}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Instagram API returned an unexpected response.")
    if payload.get("error"):
        raise RuntimeError(f"Instagram API error: {payload['error']}")
    return payload


def _iter_instagram_comments(payload: dict):
    """Yield comment notifications sent through the Instagram webhook."""
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        account_id = str(entry.get("id") or "").strip()
        entry_time = entry.get("time")
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "").strip().lower()
            if field not in {"comments", "live_comments"}:
                continue
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            comment_id = str(value.get("id") or value.get("comment_id") or "").strip()
            text = str(value.get("text") or value.get("message") or "").strip()
            if not account_id or not comment_id:
                continue

            author = value.get("from") if isinstance(value.get("from"), dict) else {}
            media = value.get("media") if isinstance(value.get("media"), dict) else {}
            yield {
                "instagram_account_id": account_id,
                "comment_id": comment_id,
                "parent_comment_id": str(
                    value.get("parent_id")
                    or value.get("parent_comment_id")
                    or ""
                ).strip(),
                "comment_text": text,
                "author_id": str(author.get("id") or value.get("from_id") or "").strip(),
                "author_username": str(
                    author.get("username")
                    or value.get("username")
                    or ""
                ).strip(),
                "media_id": str(media.get("id") or value.get("media_id") or "").strip(),
                "media_type": str(
                    media.get("media_type")
                    or media.get("media_product_type")
                    or ""
                ).strip(),
                "comment_timestamp": (
                    value.get("timestamp")
                    or value.get("created_time")
                    or entry_time
                ),
                "webhook_field": field,
                "raw_value": value,
            }


def _instagram_event_datetime(value) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return _message_datetime(value).isoformat()


def _instagram_graph_get(
    object_id: str,
    *,
    access_token: str,
    fields: str,
) -> dict:
    response = requests.get(
        f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/"
        f"{str(object_id or '').strip()}",
        headers={"Authorization": f"Bearer {str(access_token or '').strip()}"},
        params={"fields": fields},
        timeout=25,
    )
    if not response.ok:
        raise RuntimeError(
            f"Instagram Graph HTTP {response.status_code}: {response.text[:800]}"
        )
    data = response.json() if response.text.strip() else {}
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Instagram Graph returned an error: {data}")
    return data


def _enrich_instagram_comment(event: dict, access_token: str) -> dict:
    enriched = dict(event or {})
    try:
        detail = _instagram_graph_get(
            enriched["comment_id"],
            access_token=access_token,
            fields="id,text,username,timestamp,parent_id,media",
        )
        enriched["comment_text"] = str(
            detail.get("text") or enriched.get("comment_text") or ""
        ).strip()
        enriched["author_username"] = str(
            detail.get("username") or enriched.get("author_username") or ""
        ).strip()
        enriched["parent_comment_id"] = str(
            detail.get("parent_id") or enriched.get("parent_comment_id") or ""
        ).strip()
        detail_media = detail.get("media")
        if isinstance(detail_media, dict):
            enriched["media_id"] = str(
                detail_media.get("id") or enriched.get("media_id") or ""
            ).strip()
        enriched["comment_timestamp"] = (
            detail.get("timestamp") or enriched.get("comment_timestamp")
        )
    except Exception as exc:
        print(
            "INSTAGRAM_COMMENT_DETAIL_WARNING:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    media_id = str(enriched.get("media_id") or "").strip()
    if media_id:
        try:
            media = _instagram_graph_get(
                media_id,
                access_token=access_token,
                fields="id,caption,media_type,media_product_type,permalink",
            )
            enriched["media_caption"] = str(media.get("caption") or "").strip()
            enriched["media_type"] = str(
                media.get("media_product_type")
                or media.get("media_type")
                or enriched.get("media_type")
                or ""
            ).strip()
            enriched["media_permalink"] = str(media.get("permalink") or "").strip()
        except Exception as exc:
            print(
                "INSTAGRAM_COMMENT_MEDIA_WARNING:",
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
    return enriched


def _instagram_comment_thread_context(
    event: dict,
    owner_id: int,
) -> dict | None:
    """Return the replied root/thread row only for the same comment author.

    The first public comment still requires owner approval. Automatic continuation
    is allowed only when this incoming comment is a reply in a thread where
    Agency W already published an approved reply.
    """
    parent_id = str(event.get("parent_comment_id") or "").strip()
    if not parent_id:
        return None

    candidates = []
    for field in ("comment_id", "reply_id"):
        try:
            rows = _sb_get(
                "agency_instagram_comments",
                {
                    "owner_telegram_id": f"eq.{int(owner_id)}",
                    field: f"eq.{parent_id}",
                    "status": "eq.replied",
                    "select": "*",
                    "limit": 1,
                },
            )
        except Exception:
            rows = []
        if rows:
            candidates.extend(rows)

    if not candidates:
        return None

    event_author_id = str(event.get("author_id") or "").strip()
    event_username = str(event.get("author_username") or "").strip().lower()
    for row in candidates:
        row = dict(row or {})
        row_author_id = str(row.get("author_id") or "").strip()
        row_username = str(row.get("author_username") or "").strip().lower()
        same_person = bool(
            (event_author_id and row_author_id and event_author_id == row_author_id)
            or (event_username and row_username and event_username == row_username)
        )
        if same_person:
            return row
    return None


def _generate_instagram_comment_followup(
    event: dict,
    owner_name: str,
    thread_row: dict,
) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")

    prompt = (
        "Ты — Неона, секретарь-референт Агентства W. Это ПРОДОЛЖЕНИЕ уже начатой "
        "публичной ветки комментариев Instagram: первый ответ уже был утверждён "
        "владельцем аккаунта. Теперь человек ответил снова, поэтому продолжи "
        "диалог самостоятельно. Пиши по-русски, тепло и естественно, максимум "
        "2 коротких предложения. Не повторяй приветствие, не начинай разговор "
        "заново, не называй себя ботом или ИИ, не обещай доход и не выдумывай "
        "факты. Отвечай именно на последнюю реплику человека. Если он проявляет "
        "интерес к разговору или встрече, мягко двигай к следующему конкретному "
        "шагу. Верни только готовый ответ без кавычек и пояснений.\n\n"
        f"Владелец аккаунта: {str(owner_name or '').strip() or 'Агентство W'}\n"
        f"Текст публикации: {str(event.get('media_caption') or thread_row.get('media_caption') or '')[:2500]}\n"
        f"Первый комментарий человека: {str(thread_row.get('comment_text') or '')[:1200]}\n"
        f"Наш уже опубликованный ответ: {str(thread_row.get('approved_reply') or '')[:1200]}\n"
        f"Новый ответ человека @{str(event.get('author_username') or 'пользователь')}: "
        f"{str(event.get('comment_text') or '')[:2000]}"
    )
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("OPENAI_COMMENT_MODEL", "gpt-5-mini").strip()
            or "gpt-5-mini",
            "instructions": (
                "Продолжай уже начатые Instagram-диалоги кратко, безопасно и без повторного знакомства."
            ),
            "input": prompt,
            "max_output_tokens": 220,
            "store": False,
        },
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(
            f"OpenAI HTTP {response.status_code}: {response.text[:800]}"
        )
    data = response.json() if response.text.strip() else {}
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    draft = "\n".join(parts).strip().strip('"').strip()
    draft = re.sub(r"\s+", " ", draft).strip()
    if not draft:
        raise RuntimeError("Neona returned an empty Instagram follow-up.")
    return draft[:1000]


def _generate_instagram_comment_draft(event: dict, owner_name: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")

    prompt = (
        "Ты — Неона, секретарь-референт Агентства W. Подготовь ответ от лица "
        f"владельца Instagram-аккаунта {str(owner_name or '').strip() or 'Агентства W'}. "
        "Это публичный ответ на комментарий под постом или Reels. Пиши по-русски, "
        "тепло, естественно и уважительно, максимум 2 коротких предложения. "
        "Не называй себя ботом или искусственным интеллектом, не обещай доход или "
        "результат, не выдумывай факты. Если уместно, заверши одним лёгким вопросом. "
        "Верни только готовый ответ без кавычек и пояснений.\n\n"
        f"Текст публикации: {str(event.get('media_caption') or '')[:3000]}\n"
        f"Автор комментария: @{str(event.get('author_username') or 'пользователь')}\n"
        f"Комментарий: {str(event.get('comment_text') or '')[:3000]}"
    )
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("OPENAI_COMMENT_MODEL", "gpt-5-mini").strip()
            or "gpt-5-mini",
            "instructions": "Создавай только безопасные публичные ответы Instagram.",
            "input": prompt,
            "max_output_tokens": 220,
            "store": False,
        },
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(
            f"OpenAI HTTP {response.status_code}: {response.text[:800]}"
        )
    data = response.json() if response.text.strip() else {}
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    draft = "\n".join(parts).strip().strip('"').strip()
    draft = re.sub(r"\s+", " ", draft).strip()
    if not draft:
        raise RuntimeError("Neona returned an empty Instagram comment draft.")
    return draft[:1000]


def _store_instagram_comment(event: dict, connection: dict) -> None:
    comment_id = str(event.get("comment_id") or "").strip()
    if not comment_id:
        return
    existing = _sb_get(
        "agency_instagram_comments",
        {"comment_id": f"eq.{comment_id}", "select": "comment_id", "limit": 1},
    )
    if existing:
        return

    owner_id = int(connection["owner_telegram_id"])
    owner_name = str(connection.get("owner_name") or "").strip()
    thread_row = _instagram_comment_thread_context(event, owner_id)
    is_continuation = bool(thread_row)

    try:
        if is_continuation:
            draft = _generate_instagram_comment_followup(
                event,
                owner_name,
                thread_row or {},
            )
        else:
            draft = _generate_instagram_comment_draft(event, owner_name)
        draft_error = None
    except Exception as exc:
        print(
            "INSTAGRAM_COMMENT_DRAFT_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        draft = (
            "Спасибо, что написали. Расскажите, пожалуйста, "
            "что именно вы хотели бы уточнить?"
        )
        draft_error = str(exc)[:1000]

    now = datetime.now(timezone.utc).isoformat()
    row_payload = {
        "owner_telegram_id": owner_id,
        "instagram_account_id": str(event.get("instagram_account_id") or ""),
        "comment_id": comment_id,
        "parent_comment_id": str(event.get("parent_comment_id") or "") or None,
        "media_id": str(event.get("media_id") or "") or None,
        "media_type": str(event.get("media_type") or "") or None,
        "media_permalink": str(event.get("media_permalink") or "") or None,
        "media_caption": str(event.get("media_caption") or "") or None,
        "author_id": str(event.get("author_id") or "") or None,
        "author_username": str(event.get("author_username") or "") or None,
        "comment_text": str(event.get("comment_text") or ""),
        "comment_timestamp": _instagram_event_datetime(
            event.get("comment_timestamp")
        ),
        "ai_draft": draft,
        "status": "pending",
        "error_text": draft_error,
        "payload": event.get("raw_value") or {},
        "created_at": now,
        "updated_at": now,
    }

    if is_continuation and not draft_error:
        try:
            result = _send_instagram_comment_reply(
                comment_id,
                draft,
                access_token=str(connection.get("access_token") or ""),
            )
            row_payload.update(
                {
                    "approved_reply": draft,
                    "reply_id": str(result.get("id") or "") or None,
                    "status": "replied",
                    "error_text": None,
                    "replied_at": now,
                    "updated_at": now,
                }
            )
            print(
                "INSTAGRAM_COMMENT_AUTO_REPLY_SENT:",
                {
                    "owner_id": owner_id,
                    "comment_id": comment_id,
                    "parent_comment_id": str(event.get("parent_comment_id") or ""),
                    "reply_id": str(result.get("id") or ""),
                },
                flush=True,
            )
        except Exception as exc:
            # Не теряем реплику: она останется pending в центре комментариев,
            # где владелец сможет отправить её вручную.
            row_payload["error_text"] = (
                "Автоответ не отправлен: "
                f"{type(exc).__name__}: {exc}"
            )[:1000]
            print(
                "INSTAGRAM_COMMENT_AUTO_REPLY_ERROR:",
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    _sb_post(
        "agency_instagram_comments",
        row_payload,
        merge=True,
        on_conflict="comment_id",
    )


def _process_instagram_comments(payload: dict) -> None:
    for raw_event in _iter_instagram_comments(payload):
        try:
            connection = _resolve_instagram_connection(
                raw_event["instagram_account_id"]
            )
            account_id = str(connection.get("instagram_account_id") or "").strip()
            account_username = str(connection.get("instagram_username") or "").strip()
            if (
                str(raw_event.get("author_id") or "").strip() == account_id
                or (
                    account_username
                    and str(raw_event.get("author_username") or "").strip().lower()
                    == account_username.lower()
                )
            ):
                continue
            event = _enrich_instagram_comment(
                raw_event,
                str(connection.get("access_token") or ""),
            )
            print(
                "INSTAGRAM_COMMENT_EVENT:",
                {
                    "owner_id": int(connection["owner_telegram_id"]),
                    "comment_id": str(event.get("comment_id") or ""),
                    "parent_comment_id": str(event.get("parent_comment_id") or ""),
                    "author_username": str(event.get("author_username") or ""),
                },
                flush=True,
            )
            if (
                account_username
                and str(event.get("author_username") or "").strip().lower()
                == account_username.lower()
            ):
                continue
            _store_instagram_comment(event, connection)
        except Exception as exc:
            print(
                "INSTAGRAM_COMMENT_PROCESSING_ERROR:",
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


def _send_instagram_comment_reply(
    comment_id: str,
    message: str,
    *,
    access_token: str,
) -> dict:
    response = requests.post(
        f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}/"
        f"{str(comment_id or '').strip()}/replies",
        headers={"Authorization": f"Bearer {str(access_token or '').strip()}"},
        data={"message": str(message or "").strip()},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Instagram comment reply HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )
    data = response.json() if response.text.strip() else {}
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Instagram comment reply error: {data}")
    return data


def _validated_comment_action(payload: dict, required_purpose: str) -> tuple[int, str]:
    state_payload = _decode_instagram_state(str(payload.get("state") or ""))
    if str(state_payload.get("purpose") or "").strip() != required_purpose:
        raise RuntimeError("Invalid Instagram comment action.")
    owner_id = int(state_payload["owner_id"])
    comment_id = str(payload.get("comment_id") or "").strip()
    if not comment_id or comment_id != str(state_payload.get("comment_id") or "").strip():
        raise RuntimeError("Instagram comment action does not match this comment.")
    return owner_id, comment_id


async def instagram_comment_draft(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid request body.")
        owner_id, comment_id = _validated_comment_action(
            payload,
            "agency_w_instagram_comment_draft",
        )
        rows = _sb_get(
            "agency_instagram_comments",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "comment_id": f"eq.{comment_id}",
                "select": "*",
                "limit": 1,
            },
        )
        if not rows:
            return JSONResponse({"error": "Комментарий не найден."}, status_code=404)
        row = dict(rows[0])
        connection = _resolve_instagram_connection(row["instagram_account_id"])
        draft = _generate_instagram_comment_draft(
            row,
            str(connection.get("owner_name") or ""),
        )
        _sb_patch(
            "agency_instagram_comments",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "comment_id": f"eq.{comment_id}",
            },
            {
                "ai_draft": draft,
                "error_text": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return JSONResponse({"ok": True, "draft": draft})
    except Exception as exc:
        print(
            "INSTAGRAM_COMMENT_DRAFT_ENDPOINT_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return JSONResponse({"error": str(exc)}, status_code=400)


async def instagram_comment_reply(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid request body.")
        owner_id, comment_id = _validated_comment_action(
            payload,
            "agency_w_instagram_comment_reply",
        )
        message = re.sub(r"\s+", " ", str(payload.get("message") or "")).strip()
        if not message:
            raise RuntimeError("Ответ не может быть пустым.")
        if len(message) > 1000:
            raise RuntimeError("Ответ слишком длинный: максимум 1000 знаков.")

        rows = _sb_get(
            "agency_instagram_comments",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "comment_id": f"eq.{comment_id}",
                "select": "*",
                "limit": 1,
            },
        )
        if not rows:
            return JSONResponse({"error": "Комментарий не найден."}, status_code=404)
        row = dict(rows[0])
        if str(row.get("status") or "").strip() == "replied":
            return JSONResponse(
                {"error": "Ответ на этот комментарий уже отправлен."},
                status_code=409,
            )

        connection = _resolve_instagram_connection(row["instagram_account_id"])
        result = _send_instagram_comment_reply(
            comment_id,
            message,
            access_token=str(connection.get("access_token") or ""),
        )
        now = datetime.now(timezone.utc).isoformat()
        _sb_patch(
            "agency_instagram_comments",
            {
                "owner_telegram_id": f"eq.{owner_id}",
                "comment_id": f"eq.{comment_id}",
            },
            {
                "approved_reply": message,
                "reply_id": str(result.get("id") or "") or None,
                "status": "replied",
                "error_text": None,
                "replied_at": now,
                "updated_at": now,
            },
        )
        return JSONResponse(
            {"ok": True, "reply_id": str(result.get("id") or "")}
        )
    except Exception as exc:
        print(
            "INSTAGRAM_COMMENT_REPLY_ENDPOINT_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return JSONResponse({"error": str(exc)}, status_code=400)

def _neona_config(core, connection: dict | None = None):
    connection = dict(connection or {})
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SECRET_KEY": os.getenv("SUPABASE_SECRET_KEY", "").strip(),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Instagram/Neona environment variables: " + ", ".join(missing)
        )

    raw_owner_id = connection.get("owner_telegram_id") or INSTAGRAM_OWNER_ID
    raw_owner_name = connection.get("owner_name") or INSTAGRAM_OWNER_NAME
    try:
        owner_id = int(raw_owner_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Instagram connection has no valid Agency W owner id.") from exc
    if not str(raw_owner_name or "").strip():
        raise RuntimeError("Instagram connection has no Agency W owner name.")

    config = core.Config(
        supabase_url=required["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=required["SUPABASE_SECRET_KEY"],
        # Telegram transport is not used by this web service.
        fernet_key="",
        telegram_api_id=0,
        telegram_api_hash="",
        openai_api_key=required["OPENAI_API_KEY"],
    )
    return config, owner_id, _owner_name_for_russian(str(raw_owner_name))


def _build_neona_draft(event: dict) -> None:
    """Run the existing Neona policy and save channel-separated dialog memory."""
    import neona_dialog_policy_v3 as policy

    policy.apply_policy()
    core = policy.core

    connection = _resolve_instagram_connection(event["recipient_id"])
    access_token = str(connection.get("access_token") or "").strip()
    config, owner_id, owner_name = _neona_config(core, connection)
    contact_id = _instagram_contact_id(event["sender_id"])
    message_id = str(event.get("message_id") or "").strip()
    text = str(event.get("text") or "").strip()
    audio_url = str(event.get("audio_url") or "").strip()
    if audio_url:
        try:
            transcript = _transcribe_instagram_audio(audio_url)
            print(
                "INSTAGRAM_AUDIO_TRANSCRIPT:",
                {
                    "sender_id": event["sender_id"],
                    "mid": message_id,
                    "text": transcript,
                },
                flush=True,
            )
            text = (
                f"{text}\n\n{transcript}".strip()
                if text
                else transcript
            )
        except Exception as exc:
            print(
                "INSTAGRAM_AUDIO_ERROR:",
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            if not text:
                _send_instagram_text(
                    event["recipient_id"],
                    event["sender_id"],
                    "Я получила ваше голосовое сообщение, но сейчас не смогла его разобрать. "
                    "Напишите, пожалуйста, эту мысль текстом — и я сразу отвечу.",
                    access_token=access_token,
                )
                return

    message_dt = _message_datetime(event.get("timestamp"))

    state = core._dialog_state(config, owner_id, contact_id)
    if state is None:
        state = {
            "last_incoming_message_id": 0,
            "stage": "idle",
            "greeted": False,
            "context": {
                "channel": "instagram",
                "instagram_sender_id": event["sender_id"],
                "instagram_recipient_id": event["recipient_id"],
            },
        }

    context = (
        dict(state.get("context"))
        if isinstance(state.get("context"), dict)
        else {}
    )

    # Meta can retry the same webhook. Do not let Neona process the same MID twice.
    if message_id and str(context.get("instagram_last_mid") or "") == message_id:
        print(
            "INSTAGRAM_NEONA_DUPLICATE:",
            {"sender_id": event["sender_id"], "mid": message_id},
            flush=True,
        )
        return

    reply, new_stage, greeted, new_context = core._process_message(
        config,
        owner_id,
        owner_name,
        contact_id,
        "",  # Instagram display name will be connected in the next transport step.
        "",
        text,
        message_dt,
        state,
    )

    reply_text = _polish_instagram_reply(str(reply or ""), owner_name)

    print(
        "INSTAGRAM_NEONA_DRAFT:",
        {
            "sender_id": event["sender_id"],
            "incoming": text,
            "draft": reply_text,
            "stage": str(new_stage or "idle"),
            "mid": message_id,
        },
        flush=True,
    )

    if not reply_text:
        raise RuntimeError("Neona returned an empty Instagram reply.")

    send_result = _send_instagram_text(
        event["recipient_id"],
        event["sender_id"],
        reply_text,
        access_token=access_token,
    )
    sent_message_id = str(send_result.get("message_id") or "").strip()

    new_context = dict(new_context or {})
    new_context.update(
        {
            "channel": "instagram",
            "instagram_sender_id": event["sender_id"],
            "instagram_recipient_id": event["recipient_id"],
            "instagram_last_mid": message_id,
            "instagram_last_incoming_text": text,
            "instagram_last_draft": reply_text,
            "instagram_last_sent_message_id": sent_message_id,
            "instagram_last_processed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    dedupe_source = message_id or (
        f'{event["sender_id"]}|{event.get("timestamp")}|{text}'
    )
    core._save_dialog_state(
        config,
        owner_id,
        contact_id,
        last_incoming_id=_stable_message_id(dedupe_source),
        stage=str(new_stage or "idle"),
        greeted=bool(greeted),
        context=new_context,
    )

    print(
        "INSTAGRAM_NEONA_SENT:",
        {
            "recipient_id": event["sender_id"],
            "message_id": sent_message_id,
            "reply": reply_text,
        },
        flush=True,
    )


def _process_instagram_payload(payload: dict) -> None:
    """Process Direct messages and public comments from one webhook delivery."""
    try:
        _process_instagram_comments(payload)
        events = list(_iter_instagram_messages(payload))
        if not events:
            if not list(_iter_instagram_comments(payload)):
                print("INSTAGRAM_NEONA_NO_SUPPORTED_EVENTS", flush=True)
            return

        for event in events:
            try:
                _build_neona_draft(event)
            except Exception as exc:
                print(
                    "INSTAGRAM_NEONA_ERROR:",
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
    except Exception as exc:
        print(
            "INSTAGRAM_NEONA_PAYLOAD_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )



def _vk_contact_id(user_id: int) -> int:
    return -abs(_stable_message_id(f"vk-user:{int(user_id)}"))


def _vk_api(method: str, **params) -> dict:
    if not VK_ACCESS_TOKEN:
        raise RuntimeError("Missing VK_ACCESS_TOKEN")
    response = requests.post(
        f"https://api.vk.com/method/{method}",
        data={
            **params,
            "access_token": VK_ACCESS_TOKEN,
            "v": VK_API_VERSION,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"VK API HTTP {response.status_code}: {str(response.text or '')[:800]}"
        )
    data = response.json() if response.text.strip() else {}
    if not isinstance(data, dict):
        raise RuntimeError("VK API returned an unexpected response.")
    if data.get("error"):
        error = data.get("error") or {}
        raise RuntimeError(
            f"VK API error {error.get('error_code')}: {error.get('error_msg')}"
        )
    return data


def _vk_user_profile(user_id: int) -> dict:
    """Returns VK profile data. Neona is intentionally given only first_name."""
    try:
        data = _vk_api("users.get", user_ids=int(user_id))
        rows = data.get("response") or []
        if isinstance(rows, list) and rows:
            row = rows[0] if isinstance(rows[0], dict) else {}
            first_name = str(row.get("first_name") or "").strip()
            last_name = str(row.get("last_name") or "").strip()
            display_name = " ".join(
                x for x in (first_name, last_name) if x
            ).strip()
            return {
                "first_name": first_name,
                "last_name": last_name,
                "display_name": display_name,
            }
    except Exception as exc:
        print("VK_USER_NAME_ERROR:", f"{type(exc).__name__}: {exc}", flush=True)
    return {"first_name": "", "last_name": "", "display_name": ""}


def _vk_user_name(user_id: int) -> str:
    return str(_vk_user_profile(user_id).get("first_name") or "").strip()


def _send_vk_text(peer_id: int, text: str) -> dict:
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("VK send requires non-empty text.")
    return _vk_api(
        "messages.send",
        peer_id=int(peer_id),
        random_id=0,
        message=text,
    )



def _supabase_rest_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY")
    return url, key


def _supabase_headers(prefer: str = "") -> dict[str, str]:
    _, key = _supabase_rest_config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _sb_get(table: str, params: dict) -> list[dict]:
    url, _ = _supabase_rest_config()
    response = requests.get(
        f"{url}/rest/v1/{table}",
        headers=_supabase_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_post(
    table: str,
    payload: dict,
    *,
    merge: bool = False,
    on_conflict: str = "",
) -> list[dict]:
    url, _ = _supabase_rest_config()
    prefer = "return=representation"
    endpoint = f"{url}/rest/v1/{table}"
    if merge:
        prefer = "resolution=merge-duplicates,return=representation"
    if on_conflict:
        endpoint += "?on_conflict=" + quote(str(on_conflict))
    response = requests.post(
        endpoint,
        headers=_supabase_headers(prefer),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_patch(table: str, filters: dict, payload: dict) -> None:
    url, _ = _supabase_rest_config()
    response = requests.patch(
        f"{url}/rest/v1/{table}",
        headers=_supabase_headers("return=minimal"),
        params=filters,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def _vk_member_by_code(member_code: str) -> dict | None:
    code = str(member_code or "").strip()
    if not code:
        return None
    rows = _sb_get(
        "agency_members",
        {
            "member_code": f"eq.{code}",
            "select": "telegram_id,first_name,member_code,referrer_code",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _vk_member_by_telegram(telegram_id: int) -> dict | None:
    rows = _sb_get(
        "agency_members",
        {
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "telegram_id,first_name,member_code,referrer_code",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _vk_lead(vk_user_id: int) -> dict | None:
    rows = _sb_get(
        "agency_vk_leads",
        {
            "vk_user_id": f"eq.{int(vk_user_id)}",
            "select": "*",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def _fallback_vk_owner() -> dict:
    if not VK_OWNER_ID:
        raise RuntimeError("Missing VK_OWNER_ID fallback")
    try:
        owner_id = int(VK_OWNER_ID)
    except ValueError as exc:
        raise RuntimeError("VK_OWNER_ID must be numeric") from exc

    member = _vk_member_by_telegram(owner_id) or {}
    owner_name = str(
        member.get("first_name")
        or VK_OWNER_NAME
        or ""
    ).strip()
    return {
        "telegram_id": owner_id,
        "first_name": owner_name,
        "member_code": str(member.get("member_code") or "").strip(),
        "attribution_status": "director_default",
    }


def _resolve_vk_owner(ref_code: str) -> dict:
    """Resolve a personal VK ref to an Agency W member; otherwise Director."""
    code = str(ref_code or "").strip()
    if code:
        member = _vk_member_by_code(code)
        if member:
            return {
                "telegram_id": int(member["telegram_id"]),
                "first_name": str(member.get("first_name") or "").strip(),
                "member_code": str(member.get("member_code") or code).strip(),
                "attribution_status": "partner_ref",
            }
    fallback = _fallback_vk_owner()
    if code:
        fallback["attribution_status"] = "invalid_ref_director_default"
    return fallback


def _ensure_vk_lead(
    *,
    vk_user_id: int,
    peer_id: int,
    ref_code: str,
    ref_source: str,
    display_name: str,
) -> tuple[dict, dict]:
    """
    First-touch attribution: once a VK person is attached to an Agency W owner,
    later links from other partners never overwrite that owner.
    """
    existing = _vk_lead(vk_user_id)
    if existing:
        owner = {
            "telegram_id": int(existing["owner_telegram_id"]),
            "first_name": str(existing.get("owner_name") or "").strip(),
            "member_code": str(existing.get("owner_member_code") or "").strip(),
            "attribution_status": str(existing.get("attribution_status") or "locked"),
        }
        _sb_patch(
            "agency_vk_leads",
            {"vk_user_id": f"eq.{int(vk_user_id)}"},
            {
                "vk_peer_id": int(peer_id),
                "display_name": str(display_name or "").strip() or None,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "last_ref": str(ref_code or "").strip() or None,
                "last_ref_source": str(ref_source or "").strip() or None,
            },
        )
        return existing, owner

    owner = _resolve_vk_owner(ref_code)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "vk_user_id": int(vk_user_id),
        "vk_peer_id": int(peer_id),
        "display_name": str(display_name or "").strip() or None,
        "owner_telegram_id": int(owner["telegram_id"]),
        "owner_member_code": str(owner.get("member_code") or "").strip() or None,
        "owner_name": str(owner.get("first_name") or "").strip() or None,
        "first_ref": str(ref_code or "").strip() or None,
        "first_ref_source": str(ref_source or "").strip() or None,
        "last_ref": str(ref_code or "").strip() or None,
        "last_ref_source": str(ref_source or "").strip() or None,
        "attribution_status": str(owner.get("attribution_status") or "partner_ref"),
        "dialog_stage": "idle",
        "first_seen_at": now,
        "last_seen_at": now,
        "last_message_at": now,
    }
    created = _sb_post("agency_vk_leads", payload)
    lead = created[0] if created else payload
    return lead, owner


def _update_vk_lead_after_dialog(
    vk_user_id: int,
    *,
    stage: str,
    incoming_text: str,
) -> None:
    _sb_patch(
        "agency_vk_leads",
        {"vk_user_id": f"eq.{int(vk_user_id)}"},
        {
            "dialog_stage": str(stage or "idle"),
            "last_message_text": str(incoming_text or "")[:1000] or None,
            "last_message_at": datetime.now(timezone.utc).isoformat(),
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _vk_time_entry_reply(policy, owner_name: str, first_name: str) -> str:
    person = str(first_name or "").strip()
    hello = f"Здравствуйте, {person}!" if person else "Здравствуйте!"
    forms = {}
    try:
        forms = policy._owner_forms(owner_name)
    except Exception:
        forms = {}
    owner_genitive = str(forms.get("genitive") or "владельца аккаунта").strip()
    return (
        f"{hello} Я Неона, секретарь-референт {owner_genitive}. "
        "Вы написали «ВРЕМЯ». А что сейчас отнимает у вас его больше всего — "
        "поиск людей, переписка, организация работы или что-то совсем другое?"
    )


def _is_vk_time_keyword(text: str) -> bool:
    normalized = re.sub(r"[^а-яёa-z0-9]+", "", str(text or "").casefold())
    return normalized == "время"

def _vk_neona_config(core, owner: dict | None = None):
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SECRET_KEY": os.getenv("SUPABASE_SECRET_KEY", "").strip(),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing VK/Neona environment variables: " + ", ".join(missing)
        )

    owner = dict(owner or _fallback_vk_owner())
    owner_id = int(owner["telegram_id"])
    owner_name = str(owner.get("first_name") or VK_OWNER_NAME or "").strip()

    config = core.Config(
        supabase_url=required["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=required["SUPABASE_SECRET_KEY"],
        fernet_key="",
        telegram_api_id=0,
        telegram_api_hash="",
        openai_api_key=required["OPENAI_API_KEY"],
    )
    return config, owner_id, _owner_name_for_russian(owner_name)


def _process_vk_message(payload: dict) -> None:
    """Route incoming VK community messages to Theo, not Neona."""
    try:
        obj = payload.get("object")
        if not isinstance(obj, dict):
            return

        message = obj.get("message")
        if not isinstance(message, dict):
            return

        # Ignore messages sent by the community itself.
        if int(message.get("out") or 0) != 0:
            return

        from_id = int(message.get("from_id") or 0)
        peer_id = int(message.get("peer_id") or 0)
        if from_id <= 0 or peer_id <= 0:
            return

        incoming_text = str(message.get("text") or "").strip()
        if not incoming_text:
            return

        ref_code = str(message.get("ref") or "").strip()
        ref_source = str(message.get("ref_source") or "").strip()

        profile = _vk_user_profile(from_id)
        first_name = str(profile.get("first_name") or "").strip()
        display_name = str(profile.get("display_name") or first_name).strip()

        # Preserve the existing Agency W first-touch attribution:
        # the visitor stays attached to the Director/partner who first invited them.
        _, owner = _ensure_vk_lead(
            vk_user_id=from_id,
            peer_id=peer_id,
            ref_code=ref_code,
            ref_source=ref_source,
            display_name=display_name,
        )

        message_key = str(
            message.get("id")
            or message.get("conversation_message_id")
            or ""
        ).strip()

        # Theo has his own VK-community memory in agency_theo_vk_dialogs.
        # Import locally so Instagram and the rest of this service stay isolated.
        from theo_consultant import process_vk_community_message

        result = process_vk_community_message(
            vk_user_id=from_id,
            vk_peer_id=peer_id,
            owner=owner,
            visitor_first_name=first_name,
            incoming_text=incoming_text,
            message_id=message_key,
        )

        # VK can retry the same webhook. Theo detects this from his own memory.
        # Never send the cached reply again on a duplicate delivery.
        if bool(result.get("duplicate")):
            print(
                "VK_THEO_DUPLICATE:",
                {
                    "peer_id": peer_id,
                    "vk_user_id": from_id,
                    "message_id": message_key,
                },
                flush=True,
            )
            return

        reply_text = str(result.get("reply") or "").strip()
        if not reply_text:
            raise RuntimeError("Theo returned an empty VK reply.")

        stage = str(result.get("stage") or "consulting").strip() or "consulting"

        _send_vk_text(peer_id, reply_text)

        _update_vk_lead_after_dialog(
            from_id,
            stage=stage,
            incoming_text=incoming_text,
        )

        print(
            "VK_THEO_SENT:",
            {
                "peer_id": peer_id,
                "vk_user_id": from_id,
                "owner_id": owner.get("telegram_id"),
                "owner_code": owner.get("member_code"),
                "ref": ref_code,
                "stage": stage,
                "incoming": incoming_text,
                "reply": reply_text,
            },
            flush=True,
        )
    except Exception as exc:
        print("VK_THEO_ERROR:", f"{type(exc).__name__}: {exc}", flush=True)


async def vk_webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return PlainTextResponse("Bad Request", status_code=400)

    if not isinstance(payload, dict):
        return PlainTextResponse("Bad Request", status_code=400)

    incoming_group_id = str(payload.get("group_id") or "").strip()
    if VK_GROUP_ID and incoming_group_id != VK_GROUP_ID:
        return PlainTextResponse("Forbidden", status_code=403)

    event_type = str(payload.get("type") or "").strip()

    # VK's confirmation POST contains type + group_id. It does NOT have to
    # contain the callback secret, so confirm first.
    if event_type == "confirmation":
        if not VK_CALLBACK_CONFIRMATION:
            return PlainTextResponse(
                "Missing VK_CALLBACK_CONFIRMATION",
                status_code=500,
            )
        return PlainTextResponse(VK_CALLBACK_CONFIRMATION, status_code=200)

    # For real event deliveries, require the secret configured in VK + Render.
    incoming_secret = str(payload.get("secret") or "")
    if VK_CALLBACK_SECRET and incoming_secret != VK_CALLBACK_SECRET:
        return PlainTextResponse("Forbidden", status_code=403)

    if event_type == "message_new":
        print("VK_WEBHOOK_EVENT:", payload, flush=True)
        return PlainTextResponse(
            "ok",
            status_code=200,
            background=BackgroundTask(_process_vk_message, payload),
        )

    return PlainTextResponse("ok", status_code=200)


async def instagram_webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return PlainTextResponse("Bad Request", status_code=400)

    if not isinstance(payload, dict):
        return PlainTextResponse("Bad Request", status_code=400)

    # Keep the proven webhook acknowledgement fast. Neona works after the 200 response.
    print("INSTAGRAM_WEBHOOK_EVENT:", payload, flush=True)
    return PlainTextResponse(
        "EVENT_RECEIVED",
        status_code=200,
        background=BackgroundTask(_process_instagram_payload, payload),
    )


def _restore_instagram_subscriptions_on_startup() -> None:
    """Idempotently restore comments/messages webhook subscriptions after deploys."""
    try:
        rows = _sb_get(
            "agency_instagram_connections",
            {
                "status": "eq.connected",
                "select": "*",
                "limit": 500,
            },
        )
    except Exception as exc:
        print(
            "INSTAGRAM_SUBSCRIPTION_RESTORE_LOOKUP_ERROR:",
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return

    restored = 0
    for raw_row in rows or []:
        try:
            row = dict(raw_row or {})
            token = _decrypt_instagram_secret(
                row.get("access_token_encrypted") or ""
            )
            if not token:
                continue
            row["access_token"] = token
            row = _ensure_fresh_instagram_token(row)
            _subscribe_instagram_webhooks(
                str(row.get("instagram_account_id") or ""),
                str(row.get("access_token") or ""),
            )
            restored += 1
        except Exception as exc:
            print(
                "INSTAGRAM_SUBSCRIPTION_RESTORE_ERROR:",
                {
                    "owner_id": raw_row.get("owner_telegram_id"),
                    "account_id": raw_row.get("instagram_account_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                flush=True,
            )
    print(
        "INSTAGRAM_SUBSCRIPTIONS_RESTORED:",
        {"count": restored},
        flush=True,
    )


routes = [
    Route("/", health, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/privacy", privacy, methods=["GET"]),
    Route("/terms", terms, methods=["GET"]),
    Route("/data-deletion", data_deletion, methods=["GET"]),
    Route("/instagram/oauth-config", instagram_oauth_config, methods=["GET"]),
    Route("/instagram/connect", instagram_connect, methods=["GET"]),
    Route("/instagram/callback", instagram_oauth_callback, methods=["GET"]),
    Route("/instagram/comments/draft", instagram_comment_draft, methods=["POST"]),
    Route("/instagram/comments/reply", instagram_comment_reply, methods=["POST"]),
    Route("/facebook/connect", facebook_connect, methods=["GET"]),
    Route("/facebook/exchange", facebook_exchange, methods=["POST"]),
    Route("/facebook/callback", facebook_oauth_callback, methods=["GET"]),
    Route(
        "/instagram/webhook",
        instagram_webhook_verify,
        methods=["GET"],
    ),
    Route(
        "/instagram/webhook",
        instagram_webhook_receive,
        methods=["POST"],
    ),
    Route(
        "/vk/webhook",
        vk_webhook_receive,
        methods=["POST"],
    ),
]

@asynccontextmanager
async def _app_lifespan(app):
    _restore_instagram_subscriptions_on_startup()
    yield


app = Starlette(
    routes=routes,
    lifespan=_app_lifespan,
)
