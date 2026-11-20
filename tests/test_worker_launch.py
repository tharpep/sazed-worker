"""Worker jobs after seam 2 keep main's stop reasons, rounds and failure point (S2-7)."""

import unittest
from unittest import mock

from sazed_worker import jobs
from tests.test_worker_launch_pins import FakePopen, WorkerLaunch


class WorkerBehaviour(unittest.TestCase):
    setUp = WorkerLaunch.setUp
    post = WorkerLaunch.post
    run_job = WorkerLaunch.run_job
    job = WorkerLaunch.job

    def test_a_harness_ending_never_becomes_a_job_stop_reason(self):
        endings = (
            {"subtype": "error_during_execution", "stop_reason": "refusal"},
            {"subtype": "error_during_execution", "stop_reason": "max_tokens"},
            {"subtype": "error_max_budget_usd"},
        )
        for ending in endings:
            with self.subTest(ending=ending):
                result = {"type": "result", "is_error": True, "result": "stopped",
                          "session_id": "s1", "num_turns": 1} | ending
                job = self.job(agent="claude", profile="change", prompt="Change it.")
                fake = FakePopen([result])
                out = self.run_job(job, fake)
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual((out["status"], out["error"]), ("failed", "stopped"))
                self.assertEqual((out["meta"]["stop_reason"], out["meta"]["rounds"]), (None, 1))

    def test_a_null_limit_fails_before_the_checkout(self):
        for params in ({"timeout_s": None}, {"max_tool_calls": None}):
            with self.subTest(params=params):
                job = self.job(agent="claude", profile="review", prompt="x", params=params)
                with mock.patch.object(jobs, "prepare_repo") as prepare:
                    with self.assertRaises(TypeError):
                        jobs.run_job(job)
                prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
