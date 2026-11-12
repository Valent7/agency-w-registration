"""Owner-approved team training calendar, effective 12 Nov 2026.

One source of truth for recurring team learning sessions used by the
calendar UI, Theo (VK) and Neona (Telegram). Older generic team-meeting
blocks are retired only when they match the exact legacy label and times.
Personal meetings and other blocks are never filtered or modified.
"""
from datetime import date

START_DAY = date(2026, 11, 12)
DAYS = (0, 2, 4)  # Monday, Wednesday, Friday
START = "19:00"
END = "19:30"
TITLE = "Практическое занятие Агентства W"


def retired_team_block(block):
    """Match only the old standard team calendar blocks, never arbitrary events."""
    try:
        weekday = int(block.get("weekday", -1))
    except (TypeError, ValueError):
        return False
    title = str(block.get("title") or "").strip().casefold()
    start = str(block.get("start_time") or "")[:5]
    end = str(block.get("end_time") or "")[:5]
    return (
        weekday in (0, 1, 2, 3, 4)
        and title == "командная встреча"
        and (start, end) in {("20:00", "21:00"), ("17:00", "18:00")}
    )


def effective_blocks(blocks, day):
    """Return non-mutating effective availability for a specific Moscow day."""
    raw = list(blocks) if isinstance(blocks, (tuple, list)) else []
    if not isinstance(day, date) or day < START_DAY:
        return raw
    result = [x for x in raw if not retired_team_block(x)]
    if day.weekday() in DAYS:
        already = any(
            str(x.get("title") or "").strip() == TITLE
            and str(x.get("start_time") or "")[:5] == START
            and str(x.get("end_time") or "")[:5] == END
            and int(x.get("weekday", -1)) == day.weekday()
            and bool(x.get("active", True))
            for x in result if isinstance(x, dict)
        )
        if not already:
            result.append({
                "id": None,
                "weekday": day.weekday(),
                "title": TITLE,
                "start_time": START + ":00",
                "end_time": END + ":00",
                "active": True,
                "meeting_format": "Онлайн",
                "notes": "Для действующей команды. Пн, ср, пт, 19:00 МСК, 30 минут.",
                "_agency_team_schedule": True,
            })
    return result
