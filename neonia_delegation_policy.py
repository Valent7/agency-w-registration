"""Owner-approved professional evidence rules for the Issue 126 pilot."""
from datetime import datetime, timedelta, timezone
import re

PILOT_OWNER = 235805138
POLICY_MARKER = "Политика Неонии: делегирование (#126)"
POLICY_VERSION = "delegation-126-v1"

_DATE_WINDOW = timedelta(days=30)
_PUBLIC_EVIDENCE = re.compile(
    r"https://(?:vk\.com/wall(?:-\d+_\d+\?reply=\d+|[1-9]\d*_\d+)"
    r"|t\.me/[A-Za-z0-9_]+/(?:s/)?[1-9]\d*)"
)
_OPERATOR = re.compile(
    r"\b(?:я\s+(?:предприниматель|основател\w*|руководител\w*|наставник\w*|"
    r"директор\w*|владел\w*)|(?:мой|моя|мои|наш|наша|наши)\s+"
    r"(?:бизнес\w*|компан\w*|команд\w*|(?:крипто)?проект\w*|агентств\w*|школ\w*|"
    r"практик\w*|магазин\w*)|руковожу|управляю|основал\w*)\b", re.I
)
_PROCESS = re.compile(
    r"\b(?:ищу|ищем|набираю|набираем|привлекаю|привлекаем|приглашаю|приглашаем|"
    r"подбираю|подбираем|обучаю|обучаем|сопровождаю|сопровождаем|"
    r"адаптирую|адаптируем|наставляю|наставляем)\b[^.!?;]{0,60}"
    r"\b(?:партн[её]р\w*|клиент\w*|представител\w*|команд\w*|"
    r"сотрудник\w*|дистрибьютор\w*)\b", re.I
)
_RISK = re.compile(
    r"финансов\w+\s+пирамид\w*|\b(?:скам|scam|ponzi)\b|"
    r"гарантированн\w+\s+(?:доход\w*|прибыл\w*|доходност\w*)|"
    r"гарантиру\w+\s+(?:доход\w*|прибыл\w*|доходност\w*)|доход\s+без\s+риска",
    re.I,
)
_NEGATED = re.compile(
    r"\b(?:не\s+(?:ищу|ищем|набираю|набираем|приглашаю|приглашаем|"
    r"обучаю|обучаем|сопровождаю|сопровождаем)|раньше|бывш\w*)\b", re.I
)


def approved_for_owner(owner_id, target):
    if str(owner_id) != str(PILOT_OWNER) or not isinstance(target, dict):
        return False
    summary = target.get("approved_marketing_core")
    version = target.get("marketing_passport_version")
    return (
        type(version) is int and version > 83 and isinstance(summary, str)
        and POLICY_MARKER in summary.splitlines()
    )


def read_owner_policy(owner_id):
    if str(owner_id) != str(PILOT_OWNER):
        return None
    from vk_scout_worker import _load_target_profiles
    target = _load_target_profiles([int(owner_id)]).get(int(owner_id))
    return target if approved_for_owner(owner_id, target) else None


def risk_signal(text):
    return bool(_RISK.search(str(text or "")))


def delegation_proof(records, *, now=None, max_age=_DATE_WINDOW):
    """Require an attributable current role and an explicit operating process."""
    now = now or datetime.now(timezone.utc)
    available = []
    for record in records or []:
        if not isinstance(record, dict) or not _PUBLIC_EVIDENCE.fullmatch(str(record.get("url") or "")):
            continue
        try:
            timestamp = int(record["timestamp"])
            age = now - datetime.fromtimestamp(timestamp, timezone.utc)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            continue
        if timedelta(0) <= age <= max_age:
            available.append(record)
    if any(risk_signal(record.get("text")) for record in available):
        return None
    for record in sorted(available, key=lambda item: int(item["timestamp"]), reverse=True):
        text = str(record.get("text") or "")
        if _OPERATOR.search(text) and _PROCESS.search(text) and not _NEGATED.search(text):
            return record
    return None


def vk_delegation_proof(candidate):
    raw = candidate.get("raw_profile")
    if not isinstance(raw, dict) or risk_signal(candidate.get("status_text")):
        return None
    uid = str(candidate.get("vk_user_id") or "")
    posts = [
        post for post in (raw.get("public_professional_posts") or [])
        if isinstance(post, dict) and uid.isdecimal()
        and re.fullmatch(rf"https://vk\.com/wall{uid}_[1-9]\d*", str(post.get("url") or ""))
    ]
    return delegation_proof([*(raw.get("public_professional_comments") or []), *posts])


def stage_delegation_draft(chat, supplement):
    """Extend v83 without replacing a separately edited draft or publishing it."""
    approved = chat.get("approved") or {}
    base = approved.get("summary")
    if approved.get("version") != 83 or not isinstance(base, str) or not base.strip():
        raise ValueError("Ожидалась действующая версия ядра v83.")
    merged = base.rstrip() + "\n\n" + supplement.strip()
    if POLICY_MARKER not in supplement.splitlines() or len(merged) > 30000:
        raise ValueError("Проект новой редакции неполный или превышает лимит кабинета.")
    if chat.get("summary") not in (None, "", base, merged):
        raise ValueError("В кабинете уже есть отдельный черновик.")
    return {**chat, "summary": merged, "draft_needs_regeneration": False}
