import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route


VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")
CONTACT_EMAIL = "polesski.immobilien@gmail.com"


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


async def instagram_webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return PlainTextResponse("Bad Request", status_code=400)

    # Пока только безопасно принимаем событие от Instagram.
    # Следующим этапом подключим передачу входящего сообщения Неоне.
    if not isinstance(payload, dict):
        return PlainTextResponse("Bad Request", status_code=400)

    print("INSTAGRAM_WEBHOOK_EVENT:", payload, flush=True)
    return PlainTextResponse("EVENT_RECEIVED", status_code=200)


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
