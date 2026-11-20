"""Regression tests for a single safe Neona calculator farewell."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import neona_farewell_gift as gift


class FakeResponse:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self.rows


class NeonaFarewellGiftTest(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            supabase_url="https://example.supabase.co",
            supabase_secret_key="unit-test-only",
        )

    def test_soft_refusals_and_questions(self):
        for text in (
            "Нет, спасибо",
            "Нет,   спасибо!",
            "Мне это неинтересно",
            "Спасибо, мне не интересно",
            "Пока не готова",
            "Не хочу встречаться",
            "Это предложение мне не подходит",
        ):
            with self.subTest(text=text):
                self.assertTrue(gift.polite_offer_refusal(text))
        for text in (
            "Хочу узнать подробнее",
            "А если мне не интересно?",
            "У меня нет времени",
            "Нет",
            "Не хочу общаться с ИИ, хочу живого человека",
        ):
            with self.subTest(text=text):
                self.assertFalse(gift.polite_offer_refusal(text, "idle"))
        self.assertTrue(gift.polite_offer_refusal("Нет", "awaiting_meeting_consent"))

    def test_strong_stop_never_sends_gift(self):
        stops = (
            "Не пишите мне больше",
            "Отстаньте",
            "Мне неинтересно с вами общаться",
            "Не хочу общаться с вами",
            "Удалите мой контакт",
            "Прекратите писать",
        )
        for text in stops:
            with self.subTest(text=text):
                self.assertTrue(gift.is_hard_stop(text))
                with patch.object(gift.requests, "get") as get:
                    self.assertIsNone(
                        gift.closing_gift(text, "idle", {}, self.config, 123456)
                    )
                    get.assert_not_called()

    def test_partner_attribution_and_no_repeat(self):
        with patch.object(gift.requests, "get", return_value=FakeResponse(
            [{"telegram_id": 123456, "member_code": "W123456"}]
        )) as get:
            message, context = gift.closing_gift(
                "Нет, спасибо", "idle", {}, self.config, 123456
            )
            self.assertIn("?ref=W123456", message)
            self.assertIn("калькулятор", message.casefold())
            self.assertNotIn("Хотите", message)
            self.assertTrue(context["neona_time_calculator_offered"])
            self.assertTrue(context[gift.GIFT_FLAG])
            self.assertEqual(get.call_count, 1)
            second, context = gift.closing_gift(
                "Нет, спасибо", "idle", context, self.config, 123456
            )
            self.assertNotIn("http", second)
            self.assertTrue(context[gift.GIFT_FLAG])
            self.assertEqual(get.call_count, 1)

    def test_no_unverified_or_wrong_partner_link(self):
        for rows in (
            [],
            [{"telegram_id": 987654, "member_code": "W987654"}],
            [{"telegram_id": 123456, "member_code": "invalid-code"}],
        ):
            with self.subTest(rows=rows), patch.object(
                gift.requests, "get", return_value=FakeResponse(rows)
            ):
                msg, ctx = gift.closing_gift(
                    "Мне это не нужно", "idle", {}, self.config, 123456
                )
                self.assertNotIn("http", msg)
                self.assertTrue(ctx[gift.GIFT_FLAG])

    def test_prior_calculator_offer_is_not_repeated(self):
        with patch.object(gift.requests, "get") as get:
            msg, _ = gift.closing_gift(
                "Нет, спасибо", "idle",
                {"neona_time_calculator_offered": True}, self.config, 123456
            )
            self.assertNotIn("http", msg)
            get.assert_not_called()

    def test_human_preference_is_not_hard_stop(self):
        self.assertFalse(gift.is_hard_stop(
            "Не хочу общаться с ИИ, хочу поговорить с человеком"
        ))


if __name__ == "__main__":
    unittest.main()
