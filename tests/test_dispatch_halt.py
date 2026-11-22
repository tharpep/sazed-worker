"""A worker job's stop reaches the agents, the jailed checks and the tasks not started (D7 W-6)."""

import threading
import time
import unittest

from sazed_worker.dispatch import integrate
from sazed_worker.dispatch.halt import Halt
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.unattended import UnattendedWorkspace
from sazed_worker.dispatch.workspace import Workspace
from tests import test_dispatch_engine as engine
from tests.test_dispatch_unattended import Case, needs_jail


def _plan() -> Plan:
    tasks = [
        Task(id="api", goal="g", done="d", owns=["src/api/**"]),
        Task(id="ui", goal="g", done="d", owns=["src/ui/**"]),
    ]
    return Plan(id="p1", tasks=tasks, check=engine.CHECK)


class Halted(unittest.TestCase):
    setUp, tearDown = engine.Engine.setUp, engine.Engine.tearDown
    modes, reason = engine.Engine.modes, engine.Engine.reason

    def stop_when_running(self, halt: Halt, reason) -> None:
        def wait() -> None:
            while self.board.state("api") != "running":
                time.sleep(0.02)
            halt.stop(reason)

        threading.Thread(target=wait, daemon=True).start()

    def test_a_stop_ends_the_running_task_and_starts_no_other(self):
        for reason, state, code in (
            ("timed_out", "timed_out", "timeout"),
            ("cancelled", "failed", "cancelled"),
        ):
            with self.subTest(reason=reason):
                self.modes(api="hang", ui="hang")
                halt = Halt()
                self.stop_when_running(halt, reason)
                started = time.monotonic()
                states = integrate.go(_plan(), self.ws, self.board, "main", {}, 1, halt)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(states, {"api": state, "ui": "not started"})
                self.assertEqual(self.reason("api"), code)
                integrate.clean(_plan(), self.ws, self.board)

    def test_the_plan_limit_in_one_task_stops_the_rest_and_is_kept(self):
        self.modes(api="ratelimit")
        halt = Halt()
        states = integrate.go(_plan(), self.ws, self.board, "main", {}, 1, halt)
        self.assertEqual(states, {"api": "failed", "ui": "not started"})
        self.assertEqual(self.reason("api"), "plan_limit")
        self.assertIn("five hour plan limit", self.board.rows("api")[-1]["line"])
        self.assertEqual(halt.reason, "rate_limit")
        self.assertEqual(halt.rate_limited, {"window": "five_hour", "resets_at": 1794103800})

    def test_a_check_the_stop_killed_is_not_a_failed_check(self):
        halt = Halt()

        class Stopping(Workspace):
            def run_code(self, *args):
                halt.stop("timed_out")
                return -15, ""

        ws = Stopping(self.repo, "p1", self.home)
        states = integrate.go(_plan(), ws, self.board, "main", {}, 1, halt)
        self.assertEqual(states["api"], "timed_out")
        self.assertEqual(self.reason("api"), "timeout")
        self.assertEqual(states["ui"], "done")

    def test_the_first_reason_wins_and_a_late_start_is_stopped_at_once(self):
        halt, seen = Halt(), []
        halt.stop("cancelled")
        halt.stop("timed_out")
        with halt.over(seen.append):
            pass
        self.assertEqual((halt.reason, seen), ("cancelled", ["cancelled"]))
        with self.assertRaises(ValueError):
            halt.stop("budget")


@needs_jail
class JailedCheck(Case):
    def test_a_stop_kills_a_jailed_check(self):
        ws: UnattendedWorkspace = self.ws
        ws.open("main")
        ws.halt = Halt()
        threading.Timer(1.0, ws.halt.stop, ("cancelled",)).start()
        started = time.monotonic()
        code, _ = ws.run_code("sleep 100", ws.integ_dir, 60)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main()
