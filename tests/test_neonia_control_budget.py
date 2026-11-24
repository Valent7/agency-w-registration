from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from neonia_control_budget import (
    CONTROL_RUN_ID, ControlBudgetExceeded, cloud_vk_assessor,
    request_control_assessment, reserve_control_request,
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

    def test_second_cloud_worker_cannot_send_after_first_reserved_and_timed_out(self):
        first, second = Mock(), Mock()
        count = Mock()
        count.json.return_value = {"input_tokens": 1_000}
        first.post.side_effect = [count, TimeoutError()]
        second.post.return_value = count
        insert = Mock(side_effect=[[{"run_id": CONTROL_RUN_ID}], RuntimeError("duplicate")])
        with self.assertRaises(TimeoutError):
            cloud_vk_assessor(first, 77, "fake", insert, {})("instructions", "prompt")
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            cloud_vk_assessor(second, 77, "fake", insert, {})("instructions", "prompt")
        self.assertEqual(first.post.call_count, 2)
        self.assertEqual(second.post.call_count, 1)
        self.assertEqual(insert.call_args.args[0]["reserved_units"], 291_000)

    def test_over_cap_request_does_not_reserve_or_generate(self):
        session = Mock()
        session.post.return_value.json.return_value = {"input_tokens": 300_000}
        insert = Mock()
        with self.assertRaises(ControlBudgetExceeded):
            cloud_vk_assessor(session, 77, "fake", insert, {})("instructions", "prompt")
        self.assertEqual(session.post.call_count, 1)
        insert.assert_not_called()
