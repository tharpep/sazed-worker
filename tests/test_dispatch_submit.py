"""submit: what it refuses, the body it posts, and the plan hash both repos pin (D7 W-1, W-2)."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sazed_worker.dispatch import admission, mcp
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.plan import Plan, Task
from tests import registry

KEY = "console-key-for-tests"
JOB = "5f0c7e1a-0000-4000-8000-000000000001"
PLAN = {
    "id": "p1",
    "check": "true",
    "tasks": [
        {"id": "api", "goal": "g", "done": "d", "owns": ["src/api/**"], "budget_usd": 1.1},
        {"id": "ui", "goal": "g", "done": "d", "owns": ["src/ui/**"], "budget_usd": 2.2},
    ],
}


class HashPin(unittest.TestCase):
    def test_the_hash_is_the_one_sazed_pins(self):
        """The literal in sazed's S-3 test, so neither repo can change the expression alone."""
        plan = {
            "id": "hash-pin",
            "tasks": [{"id": "a", "goal": "g", "done": "d", "owns": ["x.py"], "check": "true"}],
        }
        self.assertEqual(
            admission.plan_hash(plan, {}),
            "927a43a75f48debbf6b4815b47e4c543abc37e92239d4a4a595d80d4dca9ae2e",
        )

    def test_key_order_never_matters_and_every_value_does(self):
        self.assertEqual(admission.plan_hash({"a": 1, "b": 2}, {}),
                         admission.plan_hash({"b": 2, "a": 1}, {}))
        self.assertNotEqual(admission.plan_hash({"a": 1}, {}), admission.plan_hash({"a": 1.0}, {}))
        self.assertNotEqual(admission.plan_hash({}, {}), admission.plan_hash({}, {"check": "true"}))


class Names(unittest.TestCase):
    def test_ids_and_interface_files_that_could_leave_their_folder_are_refused(self):
        def plan(pid="p1", tid="a", files=None):
            return Plan(id=pid, tasks=[Task(id=tid, goal="g", done="d")],
                        interface_files=files or {})

        self.assertIsNone(admission.name_refusal(plan(files={"src/types.py": ""})))
        for bad in ("../x", "a/b", "..", ".", "", "-rf", ".git", "a b", "a\n", "x.lock", 7):
            with self.subTest(bad=bad):
                self.assertIn("plan id", admission.name_refusal(plan(pid=bad)))
                self.assertIn("task id", admission.name_refusal(plan(tid=bad)))
        for bad in ("/etc/x", "../x", "a/../../x", ".git/config", "sub/.GIT/hooks/x", ""):
            with self.subTest(bad=bad):
                self.assertIn("interface file", admission.name_refusal(plan(files={bad: ""})))


class Sazed(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply(200, {"/agents/profiles": registry.ROWS}.get(self.path, []))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, self.headers.get("X-API-Key"), body))
        status, answer = self.server.answer or (
            201,
            {"job_id": JOB, "plan": body["plan"]["id"], "status": "needs_approval",
             "plan_hash": admission.plan_hash(body["plan"], body["config"])},
        )  # fmt: skip
        self.reply(status, answer)

    def reply(self, status, answer):
        raw = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class Submit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="submit-"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Sazed)
        self.server.seen, self.server.answer = [], None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.dict(os.environ, {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_SAZED_URL": f"http://127.0.0.1:{self.server.server_port}/",
            "DISPATCH_SAZED_KEY": KEY,
        })  # fmt: skip
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(registry.install)
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))

    def submit(self, *argv, plan=PLAN, config=None) -> tuple[int, str]:
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(json.dumps(plan))
        args = ["submit", str(plan_file), "--repo-name", "o/r", "--title", "The PR", *argv]
        if config is not None:
            (self.tmp / "ws.toml").write_text(config)
            args += ["--config", str(self.tmp / "ws.toml")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(args)
        return code, out.getvalue()

    def refused(self, *argv, says: str, **kwargs) -> None:
        code, out = self.submit(*argv, **kwargs)
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("refused: "), out)
        self.assertIn(says, out)

    def test_it_posts_the_contract_body_and_says_approve_it(self):
        code, out = self.submit(config='check = "make check"\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.server.seen, [(
            "/dispatch/plans/p1/submit", KEY,
            {"plan": PLAN, "config": {"check": "make check"}, "repo": "o/r", "base": "main",
             "budget_usd": 5.0, "task_caps": {"api": 1.1, "ui": 2.2}, "timeout_s": 7200,
             "parallel": 1, "title": "The PR", "body": None, "board_card": None},
        )])  # fmt: skip
        self.assertIn(f"job {JOB} waits for approval", out)
        self.assertIn("approves it in the Sazed console", out)

    def test_every_flag_reaches_the_body(self):
        code, out = self.submit("--base", "dev", "--budget", "9", "--timeout", "900",
                                "--parallel", "2", "--body", "why", "--card", "item:1")
        self.assertEqual(code, 0, out)
        body = self.server.seen[0][2]
        self.assertEqual(
            [body[k] for k in ("base", "budget_usd", "timeout_s", "parallel", "body",
                               "board_card")],
            ["dev", 9.0, 900, 2, "why", "item:1"],
        )  # fmt: skip

    def test_caps_that_meet_the_budget_are_accepted_and_over_it_refused(self):
        self.assertEqual(self.submit("--budget", "3.3")[0], 0)
        self.server.seen.clear()
        self.refused("--budget", "3.29", says="add up to $3.30, over the $3.29 budget")
        self.assertEqual(self.server.seen, [])

    def task(self, **fields) -> dict:
        return PLAN | {"tasks": [PLAN["tasks"][0] | fields]}

    def test_nothing_is_posted_for_a_plan_that_could_not_run_unattended(self):
        no_check = {"id": "p1", "tasks": [{"id": "r", "goal": "g", "done": "d",
                                           "profile": "sweep"}]}
        self.refused(plan=PLAN | {"tasks": []}, says="the plan has no tasks")
        self.refused(plan=self.task(profile="flash"), says="runs on agy")
        self.refused(plan=no_check, says="has no check")
        self.refused(plan=PLAN | {"regen": "make gen"}, says="names no regen_outputs")
        self.refused(plan=self.task(budget_usd=None), says="task api has no budget_usd")
        self.refused(plan=self.task(id="../api"), says="task id '../api'")
        self.refused(plan=PLAN | {"interface_files": {"../x": ""}}, says="interface file")
        self.refused(config="day = 2026-11-22\n", says="JSON can't carry")
        self.refused(config="check = nan\n", says="JSON can't carry")
        self.refused("--title", "t" * 257, says="the title is over 256")
        self.refused("--config", str(self.tmp / "absent.toml"), says="there is no config")
        self.assertEqual(self.server.seen, [])

    def test_cached_profiles_are_refused(self):
        self.submit()
        self.server.seen.clear()
        with mock.patch.dict(os.environ, {"DISPATCH_SAZED_URL": "http://127.0.0.1:9"}):
            self.refused(says="these came from the cache")
        self.assertEqual(self.server.seen, [])

    def test_sazed_s_own_refusal_is_shown(self):
        self.server.answer = (409, {"detail": "plan 'p1' already has a dispatch job"})
        self.refused(says="Sazed answered 409: plan 'p1' already has a dispatch job")

    def test_another_hash_from_sazed_is_not_offered_for_approval(self):
        self.server.answer = (201, {"job_id": JOB, "plan_hash": "0" * 64})
        self.refused(says=f"job {JOB} holds another plan hash than this plan's. Cancel it.")


if __name__ == "__main__":
    unittest.main()
