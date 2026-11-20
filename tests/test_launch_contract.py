"""The adapter contract both launchers share: spec, normalized events, stop reasons, result."""

import dataclasses
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from sazed_worker.adapters import claude
from sazed_worker.adapters.contract import (
    STOP_REASONS,
    PermissionDenied,
    RunSpec,
    ToolCall,
)
from sazed_worker.agent_process import as_agent
from sazed_worker.run import Run, launch_args

ROOT = Path(__file__).resolve().parents[1]


class Quiet:
    def event(self, line, event, saved):
        return False

    def beat(self, force):
        return False

    def close(self):
        return False


def spec(**fields) -> RunSpec:
    base = {
        "engine": "claude",
        "binary": ("claude",),
        "prompt": "p",
        "model": "haiku",
        "cwd": Path("/tmp"),
        "permission_mode": "acceptEdits",
        "timeout_s": 30,
        "env": "inherited",
        "env_extra": {},
        "as_agent": False,
        "argv_layout": "dispatch",
        "setting_sources": "project",
    }
    return RunSpec(**(base | fields))


def tool_use(i: int, name: str = "Read") -> dict:
    block = {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {}}
    return {"type": "assistant", "message": {"content": [block]}}


class Spec(unittest.TestCase):
    def test_permission_mode_has_no_default(self):
        field = {f.name: f for f in dataclasses.fields(RunSpec)}["permission_mode"]
        self.assertIs(field.default, dataclasses.MISSING)
        with self.assertRaises(TypeError):
            RunSpec(engine="claude", binary=("c",), prompt="p", model="m", cwd=Path("."),
                    timeout_s=1, env="minimal", env_extra={}, as_agent=False,
                    argv_layout="job")

    def test_each_adapter_refuses_a_mode_it_cannot_set(self):
        with self.assertRaises(ValueError):
            launch_args(spec(permission_mode="bypassPermissions"))
        with self.assertRaises(ValueError):
            launch_args(spec(engine="agy", permission_mode="dontAsk"))

    def test_claude_always_isolates_settings_and_mcp(self):
        """Every claude launch names its setting sources and refuses the operator's MCP servers;
        a spec that leaves the sources out is refused."""
        for layout in ("job", "dispatch"):
            argv = launch_args(spec(argv_layout=layout))[0]
            self.assertIn("--strict-mcp-config", argv)
            self.assertTrue(any(a.startswith("--setting-sources") for a in argv))
        with self.assertRaises(ValueError):
            launch_args(spec(setting_sources=None))

    def test_the_user_switch_is_only_attempted_when_the_spec_asks(self):
        self.assertEqual(launch_args(spec())[2], {})
        self.assertEqual(launch_args(spec(as_agent=True))[2], as_agent())

    def test_imports_without_agent_user(self):
        """S2-6: the adapters, the runner and dispatch import on a machine with no agent user;
        the lookup happens only when an agent path is used."""
        code = (
            "import sazed_worker.adapters, sazed_worker.run, sazed_worker.dispatch.run_agent\n"
            "from sazed_worker import config\n"
            "try:\n    config.AGENT_HOME\nexcept KeyError:\n    print('lookup deferred')\n"
        )
        env = os.environ | {"SAZED_AGENT_USER": "seam2-no-such-user"}
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
        )
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "lookup deferred"), out.stderr)


class Recording(Run):
    def __init__(self):
        super().__init__(Quiet())
        self.seen = []

    def emit(self, event):
        self.seen.append(event)
        super().emit(event)


class Events(unittest.TestCase):
    def setUp(self):
        self.run = Recording()
        self.run.spec = spec(max_tool_calls=3)

    def test_a_tool_use_comes_out_as_a_normalized_tool_call(self):
        claude.handle_event(self.run, tool_use(1, "Grep"))
        self.assertEqual(self.run.seen, [ToolCall("toolu_1", "Grep", "search", "pending")])
        self.assertEqual((self.run.tool_calls, self.run.tools_used), (1, ["Grep"]))

    def test_the_cap_counts_tool_calls(self):
        for i in range(2):
            claude.handle_event(self.run, tool_use(i))
        self.assertIsNone(self.run.stop_reason)
        claude.handle_event(self.run, tool_use(2))
        self.assertEqual(self.run.stop_reason, "tool_cap")

    def test_a_null_cap_means_no_cap(self):
        self.run.spec = spec(max_tool_calls=None)
        for i in range(500):
            claude.handle_event(self.run, tool_use(i))
        self.assertIsNone(self.run.stop_reason)

    def test_the_result_is_typed_and_a_denial_is_also_an_event(self):
        denial = {"tool_name": "Write", "tool_use_id": "toolu_9", "tool_input": {}}
        claude.handle_event(self.run, {
            "type": "result", "subtype": "success", "is_error": False, "result": " ok ",
            "session_id": "s1", "num_turns": 3, "duration_ms": 1500, "total_cost_usd": 0.25,
            "usage": {"output_tokens": 5}, "structured_output": {"state": "done"},
            "permission_denials": [denial],
        })
        self.assertEqual(self.run.result, {
            "response": "ok", "ok": True, "error": None, "usage": {"output_tokens": 5},
            "turns": 3, "duration_seconds": 1.5, "session_id": "s1", "cost_usd": 0.25,
            "structured_output": {"state": "done"}, "denied": [denial],
        })
        self.assertEqual(self.run.seen, [PermissionDenied("Write", "toolu_9")])


class StopReasons(unittest.TestCase):
    def test_stop_reasons_are_one_closed_set(self):
        """S2-3: the five reasons in use plus budget, refusal and max_tokens; an unknown reason
        raises, the first reason wins, and no code sets stop_reason except Run.stop."""
        self.assertEqual(STOP_REASONS, {"cancelled", "timed_out", "tool_cap", "shutdown",
                                        "rate_limit", "budget", "refusal", "max_tokens"})
        run = Run(Quiet())
        with self.assertRaises(ValueError):
            run.stop("bored")
        run.stop("timed_out")
        run.stop("tool_cap")
        self.assertEqual(run.stop_reason, "timed_out")
        for path in (ROOT / "sazed_worker").rglob("*.py"):
            text = path.read_text()
            for reason in re.findall(r"\.stop\(\"([a-z_]+)\"\)", text):
                self.assertIn(reason, STOP_REASONS, f"{path}: {reason}")
            assigned = re.findall(r"stop_reason\s*=(?!=)", text)
            expected = 1 if path.name == "run.py" else 0
            self.assertEqual(len(assigned), expected, f"{path} sets stop_reason directly")

    def test_a_harness_ending_applies_after_exit_without_a_kill(self):
        """Only a spec that asks for harness stops gets one; worker jobs keep none (S2-7)."""
        event = ('{"type": "result", "subtype": "error_max_budget_usd", "is_error": true, '
                 '"total_cost_usd": 2.1}')
        code = f"import sys, time; print({event!r}, flush=True); time.sleep(0.5); sys.exit(1)"
        for asked, expected in ((True, "budget"), (False, None)):
            run = Run(Quiet())
            exit_code = run.run(spec(binary=(sys.executable, "-c", code), harness_stops=asked))
            self.assertEqual((exit_code, run.stop_reason), (1, expected))
            self.assertEqual(run.result["cost_usd"], 2.1)


if __name__ == "__main__":
    unittest.main()
