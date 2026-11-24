"""Prepare a sourced news draft without calling a paid writing service."""
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from journal_research import news_candidates


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _SourceTitle(HTMLParser):
    value = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and fields.get("property") == "og:title":
            self.value = fields.get("content", "")


def _fetch(url):
    opener = build_opener(_NoRedirect())
    for _ in range(3):
        source = urlsplit(url)
        if source.scheme != "https" or source.netloc not in {"openai.com", "www.openai.com"}:
            raise ValueError("Неподдерживаемый источник.")
        request = Request(url, headers={"User-Agent": "Agency W Journal source check"})
        try:
            with opener.open(request, timeout=5) as response:
                if response.status != 200 or response.headers.get_content_type() != "text/html":
                    raise ValueError("Первоисточник недоступен.")
                return response.read(2_000_001)
        except HTTPError as error:
            with error:
                location = error.headers.get("Location")
                if error.code not in {301, 302, 307, 308} or not location:
                    raise
            url = urljoin(url, location)
    raise ValueError("Слишком много переадресаций первоисточника.")


def _words(value):
    return re.findall(r"\w+", value.casefold().removesuffix("| openai"))


def draft_news(candidates, fetch=_fetch):
    for item in candidates[:5]:
        try:
            raw = fetch(item["url"])
            if len(raw) > 2_000_000 or len(item["title"]) > 95:
                continue
            source = _SourceTitle()
            source.feed(raw.decode("utf-8"))
        except (ValueError, UnicodeError, OSError, URLError):
            continue
        if not source.value or _words(source.value) != _words(item["title"]):
            continue
        day = item["date"]
        title = item["title"]
        if re.search(r"https?://|<script|\[\s*источник\s*\]", title, re.IGNORECASE):
            continue
        slug = "openai-" + sha256(item["url"].encode()).hexdigest()[:16]
        article = {
            "title": f"Публикация OpenAI · {title}",
            "summary": "Редакция сверила объявление с первоисточником. Вот как оценить его применимость к задачам небольшой команды и запланировать проверку.",
            "paragraphs": [
                f"В официальной ленте OpenAI {day} появилась публикация «{title}». Заголовок сверён с открытой страницей издателя.",
                "Прежде чем пересказывать объявление коллегам, откройте первоисточник и отметьте, что именно компания сообщила. Выделите отдельной строкой условия, которые касаются вашей команды.",
                "Если в публикации описана новая возможность, найдите её в документации и в своём рабочем аккаунте. Доступность зависит от условий конкретного продукта и аккаунта.",
                "Для первой проверки выберите одну повторяющуюся операцию с понятным итогом. Подготовьте обезличенный пример и решите заранее, как сравните результат с привычным способом.",
                "Зафиксируйте время на подготовку, проверку и исправления отдельно. Итоги сравнения имеет смысл обсуждать после повторной пробы на похожей задаче.",
                "Если первоисточник неполон, запишите открытые вопросы. В редакционном обсуждении различайте сведения из объявления, собственные наблюдения и ожидания от инструмента.",
            ],
        }
        return {
            "topic": {"slug": slug, "category": "Новости искусственного интеллекта"},
            "article": article, "status": "needs_review",
            "source": {"url": item["url"], "date": day, "title": title,
                       "response_sha256": sha256(raw).hexdigest()},
        }
    return None


if __name__ == "__main__":
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    with Path(sys.argv[1]).open("rb") as feed:
        result = draft_news(news_candidates(feed.read(4_000_001), today, sys.argv[2:]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
