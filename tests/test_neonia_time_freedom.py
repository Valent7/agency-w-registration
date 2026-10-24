"""Поведение Неонии: доказательства важнее пустых карточек."""
import unittest

from neonia_candidate_policy import (
    TIME_FREEDOM_MISSION,
    assess_time_freedom_fit,
    apply_business_gate,
)


class NeoniaTimeFreedomTests(unittest.TestCase):
    def test_mission(self):
        self.assertIn("освободить", TIME_FREEDOM_MISSION)
        self.assertIn("прогулок", TIME_FREEDOM_MISSION)

    def test_explicit_ai_delegation_matches(self):
        data = {
            "about": "Я хочу передать ИИ все повторяющиеся задачи и переписки, чтобы больше гулять."
        }
        result = assess_time_freedom_fit(data)
        self.assertTrue(result["eligible"])
        self.assertIn("Описание профиля:", result["evidence"][0])

    def test_automation_of_routine_matches(self):
        data = {"status_text": "Ищу автоматизацию повторяющихся задач в работе, чтобы экономить время."}
        result = assess_time_freedom_fit(data)
        self.assertTrue(result["eligible"])
        self.assertIn("Статус VK:", result["evidence"][0])

    def test_empty_profile_cannot_be_recommended(self):
        data = {"telegram_id": 123, "name": "Человек", "username": "someone", "photo_200": None, "phone": None}
        self.assertFalse(assess_time_freedom_fit(data)["eligible"])

    def test_ai_interest_without_need_is_not_enough(self):
        self.assertFalse(assess_time_freedom_fit({"about": "Криптовалюта TON, ИИ, Web3, MLМ."})["eligible"])

    def test_time_need_without_ai_readiness_is_unverified(self):
        self.assertFalse(assess_time_freedom_fit({"about": "У меня мало времени, хочу больше гулять."})["eligible"])

    def test_owner_message_is_not_candidates_interest(self):
        data = {"recent_messages": [
            {"direction": "от владельца", "text": "Я хочу передать ИИ рутину и ежедневные задачи."}
        ]}
        self.assertFalse(assess_time_freedom_fit(data)["eligible"])

    def test_public_chat_message_has_evidence(self):
        data = {"recent_public_messages": [
            {"text": "Нужно автоматизировать ежедневные заявки и переписки, сил уже нет."}
        ]}
        result = assess_time_freedom_fit(data)
        self.assertTrue(result["eligible"])
        self.assertIn("Сообщение в доступном чате", result["evidence"][0])

    def test_model_cannot_invent_evidence(self):
        llm = {"business_relevant": True, "business_evidence": ["Очень хочет ИИ"]}
        score, recommendation, evidence, reason = apply_business_gate(
            llm, 90, pass_recommendation="Передать Неоне",
            reject_recommendation="Нужно больше данных",
            source_context={"username": "empty"},
        )
        self.assertLessEqual(score, 39)
        self.assertEqual(recommendation, "Нужно больше данных")
        self.assertEqual(evidence, [])
        self.assertIn("Недостаточно", reason)

    def test_true_source_signal_works_even_if_model_is_conservative(self):
        llm = {"business_relevant": False, "business_evidence": []}
        score, recommendation, evidence, reason = apply_business_gate(
            llm, 75, pass_recommendation="Решает владелец",
            source_context={"about": "Хочу автоматизировать работу с клиентами и сэкономить время."},
        )
        self.assertEqual(score, 75)
        self.assertEqual(recommendation, "Решает владелец")
        self.assertTrue(evidence)

    def test_no_source_cannot_certify_candidate(self):
        score, recommendation, evidence, reason = apply_business_gate(
            {"business_relevant": True, "business_evidence": ["Некая цитата"]},
            100,
        )
        self.assertEqual(score, 39)
        self.assertFalse(evidence)


if __name__ == "__main__":
    unittest.main()
