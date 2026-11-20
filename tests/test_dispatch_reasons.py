"""Every red board row carries a reason from the one closed list (seam 3, S3-1 and S3-2)."""

import tempfile
import unittest
from pathlib import Path

from sazed_worker.dispatch import run_agent
from sazed_worker.dispatch.board import STATES, Board
from sazed_worker.dispatch.plan import Task
from sazed_worker.dispatch.reasons import NEEDS_REASON, REASONS
from sazed_worker.dispatch.run_agent import task_spec


class NoCommits:
    def commits(self, task_id: str) -> int:
        return 0


def outcome(stop_reason=None, result=None, **fields) -> dict:
    return {
        "code": 0,
        "stop_reason": stop_reason,
        "rate_limited": None,
        "result": result,
        "evidence": 0,
        "stderr": "",
    } | fields


def answered(state: str | None, **structured) -> dict:
    body = {"summary": "s"} | structured
    return {"structured_output": {"state": state} | body if state else body}


class Rows(unittest.TestCase):
    def setUp(self):
        self.board = Board(Path(tempfile.mkdtemp(prefix="seam3-reasons-")))

    def test_a_red_row_without_a_reason_is_refused(self):
        self.assertEqual(NEEDS_REASON, {"failed", "blocked", "conflict", "timed_out"})
        for state in NEEDS_REASON:
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "needs a reason"):
                self.board.write("t", state, "line")
        self.assertEqual(self.board.rows("t"), [])

    def test_a_reason_outside_the_list_is_refused(self):
        for reason in ("no_heartbeat", "timed out", ""):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "unknown reason"):
                self.board.write("t", "failed", "line", reason=reason)

    def test_the_reason_lands_in_meta_and_other_rows_carry_none(self):
        row = self.board.write("t", "failed", "line", reason="budget", cost_usd=0.1)
        self.assertEqual(row["meta"], {"reason": "budget"})
        self.assertNotIn("reason", self.board.write("t", "landed", "l", reason=None)["meta"])
        self.assertIn("timed_out", STATES)


class Finish(unittest.TestCase):
    def test_each_failure_keeps_its_line_and_gets_its_code(self):
        """S3-2: one case per way _finish fails a task, with today's sentence."""
        board = Board(Path(tempfile.mkdtemp(prefix="seam3-finish-")))
        task = Task(id="t", goal="g", done="d", owns=["src/**"])
        cfg = {"model": "m", "timeout_s": 5, "max_tool_calls": 7}
        spec = task_spec(cfg, Path("/tmp"), "p", Path("/tmp/s"), False)
        limited = {"window": "five_hour", "resets_at": None}
        cases = [
            (outcome("timed_out"), "timed_out", "timed out after 5 s", "timeout"),
            (outcome("tool_cap"), "failed", "hit the tool cap of 7 calls", "tool_cap"),
            (outcome("budget"), "failed", "went over its budget", "budget"),
            (outcome("rate_limit", rate_limited=limited), "failed",
             "hit Pryce's five hour plan limit", "plan_limit"),
            (outcome("refusal"), "failed", "the model refused to continue", "refused"),
            (outcome("max_tokens"), "failed", "stopped at the model's output token limit",
             "max_tokens"),
            (outcome("shutdown"), "failed", "stopped because dispatch was shutting down",
             "cancelled"),
            (outcome("cancelled"), "failed", "cancelled", "cancelled"),
            (outcome(code=1), "failed", "no result (exit 1): no output", "no_result"),
            (outcome(result=answered(None)), "failed",
             "finished without a done or waiting result", "no_done_state"),
            (outcome(result=answered("done")), "failed", "said done but made no commits: s",
             "no_change"),
        ]
        for out, state, line, reason in cases:
            with self.subTest(reason=reason, line=line):
                row = run_agent._finish(task, NoCommits(), board, out, spec, False)
                self.assertEqual((row["state"], row["line"], row["meta"]["reason"]),
                                 (state, line, reason))
        row = run_agent._finish(task, NoCommits(), board, outcome(result=answered("done")),
                                spec, True, evidence_required=True)
        self.assertEqual((row["line"], row["meta"]["reason"]), ("no evidence read", "no_evidence"))

    def test_the_list_holds_the_worker_codes_and_not_the_reapers(self):
        self.assertEqual(len(REASONS), 18)
        self.assertNotIn("no_heartbeat", REASONS)


if __name__ == "__main__":
    unittest.main()
