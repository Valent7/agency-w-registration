"""Deterministic zero-cost provider contract for editorial testing."""
from dataclasses import dataclass

from marketing_revid import RevidTestAdapter, RevidTestRequest


@dataclass(frozen=True)
class HeyGenTestRequest:
    script: str
    approved_avatar: str
    duration_seconds: int
    max_credits: int


def preview_video(provider, request, *, quoted_test_credits):
    if provider == "revid" and isinstance(request, RevidTestRequest):
        return RevidTestAdapter().preview(request, quoted_test_credits=quoted_test_credits)
    if provider != "heygen" or not isinstance(request, HeyGenTestRequest):
        raise ValueError("Неизвестный адаптер или несовместимый сценарий.")
    if (not request.script.strip() or not request.approved_avatar.strip()
            or type(request.duration_seconds) is not int or not 1 <= request.duration_seconds <= 180):
        raise ValueError("Нужны утверждённый аватар, сценарий и длительность от 1 до 180 секунд.")
    if any(type(value) is not int or value < 0 for value in (quoted_test_credits, request.max_credits)):
        raise ValueError("Тестовая оценка и лимит должны быть целыми неотрицательными числами.")
    return {"mode": "simulation", "estimated_test_credits": quoted_test_credits,
            "within_budget": quoted_test_credits <= request.max_credits, "charged_credits": 0}
