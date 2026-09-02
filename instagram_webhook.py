import hashlib
import json
import os
import re
import mimetypes
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

import requests

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route


VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")
CONTACT_EMAIL = "polesski.immobilien@gmail.com"

# The Instagram account is mapped to the existing Agency W owner.
# Keep these values in Render Environment, not in GitHub.
INSTAGRAM_OWNER_ID = os.getenv("INSTAGRAM_OWNER_ID", "").strip()
INSTAGRAM_OWNER_NAME = os.getenv("INSTAGRAM_OWNER_NAME", "").strip()
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
INSTAGRAM_API_VERSION = os.getenv("INSTAGRAM_API_VERSION", "v23.0").strip() or "v23.0"

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
  <li>messages and content voluntarily sent to the Instagram account <strong>ai_v_ton</strong>;</li>
  <li>technical metadata required to receive, process, secure and troubleshoot messages.</li>
</ul>

<h2>How we use data</h2>
<ul>
  <li>to receive and respond to Instagram messages;</li>
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



def _send_instagram_text(sender_account_id: str, recipient_id: str, text: str) -> dict:
    """Send one text reply through Instagram API with Instagram Login."""
    if not INSTAGRAM_ACCESS_TOKEN:
        raise RuntimeError("Missing INSTAGRAM_ACCESS_TOKEN")

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
            "Authorization": f"Bearer {INSTAGRAM_ACCESS_TOKEN}",
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

def _neona_config(core):
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SECRET_KEY": os.getenv("SUPABASE_SECRET_KEY", "").strip(),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "").strip(),
        "INSTAGRAM_OWNER_ID": INSTAGRAM_OWNER_ID,
        "INSTAGRAM_OWNER_NAME": INSTAGRAM_OWNER_NAME,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Instagram/Neona environment variables: " + ", ".join(missing)
        )

    try:
        owner_id = int(required["INSTAGRAM_OWNER_ID"])
    except ValueError as exc:
        raise RuntimeError("INSTAGRAM_OWNER_ID must be a numeric Agency W owner id.") from exc

    config = core.Config(
        supabase_url=required["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=required["SUPABASE_SECRET_KEY"],
        # Telegram transport is not used by this web service.
        fernet_key="",
        telegram_api_id=0,
        telegram_api_hash="",
        openai_api_key=required["OPENAI_API_KEY"],
    )
    return config, owner_id, _owner_name_for_russian(required["INSTAGRAM_OWNER_NAME"])


def _build_neona_draft(event: dict) -> None:
    """Run the existing Neona policy and save channel-separated dialog memory."""
    import neona_dialog_policy as policy

    policy.apply_policy()
    core = policy.core

    config, owner_id, owner_name = _neona_config(core)
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
    """Process one webhook delivery, generate Neona replies and send them to Direct."""
    try:
        events = list(_iter_instagram_messages(payload))
        if not events:
            print("INSTAGRAM_NEONA_NO_SUPPORTED_MESSAGES", flush=True)
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


def _vk_user_name(user_id: int) -> str:
    try:
        data = _vk_api("users.get", user_ids=int(user_id))
        rows = data.get("response") or []
        if isinstance(rows, list) and rows:
            first_name = str(rows[0].get("first_name") or "").strip()
            last_name = str(rows[0].get("last_name") or "").strip()
            return " ".join(x for x in (first_name, last_name) if x).strip()
    except Exception as exc:
        print("VK_USER_NAME_ERROR:", f"{type(exc).__name__}: {exc}", flush=True)
    return ""


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


def _vk_neona_config(core):
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SECRET_KEY": os.getenv("SUPABASE_SECRET_KEY", "").strip(),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "").strip(),
        "VK_OWNER_ID": VK_OWNER_ID,
        "VK_OWNER_NAME": VK_OWNER_NAME,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing VK/Neona environment variables: " + ", ".join(missing)
        )

    try:
        owner_id = int(required["VK_OWNER_ID"])
    except ValueError as exc:
        raise RuntimeError("VK_OWNER_ID must be a numeric Agency W owner id.") from exc

    config = core.Config(
        supabase_url=required["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=required["SUPABASE_SECRET_KEY"],
        fernet_key="",
        telegram_api_id=0,
        telegram_api_hash="",
        openai_api_key=required["OPENAI_API_KEY"],
    )
    return config, owner_id, _owner_name_for_russian(required["VK_OWNER_NAME"])


def _process_vk_message(payload: dict) -> None:
    try:
        obj = payload.get("object")
        if not isinstance(obj, dict):
            return
        message = obj.get("message")
        if not isinstance(message, dict):
            return
        if int(message.get("out") or 0) != 0:
            return

        from_id = int(message.get("from_id") or 0)
        peer_id = int(message.get("peer_id") or 0)
        if from_id <= 0 or peer_id <= 0:
            return

        incoming_text = str(message.get("text") or "").strip()
        if not incoming_text:
            return

        import neona_dialog_policy as policy

        policy.apply_policy()
        core = policy.core
        config, owner_id, owner_name = _vk_neona_config(core)

        contact_id = _vk_contact_id(from_id)
        message_key = str(
            message.get("id")
            or message.get("conversation_message_id")
            or ""
        ).strip()
        message_dt = _message_datetime(message.get("date"))

        state = core._dialog_state(config, owner_id, contact_id)
        if state is None:
            state = {
                "last_incoming_message_id": 0,
                "stage": "idle",
                "greeted": False,
                "context": {
                    "channel": "vk",
                    "vk_user_id": from_id,
                    "vk_peer_id": peer_id,
                },
            }

        context = dict(state.get("context")) if isinstance(state.get("context"), dict) else {}
        if message_key and str(context.get("vk_last_message_id") or "") == message_key:
            return

        display_name = _vk_user_name(from_id)
        reply, new_stage, greeted, new_context = core._process_message(
            config,
            owner_id,
            owner_name,
            contact_id,
            display_name,
            "",
            incoming_text,
            message_dt,
            state,
        )

        reply_text = _polish_instagram_reply(str(reply or ""), owner_name)
        if not reply_text:
            raise RuntimeError("Neona returned an empty VK reply.")

        _send_vk_text(peer_id, reply_text)

        new_context = dict(new_context or {})
        new_context.update(
            {
                "channel": "vk",
                "vk_user_id": from_id,
                "vk_peer_id": peer_id,
                "vk_display_name": display_name,
                "vk_last_message_id": message_key,
                "vk_last_incoming_text": incoming_text,
                "vk_last_reply": reply_text,
                "vk_last_processed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        dedupe_source = message_key or f"{from_id}|{message.get('date')}|{incoming_text}"
        core._save_dialog_state(
            config,
            owner_id,
            contact_id,
            last_incoming_id=_stable_message_id(f"vk:{dedupe_source}"),
            stage=str(new_stage or "idle"),
            greeted=bool(greeted),
            context=new_context,
        )

        print(
            "VK_NEONA_SENT:",
            {"peer_id": peer_id, "incoming": incoming_text, "reply": reply_text},
            flush=True,
        )
    except Exception as exc:
        print("VK_NEONA_ERROR:", f"{type(exc).__name__}: {exc}", flush=True)


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


routes = [
    Route("/", health, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/privacy", privacy, methods=["GET"]),
    Route("/terms", terms, methods=["GET"]),
    Route("/data-deletion", data_deletion, methods=["GET"]),
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

app = Starlette(routes=routes)
