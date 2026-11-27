"""One scheduled, model-free Neonia cycle confined to the v85 pilot."""
from datetime import datetime, timedelta, timezone
import argparse
import asyncio
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from neonia_delegation_policy import PILOT_OWNER, read_owner_policy
import telegram_scout_worker as telegram

STORY_TASK = "neonia_v85_stories"
BERLIN = ZoneInfo("Europe/Berlin")


def _story_task_id(day):
    return str(uuid5(NAMESPACE_URL, f"agency-w:{PILOT_OWNER}:{STORY_TASK}:{day}"))


def load_daily_stories(owner_id):
    if owner_id != PILOT_OWNER:
        return None
    rows = telegram._sb_get("agency_stagirite_tasks", {
        "id": f"eq.{_story_task_id(datetime.now(BERLIN).date())}",
        "owner_telegram_id": f"eq.{owner_id}", "task_kind": f"eq.{STORY_TASK}",
        "select": "result", "limit": 1,
    })
    return rows[0]["result"] if rows else None


def _run_daily_stories():
    from telegram_stories_ui import (
        _delegation_story_recommendations, _persisted_story_cooldown_authors,
        fetch_telegram_story_feed,
    )

    existing = load_daily_stories(PILOT_OWNER)
    if existing is not None:
        return len(existing.get("recommendations") or [])
    then = datetime.now(timezone.utc) - timedelta(days=30)
    previous = telegram._sb_get("agency_stagirite_tasks", {
        "owner_telegram_id": f"eq.{PILOT_OWNER}", "task_kind": f"eq.{STORY_TASK}",
        "created_at": f"gte.{then.isoformat()}", "select": "result",
    })
    excluded_authors = _persisted_story_cooldown_authors(PILOT_OWNER)
    state, _ = telegram._workspace_state(PILOT_OWNER)
    excluded_authors.update(
        int(event["telegram_id"]) for event in state.get("story_rotation_history", [])
        if isinstance(event, dict) and str(event.get("telegram_id", "")).isdigit()
        and event.get("action") in {"shown", "replied"} and str(event.get("at", "")) >= then.isoformat()
    )
    for row in previous:
        excluded_authors.update(
            int(item["telegram_id"]) for item in (row.get("result") or {}).get("recommendations", [])
        )
    feed = asyncio.run(asyncio.wait_for(fetch_telegram_story_feed(
        PILOT_OWNER, limit=100, public_captions_only=True, excluded_author_ids=excluded_authors,
    ), timeout=120))
    candidates = {}
    for item in _delegation_story_recommendations(feed["stories"]):
        candidates.setdefault(int(item["telegram_id"]), item)
    recommendations = list(candidates.values())[:5]
    for index, item in enumerate(recommendations, 1):
        item["story_index"] = index
    today = datetime.now(BERLIN).date()
    telegram._sb_post("agency_stagirite_tasks", {
        "id": _story_task_id(today), "owner_telegram_id": PILOT_OWNER,
        "assignment": "Ежедневные публичные Telegram Stories Неонии",
        "task_kind": STORY_TASK, "status": "Выполнено",
        "result": {
            "stories": recommendations, "recommendations": recommendations, "target": 5,
            "checked": len(feed["stories"]), "available_people": feed["available_people"],
            "skipped_repeats": feed["skipped_repeats"], "delegation_core_version": 85,
        },
    }, prefer="resolution=ignore-duplicates,return=representation")
    return len(recommendations)


def _run_vk():
    from vk_scout_worker import _sb_get, top_up_vk_owner_now

    matches = _sb_get("agency_members", {
        "telegram_id": f"eq.{PILOT_OWNER}", "select": "member_code", "limit": 2,
    })
    if len(matches) != 1 or not matches[0].get("member_code"):
        raise RuntimeError("Pilot membership cannot be verified")
    result = top_up_vk_owner_now(
        PILOT_OWNER, matches[0]["member_code"], require_delegation=True,
    )
    return result["count"]


def _run_telegram():
    from telegram_stories_ui import _load_session

    result = telegram.process_owner_once(
        PILOT_OWNER, _load_session(PILOT_OWNER), require_delegation=True,
    )
    if result.get("status") in {"storage_error", "qualification_stopped", "skip"}:
        return {"error": result["status"]}
    return result.get("count", 0)


CHANNELS = {"vk": _run_vk, "telegram": _run_telegram, "stories": _run_daily_stories}


def run_once(channel="all"):
    policy = read_owner_policy(PILOT_OWNER)
    if not policy or policy["marketing_passport_version"] != 85:
        raise RuntimeError("No approved v85 for the pilot owner")
    actions = CHANNELS if channel == "all" else {channel: CHANNELS[channel]}
    return {name: _channel(action) for name, action in actions.items()}


def _channel(action):
    try:
        return action()
    except Exception as exc:
        return {"error": type(exc).__name__}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("all", *CHANNELS), default="all")
    report = run_once(parser.parse_args().channel)
    print("NEONIA_V85_DAILY", report, flush=True)
    if any(isinstance(value, dict) for value in report.values()):
        raise SystemExit(1)
