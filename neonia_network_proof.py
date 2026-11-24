"""Conservative admission for owners with an explicitly approved network-only audience."""
from datetime import datetime, timedelta, timezone
import os
import re


NETWORK = re.compile(r"\b(?:работаю|занимаюсь|развиваю|веду)\b.{0,40}\b(?:mlm|млм|сетев\w+\s+(?:маркетинг\w*|бизнес\w*|компани\w*))\b", re.I)
PRODUCT = re.compile(r"\b(?:продвигаю|продаю|рекомендую|предлагаю|консультирую)\b.{0,70}\b(?:продукт\w*|продукци\w*|товар\w*|услуг\w*)", re.I)
DEVELOP = re.compile(r"\b(?:развиваю|строю|набираю|собираю|ищу|привлекаю|приглашаю)\b.{0,55}\b(?:команд\w*|клиент\w*|партн[её]р\w*)", re.I)
PAST = re.compile(r"\b(?:раньше|бывш\w*|перестал\w*|уш[её]л\w*|ушла|больше не|не занимаюсь|не работаю|никогда)\b", re.I)
NETWORK_TOPIC = re.compile(r"\b(?:mlm|млм|сетев\w+\s+(?:маркетинг\w*|бизнес\w*|компани\w*))\b", re.I)
LINK = re.compile(r"https://(?:vk\.com/wall-\d+_\d+\?reply=\d+|t\.me/[a-zA-Z0-9_]+/\d+)")


def network_only(owner_id):
    return str(owner_id) in {part.strip() for part in os.getenv("NEONIA_NETWORK_ONLY_OWNERS", "").split(",")}


def current_network_proof(records, now=None):
    now = now or datetime.now(timezone.utc)
    ordered = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        try:
            when = datetime.fromtimestamp(int(item["timestamp"]), timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if timedelta(0) <= now - when <= timedelta(days=30):
            ordered.append((when, item))
    for _, item in sorted(ordered, key=lambda pair: pair[0], reverse=True):
        text = str(item.get("text") or "")
        if not NETWORK_TOPIC.search(text) or not LINK.fullmatch(str(item.get("url") or "")):
            continue
        if PAST.search(text):
            return None
        if NETWORK.search(text) and PRODUCT.search(text) and DEVELOP.search(text):
            return item
    return None


def vk_network_proof(candidate):
    raw = candidate.get("raw_profile")
    if not isinstance(raw, dict):
        return None
    return current_network_proof(raw.get("public_professional_comments"))
