from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests

import neonia_chat_daily_worker as groups
import vk_scout_worker as vk


def test_vk_json_mode_names_json_in_input_and_rejects_incomplete():
    response = Mock(status_code=200)
    response.json.return_value = {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": '{"candidates":[]}',
    }
    with patch.object(vk, "_required_env", return_value="unused"), patch.object(
        vk.requests, "post", return_value=response
    ) as post:
        with pytest.raises(RuntimeError, match="max_output_tokens"):
            vk._ask_openai("Classify candidates", "Candidate data", bounded=True)

    post.assert_called_once()
    payload = post.call_args.kwargs["json"]
    assert "json" in payload["input"].lower()
    assert payload["text"]["format"]["type"] == "json_object"
    assert payload["store"] is False


def test_vk_pilot_stops_paid_batches_on_assignment_failure():
    target = {
        "approved_marketing_core": {"audience": "network"},
        "marketing_passport_version": 2,
        "marketing_project_id": "a7155944-3308-5ead-a160-152c6c74ff62",
    }
    empty = {"count": 0, "complete": False}
    failed = {**empty, "diagnostics": {"insert_errors": 1}}
    with patch.object(vk, "ensure_quality_vk_assignments", side_effect=[empty, failed]), patch.object(
        vk, "score_vk_candidates", return_value={"analyzed": 10}
    ) as score:
        result, analyzed, _ = vk._score_until_daily_five(
            235805138, "pilot", target, daily_limit=5, min_score=50, analysis_limit=30,
        )

    assert result["diagnostics"]["insert_errors"] == 1
    assert analyzed == 10
    score.assert_called_once()
    assert score.call_args.kwargs["limit"] == 10
    assert score.call_args.kwargs["reanalyze_exhausted"] is False


def test_group_stops_on_quota_and_never_submits_second_batch():
    now = datetime.now(timezone.utc)
    authors = [
        {"telegram_id": uid, "username": f"person{uid}",
         "activity_precision": "group_message_exact", "last_seen_at": now.isoformat()}
        for uid in range(1, 22)
    ]
    storage = Mock()
    storage._classify_workability.return_value = {"work_state": "available"}
    storage._split_analysis_value.side_effect = lambda items: (items, [])
    response = requests.Response()
    response.status_code = 429
    storage._analyze_candidates.side_effect = requests.HTTPError(response=response)

    with pytest.raises(groups.TelegramQualificationUnavailable):
        groups._rank_active_group_authors({"passport": {"analysis": {"audience": "MLM"}}},
                                          storage, authors, 5)
    storage._analyze_candidates.assert_called_once()
    assert not groups._eligible_group_author(
        {**authors[0], "last_seen_at": (now - timedelta(hours=49)).isoformat()}, now
    )


def test_story_feed_cannot_charge_twice_for_same_page():
    import telegram_stories_ui as stories

    batch = [
        {"telegram_id": uid, "story_id": 1}
        for uid in range(1, stories.STORY_ANALYSIS_BATCH + 1)
    ]
    with patch.object(stories, "_story_rotation_exclusions", return_value=(set(), set())), patch.object(
        stories, "fetch_telegram_story_feed", return_value={"stories": batch}
    ) as fetch, patch.object(
        stories, "_analyze_story_feed", return_value=[]
    ) as analyze, patch.object(
        stories, "_append_story_rotation_event"
    ), patch.object(
        stories, "persist_workspace_if_changed"
    ), patch.object(stories, "st", Mock(session_state={})):
        result = stories.prepare_telegram_stories_radar(77)

    assert fetch.call_count == 2
    analyze.assert_called_once_with(batch)
    assert not result["complete"]
    assert result["checked"] == len(batch)


def test_vk_terminal_card_releases_only_its_daily_position():
    import vk_quality_daily as quality

    hidden = {"id": 99, "vk_user_id": 123, "status": "skipped", "daily_position": 1}
    with patch.multiple(
        quality,
        load_vk_sources=Mock(return_value=[{"community_id": 27}]),
        _qualified_scores=Mock(return_value=[]),
        _candidate_rows=Mock(return_value={}),
        _today_raw=Mock(side_effect=[[hidden], []]),
        load_today_vk_assignments=Mock(return_value=[]),
        _owner_processed_vk_ids=Mock(return_value=set()),
        _used_vk_ids=Mock(return_value=set()),
        _partner_vk_ids=Mock(return_value=set()),
        _sb_patch=Mock(),
    ):
        quality.ensure_quality_vk_assignments(77, "owner")
        table, filters, update = quality._sb_patch.call_args.args

    assert table == "agency_vk_assignments"
    assert filters == {"id": "eq.99", "owner_telegram_id": "eq.77", "status": "eq.skipped"}
    assert update["daily_position"] is None
    assert "status" not in update


def test_vk_control_preparation_skips_ordinary_paid_route():
    empty = {"count": 0, "complete": False}
    with patch.object(vk, "ensure_quality_vk_assignments", return_value=empty), patch.object(
        vk, "_marketing_vk_analysis"
    ) as factory, patch.object(vk, "score_vk_candidates") as score:
        result, analyzed, _ = vk._score_until_daily_five(
            77, "pilot", {}, daily_limit=5, min_score=50, analysis_limit=30, allow_paid=False,
        )

    assert result == empty
    assert analyzed == 0
    factory.assert_not_called()
    score.assert_not_called()
