"""Dispatch sends Sazed a heartbeat per running task, and a failed beat never fails the task."""

import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from sazed_worker.dispatch import run_agent
from sazed_worker.dispatch.mirror import Mirror
from tests.test_dispatch_mirror import Recorder


def sleeping(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


class Heartbeats(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mirror = Mirror(f"http://127.0.0.1:{self.server.server_port}/api", "p1", "k1")
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-beat-"))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def beats(self):
        return [(p, k, b) for p, k, b in self.server.seen if p.endswith("/heartbeat")]

    def test_a_running_agent_beats_with_its_plan_and_task_until_it_exits(self):
        """S4-3: beats arrive while the agent runs, and stop once it has exited."""
        with mock.patch.object(run_agent, "HEARTBEAT_S", 0.1):
            out = run_agent._run(
                sleeping(0.7), self.tmp, {}, 30, self.tmp / "a.jsonl", self.mirror, "a"
            )
            after_exit = len(self.beats())
            threading.Event().wait(0.3)
        self.assertEqual(out["code"], 0)
        self.assertGreaterEqual(after_exit, 3)
        self.assertEqual(len(self.beats()), after_exit)
        self.assertEqual(
            set((p, k, b["plan"], b["task"]) for p, k, b in self.beats()),
            {("/api/dispatch/heartbeat", "k1", "p1", "a")},
        )

    def test_beats_stop_once_the_time_cap_passes(self):
        """A run still going past its cap is stuck: silence lets Sazed's reaper end it."""
        stop = threading.Event()
        with mock.patch.object(run_agent, "HEARTBEAT_S", 0.05):
            beater = threading.Thread(target=run_agent._beat, args=(self.mirror, "a", stop, 0))
            beater.start()
            beater.join(timeout=2)
        self.assertFalse(beater.is_alive())
        self.assertEqual(self.beats(), [])

    def test_an_unreachable_sazed_never_fails_the_task(self):
        """S4-8: every beat fails, the run still ends normally and nothing raises."""
        dead = Mirror("http://127.0.0.1:1/api", "p1")
        with mock.patch.object(run_agent, "HEARTBEAT_S", 0.05):
            out = run_agent._run(sleeping(0.4), self.tmp, {}, 30, self.tmp / "b.jsonl", dead, "b")
        self.assertEqual((out["code"], out["timed_out"]), (0, False))
        dead.heartbeat("b")
        self.assertFalse(dead.failed)

    def test_a_beat_still_goes_out_after_a_row_post_failed(self):
        """One blip on a row must not silence the beats and get a healthy task reaped."""
        self.mirror.failed = True
        self.mirror.heartbeat("a")
        self.assertEqual(len(self.beats()), 1)


if __name__ == "__main__":
    unittest.main()
