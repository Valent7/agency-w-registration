"""Pure, read-only helpers for an Instagram prospecting assistant without Meta login.

No Instagram API calls, scraping, cookies, credentials, automated posting or DMs.
Only profile/post URLs and observations that a human explicitly chooses to work with.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
POST_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "developer", "privacy", "legal", "challenge", "api",
}


def instagram_username(value: str) -> str:
    """Accept an @handle or an Instagram *profile* URL, never an arbitrary URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("https://", "http://")):
        parts = urlsplit(raw)
        if parts.scheme != "https" or parts.hostname not in INSTAGRAM_HOSTS:
            return ""
        segments = [p for p in parts.path.split("/") if p]
        if len(segments) != 1:
            return ""
        raw = segments[0]
    else:
        raw = raw.lstrip("@")
    if not USERNAME_RE.fullmatch(raw) or raw.lower() in RESERVED:
        return ""
    if raw.startswith(".") or raw.endswith(".") or ".." in raw:
        return ""
    return raw


def instagram_profile_url(username: str) -> str:
    normalized = instagram_username(username)
    return f"https://www.instagram.com/{normalized}/" if normalized else ""


def instagram_post_url(value: str) -> str:
    """Validate a human-supplied post/reel link, avoiding arbitrary redirects."""
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    if parts.scheme != "https" or parts.hostname not in INSTAGRAM_HOSTS:
        return ""
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) != 2 or segments[0] not in {"p", "reel", "tv"}:
        return ""
    if not POST_CODE_RE.fullmatch(segments[1]):
        return ""
    return f"https://www.instagram.com/{segments[0]}/{segments[1]}/"


def google_profile_search_url(target_description: str) -> str:
    """A human-operated browser search: no automated Instagram page retrieval."""
    query = str(target_description or "").strip()[:200]
    return "https://www.google.com/search?" + urlencode(
        {"q": f'site:instagram.com/ {query} -inurl:/p/ -inurl:/reel/'}
    )


def suggested_comment_prompt(username: str, observation: str) -> tuple[str, str]:
    """Prepare grounded wording from owner-provided observations, not scraped content."""
    handle = instagram_username(username)
    detail = str(observation or "").strip()[:2000]
    if not handle or len(detail) < 12:
        raise ValueError("Нужны профиль и краткое описание реальной публикации.")
    system = (
        "Ты Неона, редактор осмысленных комментариев. Подготовь ОДИН короткий "
        "комментарий на русском (1–2 предложения, до 260 знаков), который "
        "относится только к описанной человеком публикации. Без выдуманных "
        "фактов, ложной близости, рекламных обещаний, ссылок, приглашения в "
        "бизнес, шаблонной лести и просьб написать в Direct. Если уместно, "
        "задай один естественный вопрос. Возврати только текст комментария. "
        "Не считай, что ты лично видела Instagram-публикацию."
    )
    user = (
        f"Instagram-профиль: @{handle}.\n"
        f"Наблюдение партнёра о конкретной публикации:\n{detail}\n"
        "Предложи уважительный комментарий для ручной отправки владельцем."
    )
    return system, user
