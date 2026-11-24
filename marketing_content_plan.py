"""Propose a 7- or 30-day content plan drawn from approved search segments."""
from datetime import timedelta

from marketing_passport import scout_audience


FORMATS = (
    ("Практический разбор", "Попросить читателя привести свой пример"),
    ("Пошаговая памятка", "Предложить сохранить публикацию"),
    ("Вопрос аудитории", "Пригласить поделиться рабочим опытом"),
    ("Короткое видео", "Пригласить посмотреть демонстрацию"),
    ("Ответ на типичный вопрос", "Пригласить задать вопрос"),
)


def create_content_plan(passport, version, start, days, *, channel="studio"):
    if days not in (7, 30):
        raise ValueError("Выберите план на 7 или 30 дней.")
    if channel == "journal":
        from marketing_journal import JOURNAL_FORMATS
        formats = JOURNAL_FORMATS
    elif channel == "studio":
        formats = FORMATS
    else:
        raise ValueError("Неизвестная площадка контента.")
    audience = scout_audience(passport, version)
    if not audience:
        raise ValueError("Утвердите хотя бы один действующий сегмент.")
    topics = [
        (segment["title"], segment["role"], criterion)
        for segment in audience["segments"] for criterion in segment["required_public_evidence"]
    ]
    plan = []
    for index in range(days):
        segment, role, criterion = topics[index % len(topics)]
        content_format, action = formats[index % len(formats)]
        if channel == "journal":
            # A topic is a prompt for editorial verification, not a public claim.
            angle = ("Что проверить", "Практический пример", "Что изменилось")[index // len(formats)]
            criterion = f"{angle} · {criterion}"
        plan.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "segment": segment,
            "role": role,
            "format": content_format,
            "topic": criterion,
            "title": f"{content_format} · {criterion}",
            "call_to_action": action,
            "status": "draft",
            "passport_version": version,
        })
    return plan
