"""Connect the existing journal feed to the marketing cabinet."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from unicodedata import normalize

import requests


JOURNAL_URL = "https://agency-w-journal.onrender.com/"
FEED_PATH = "editorial/feed.json"
JOURNAL_FORMATS = (
    ("Экспертная статья", "Разобрать одну повторяющуюся задачу"),
    ("Новости искусственного интеллекта", "Проверить первоисточник и дату новости"),
    ("Новости Агентства W", "Сверить объявление с владельцем"),
    ("Аналитический обзор", "Сопоставить подходы на проверяемых примерах"),
    ("История успеха", "Получить согласие и подтверждение результата"),
    ("Видеообзор", "Передать утверждённый сценарий Кадриану"),
    ("Reels", "Подготовить сценарий короткой демонстрации"),
    ("Карусель", "Разложить одну задачу на последовательные шаги"),
    ("Инфографика", "Показать процесс на проверяемых данных"),
    ("Интервью", "Подготовить вопросы и согласовать цитаты"),
    ("Образовательный материал", "Предложить упражнение для читателя"),
    ("Коммерческое предложение", "Согласовать условия и текст с владельцем"),
)


def normalized_title(value):
    return " ".join(re.findall(r"\w+", normalize("NFKC", value).casefold()))


def _get(path, request):
    response = request(JOURNAL_URL + path, timeout=8, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("Материал журнала сейчас недоступен.")
    return response


def read_feed(request=requests.get):
    feed = _get(FEED_PATH, request).json()
    articles = feed.get("articles") if isinstance(feed, dict) else None
    if not isinstance(articles, list):
        raise ValueError("Список выпусков журнала имеет неизвестный формат.")
    entries = {}
    for article in articles:
        if not isinstance(article, dict):
            raise ValueError("Некорректная запись выпуска.")
        slug = article.get("slug")
        title = article.get("title")
        if (not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
                or slug in entries or not isinstance(title, str) or not normalized_title(title)
                or article.get("url") != f"editorial/{slug}.html"):
            raise ValueError("Некорректная или повторная запись выпуска.")
        entries[slug] = article
    return entries


class _PageTitles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_title = ""
        self.has_heading = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.og_title = attributes.get("content") or ""
        if tag == "h1":
            self.has_heading = True


def verify_publication(slug, request=requests.get):
    article = read_feed(request).get(slug)
    if article is None:
        raise ValueError("Материал отсутствует в публичном списке выпусков.")
    page = _get(article["url"], request)
    if "text/html" not in page.headers.get("Content-Type", "").lower():
        raise ValueError("Публичная ссылка не возвращает страницу статьи.")
    titles = _PageTitles()
    titles.feed(page.content.decode("utf-8-sig"))
    if not titles.has_heading or normalized_title(titles.og_title) != normalized_title(article["title"]):
        raise ValueError("Содержимое страницы не совпадает с записью выпуска.")
    return {
        "slug": slug, "title": article["title"], "url": JOURNAL_URL + article["url"],
        "date": article.get("date"), "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def latest_editorial_run(request=requests.get):
    url = ("https://api.github.com/repos/Valent7/agency-w-registration/actions/"
           "workflows/journal-pilot.yml/runs")
    response = request(url, params={"branch": "main", "per_page": 1},
                       timeout=5, allow_redirects=False)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ValueError("Статус редакционного задания недоступен.")
    runs = response.json().get("workflow_runs", [])
    if not runs:
        return None
    entry = runs[0]
    run_id = entry["id"]
    if type(run_id) is not int or run_id < 1:
        raise ValueError("Некорректная запись редакционного задания.")
    return {
        "status": entry["status"], "conclusion": entry.get("conclusion"),
        "url": f"https://github.com/Valent7/agency-w-registration/actions/runs/{run_id}",
    }
