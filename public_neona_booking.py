"""Restricted public Neona identity and appointment bridge for Agency W.

Public visitors never get a partner ID, service key, internal file, or calendar data.
The verified referral selects whose calendar gets a meeting; no redirect to
a random account. All writes occur server-side after explicit confirmation.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from starlette.responses import JSONResponse, Response

ORIGINS = frozenset(("https://agency-w-time.onrender.com", "https://agency-w-journal.onrender.com"))
TZ_CHOICES = ("Europe/Moscow", "Europe/Berlin", "Europe/London", "Asia/Almaty",
              "Asia/Astana", "Asia/Tbilisi", "Asia/Yerevan", "Asia/Tashkent", "America/New_York")
FORMATS = ("Zoom", "Telegram", "WhatsApp")
_LOCK = threading.Lock()
_BOOKINGS_IP = defaultdict(deque)
_BOOKINGS_GLOBAL = deque()


def cors(origin):
    return {"Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600", "Cache-Control": "no-store",
            "Vary": "Origin", "X-Content-Type-Options": "nosniff"}


def reply(data, status=200, origin=""):
    return JSONResponse(data, status_code=status, headers=cors(origin) if origin in ORIGINS else {"Cache-Control":"no-store"})


def _secret():
    key = (os.getenv("PUBLIC_NEONA_SIGNING_SECRET")
           or os.getenv("FERNET_KEY")
           or os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if len(key) < 32:
        raise RuntimeError("A strong signing secret is required")
    return key.encode("utf-8")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * ((-len(s)) % 4))


def sign(data):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = _b64(raw)
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + _b64(mac)


def verify(token, expected_kind):
    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("token")
    payload, seal = token.split(".", 1)
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(seal)):
        raise ValueError("signature")
    data = json.loads(_unb64(payload))
    if not isinstance(data, dict) or data.get("kind") != expected_kind:
        raise ValueError("kind")
    if abs(int(time.time()) - int(data.get("issued") or 0)) > 5400:
        raise ValueError("expired")
    if not isinstance(data.get("sid"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["sid"]):
        raise ValueError("sid")
    if int(data.get("owner_id") or 0) <= 0:
        raise ValueError("owner")
    return data


def _rows(table, params):
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    secret = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if not url or not secret:
        raise RuntimeError("Supabase server settings are missing")
    result = requests.get(url + "/rest/v1/" + table,
                          headers={"apikey":secret,"Authorization":"Bearer " + secret},
                          params=params, timeout=12)
    result.raise_for_status()
    data = result.json()
    return data if isinstance(data, list) else []


def resolve_owner(ref_code):
    """Only a database-verified active partner owns a referred session."""
    code = str(ref_code or "").strip()
    director_id = str(os.getenv("VK_OWNER_ID") or "").strip()
    if director_id.isdigit():
        director_rows = _rows("agency_members", {
            "telegram_id": "eq." + director_id,
            "select":"telegram_id,first_name,member_code", "limit":1})
    else:
        director_rows = _rows("agency_members",{
            "select":"telegram_id,first_name,member_code", "order":"created_at.asc", "limit":1})
    if not director_rows:
        raise RuntimeError("Cannot verify Agency W director")
    director = director_rows[0]

    if code and re.fullmatch(r"W[1-9][0-9]{4,14}", code):
        rows = _rows("agency_members", {
            "member_code":"eq."+code, "select":"telegram_id,first_name,member_code", "limit":1})
        if rows:
            person = rows[0]
            active = _rows("partner_activations",{
                "telegram_id":"eq."+str(int(person["telegram_id"])),
                "select":"status", "limit":1})
            if active and active[0].get("status") in ("confirmed","legacy_active"):
                return {"owner_id":int(person["telegram_id"]),
                        "owner_name":str(person.get("first_name") or "партнёра").strip()[:65],
                        "code":code, "attribution":"partner_ref"}
    return {"owner_id":int(director["telegram_id"]),
            "owner_name":str(director.get("first_name") or "Валентины").strip()[:65],
            "code":str(director.get("member_code") or "").strip()[:24],
            "attribution":"director_default"}


def create_identity(ref_code=""):
    owner = resolve_owner(ref_code)
    data = {"kind":"identity","sid":uuid.uuid4().hex,
            "issued":int(time.time()), **owner}
    return sign(data), owner


def _client_ip(request):
    # Never trust X-Forwarded-For from a public browser.
    return request.client.host if request.client else "unknown"


async def identity(request):
    origin = request.headers.get("origin", "")
    if origin not in ORIGINS:
        return reply({"error":"Доступ запрещён."},403)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors(origin))
    try:
        ref = request.query_params.get("ref") or ""
        if len(ref) > 40:
            raise ValueError("ref")
        token, owner = create_identity(ref)
        return reply({"session":token,"owner_name":owner["owner_name"],
                      "attribution":owner["attribution"]},origin=origin)
    except ValueError:
        return reply({"error":"Неверная ссылка."},400,origin)
    except Exception:
        return reply({"error":"Временно не удалось определить владельца аккаунта."},503,origin)


def _booking_rate(ip, consume=False):
    now = time.monotonic()
    with _LOCK:
        q = _BOOKINGS_IP[ip]
        while q and now - q[0] > 86400:
            q.popleft()
        while _BOOKINGS_GLOBAL and now-_BOOKINGS_GLOBAL[0] > 86400:
            _BOOKINGS_GLOBAL.popleft()
        if len(q) >= 2 or len(_BOOKINGS_GLOBAL) >= 12:
            return False
        if consume:
            q.append(now)
            _BOOKINGS_GLOBAL.append(now)
        return True


def _format_slot(start_utc, tz_name):
    local = start_utc.astimezone(ZoneInfo(tz_name))
    msk = start_utc.astimezone(ZoneInfo("Europe/Moscow"))
    return (local.strftime("%d.%m.%Y в %H:%M") + " (" + tz_name + "); "
            + msk.strftime("%d.%m.%Y в %H:%M МСК"))


def _candidate(form):
    if not isinstance(form, dict):
        raise ValueError("form")
    name = str(form.get("name") or "").strip()
    email = str(form.get("email") or "").strip()
    day = str(form.get("date") or "").strip()
    clock = str(form.get("time") or "").strip()
    tz = str(form.get("timezone") or "").strip()
    fmt = str(form.get("format") or "").strip()
    if len(name) < 2 or len(name) > 80 or any(c in name for c in "<>{}"):
        raise ValueError("name")
    if len(email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("email")
    if tz not in TZ_CHOICES or fmt not in FORMATS:
        raise ValueError("timezone-or-format")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not re.fullmatch(r"\d{2}:\d{2}",clock):
        raise ValueError("date-time")
    local = datetime.fromisoformat(day + "T" + clock).replace(tzinfo=ZoneInfo(tz))
    if local.strftime("%Y-%m-%dT%H:%M") != day + "T" + clock:
        raise ValueError("date-time")
    start_utc = local.astimezone(timezone.utc)
    if start_utc < datetime.now(timezone.utc) + timedelta(minutes=60):
        raise ValueError("too-soon")
    if start_utc > datetime.now(timezone.utc) + timedelta(days=30):
        raise ValueError("too-far")
    return {"name":name,"email":email,"date":day,"time":clock,
            "timezone":tz,"format":fmt,"start":start_utc.isoformat()}


def _check_slot(owner_id, start):
    from theo_consultant import _slot_availability
    start_at = datetime.fromisoformat(start).astimezone(timezone.utc)
    return _slot_availability(owner_id, start_at, start_at + timedelta(minutes=30))


async def appointment(request):
    origin = request.headers.get("origin", "")
    if origin not in ORIGINS:
        return reply({"error":"Доступ запрещён."},403)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors(origin))
    if request.headers.get("content-type","").split(";",1)[0].strip().lower() != "application/json":
        return reply({"error":"Неверный формат данных."},415,origin)
    try:
        data_raw = await request.body()
        if len(data_raw) > 5000:
            raise ValueError("size")
        data = json.loads(data_raw)
        if not isinstance(data,dict) or data.get("consent") is not True:
            return reply({"error":"Для записи необходимо согласие на обработку контактных данных."},400,origin)
        mode = str(data.get("action") or "")
        session = verify(data.get("session"), "identity")
        if mode == "check":
            form = _candidate(data.get("form"))
            if not _booking_rate(_client_ip(request)):
                return reply({"error":"Для защиты календаря лимит записей исчерпан."},429,origin)
            status = await __import__("asyncio").to_thread(_check_slot,session["owner_id"],form["start"])
            if status != "free":
                message = "Это время недоступно. Выберите, пожалуйста, другое."
                if status == "unavailable":
                    message = "Не удалось проверить календарь, поэтому встречу пока не назначаем."
                return reply({"error":message},409,origin)
            offer = sign({"kind":"offer","issued":int(time.time()),
                          "sid":session["sid"],"owner_id":session["owner_id"],
                          "owner_name":session["owner_name"],"form":form})
            start = datetime.fromisoformat(form["start"]).astimezone(timezone.utc)
            return reply({"offer":offer,"slot":_format_slot(start,form["timezone"]),
                          "owner_name":session["owner_name"]},origin=origin)
        if mode != "confirm":
            raise ValueError("action")
        offer = verify(data.get("offer"),"offer")
        if offer["sid"] != session["sid"] or offer["owner_id"] != session["owner_id"]:
            raise ValueError("owner mismatch")
        if not _booking_rate(_client_ip(request)):
            return reply({"error":"Лимит записей исчерпан. Свяжитесь с Агентством напрямую."},429,origin)
        from theo_consultant import _table_get, _create_meeting, _load_zoom_link
        source = "Неона — сайт " + session["sid"]
        def book():
            found = _table_get("agency_meetings",{
                "owner_telegram_id":"eq."+str(session["owner_id"]),
                "source":"eq."+source,"select":"id,start_at", "limit":1})
            if found:
                return found[0]
            form = offer["form"]
            status = _check_slot(session["owner_id"], form["start"])
            if status != "free":
                raise ValueError("slot no longer available")
            zoom_link, _ = _load_zoom_link(session["owner_id"]) if form["format"] == "Zoom" else ("","")
            start = datetime.fromisoformat(form["start"]).astimezone(timezone.utc)
            payload = {
                "owner_telegram_id":int(session["owner_id"]),
                "owner_name":str(session["owner_name"]),
                "contact_name":form["name"],
                "contact_city":form["timezone"],
                "contact_timezone":form["timezone"],
                "start_at":start.isoformat(),
                "end_at":(start+timedelta(minutes=30)).isoformat(),
                "meeting_format":form["format"],
                "meeting_link":zoom_link or None,
                "status":"Подтверждена",
                "notes":"Публичный сайт: контактный email "+form["email"]+"; пользователь подтвердил запись.",
                "source":source,
            }
            return _create_meeting(payload)
        try:
            record = await __import__("asyncio").wait_for(__import__("asyncio").to_thread(book),timeout=25)
        except ValueError:
            return reply({"error":"Время стало недоступно. Пожалуйста, выберите другое."},409,origin)
        except Exception:
            return reply({"error":"Календарь не подтвердил запись. Обратитесь к Агентству напрямую."},503,origin)
        _booking_rate(_client_ip(request),consume=True)
        start=datetime.fromisoformat(offer["form"]["start"])
        return reply({"booked":True,"slot":_format_slot(start,offer["form"]["timezone"]),
                      "owner_name":session["owner_name"],"format":offer["form"]["format"]},origin=origin)
    except (ValueError,KeyError,TypeError,AttributeError):
        return reply({"error":"Проверьте данные встречи или обновите страницу."},400,origin)
    except Exception:
        return reply({"error":"Сервис встреч временно недоступен."},503,origin)
