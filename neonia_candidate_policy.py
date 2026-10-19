"""Единое правило Неонии: время человека важнее количества лидов.

Оцениваем только доступные слова и действия кандидата. Отсутствие аватара,
телефона, возраста или заполненной анкеты не является отрицательным признаком.
Но имя/username/подписка/членство в чате сами по себе не подтверждают ЦА.
"""

from __future__ import annotations

import re
from typing import Any

TIME_FREEDOM_MISSION = (
    "Главная целевая аудитория Агентства W — люди, которые хотят освободить "
    "время для жизни, семьи, отдыха, творчества или прогулок, передав "
    "повторяющуюся повседневную работу искусственному интеллекту."
)

# Эти выражения — только консервативная проверка ОБЪЯВЛЕННОГО интереса.
# Не выводим желание человека из профессии, фото, подписок или ID.
_INTENT = re.compile(
    r"\b(?:хочу|хотел[аи]?|хотим|ищу|ищем|нужен|нужно|надо|готов[аы]?|"
    r"планир\w*|интересу\w*|мечта\w*|устал\w*|помогите|"
    r"want|looking for|need|ready|suche|möchte|will|brauche)\b",
    re.I,
)
_AI_OR_DELEGATION = re.compile(
    r"(?:\bии\b|\bai\b|нейросет\w*|искусственн\w* интеллект\w*|"
    r"автоматиз\w*|автоматизац\w*|делегир\w*|ai.agent\w*|"
    r"автопилот\w*|ki.assistent\w*|automatisier\w*)",
    re.I,
)
_TIME_OR_ROUTINE = re.compile(
    r"(?:рутин\w*|переписк\w*|повторяющ\w*|однообраз\w*|"
    r"времен\w*|время|час\w*|задач\w*|процесс\w*|"
    r"работ\w*|клиент\w*|заявк\w*|отч[её]т\w*|"
    r"лишн\w* дел\w*|frei(?:e)? zeit|zeit sparen|routine\w*|task\w*)",
    re.I,
)
_DIRECT_ACTION = re.compile(
    r"(?:автоматиз\w*|делегир\w*|переда\w*|освобод\w*|"
    r"избав\w*|возлож\w*|automatisier\w*)",
    re.I,
)

BUSINESS_GATE_RULES = f"""
ГЛАВНЫЙ ФИЛЬТР НЕОНИИ В АГЕНТСТВЕ W:

{TIME_FREEDOM_MISSION}

1. На первом месте — реальная потребность человека сократить рутину
   и ОТКРЫТО ВЫРАЖЕННАЯ готовность рассмотреть ИИ/автоматизацию.
   Профессия, MLM, доход, криптовалюта и количество подписчиков вторичны.
2. Используй только переданные содержательные поля: bio, статус,
   публичные сообщения либо доступные владельцу сообщения.
   Подписка на канал, клик, просмотр PDF/видео, скачивание файла,
   переход по реферальной ссылке НЕ видны автоматически; не заявляй
   об этих действиях без подтверждённых данных источника.
3. Всегда верни КОНКРЕТНУЮ цитату из доступных данных, раскрывающую
   проблему со временем и интерес к передаче рутины ИИ.
4. Если обоснованного признака нет, business_relevant=false,
   business_evidence=[], score не выше 39, не передавай кандидата
   Неоне как рекомендованного. Отдельно обозначь «Недостаточно данных».
5. Аватар и открытый телефон НЕ обязательны. Один лишь факт нахождения
   в бизнес-чате, возраст, пол, фотография или username ничего не доказывают.
6. Лучше 0–3 подтверждённых кандидата, чем 5 случайных.
7. Решение общаться всегда принимает владелец. Не приписывай
   готовность купить, вложить деньги или стать партнёром.
8. У каждого подтверждённого кандидата указывай источник и реальную
   цитату: business_relevant, business_evidence, business_gate_reason.
""".strip()


def _source_statements(source: dict[str, Any]) -> list[tuple[str, str]]:
    """Возвращает только наблюдаемые высказывания, без имён и биометрии."""
    found: list[tuple[str, str]] = []
    for key, label in (
        ("about", "Описание профиля"), ("bio", "Описание профиля"),
        ("description", "Описание"), ("status_text", "Статус VK"),
        ("status", "Статус"), ("activity", "Активность VK"),
        ("public_info", "Публичные сведения"),
    ):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            found.append((label, value.strip()[:1200]))

    for key, label in (
        ("recent_public_messages", "Сообщение в доступном чате"),
        ("recent_messages", "Доступное владельцу сообщение"),
    ):
        value = source.get(key)
        if not isinstance(value, list):
            continue
        for message in value[:8]:
            if isinstance(message, dict):
                # Чужая исходящая речь, а не слова владельца.
                if message.get("direction") == "от владельца":
                    continue
                text = message.get("text") or message.get("message")
            else:
                text = message
            if isinstance(text, str) and text.strip():
                found.append((label, text.strip()[:1200]))
    return found


def assess_time_freedom_fit(source: dict[str, Any]) -> dict[str, Any]:
    """Проверка конкретной потребности. Нет данных -> нет рекомендации.

    Это НЕ оценка характера, доходов или обещание покупки. Все совпадения
    возвращаются с исходной цитатой и типом наблюдаемого источника.
    """
    if not isinstance(source, dict):
        source = {}
    for label, statement in _source_statements(source):
        if (
            _INTENT.search(statement)
            and _AI_OR_DELEGATION.search(statement)
            and _TIME_OR_ROUTINE.search(statement)
        ):
            evidence = f"{label}: «{statement[:240]}»"
            return {
                "eligible": True,
                "evidence": [evidence],
                "reason": "Есть открыто выраженная потребность в экономии времени или автоматизации рутины.",
                "source": label,
            }
        # Например «хочу, чтобы ИИ забрал всю рутину» — короткая прямая формулировка.
        if (
            _DIRECT_ACTION.search(statement)
            and _AI_OR_DELEGATION.search(statement)
            and _TIME_OR_ROUTINE.search(statement)
            and _INTENT.search(statement)
        ):
            evidence = f"{label}: «{statement[:240]}»"
            return {
                "eligible": True, "evidence": [evidence],
                "reason": "Человек прямо говорит о делегировании повседневных задач.",
                "source": label,
            }
    return {
        "eligible": False,
        "evidence": [],
        "reason": "Недостаточно проверяемых данных о потребности делегировать рутину ИИ.",
        "source": "",
    }


def apply_business_gate(
    item: dict[str, Any],
    score: int,
    *,
    pass_recommendation: str | None = None,
    reject_recommendation: str | None = None,
    source_context: dict[str, Any] | None = None,
) -> tuple[int, str | None, list[str], str]:
    """Совместимый API: business_relevant здесь означает пригодность по миссии W.

    При переданном source_context игнорируем неподтверждённые «доказательства»
    от языковой модели и проверяем только исходный текст кандидата.
    """
    try:
        score = max(0, min(100, int(score or 0)))
    except (TypeError, ValueError):
        score = 0

    if source_context is not None:
        result = assess_time_freedom_fit(source_context)
        if not result["eligible"]:
            return min(score, 39), reject_recommendation, [], result["reason"]
        return score, pass_recommendation, result["evidence"], result["reason"]

    # Старый вызов без источника консервативен: отсутствие независимой проверки
    # не разрешает объявлять кандидата подходящим.
    return (
        min(score, 39), reject_recommendation, [],
        "Нет проверенного исходного высказывания кандидата."
    )
