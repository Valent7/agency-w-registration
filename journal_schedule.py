"""Editorial week and approval checks shared by the cabinet and release tooling."""
from datetime import date
from hashlib import sha256
from urllib.parse import urlsplit


WEEK = (
    ("Обзор новостей искусственного интеллекта", "Повышение узнаваемости"),
    ("ИИ для сетевого бизнеса", "Демонстрация экспертности"),
    ("Видеообзор технологии или инструмента", "Знакомство с продуктами"),
    ("Образовательная карусель", "Формирование доверия"),
    ("Новости и возможности Агентства W", "Получение обращения"),
    ("Подтверждённая история успеха или практический кейс", "Формирование доверия"),
    ("Время, деньги и качество жизни", "Переход на калькулятор времени"),
)
DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def calendar_topics(plan, directions):
    if len(directions) != 7 or any(not item.strip() for item in directions):
        raise ValueError("Укажите направление для каждого дня недели.")
    return [
        {**entry, "direction": directions[day], "goal": WEEK[day][1]}
        for entry in plan for day in [date.fromisoformat(entry["date"]).weekday()]
    ]


def reviewed_release(entry, raw, today):
    """Validate a queued, already reviewed artifact. Verification of claims is editorial."""
    scheduled = date.fromisoformat(entry["date"])
    if scheduled != today:
        raise ValueError("Дата выпуска не совпадает с расписанием.")
    if not entry.get("review_reference") or not entry.get("approval_reference"):
        raise ValueError("Нужны подтверждения редактора и владельца.")
    if not entry.get("goal"):
        raise ValueError("Укажите коммерческую цель.")
    if sha256(raw).hexdigest() != entry.get("sha256"):
        raise ValueError("Материал изменился после утверждения.")
    if type(entry.get("cost_cents")) is not int or entry["cost_cents"] != 0:
        raise ValueError("Тестовый выпуск поддерживает только нулевую стоимость.")
    if entry.get("kind") in {"news", "success", "commercial"} and not entry.get("sources"):
        raise ValueError("Нужны источники для проверки утверждений.")
    domains = set()
    for source in entry.get("sources", []):
        url = urlsplit(source["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Укажите публичный HTTPS-источник.")
        domains.add(url.hostname.removeprefix("www."))
        source_date = date.fromisoformat(source["date"])
        if source_date > today or entry.get("kind") == "news" and (today - source_date).days > 14:
            raise ValueError("Дата источника не подходит для выпуска.")
    if entry.get("kind") == "news":
        if (len(domains) < 2 or not str(entry.get("factcheck_reference") or "").strip()
                or entry.get("factcheck_sha256") != entry.get("sha256")):
            raise ValueError("Для новости нужны два источника и отдельная проверка утверждённой версии.")
