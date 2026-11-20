"""Dispatch through the adapters: the tool cap, Sazed down, a slow sink, the unattended spec."""

import dataclasses
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sazed_worker.adapters import claude
from sazed_worker.agent_process import as_agent
from sazed_worker.dispatch import integrate, run_agent
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, PlanError, Task, resolve
from sazed_worker.dispatch.run_agent import run_task, task_spec
from sazed_worker.dispatch.workspace import Workspace, git
from sazed_worker.run import launch_args
from tests import registry
from tests.test_dispatch_steps import spec_for

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"


class SlowMirror(Mirror):
    def __init__(self):
        super().__init__("http://127.0.0.1:1", "p1")
        self.readings = 0

    def reading(self, info):
        time.sleep(1)
        self.readings += 1

    def steps(self, task, steps):
        time.sleep(1)

    def heartbeat(self, task):
        time.sleep(1)


class CountingMirror(Mirror):
    def __init__(self):
        super().__init__("http://127.0.0.1:1", "p1")
        self.beats = 0

    def heartbeat(self, task):
        self.beats += 1


class DispatchLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seam2-dispatch-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
        ):
            git(*args, cwd=self.repo)
        (self.repo / "README.md").write_text("# repo\n")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "seed", cwd=self.repo)
        env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": f"{sys.executable} {FAKE}",
            "DISPATCH_FAKE_MODES": json.dumps({"t": "tools"}),
        }
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(self.restore, old)
        registry.install()
        self.home = self.tmp / "home" / "p1"
        self.ws = Workspace(self.repo, "p1", self.home)

    def restore(self, old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_one(self, overrides: dict, mirror: Mirror | None = None) -> dict:
        task = Task(id="t", goal="g", done="d", profile="sweep", overrides=overrides)
        plan = Plan(id="p1", tasks=[task])
        board = Board(self.home, mirror)
        integrate.init(plan, self.ws, board, "main")
        return run_task(plan, task, self.ws, board)

    def test_a_profile_tool_cap_stops_the_task(self):
        """S2-10: over the cap the agent is stopped and the row says it hit the tool cap."""
        row = self.run_one({"max_tool_calls": 2})
        self.assertEqual((row["state"], row["line"]), ("failed", "hit the tool cap of 2 calls"))

    def test_a_null_tool_cap_changes_nothing(self):
        row = self.run_one({"max_tool_calls": None})
        self.assertEqual((row["state"], row["line"]), ("done", "t tools"))

    def test_a_task_runs_with_sazed_unreachable(self):
        """S2-11: every post to Sazed fails, and the task still finishes on its own terms."""
        row = self.run_one({}, Mirror("http://127.0.0.1:1/api", "p1"))
        self.assertEqual((row["state"], row["line"]), ("done", "t tools"))

    def test_a_slow_mirror_never_stalls_the_agent(self):
        """S2-5: a mirror that takes a second per call never blocks reading the agent's output:
        the agent writes well past a pipe buffer and exits before the first reading is sent."""
        done = self.tmp / "done"
        windows = [{"five_hour": {"utilization": u}} for u in (0.1, 0.2, 0.3)]
        script = (
            "import json, time\n"
            f"for w in {windows!r}:\n"
            "    print(json.dumps({'type': 'rate_limit_event', "
            "'rate_limit_info': {'unifiedWindows': w}}), flush=True)\n"
            "for i in range(4000):\n"
            "    print(json.dumps({'type': 'system', 'pad': 'x' * 100}))\n"
            f"open({str(done)!r}, 'w').write(str(time.time()))\n"
        )
        mirror = SlowMirror()
        log = self.tmp / "agent.jsonl"
        started = time.time()
        run_agent._run(spec_for([sys.executable, "-c", script], self.tmp), log, mirror, "t")
        self.assertLess(float(done.read_text()) - started, 1.0)
        self.assertEqual(len(log.read_text().splitlines()), 4003)
        self.assertEqual(mirror.readings, 3)

    def test_the_unattended_spec_runs_as_the_agent_with_a_minimal_environment(self):
        """S2-12: the spec can describe the unattended launch (agent user, minimal environment
        with no worker key and nothing inherited), though nothing selects it yet."""
        os.environ["SAZED_WORKER_KEY"] = "worker-secret"
        os.environ["SEAM2_INHERITED"] = "1"
        self.addCleanup(os.environ.pop, "SAZED_WORKER_KEY")
        self.addCleanup(os.environ.pop, "SEAM2_INHERITED")
        cfg = registry.ROWS[3] | {"read_only": False}
        task_env = {"DISPATCH_TASK": "t", "DISPATCH_ENGINE": "claude"}
        hand = task_spec(cfg, self.tmp, "p", self.tmp / "s.json", False, None, task_env)
        unattended = dataclasses.replace(
            hand, env="minimal", env_extra=claude.extra_env() | task_env, as_agent=True
        )
        hand_argv, hand_env, hand_user = launch_args(hand)
        argv, env, user = launch_args(unattended)
        self.assertEqual(argv, hand_argv)
        self.assertEqual(
            set(env),
            {"HOME", "USER", "PATH", "LANG", "GIT_TERMINAL_PROMPT", "CLAUDE_CODE_OAUTH_TOKEN",
             "DISABLE_AUTOUPDATER", "DISPATCH_TASK", "DISPATCH_ENGINE"},
        )
        self.assertNotIn("worker-secret", env.values())
        self.assertEqual(user, as_agent())
        self.assertIn("SEAM2_INHERITED", hand_env)
        self.assertEqual(hand_user, {})

    def run_mode(self, mode: str, overrides: dict | None = None) -> dict:
        os.environ["DISPATCH_FAKE_MODES"] = json.dumps({"t": mode})
        return self.run_one(overrides or {})

    def test_every_tool_use_block_in_one_message_counts(self):
        self.assertEqual(self.run_mode("multi", {"max_tool_calls": 3})["state"], "failed")
        self.assertEqual(self.run_mode("multi", {"max_tool_calls": 4})["state"], "done")

    def test_sub_agent_and_mcp_calls_count_toward_the_cap(self):
        row = self.run_mode("subagent", {"max_tool_calls": 2})
        self.assertEqual(row["line"], "hit the tool cap of 2 calls")
        self.assertEqual(self.run_mode("subagent", {"max_tool_calls": 3})["state"], "done")

    def test_a_rejected_rate_limit_names_the_plan_limit(self):
        row = self.run_mode("ratelimit")
        self.assertEqual(
            (row["state"], row["line"]),
            ("failed", "hit Pryce's five hour plan limit; it resets at 2026-11-08 02:10 UTC"),
        )

    def test_a_harness_stop_reason_gets_its_own_line(self):
        for mode, line in (("refusal", "the model refused to continue"),
                           ("max_tokens", "stopped at the model's output token limit")):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_mode(mode)["line"], line)

    def test_shutdown_and_cancel_get_their_own_lines(self):
        board = Board(self.home)
        task = Task(id="t", goal="g", done="d")
        spec = task_spec({"model": "m", "timeout_s": 5}, self.tmp, "p", self.tmp / "s", True)
        for stopped, line in (("shutdown", "stopped because dispatch was shutting down"),
                              ("cancelled", "cancelled")):
            outcome = {"code": -15, "stop_reason": stopped, "rate_limited": None,
                       "result": None, "evidence": 0, "stderr": ""}
            row = run_agent._finish(task, self.ws, board, outcome, spec, True)
            self.assertEqual((row["state"], row["line"]), ("failed", line))

    def test_a_cap_that_is_not_a_whole_number_is_refused(self):
        for cap in ("2", 0, True, 1.5):
            with self.subTest(cap=cap), self.assertRaisesRegex(PlanError, "max_tool_calls"):
                resolve(Task(id="t", goal="g", done="d", profile="sweep",
                             overrides={"max_tool_calls": cap}), {})

    def test_a_failed_launch_stops_the_heartbeat(self):
        """The launch error surfaces, no beat follows it, and no sink thread survives."""
        mirror = CountingMirror()
        before = threading.active_count()
        os.environ["DISPATCH_CLAUDE"] = str(self.tmp / "no-such-claude")
        with mock.patch.object(run_agent, "HEARTBEAT_S", 0.05):
            with self.assertRaises(FileNotFoundError):
                run_agent._run(spec_for([str(self.tmp / "no-such-claude")], self.tmp),
                               self.tmp / "agent.jsonl", mirror, "t")
            beats = mirror.beats
            time.sleep(0.3)
        self.assertEqual(mirror.beats, beats)
        self.assertLessEqual(threading.active_count(), before)

    def test_a_descendant_holding_stdout_does_not_hang_the_task(self):
        """A process in its own session keeps the pipe open; the task still returns."""
        pid_file = self.tmp / "pid"
        script = (
            "import json, subprocess, sys\n"
            "child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
            " 'result': 'x', 'structured_output': {'state': 'done', 'summary': 'x'}}),"
            " flush=True)\n"
        )
        self.addCleanup(lambda: os.kill(int(pid_file.read_text()), signal.SIGKILL))
        started = time.monotonic()
        out = run_agent._run(spec_for([sys.executable, "-c", script], self.tmp),
                             self.tmp / "agent.jsonl", None, "t")
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(out["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
