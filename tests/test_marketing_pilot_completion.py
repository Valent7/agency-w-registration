from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import vk_quality_daily as vk
from journal_assignments import assign_video, assignment_key, receive_video
from journal_drafting import draft_news
from marketing_results import recorded_agent_spending


class PilotCompletionTests(TestCase):
    def test_news_requires_a_matching_primary_page(self):
        item = {"title": "Agency news for a small team", "date": "2026-11-24",
                "url": "https://openai.com/index/example"}
        self.assertIsNone(draft_news([item], lambda _: b'<meta property="og:title" content="Other">'))
        result = draft_news([item], lambda _: b'<meta property="og:title" content="Agency news for a small team">')
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["source"]["url"], item["url"])
        self.assertEqual(len(result["article"]["paragraphs"]), 6)

    def test_editorial_handoff_needs_an_actual_verified_project_asset(self):
        brief = {"format": "Reels", "passport_version": 2, "date": "2026-11-24",
                 "topic": "Пример", "segment": "Партнёры"}
        state = assign_video({}, brief)
        self.assertIs(assign_video(state, brief), state)
        key = assignment_key(brief)
        data = b"synthetic video fixture"
        asset = {"mime_type": "video/mp4", "storage_path": "1/project/job/film.mp4",
                 "filename": "film.mp4", "sha256": sha256(data).hexdigest()}
        store = Mock()
        store.get_job.return_value = {"project_id": "foreign", "state": "ready", "assets": [asset]}
        probe = Mock()
        with self.assertRaises(ValueError):
            receive_video(state, key, 1, "project", "job", store, probe)
        store.read_asset.assert_not_called()
        store.get_job.return_value["project_id"] = "project"
        store.read_asset.return_value = b"invalid"
        with self.assertRaises(ValueError):
            receive_video(state, key, 1, "project", "job", store, probe)
        probe.assert_not_called()
        store.read_asset.return_value = data
        completed = receive_video(state, key, 1, "project", "job", store, probe)
        self.assertEqual(completed["journal_assignments"][key]["status"], "media_received")
        self.assertEqual(state["journal_assignments"][key]["status"], "assigned")
        probe.assert_called_once_with(data)

    def test_costs_show_actuals_and_pending_reservations_separately(self):
        request = Mock(return_value=[{"agent_id": "neonia", "actual_cents": 12, "estimate_cents": 90},
                                     {"agent_id": "neonia", "actual_cents": None, "estimate_cents": 50}])
        store = SimpleNamespace(filters={"owner_id": "eq.1", "project_id": "eq.project"}, request=request)
        result = recorded_agent_spending(store, date(2026, 11, 22), date(2026, 11, 24))
        self.assertEqual(result, {"neonia": {"actual": 12, "reserved": 50, "unsettled": 1}})
        self.assertEqual(request.call_args.kwargs["params"]["project_id"], "eq.project")

    def test_network_evidence_wins_all_five_places_over_higher_second_segment_scores(self):
        assigned = []
        candidates = {n: {"id": n, "source_community_id": 7} for n in range(1, 11)}
        scores = [{"vk_user_id": n, "score": 99 if n <= 5 else 62} for n in candidates]

        def assign(table, payload):
            assigned.append(payload)
            return [payload]

        with patch.multiple(
            vk, load_vk_sources=Mock(return_value=[{"community_id": 7}]),
            _qualified_scores=Mock(return_value=scores), _candidate_rows=Mock(return_value=candidates),
            _vk_recent_activity=Mock(return_value={"active": True, "age_hours": 1}),
            vk_excluded_profession=Mock(return_value=False), _today_raw=Mock(return_value=[]),
            _deterministic_public_work_signals=Mock(side_effect=lambda item: {"network_business_signal": item["id"] > 5}),
            _owner_processed_vk_ids=Mock(return_value=set()), _used_vk_ids=Mock(return_value=set()),
            _partner_vk_ids=Mock(return_value=set()), _sb_post=Mock(side_effect=assign),
            _sb_get=Mock(return_value=[]), load_today_vk_assignments=Mock(side_effect=lambda owner: list(assigned)),
        ):
            result = vk.ensure_quality_vk_assignments(11, "test", passport_version=2, marketing_project_id="project")
        self.assertEqual([item["vk_user_id"] for item in result["assignments"]], [6, 7, 8, 9, 10])
        self.assertEqual((result["network_count"], result["secondary_count"]), (5, 0))

    def test_stagirite_collects_editorial_alerts_only_for_the_pilot(self):
        from datetime import datetime, timezone
        from stagirite_night_control import journal_issues

        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        project = "a6c046ac-6616-4fd8-a840-f9c260200a2b"
        storage = Mock()
        storage._sb_get.return_value = [{"encrypted_state": "fixture"}]
        state = {"schema": 1, "journal_assignments": {
            "fixture": {"status": "assigned", "brief": {"date": "2026-11-22"}},
        }}
        with (
            patch.dict("os.environ", {"AGENCY_MARKETING_PILOT_OWNER": "11",
                                      "AGENCY_MARKETING_PILOT_PROJECT": project}),
            patch("vk_scout_worker._decrypt_workspace_state", return_value=state),
            patch("marketing_journal.latest_editorial_run", return_value={
                "conclusion": "failure", "url": "https://github.com/example/run",
            }) as read,
        ):
            self.assertEqual(journal_issues(12, storage, now), [])
            storage._sb_get.assert_not_called()
            read.assert_not_called()
            issues = journal_issues(11, storage, now)
        self.assertEqual({item["code"] for item in issues},
                         {"journal_video_overdue", "journal_release_failed"})
