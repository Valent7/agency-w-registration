"""Commercial measures from verified outcomes within a defined reporting period."""
from decimal import Decimal, InvalidOperation


COUNTS = {
    "qualified": "Качественные новые кандидаты",
    "interested": "Заинтересованные люди",
    "productive_dialogues": "Результативные диалоги",
    "meetings_booked": "Назначенные встречи",
    "meetings_held": "Состоявшиеся встречи",
    "new_clients": "Новые клиенты",
    "new_partners": "Новые партнёры",
    "sales": "Продажи",
    "content_conversions": "Целевые действия после контента",
}
AMOUNTS = {
    "revenue": "Выручка",
    "marketing_margin_before_cost": "Валовая прибыль от продаж, отнесённых к маркетингу",
    "agent_cost": "Расходы на ИИ-сотрудников",
    "other_marketing_cost": "Прочие расходы на маркетинг",
    "other_business_cost": "Прочие расходы бизнеса",
    "content_revenue": "Выручка, связанная с контентом",
    "content_cost": "Расходы на контент (уже входят в расходы выше)",
}


def commercial_results(counts, amounts):
    count = {key: _number(counts.get(key), whole=True) for key in COUNTS}
    amount = {key: _number(amounts.get(key)) for key in AMOUNTS}
    marketing = amount["agent_cost"] + amount["other_marketing_cost"]
    profit = amount["revenue"] - marketing - amount["other_business_cost"]
    return {
        "profit": profit,
        "cost_per_customer": _ratio(marketing, count["new_clients"]),
        "marketing_roi_pct": _ratio(100 * (amount["marketing_margin_before_cost"] - marketing), marketing),
        "content_roi_pct": _ratio(100 * (amount["content_revenue"] - amount["content_cost"]),
                                  amount["content_cost"]),
    }


def _number(value, *, whole=False):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Укажите фактическое число для каждого показателя.") from None
    if not number.is_finite() or number < 0 or whole and number != int(number):
        raise ValueError("Проверьте суммы и количества.")
    return number


def _ratio(numerator, denominator):
    return (numerator / denominator).quantize(Decimal("0.01")) if denominator else None


def commercial_proposals(counts, amounts):
    """Translate reported outcomes into reviewable recommendations."""
    results = commercial_results(counts, amounts)
    proposals = []
    if counts["meetings_booked"] > counts["meetings_held"]:
        proposals.append("Сверить назначенные встречи с календарём и выяснить причины несостоявшихся.")
    if counts["qualified"] > 0 and counts["interested"] == 0:
        proposals.append("Проверить, какое предложение получили отобранные люди и на что они ответили.")
    if results["marketing_roi_pct"] is not None and results["marketing_roi_pct"] < 0:
        proposals.append("Проверить расходы и подтверждённую выручку по источникам до расширения бюджета.")
    return proposals


def recorded_agent_spending(store, start, end):
    """Report confirmed costs separately from reservations awaiting settlement."""
    if end < start:
        raise ValueError("Конец периода раньше начала.")
    totals = {}
    offset = 0
    while True:
        rows = store.request("GET", "marketing_spend", params={
            **store.filters, "select": "agent_id,actual_cents,estimate_cents",
            "and": f"(budget_day.gte.{start.isoformat()},budget_day.lte.{end.isoformat()})",
            "order": "budget_day,agent_id,operation_id", "limit": 500, "offset": offset,
        })
        for row in rows:
            entry = totals.setdefault(row["agent_id"], {"actual": 0, "reserved": 0, "unsettled": 0})
            if row["actual_cents"] is None:
                entry["reserved"] += int(row["estimate_cents"])
                entry["unsettled"] += 1
            else:
                entry["actual"] += int(row["actual_cents"])
        if len(rows) < 500:
            return totals
        offset += len(rows)
