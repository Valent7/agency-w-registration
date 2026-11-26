from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

import neonia_chat_daily_worker as groups
from neonia_delegation_policy import PILOT_OWNER, POLICY_MARKER, approved_for_owner, delegation_proof
import telegram_stories_ui as stories
import vk_quality_daily as quality


TEXTS = [
    "Наш бизнес в MLM. Приглашаем партнёров на вводную встречу.",
    "Я основатель онлайн-школы. Ищу клиентов на регулярные курсы.",
    "Руковожу инвестиционным проектом. Набираю представителей в регионах.",
    "Наш криптопроект проводит открытые демонстрации. Ищем партнёров.",
    "Я наставник. Обучаю сотрудников нашей региональной команды.",
]


def evidence(text, now, url="https://t.me/public_business/42"):
    return {"text": text, "timestamp": int(now.timestamp()), "url": url}


@pytest.mark.parametrize("text", TEXTS)
def test_sector_is_secondary_to_public_operating_process(text):
    now = datetime.now(timezone.utc)
    record = evidence(text, now)
    assert delegation_proof([record], now=now) == record


@pytest.mark.parametrize("text", [
    "Интересуюсь криптовалютой и инвестициями.",
    "Наш бизнес. Не ищем клиентов.",
    "Наш инвестиционный проект. Ищем партнёров. Гарантируем доход.",
    "Спасибо за эфир!",
    "Развиваем новое направление, все подробности в личку.",
])
def test_interest_topic_or_risky_promises_do_not_qualify(text):
    now = datetime.now(timezone.utc)
    assert delegation_proof([evidence(text, now)], now=now) is None


def test_opt_in_requires_new_approved_revision_for_pilot_owner_only():
    approved = {"approved_marketing_core": POLICY_MARKER, "marketing_passport_version": 84}
    assert approved_for_owner(PILOT_OWNER, approved)
    assert not approved_for_owner(PILOT_OWNER + 1, approved)
    assert not approved_for_owner(PILOT_OWNER, {**approved, "marketing_passport_version": 83})
    assert not approved_for_owner(PILOT_OWNER, {"draft": POLICY_MARKER, "marketing_passport_version": 84})


def test_telegram_and_stories_qualify_five_without_provider_calls():
    now = datetime.now(timezone.utc)
    authors = [
        {"telegram_id": uid, "username": f"person{uid}", "last_seen_at": now.isoformat(),
         "activity_precision": "group_message_exact", "professional_messages": [evidence(text, now)]}
        for uid, text in enumerate(TEXTS, 1)
    ]
    storage = Mock()
    storage._classify_workability.return_value = {"work_state": "available"}
    candidates = groups._rank_active_group_authors(
        {"passport": {"analysis": POLICY_MARKER}}, storage, authors + authors, 5, expanded_version=84,
    )
    assert len(candidates) == 5
    assert all(groups._recommended_group_candidate(row, expanded_version=84) for row in candidates)
    assert not groups._recommended_group_candidate(candidates[0], expanded_version=85)
    storage._analyze_candidates.assert_not_called()

    feed = [
        {"telegram_id": uid, "story_id": 1, "date": now.isoformat(),
         "story_url": f"https://t.me/person{uid}/s/1", "caption": text}
        for uid, text in enumerate(TEXTS, 1)
    ]
    feed.insert(0, {**feed[0], "telegram_id": 92, "date": (now - timedelta(hours=49)).isoformat()})
    with patch.object(stories, "read_owner_policy", return_value={"marketing_passport_version": 84}), patch.object(
        stories, "fetch_telegram_story_feed", return_value={"stories": feed}
    ) as fetch, patch.object(stories, "_analyze_story_feed") as paid, patch.object(
        stories, "_story_rotation_exclusions", return_value=(set(), set())
    ), patch.object(stories, "_append_story_rotation_event"), patch.object(
        stories, "persist_workspace_if_changed"
    ), patch.object(stories, "st", Mock(session_state={})):
        result = stories.prepare_telegram_stories_radar(PILOT_OWNER)

    assert result["complete"]
    assert len(result["recommendations"]) == 5
    assert fetch.call_args.kwargs["public_captions_only"] is True
    paid.assert_not_called()


def test_vk_persists_five_qualified_non_network_profiles():
    now = datetime.now(timezone.utc)
    candidates = {
        uid: {"vk_user_id": uid, "source_community_id": 25, "raw_profile": {
            "last_seen": {"time": int(now.timestamp())},
            "public_professional_posts": [evidence(text, now, f"https://vk.com/wall{uid}_3")],
        }}
        for uid, text in enumerate(TEXTS, 1)
    }
    created = []
    def save(table, payload):
        assert payload["owner_telegram_id"] == PILOT_OWNER
        assert payload["assignment_date"] == date.today().isoformat()
        created.append(payload)
        return [payload]

    with patch.multiple(
        quality, load_vk_sources=Mock(return_value=[{"community_id": 25}]),
        _qualified_scores=Mock(return_value=[{"vk_user_id": uid, "score": 72} for uid in candidates]),
        _candidate_rows=Mock(return_value=candidates), _today_raw=Mock(return_value=[]),
        load_today_vk_assignments=Mock(side_effect=lambda _: created),
        _owner_processed_vk_ids=Mock(return_value=set()), _used_vk_ids=Mock(return_value=set()),
        _partner_vk_ids=Mock(return_value=set()), _sb_get=Mock(return_value=[]), _sb_post=Mock(side_effect=save),
    ):
        result = quality.ensure_quality_vk_assignments(
            PILOT_OWNER, "pilot", passport_version=84, marketing_project_id="project", expanded=True,
        )

    assert result["complete"]
    assert [row["daily_position"] for row in created] == [1, 2, 3, 4, 5]
    assert all(row["score"] == 72 for row in created)


def test_staging_preserves_active_core_and_history_and_requires_normal_approval():
    from marketing_audience_chat import approve_marketing_draft
    from neonia_delegation_policy import stage_delegation_draft

    core = {"version": 83, "summary": "Утверждённый текст"}
    previous = {"version": 82, "summary": "Предыдущая версия"}
    state = {"approved": core, "approved_history": [previous], "summary": core["summary"]}
    staged = stage_delegation_draft(state, POLICY_MARKER + "\nНовый критерий.")
    assert staged["approved"] == core
    assert staged["approved_history"] == [previous]
    assert staged["summary"].startswith(core["summary"])
    assert stage_delegation_draft(staged, POLICY_MARKER + "\nНовый критерий.") == staged
    active = approve_marketing_draft(staged, 84)
    assert active["approved_history"] == [previous, core]
    assert active["approved"]["version"] == 85
    with pytest.raises(ValueError, match="отдельный черновик"):
        stage_delegation_draft({**state, "summary": "Редакция владельца"}, POLICY_MARKER)
