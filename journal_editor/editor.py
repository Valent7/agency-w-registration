#!/usr/bin/env python3
"""Safe editorial pilot for the existing static journal, using only Python stdlib.

Run from the repository root:
  python journal_editor/editor.py --check
  OPENAI_API_KEY=... python journal_editor/editor.py --draft
  JOURNAL_AUTOPUBLISH=1 OPENAI_API_KEY=... python journal_editor/editor.py --publish

This script does NOT schedule itself, deploy, or send messages to readers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "journal-public"
EDITOR = ROOT / "journal_editor"
FEED = SITE / "editorial" / "feed.json"
DRAFTS = EDITOR / "drafts"
TOPICS = EDITOR / "topics.json"
MODEL = os.getenv("JOURNAL_MODEL", "gpt-4.1")
SITE_URL = "https://agency-w-journal.onrender.com"
TIME_URL = "https://agency-w-time.onrender.com/"


class EditorialError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise EditorialError(f"Не найден файл: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def choose_topic() -> dict:
    topics = load_json(TOPICS)["topics"]
    feed = load_json(FEED)
    published = {item["slug"] for item in feed.get("articles", [])}
    drafts = {p.stem for p in DRAFTS.glob("*.json")} if DRAFTS.exists() else set()
    for topic in topics:
        slug = str(topic.get("slug", ""))
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise EditorialError(f"Некорректный slug: {slug!r}")
        if slug not in published and slug not in drafts:
            return topic
    raise EditorialError("Темы из редакционного плана закончились; нужен новый список.")


def ask_editor(topic: dict, api_key: str) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "paragraphs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "summary", "paragraphs"],
        "additionalProperties": False,
    }
    guidance = (
        "Ты главный редактор журнала «Время жить» Агентства W. Пиши по-русски "
        "для людей без технического образования: ясно, глубоко, с образными примерами. "
        "Служение читателю важнее продаж. Уважай его время. "
        "Не выдумывай исследования, статистику, цитаты, клиентов, результаты или ссылки. "
        "Без доходных обещаний, без манипуляций и без навязчивой рекламы. "
        "Любые практические советы должны быть осторожными, проверяемыми и применимыми. "
        "Напиши 6–9 содержательных абзацев, каждый 80–700 символов. "
        "Избегай заголовков-кликбейтов, повторов и рекламных клише. "
        "Не вставляй HTML, Markdown и URL: только текст."
    )
    payload = {
        "model": MODEL,
        "input": [
            {"role": "developer", "content": guidance},
            {"role": "user", "content": (
                f"Напиши оригинальную статью на тему: {topic['question']}. "
                f"Направление: {topic['category']}. "
                "Объясни с конкретной жизненной ситуацией, короткой практикой для читателя "
                "и выводом о ценности времени. Не утверждай, что тема основана на "
                "поисковой статистике: это редакционная гипотеза."
            )},
        ],
        "text": {"format": {"type": "json_schema", "name": "editorial_article",
            "strict": True, "schema": schema}},
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise EditorialError(f"Сбой генерации: {exc}") from exc
    try:
        segments = [
            part["text"]
            for item in result.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        if not segments:
            raise KeyError("output_text")
        return json.loads("".join(segments))
    except (ValueError, KeyError, TypeError) as exc:
        raise EditorialError("Модель не вернула ожидаемый материал в JSON.") from exc


def validate(article: dict) -> None:
    title = article.get("title")
    summary = article.get("summary")
    paragraphs = article.get("paragraphs")
    if not isinstance(title, str) or not 20 <= len(title.strip()) <= 115:
        raise EditorialError("Заголовок требует редактуры.")
    if not isinstance(summary, str) or not 70 <= len(summary.strip()) <= 330:
        raise EditorialError("Описание требует редактуры.")
    if not isinstance(paragraphs, list) or not 6 <= len(paragraphs) <= 10:
        raise EditorialError("Нужно 6–10 содержательных абзацев.")
    if any(not isinstance(p, str) or not 65 <= len(p.strip()) <= 1100 for p in paragraphs):
        raise EditorialError("Абзацы слишком короткие, длинные или повреждены.")
    full = " ".join([title, summary] + paragraphs)
    if len(full) < 1000:
        raise EditorialError("Статья слишком короткая.")
    forbidden = [r"гарантир\w+ доход", r"заработа\w+ \d+", r"100% результат",
                 r"https?://", r"<script", r"\[\s*источник\s*\]"]
    if any(re.search(pattern, full, re.IGNORECASE) for pattern in forbidden):
        raise EditorialError("Обнаружено утверждение, требующее ручной проверки.")
    if len(set(p[:60] for p in paragraphs)) < len(paragraphs):
        raise EditorialError("В статье есть повторяющиеся абзацы.")


def page_html(article: dict, topic: dict, date: str) -> str:
    title = html.escape(article["title"])
    summary = html.escape(article["summary"])
    category = html.escape(topic["category"])
    body = "\n".join("<p>" + html.escape(p) + "</p>" for p in article["paragraphs"])
    slug = topic["slug"]
    canonical = f"{SITE_URL}/editorial/{slug}.html"
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Время жить</title>
<meta name="description" content="{summary}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{summary}">
<style>
:root{{--bg:#061325;--gold:#e3ba64;--light:#f5f1e9;--muted:#b6c4d1}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--light);font:17px/1.8 system-ui,Arial,sans-serif}}
header,footer{{padding:20px max(20px,calc((100vw - 790px)/2));border-bottom:1px solid #284158}}
footer{{border-bottom:0;border-top:1px solid #284158;color:var(--muted);font-size:14px}}
a{{color:var(--gold)}}main{{max-width:790px;margin:55px auto 80px;padding:0 24px}}
h1{{font:normal clamp(35px,5vw,55px)/1.18 Georgia,serif;margin:16px 0 26px}}
.lead{{font-size:20px;color:var(--muted)}}
.meta{{color:var(--gold);font-size:13px;letter-spacing:.08em}}
.article-body p{{margin:26px 0}}
.cta{{margin-top:48px;padding:24px;border:1px solid #796034;background:#0b2337}}
</style></head>
<body>
<header><a href="/">← Журнал «Время жить»</a></header>
<main><div class="meta">{category} · <time datetime="{date}">{date}</time></div>
<h1>{title}</h1><p class="lead">{summary}</p>
<div class="article-body">{body}</div>
<div class="cta"><strong>А куда уходит ваше время?</strong><p>Выпишите три повторяющиеся задачи, которые вы хотели бы делегировать.</p>
<a href="{TIME_URL}">Посчитать время, которое забирает рутина →</a></div>
</main><footer>Агентство W · Мы возвращаем человеку время · <a href="/privacy.html">Статистика и конфиденциальность</a> · <button type="button" data-analytics-settings>Настройки аналитики</button></footer>
<script src="../assets/reader-contact.js" defer></script>
<script src="../assets/metrika-consent.js" defer></script>
</body></html>
"""


def write_sitemap(feed: dict) -> None:
    """List only published, canonical journal pages; exclude referral URLs."""
    pages = [(SITE_URL + "/", None)]
    for entry in feed.get("articles", []):
        url = str(entry.get("url", ""))
        stamp = str(entry.get("date", ""))
        if re.fullmatch(r"editorial/[a-z0-9-]+\.html", url):
            pages.append((SITE_URL + "/" + url,
                          stamp if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp) else None))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, lastmod in pages:
        lines.append("  <url><loc>" + html.escape(url) + "</loc>"
                     + ("<lastmod>" + lastmod + "</lastmod>" if lastmod else "")
                     + "</url>")
    lines.append("</urlset>")
    (SITE / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def publish(article: dict, topic: dict, *, automatic: bool = True) -> Path:
    # The operating policy requires deliberate activation only after a reviewed pilot.
    if automatic and os.getenv("JOURNAL_AUTOPUBLISH") != "1":
        raise EditorialError("Публикация отключена: JOURNAL_AUTOPUBLISH должен быть 1.")
    feed = load_json(FEED)
    slug = topic["slug"]
    if any(x["slug"] == slug for x in feed["articles"]):
        raise EditorialError("Такая статья уже опубликована.")
    stamp = dt.datetime.now(dt.timezone.utc).date().isoformat()
    directory = SITE / "editorial"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (slug + ".html")
    if path.exists():
        raise EditorialError("Файл статьи уже существует, перезапись запрещена.")
    path.write_text(page_html(article, topic, stamp), encoding="utf-8")
    item = {"slug": slug, "title": article["title"], "summary": article["summary"],
            "category": topic["category"], "date": stamp,
            "url": f"editorial/{slug}.html", "origin": "editorial_plan"}
    feed["articles"].insert(0, item)
    FEED.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_sitemap(feed)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description="ИИ-редактор журнала Агентства W")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Проверить план без затрат")
    mode.add_argument("--draft", action="store_true", help="Сохранить материал на проверку")
    mode.add_argument("--publish", action="store_true", help="Автопубликация при включённом флаге")
    mode.add_argument("--approve", metavar="SLUG", help="Утвердить существующий черновик")
    args = p.parse_args()
    try:
        if args.approve:
            slug = args.approve
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                raise EditorialError("Некорректное имя черновика.")
            draft_path = DRAFTS / (slug + ".json")
            draft = load_json(draft_path)
            topic, article = draft["topic"], draft["article"]
            if topic.get("slug") != slug:
                raise EditorialError("Имя черновика не совпадает с темой.")
            validate(article)
            path = publish(article, topic, automatic=False)
            draft_path.unlink()
            print("Утверждённый материал опубликован:", path.relative_to(ROOT))
            return 0
        topic = choose_topic()
        if args.check:
            print("Проверка успешна. Следующая тема:", topic["question"])
            return 0
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise EditorialError("Не задан OPENAI_API_KEY; платные запросы не выполнялись.")
        article = ask_editor(topic, key)
        validate(article)
        if args.publish:
            path = publish(article, topic)
        else:
            DRAFTS.mkdir(parents=True, exist_ok=True)
            path = DRAFTS / (topic["slug"] + ".json")
            path.write_text(json.dumps({"topic": topic, "article": article},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Материал сохранён:", path.relative_to(ROOT))
        return 0
    except (EditorialError, OSError, ValueError, KeyError) as exc:
        print("Редактор остановлен:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
