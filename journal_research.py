"""Select current leads from a primary public source for human fact-checking."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


def news_candidates(raw, today, terms=()):
    if len(raw) > 4_000_000 or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise ValueError("Недопустимый формат новостной ленты.")
    ranked = []
    seen = set()
    keywords = [part.casefold() for part in terms if part.strip()]
    for item in ElementTree.fromstring(raw).findall("./channel/item"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).date()
        except (ValueError, TypeError, OverflowError):
            continue
        link = item.findtext("link", "").strip()
        title = item.findtext("title", "").strip()
        url = urlsplit(link)
        if (not 0 <= (today - published).days <= 14 or link in seen or not title
                or url.scheme != "https" or url.hostname not in {"openai.com", "www.openai.com"}
                or url.username or url.password):
            continue
        seen.add(link)
        matches = sum(term in title.casefold() for term in keywords)
        ranked.append((matches, published, {
            "title": title, "url": link, "date": published.isoformat(), "status": "needs_review",
        }))
    return [item[2] for item in sorted(ranked, key=lambda row: row[:2], reverse=True)[:20]]


if __name__ == "__main__":
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    with Path(sys.argv[1]).open("rb") as feed:
        print(json.dumps(news_candidates(feed.read(4_000_001), today, sys.argv[2:]),
                         ensure_ascii=False, indent=2))
