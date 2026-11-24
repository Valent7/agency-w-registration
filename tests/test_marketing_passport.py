from datetime import date
from decimal import Decimal
from unittest import TestCase

from marketing_content_plan import create_content_plan
from marketing_passport import matched_segment, passport_draft, scout_audience, search_segment
from marketing_results import AMOUNTS, COUNTS, commercial_results
from marketing_revid import RevidTestAdapter, RevidTestRequest


class MarketingPassportTests(TestCase):
    def test_approved_projection_excludes_interview_and_original_snapshot(self):
        legacy = {"profile": {"portrait": "Сохранённый портрет", "notes": ["личное"]}}
        enabled = search_segment("partner", "Сетевые предприниматели", "Ищет партнёров\nВедёт команду",
                                 "Разработка игр", enabled=True)
        disabled = search_segment("client", "", "", "", enabled=False)
        passport = passport_draft({"answers": {"personality-10": "Личные ценности"}}, legacy,
                                  [enabled, disabled])
        legacy["profile"].clear()
        self.assertEqual(passport["audience"]["legacy_profile"]["profile"]["portrait"],
                         "Сохранённый портрет")
        audience = scout_audience(passport, 3)
        self.assertEqual(audience["marketing_passport_version"], 3)
        self.assertEqual(audience["segments"][0]["role"], "partner")
        self.assertEqual(len(audience["segments"]), 1)
        self.assertNotIn("Личные ценности", str(audience))
        self.assertNotIn("Сохранённый портрет", str(audience))
        self.assertIsNone(passport["expertise"]["offer"])

        for days in (7, 30):
            plan = create_content_plan(passport, 3, date(2026, 11, 24), days)
            self.assertEqual(len(plan), days)
            self.assertEqual(len({item["date"] for item in plan}), days)
            self.assertTrue(all(item["role"] == "partner" and item["passport_version"] == 3
                                for item in plan))
        with self.assertRaises(ValueError):
            search_segment("client", "Клиенты", "", "", enabled=True)

    def test_financial_calculations_use_confirmed_amounts(self):
        counts = dict.fromkeys(COUNTS, 0)
        amounts = dict.fromkeys(AMOUNTS, 0)
        amounts.update(revenue=100, agent_cost=15, other_marketing_cost=5,
                       other_business_cost=10, marketing_margin_before_cost=60)
        self.assertIsNone(commercial_results(counts, amounts)["cost_per_customer"])
        counts["new_clients"] = 4
        result = commercial_results(counts, amounts)
        self.assertEqual(result["profit"], Decimal("70"))
        self.assertEqual(result["cost_per_customer"], Decimal("5.00"))
        self.assertEqual(result["marketing_roi_pct"], Decimal("200.00"))

    def test_revid_quote_is_a_fixture_with_zero_spend(self):
        request = RevidTestRequest("Синтетический сценарий", "9:16", 30, (), None, "", True, False, 10)
        adapter = RevidTestAdapter()
        self.assertFalse(adapter.preview(request, quoted_test_credits=11)["within_budget"])
        self.assertTrue(adapter.preview(request, quoted_test_credits=10)["within_budget"])
        self.assertEqual(adapter.preview(request, quoted_test_credits=10)["charged_credits"], 0)


    def test_evidence_must_come_from_the_public_profile_and_exclusions_win(self):
        segment = search_segment("partner", "Сетевики", "Ведёт команду", "Разработка игр", enabled=True)
        analysis = {"marketing_segment_index": 0, "marketing_evidence": ["Развиваю команду"]}
        public = {"career": [{"position": "Развиваю команду в прямых продажах"}]}
        self.assertEqual(matched_segment([segment], analysis, public), segment)
        self.assertIsNone(matched_segment([segment], analysis, {"status": "Занят"}))
        self.assertIsNone(matched_segment([segment], analysis, {**public, "occupation": "Разработка игр"}))

    def test_vk_queries_scope_existing_scores_to_owner_project_and_version(self):
        from unittest.mock import patch
        from vk_quality_daily import _qualified_scores

        project = "00000000-0000-0000-0000-000000000001"
        with patch("vk_quality_daily._vk_owner_pages", return_value=[]) as fetch:
            _qualified_scores(101, 50, 2, project)
        table, owner_id, filters = fetch.call_args.args
        self.assertEqual((table, owner_id), ("agency_vk_candidate_scores", 101))
        self.assertEqual(filters["target_profile_snapshot->>marketing_project_id"], f"eq.{project}")
        self.assertEqual(filters["target_profile_snapshot->>marketing_passport_version"], "eq.2")
        self.assertEqual(filters["target_profile_snapshot->>marketing_segment_matched"], "eq.true")
        with self.assertRaises(ValueError):
            _qualified_scores(101, 50, 2)

    def test_five_secondary_candidates_do_not_end_the_pilot_search(self):
        from vk_scout_worker import _needs_more_candidates

        complete = {"complete": True, "network_count": 2}
        self.assertTrue(_needs_more_candidates(complete, {"marketing_passport_version": 1}, 5))
        self.assertFalse(_needs_more_candidates({**complete, "network_count": 5},
                                                {"marketing_passport_version": 1}, 5))
        self.assertFalse(_needs_more_candidates(complete, {}, 5))
