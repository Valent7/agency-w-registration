import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

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


def _iter_instagram_text_messages(payload: dict):
    """Yield only real incoming text messages from Instagram."""
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

            if not sender_id or not recipient_id or not text:
                continue
            if sender_id == recipient_id:
                continue
            if bool(message.get("is_echo")):
                continue

            yield {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message_id": message_id,
                "text": text,
                "timestamp": event.get("timestamp") or entry_timestamp,
            }



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
    return config, owner_id, required["INSTAGRAM_OWNER_NAME"]


def _build_neona_draft(event: dict) -> None:
    """Run the existing Neona policy and save channel-separated dialog memory."""
    import neona_dialog_policy as policy

    policy.apply_policy()
    core = policy.core

    config, owner_id, owner_name = _neona_config(core)
    contact_id = _instagram_contact_id(event["sender_id"])
    message_id = str(event.get("message_id") or "").strip()
    text = str(event.get("text") or "").strip()
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

    reply_text = str(reply or "").strip()

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
        events = list(_iter_instagram_text_messages(payload))
        if not events:
            print("INSTAGRAM_NEONA_NO_TEXT_MESSAGES", flush=True)
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
]

app = Starlette(routes=routes)
