#!/usr/bin/env python3
"""Read-only journal state viewer plus an explicit, evidence-based event recorder.

Rules:
- A published status is only confirmed by journal-public/editorial/feed.json.
- An event is evidence of a claimed step, not proof that Render deployed it.
- Recording on disk is local until committed to Git: no automatic server sync.
- No AI API calls, keys, private messages, or new databases are needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "journal-public" / "editorial" / "feed.json"
EVENTS = ROOT / "journal_editor" / "memory" / "events.jsonl"
STAGES = {
    "topic_selected", "draft_created", "editor_checked", "owner_approved",
    "published", "illustration_attached", "site_verified", "blocked"
}
RECORD_SAFETY = re.compile(r"(?i)(api[_ -]?key|secret[_ -]?key|bearer\s+[a-z0-9_-]{10,}|password|парол[ьи]|token\s*[:=])")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class MemoryError(ValueError):
    pass


def read_events(path: Path = EVENTS) -> list[dict]:
    if not path.exists():
        return []
    events = []
    seen = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryError(f"Повреждена запись памяти № {n}") from exc
        if not isinstance(event, dict) or not event.get("id") or event["id"] in seen:
            raise MemoryError(f"Неверный или повторный ID записи № {n}")
        if event.get("stage") not in STAGES or not SLUG.fullmatch(str(event.get("slug", ""))):
            raise MemoryError(f"Некорректный этап или код статьи в записи № {n}")
        if not isinstance(event.get("evidence"), list) or not event["evidence"]:
            raise MemoryError(f"Запись № {n} без подтверждающего источника")
        seen.add(event["id"])
        events.append(event)
    return events


def read_feed(path: Path = FEED) -> list[dict]:
    if not path.exists():
        raise MemoryError("Нет журнала публикаций: статус опубликования не проверить.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("articles"), list):
        raise MemoryError("В feed.json отсутствует массив articles.")
    articles = data["articles"]
    names = [item.get("slug") for item in articles]
    if len(names) != len(set(names)):
        raise MemoryError("Повторяющиеся публикации в feed.json.")
    return articles


def make_state(feed: list[dict], events: list[dict]) -> list[dict]:
    result: dict[str, dict] = {}
    for event in events:
        slug = event["slug"]
        item = result.setdefault(slug, {"slug": slug, "title": "", "events": [], "published": False})
        item["events"].append(event)
    for record in feed:
        slug = str(record.get("slug", ""))
        if not SLUG.fullmatch(slug):
            raise MemoryError(f"Некорректный код публикации: {slug}")
        item = result.setdefault(slug, {"slug": slug, "title": "", "events": [], "published": False})
        item.update(title=record.get("title", ""), category=record.get("category", ""),
                    published=True, public_url=record.get("url", ""),
                    date=record.get("date", ""))
    for item in result.values():
        stages = {e["stage"] for e in item["events"]}
        item["stages"] = sorted(stages)
        if item["published"]:
            item["status"] = "опубликовано (по feed.json)"
            item["next"] = ("Проверить страницу, иллюстрацию и внутренние ссылки в браузере."
                            if "site_verified" not in stages else "Сохранить результаты наблюдения.")
        elif "published" in stages:
            item["status"] = "публикация заявлена, но отсутствует в feed.json — не подтверждено"
            item["next"] = "Проверить GitHub и Render, восстановить запись feed.json после проверки."
        elif "blocked" in stages:
            item["status"] = "есть препятствие"
            item["next"] = "Устранить указанное препятствие и перепроверить."
        elif "owner_approved" in stages:
            item["status"] = "разрешено к публикации, ещё не подтверждено в feed.json"
            item["next"] = "Публикация с проверкой страницы и обновлением feed.json."
        elif "editor_checked" in stages:
            item["status"] = "редактор проверил, ждём решения Директора"
            item["next"] = "Передать DOCX Директору для утверждения."
        elif "draft_created" in stages:
            item["status"] = "черновик записан; наличие файла в другой ветке проверить отдельно"
            item["next"] = "Проверить текст, факты, иллюстрацию и подготовить DOCX."
        else:
            item["status"] = "тема выбрана, создание статьи не подтверждено"
            item["next"] = "Подготовить черновик."
    return sorted(result.values(), key=lambda x: (not x["published"], x["slug"]))


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\wа-яё]+", text.casefold()) if len(w) >= 3]


# Небольшая подсказка русских формулировок. Это НЕ векторный смысловой поиск.
CONCEPTS = [
    ("врем", "час", "минут", "жизн"),
    ("памят", "истори", "прошл", "запомин", "вспомин"),
    ("чернов", "текст", "стать", "журналист"),
    ("публи", "размещ", "выпуск", "сайт"),
    ("картин", "иллюстр", "изображ", "песок"),
    ("проверк", "редактор", "одобрен", "согласован", "утвержд"),
]


def similar(a: str, b: str) -> bool:
    if a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b[:4]) or b.startswith(a[:4]))):
        return True
    return any(any(a.startswith(k) for k in group) and any(b.startswith(k) for k in group)
               for group in CONCEPTS)


def search(query: str, state: list[dict]) -> list[dict]:
    terms = words(query)
    if not terms:
        return []
    scored = []
    for item in state:
        fields = " ".join([
            item.get("title", ""), item["slug"], item.get("status", ""),
            item.get("next", ""),
            *[str(e.get("notes", "")) + " " + str(e.get("stage", "")) for e in item["events"]]
        ])
        haystack = words(fields)
        score = sum(1 for t in terms if any(similar(t, w) for w in haystack))
        if score:
            scored.append((score, item))
    return [item for _, item in sorted(scored, key=lambda x: (-x[0], x[1]["slug"]))]


def append_record(event: dict, path: Path = EVENTS) -> None:
    """Write a checked event locally; committing the modified file is required."""
    if event.get("stage") not in STAGES or not SLUG.fullmatch(str(event.get("slug", ""))):
        raise MemoryError("Некорректный этап или код статьи.")
    required = ["id", "date", "actor", "project", "status", "notes", "evidence"]
    if any(not event.get(k) for k in required):
        raise MemoryError("Не хватает обязательных полей записи.")
    if event["project"] != "journal" or not isinstance(event["evidence"], list):
        raise MemoryError("Недопустимый проект или список подтверждений.")
    if not all(isinstance(x, str) and x.strip() for x in event["evidence"]):
        raise MemoryError("Подтверждения должны быть непустыми строками.")
    if RECORD_SAFETY.search(json.dumps(event, ensure_ascii=False)):
        raise MemoryError("Нельзя сохранять секреты в открытой истории проекта.")
    prior = read_events(path)
    if any(x["id"] == event["id"] for x in prior):
        raise MemoryError("Этот этап уже записан; дублирование запрещено.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [*prior, event]), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Память действий ИИ-редакции Агентства W")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("next")
    find = sub.add_parser("find")
    find.add_argument("query")
    record = sub.add_parser("record")
    record.add_argument("--id", required=True)
    record.add_argument("--date", required=True, help="YYYY-MM-DD")
    record.add_argument("--slug", required=True)
    record.add_argument("--stage", required=True, choices=sorted(STAGES))
    record.add_argument("--actor", required=True)
    record.add_argument("--notes", required=True)
    record.add_argument("--evidence", required=True, action="append")
    args = parser.parse_args()
    try:
        if args.command == "record":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
                raise MemoryError("Нужна дата YYYY-MM-DD.")
            append_record({"id": args.id, "date": args.date, "slug": args.slug,
                           "stage": args.stage, "actor": args.actor,
                           "notes": args.notes, "evidence": args.evidence,
                           "project": "journal", "status": "confirmed_by_record"})
            print("Запись добавлена локально. Нужен commit в Git, иначе другие агенты не увидят её.")
            return 0
        state = make_state(read_feed(), read_events())
        if args.command == "find":
            state = search(args.query, state)
        if args.command == "next":
            state = [{"slug": x["slug"], "next": x["next"]} for x in state]
        elif args.command == "status":
            state = [{"slug": x["slug"], "title": x["title"], "status": x["status"],
                      "next": x["next"]} for x in state]
        else:
            state = [{"slug": x["slug"], "title": x["title"], "status": x["status"],
                      "next": x["next"], "events": len(x["events"])} for x in state]
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, OSError, ValueError, TypeError, KeyError) as exc:
        print("Ошибка памяти:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
