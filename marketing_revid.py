"""Offline Revid fixture. No network, API keys, uploads or credit purchases."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RevidTestRequest:
    script: str
    aspect_ratio: str
    duration_seconds: int
    approved_images: tuple[str, ...]
    approved_voice: str | None
    style: str
    captions: bool
    music: bool
    max_credits: int


class RevidTestAdapter:
    def preview(self, request: RevidTestRequest, *, quoted_test_credits: int):
        if not request.script.strip() or request.aspect_ratio not in {"9:16", "16:9", "1:1"}:
            raise ValueError("Укажите сценарий и поддерживаемый формат ролика.")
        if type(request.duration_seconds) is not int or request.duration_seconds <= 0:
            raise ValueError("Укажите длительность ролика.")
        if any(type(value) is not int or value < 0
               for value in (quoted_test_credits, request.max_credits)):
            raise ValueError("Укажите тестовую оценку и лимит целыми числами.")
        return {
            "mode": "simulation",
            "estimated_test_credits": quoted_test_credits,
            "within_budget": quoted_test_credits <= request.max_credits,
            "charged_credits": 0,
        }
