from datetime import date
from unittest import TestCase
from unittest.mock import Mock

from marketing_content_plan import create_content_plan
from marketing_journal import JOURNAL_URL, read_feed, verify_publication
from marketing_passport import passport_draft, search_segment


ARTICLE = {
    "slug": "example", "title": "Проверенная статья", "url": "editorial/example.html",
    "date": "2026-11-18",
}


def public_response(*, entry=None, status=200, title=ARTICLE["title"]):
    feed = Mock(status_code=200)
    feed.json.return_value = {"articles": [entry or ARTICLE]}
    page = Mock(
        status_code=status, headers={"Content-Type": "text/html; charset=utf-8"},
        content=f'<meta property="og:title" content="{title}"><h1>{title}</h1>'.encode("utf-8"),
    )
    return Mock(side_effect=[feed, page])


class MarketingJournalTests(TestCase):
    def test_require_matching_live_page_and_feed(self):
        request = public_response()
        result = verify_publication("example", request)
        self.assertEqual(result["url"], JOURNAL_URL + "editorial/example.html")
        self.assertTrue(result["verified_at"])
        self.assertEqual(request.call_count, 2)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        for failed in (public_response(status=302), public_response(status=404),
                       public_response(title="Служебная страница")):
            with self.assertRaises(ValueError):
                verify_publication("example", failed)

    def test_feed_cannot_send_reader_to_another_host(self):
        request = public_response(entry={**ARTICLE, "url": "https://example.com/"})
        with self.assertRaises(ValueError):
            read_feed(request)
        request.assert_called_once()

    def test_editorial_calendar_uses_approved_segments_and_all_required_formats(self):
        segment = search_segment("partner", "Сетевые предприниматели",
                                 "Развивает команду", "", enabled=True)
        passport = passport_draft({"answers": {}}, None, [segment])
        for days in (7, 30):
            plan = create_content_plan(passport, 2, date(2026, 11, 24), days, channel="journal")
            self.assertEqual(len(plan), days)
            self.assertEqual(len({entry["title"] for entry in plan}), days)
            self.assertTrue(all(entry["passport_version"] == 2 and entry["status"] == "draft"
                                and entry["role"] == "partner" for entry in plan))
        self.assertEqual(len({entry["format"] for entry in plan}), 12)

    def test_public_verification_is_saved_in_pilot_state(self):
        from unittest.mock import patch
        from streamlit.testing.v1 import AppTest

        receipt = {**ARTICLE, "url": JOURNAL_URL + ARTICLE["url"],
                   "verified_at": "2026-11-24T12:00:00+00:00"}
        source = """
from types import SimpleNamespace
import streamlit as st
from marketing_journal_ui import render_journal
state = st.session_state.setdefault("working_state", {})
store = SimpleNamespace(save=lambda revision, updated: st.session_state.update(working_state=updated))
render_journal(store, "owner_101_project", 1, state, 2, {})
"""
        with patch("marketing_journal_ui.verify_publication", return_value=receipt) as verify:
            app = AppTest.from_string(source).run()
            next(item for item in app.text_input if item.label.startswith("Идентификатор статьи")).set_value("example")
            next(item for item in app.button if item.label == "Проверить размещение").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["working_state"]["journal_publications"]["example"], receipt)
            verify.assert_called_once_with("example")

    def test_news_and_paid_artifacts_require_correct_gate(self):
        from hashlib import sha256
        from journal_schedule import reviewed_release

        raw = b"reviewed draft"
        entry = {"date": "2026-11-24", "sha256": sha256(raw).hexdigest(), "goal": "Знакомство",
                 "review_reference": "editor", "approval_reference": "owner", "kind": "news",
                 "cost_cents": 0, "sources": [{"url": "https://example.com/news", "date": "2026-11-17"}]}
        today = date(2026, 11, 24)
        reviewed_release(entry, raw, today)
        for invalid in ({**entry, "sources": []}, {**entry, "cost_cents": 1},
                        {**entry, "approval_reference": ""}, {**entry, "date": "2026-11-23"}):
            with self.assertRaises(ValueError):
                reviewed_release(invalid, raw, today)
        with self.assertRaises(ValueError):
            reviewed_release(entry, raw + b" ", today)

    def test_video_provider_simulation_returns_no_charges(self):
        from journal_video_preview import HeyGenTestRequest, preview_video
        from marketing_revid import RevidTestRequest

        heygen = HeyGenTestRequest("Пример сценария", "approved-test-avatar", 30, 10)
        revid = RevidTestRequest("Пример сценария", "9:16", 30, (), None, "", True, False, 10)
        for provider, request in (("heygen", heygen), ("revid", revid)):
            result = preview_video(provider, request, quoted_test_credits=11)
            self.assertFalse(result["within_budget"])
            self.assertEqual(result["charged_credits"], 0)

    def test_public_news_results_are_unverified_leads(self):
        from journal_research import news_candidates

        raw = b'<rss><channel><item><title>New tool</title><link>https://openai.com/index/new-tool</link><pubDate>Mon, 23 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>'
        items = news_candidates(raw, date(2026, 11, 24), ["tool"])
        self.assertEqual(items[0]["status"], "needs_review")
        self.assertEqual(news_candidates(raw, date(2026, 12, 24)), [])

    def test_release_confirmation_retries_and_rejects_different_title(self):
        from journal_delivery import ExpectedArticle, confirm_release

        article = ExpectedArticle("example", "Проверенная статья")
        verify = Mock(side_effect=[ValueError("not live"), {"title": "Старая страница"},
                                   {"title": article.title, "slug": article.slug}])
        wait = Mock()
        receipts, passed = confirm_release([article], verify, wait, attempts=3)
        self.assertTrue(passed)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(receipts[0]["slug"], article.slug)

    def test_no_duplicate_when_release_is_replayed(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from journal_release import run

        with TemporaryDirectory() as directory:
            root = Path(directory)
            feed = root / "journal-public/editorial/feed.json"
            feed.parent.mkdir(parents=True)
            feed.write_text(json.dumps({"articles": [{"slug": "already"}]}))
            with patch("journal_release.subprocess.run") as editor:
                self.assertEqual(run(root, [{"date": "2026-11-24", "slug": "already"}],
                                     date(2026, 11, 24)), 1)
                editor.assert_not_called()

    def test_failed_workflow_status_links_to_verified_repository_run(self):
        from marketing_journal import latest_editorial_run

        response = Mock(status_code=200)
        response.json.return_value = {"workflow_runs": [
            {"id": 123, "status": "completed", "conclusion": "failure", "html_url": "https://invalid.test"}
        ]}
        result = latest_editorial_run(Mock(return_value=response))
        self.assertEqual(result["conclusion"], "failure")
        self.assertEqual(result["url"], "https://github.com/Valent7/agency-w-registration/actions/runs/123")
