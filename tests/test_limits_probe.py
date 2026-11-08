"""The idle worker reads plan limits with a one-turn claude -p only when Sazed says it is due."""

import getpass
import json
import os
import subprocess
import unittest
from unittest import mock

os.environ.setdefault("SAZED_AGENT_USER", getpass.getuser())

from sazed_worker import limits  # noqa: E402

INFO = {"unifiedWindows": {"five_hour": {"utilization": 0.63, "resetsAt": 1794103800}}}
STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init"}',
        "not json",
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
        '{"type":"rate_limit_event","rate_limit_info":' + json.dumps(INFO) + "}",
        '{"type":"result","subtype":"success"}',
    ]
)


class Probe(unittest.TestCase):
    def setUp(self):
        self.posts = []
        patches = [
            mock.patch.object(limits.claude, "available", return_value=True),
            mock.patch.object(limits.claude, "extra_env", return_value={}),
            mock.patch.object(limits, "as_agent", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, due):
        def fake(path, body, timeout=30):
            self.posts.append((path, body))
            if path == "/worker/limits/due":
                return 200, {"due": due, "reason": "window_open" if due else "fresh"}
            return 200, {"added": 2}

        return mock.patch.object(limits, "post", side_effect=fake)

    def test_last_reading_takes_the_newest_event(self):
        self.assertEqual(limits.last_reading(STREAM.splitlines()), INFO)
        self.assertIsNone(limits.last_reading(["{}", "x"]))

    def test_posts_the_reading_when_due(self):
        done = subprocess.CompletedProcess([], 0, STREAM, "")
        with self.post(True), mock.patch.object(limits.subprocess, "run", return_value=done) as run:
            limits.probe()
        self.assertIn("--model=haiku", run.call_args.args[0])
        self.assertEqual(self.posts[-1], ("/worker/limits/reading", {"rate_limit_info": INFO}))

    def test_skips_claude_when_not_due(self):
        with self.post(False), mock.patch.object(limits.subprocess, "run") as run:
            limits.probe()
        run.assert_not_called()
        self.assertEqual([p for p, _ in self.posts], ["/worker/limits/due"])

    def test_a_failed_run_posts_nothing(self):
        boom = subprocess.TimeoutExpired("claude", 90)
        with self.post(True), mock.patch.object(limits.subprocess, "run", side_effect=boom):
            limits.probe()
        self.assertEqual([p for p, _ in self.posts], ["/worker/limits/due"])


if __name__ == "__main__":
    unittest.main()
