from unittest import TestCase
from unittest.mock import Mock

from marketing_budget import guarded_request


class MarketingBudgetTests(TestCase):
    def test_duplicate_reservation_does_not_call_paid_provider_again(self):
        reserve = Mock(side_effect=["reserved", "already_reserved", "limit_reached"])
        provider = Mock(return_value="synthetic result")
        request = guarded_request(reserve, provider, owner_id=1, project_id="test", agent="neonia",
                                  ceiling_cents=4, model="test-model")
        self.assertEqual(request("system", "first"), "synthetic result")
        with self.assertRaises(RuntimeError):
            request("system", "first")
        with self.assertRaises(RuntimeError):
            request("system", "second")
        self.assertEqual(reserve.call_args_list[0], reserve.call_args_list[1])
        provider.assert_called_once_with("system", "first")
