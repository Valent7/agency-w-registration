"""Opt-in, bounded voice-to-text for public Neona.

No audio is stored in Agency W. A recording is processed in memory and
transcribed by OpenAI only after the visitor explicitly starts recording and
presses Stop. This does not send a chat message automatically.
"""
import asyncio
import os
import threading
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse, Response

from public_neona_booking import verify

_ORIGINS = frozenset((
    "https://agency-w-time.onrender.com",
    "https://agency-w-journal.onrender.com",
))
_TYPES = {
    "audio/webm": ("voice.webm", b"\x1a\x45\xdf\xa3"),
    "audio/mp4": ("voice.mp4", None),
    "audio/ogg": ("voice.ogg", b"OggS"),
}
_MAX_BYTES = 1_500_000
_MIN_BYTES = 512
_MAX_TEXT = 1200
_LIMIT_PER_CLIENT_DAY = 8
_LIMIT_PER_CLIENT_MINUTE = 2
_LIMIT_GLOBAL_DAY = 80
_LOCK = threading.Lock()
_HITS = defaultdict(deque)
_ALL_HITS = deque()


def _headers(origin):
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Neona-Session",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _reply(data, origin="", status=200):
    return JSONResponse(
        data, status_code=status,
        headers=_headers(origin) if origin in _ORIGINS else {"Cache-Control": "no-store"},
    )


def _allowed(client):
    now = time.monotonic()
    with _LOCK:
        hits = _HITS[client]
        while hits and hits[0] < now - 86400:
            hits.popleft()
        while _ALL_HITS and _ALL_HITS[0] < now - 86400:
            _ALL_HITS.popleft()
        if (len(hits) >= _LIMIT_PER_CLIENT_DAY
                or len(_ALL_HITS) >= _LIMIT_GLOBAL_DAY
                or sum(t >= now - 60 for t in hits) >= _LIMIT_PER_CLIENT_MINUTE):
            return False
        hits.append(now)
        _ALL_HITS.append(now)
    return True


def _valid_signature(kind, audio):
    if kind == "audio/webm":
        return audio.startswith(_TYPES[kind][1])
    if kind == "audio/ogg":
        return audio.startswith(_TYPES[kind][1])
    if kind == "audio/mp4":
        return len(audio) >= 12 and audio[4:8] == b"ftyp"
    return False


async def public_neona_voice(request):
    origin = request.headers.get("origin", "")
    if origin not in _ORIGINS:
        return _reply({"error": "Доступ запрещён."}, status=403)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_headers(origin))

    # Require the same verified short-lived visitor identity as the chat.
    try:
        identity = verify(request.headers.get("x-neona-session", ""), "identity")
    except Exception:
        return _reply({"error": "Обновите страницу и попробуйте снова."}, origin, 401)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in _TYPES:
        return _reply({"error": "Этот формат аудио не поддерживается."}, origin, 415)

    try:
        length = int(request.headers.get("content-length") or "0")
    except ValueError:
        length = 0
    if length > _MAX_BYTES:
        return _reply({"error": "Запись слишком длинная. Максимум 30 секунд."}, origin, 413)
    try:
        data = await request.body()
    except Exception:
        return _reply({"error": "Не удалось принять аудиозапись."}, origin, 400)
    if not _MIN_BYTES <= len(data) <= _MAX_BYTES or not _valid_signature(content_type, data):
        return _reply({"error": "Не удалось прочитать запись. Попробуйте ещё раз."}, origin, 400)

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return _reply({"error": "Распознавание речи пока недоступно."}, origin, 503)

    # Per signed visitor session (rather than shared Render proxy IP).
    # The global daily cap still bounds total transcription costs.
    if not _allowed(identity["sid"]):
        return _reply({"error": "Лимит голосовых обращений исчерпан. Можно написать вопрос текстом."}, origin, 429)

    filename = _TYPES[content_type][0]

    def transcribe():
        from openai import OpenAI
        client = OpenAI(timeout=22, max_retries=0)
        transcript = client.audio.transcriptions.create(
            model=os.getenv("PUBLIC_NEONA_TRANSCRIPTION_MODEL", "gpt-transcribe"),
            file=(filename, data, content_type),
        )
        return (transcript.text or "").strip()

    try:
        transcript = await asyncio.wait_for(asyncio.to_thread(transcribe), timeout=27)
    except Exception:
        return _reply({"error": "Не удалось распознать запись. Попробуйте ещё раз или напишите текст."}, origin, 503)
    if not transcript:
        return _reply({"error": "Речь не распознана. Попробуйте говорить ближе к микрофону."}, origin, 422)
    if len(transcript) > _MAX_TEXT:
        transcript = transcript[:_MAX_TEXT].rsplit(" ", 1)[0] or transcript[:_MAX_TEXT]
    return _reply({"text": transcript}, origin)
