"""Dispatch posts each agent step to Sazed as it happens, in batches, best-effort."""

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path

from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.run_agent import _run
from sazed_worker.dispatch.steps import StepSender, event_steps
from tests.test_dispatch_mirror import Recorder

WT = "/work/wt"


def tool_event(path: str, name: str = "Read") -> dict:
    block = {"type": "tool_use", "name": name, "input": {"file_path": path, "x": 1}}
    return {"type": "assistant", "message": {"content": [block]}}


def emitting(events: list[dict]) -> list[str]:
    code = "import sys,json; [print(json.dumps(e), flush=True) for e in json.loads(sys.argv[1])]"
    return [sys.executable, "-c", code, json.dumps(events)]


class Converting(unittest.TestCase):
    def test_a_tool_use_block_becomes_a_tool_step_with_a_worktree_relative_path(self):
        steps = event_steps(tool_event(f"{WT}/src/a.py"), WT)
        self.assertEqual(
            steps, [{"kind": "tool", "tool": "Read", "text": "src/a.py", "error": False}]
        )

    def test_a_tool_result_becomes_its_first_line_and_carries_is_error(self):
        block = {"type": "tool_result", "content": f"boom in {WT}/a.py\nline two", "is_error": True}
        steps = event_steps({"type": "user", "message": {"content": [block]}}, WT)
        self.assertEqual(
            steps, [{"kind": "result", "tool": None, "text": "boom in a.py", "error": True}]
        )

    def test_a_list_result_and_a_text_block_are_cut_to_160_chars(self):
        result = {"type": "tool_result", "content": [{"type": "text", "text": "r" * 300}]}
        text = {"type": "text", "text": "t" * 300}
        user = event_steps({"type": "user", "message": {"content": [result]}}, WT)
        assistant = event_steps({"type": "assistant", "message": {"content": [text]}}, WT)
        self.assertEqual(user[0]["text"], "r" * 160)
        self.assertEqual(
            assistant, [{"kind": "text", "tool": None, "text": "t" * 160, "error": False}]
        )

    def test_other_events_and_blocks_are_ignored(self):
        self.assertEqual(event_steps({"type": "result", "message": {"content": []}}, WT), [])
        self.assertEqual(event_steps({"type": "system", "subtype": "init"}, WT), [])
        thinking = {"type": "assistant", "message": {"content": [{"type": "thinking"}]}}
        self.assertEqual(event_steps(thinking, WT), [])


class Posting(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mirror = Mirror(f"http://127.0.0.1:{self.server.server_port}/api", "p1", "k1")
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_25_events_post_as_batches_of_10_10_and_5_in_seq_order(self):
        sender = StepSender(self.mirror, "a", WT)
        for i in range(25):
            sender.add(tool_event(f"{WT}/f{i}"))
        sender.close()
        batches = [b for _, _, b in self.server.seen]
        self.assertEqual([len(b["steps"]) for b in batches], [10, 10, 5])
        self.assertEqual([s["seq"] for b in batches for s in b["steps"]], list(range(25)))
        self.assertEqual({(b["plan"], b["task"]) for b in batches}, {("p1", "a")})
        self.assertEqual(self.server.seen[0][:2], ("/api/dispatch/steps", "k1"))
        self.assertEqual(
            set(batches[0]["steps"][0]), {"ts", "seq", "kind", "tool", "text", "error"}
        )

    def test_a_lone_step_posts_within_3_seconds_without_close(self):
        sender = StepSender(self.mirror, "a", WT)
        self.addCleanup(sender.close)
        sender.add(tool_event(f"{WT}/slow"))
        deadline = time.monotonic() + 3
        while not self.server.seen and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([len(b["steps"]) for _, _, b in self.server.seen], [1])

    def test_a_run_through__run_posts_its_steps_and_leaves_the_log_whole(self):
        tmp = Path(tempfile.mkdtemp(prefix="dispatch-steps-"))
        text = {"type": "text", "text": "hi"}
        use = {"type": "tool_use", "name": "Read", "input": {"file_path": f"{tmp}/a.py"}}
        bad = {"type": "tool_result", "content": "bad", "is_error": True}
        events = [
            {"type": "system"},
            {"type": "assistant", "message": {"content": [text, use]}},
            {"type": "user", "message": {"content": [bad]}},
            {"type": "result", "subtype": "success"},
        ]
        log = tmp / "agent.jsonl"
        out = _run(emitting(events), tmp, {}, 30, log, self.mirror, "a")
        self.assertEqual(out["result"]["subtype"], "success")
        self.assertEqual(len(log.read_text().splitlines()), 4)
        steps = [s for _, _, b in self.server.seen for s in b["steps"]]
        self.assertEqual(
            [(s["seq"], s["kind"], s["tool"], s["text"], s["error"]) for s in steps],
            [
                (0, "text", None, "hi", False),
                (1, "tool", "Read", "a.py", False),
                (2, "result", None, "bad", True),
            ],
        )

    def test_with_no_mirror_nothing_is_sent(self):
        tmp = Path(tempfile.mkdtemp(prefix="dispatch-steps-"))
        out = _run(emitting([tool_event("x")]), tmp, {}, 30, tmp / "agent.jsonl")
        sender = StepSender(None, "a", WT)
        sender.add(tool_event("x"))
        sender.close()
        self.assertEqual(out["code"], 0)
        self.assertEqual(self.server.seen, [])


if __name__ == "__main__":
    unittest.main()
