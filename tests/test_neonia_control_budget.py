from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from neonia_control_budget import (
    ControlBudgetExceeded, request_control_assessment, reserve_control_request,
)


class ControlBudgetTests(unittest.TestCase):
    def test_reopening_ledger_does_not_reset_limit(self):
        with TemporaryDirectory() as directory:
            ledger = Path(directory) / "one-approved-run.sqlite"
            self.assertEqual(
                reserve_control_request(ledger, input_tokens=40_000, max_output_tokens=20_000),
                480_000,
            )
            reserve_control_request(ledger, input_tokens=40_000, max_output_tokens=20_000)
            with self.assertRaises(ControlBudgetExceeded):
                reserve_control_request(ledger, input_tokens=40_000, max_output_tokens=20_000)

    def test_concurrent_attempts_cannot_double_spend(self):
        with TemporaryDirectory() as directory:
            ledger = Path(directory) / "one-approved-run.sqlite"

            def attempt(_):
                try:
                    reserve_control_request(ledger, input_tokens=100_000, max_output_tokens=20_000)
                except ControlBudgetExceeded:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(sum(pool.map(attempt, range(4))), 1)

    def test_failed_request_consumes_reservation_and_is_not_retried(self):
        with TemporaryDirectory() as directory:
            ledger = Path(directory) / "one-approved-run.sqlite"
            session = Mock()
            count = Mock()
            count.json.return_value = {"input_tokens": 100_000}
            session.post.side_effect = [count, TimeoutError(), count]
            with self.assertRaises(TimeoutError):
                request_control_assessment(
                    session, ledger, api_key="fake", input_text="test", max_output_tokens=20_000,
                )
            with self.assertRaises(ControlBudgetExceeded):
                request_control_assessment(
                    session, ledger, api_key="fake", input_text="test", max_output_tokens=20_000,
                )
            self.assertEqual(session.post.call_count, 3)
            paid_call = session.post.call_args_list[1].kwargs
            self.assertEqual(paid_call["json"]["service_tier"], "default")
            self.assertNotIn("tools", paid_call["json"])
