"""Pins an unattended task's launch: argv, environment, user and prompt (S3-7, S3-8, S3-9)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sazed_worker.adapters import claude
from sazed_worker.agent_process import as_agent
from sazed_worker.dispatch import integrate, mcp, run_agent
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.unattended import CHECK_TOOL, UnattendedWorkspace
from sazed_worker.dispatch.workspace import git
from sazed_worker.run import launch_args
from tests import registry

SCHEMA = (
    '{"type": "object", "properties": {"state": {"enum": ["done", "waiting"]}, '
    '"summary": {"type": "string"}, "question": {"type": "string"}}, '
    '"required": ["state", "summary"]}'
)
SECRET_READS = (
    "Read(~/.claude/**),Read(~/.claude.json),Read(~/.gemini/**),Read(//proc/**),"
    "Read(//etc/sazed-eco/**)"
)
WRITABLE_PROMPT = """You are one task in the dispatch plan p1. Task w: g

Done means: d
You own these paths, and only these: src/**
Shared interfaces, which you read and never edit: none
The check that must pass once your work lands: sh check.sh

Rules:
- Work only in this directory. You have no shell. Call the run_check tool to test your work: \
it takes no arguments and runs the check above. Do not commit: when you finish with state done, \
your changes inside your paths are committed for you, and anything else is discarded.
- If you need a change outside your paths, stop and finish with state waiting, naming the change.
- Finish with the structured result: state done with a one-line summary, or state waiting with
  the question you need answered."""
READ_ONLY_RULE = (
    "- Work only in this directory. You have no shell. The run_check tool runs the check above; "
    "it takes no arguments.\n"
)


class UnattendedLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seam3b-pins-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "seed", cwd=self.repo)  # fmt: skip
        old = {k: os.environ.get(k) for k in ("DISPATCH_HOME", "DISPATCH_CLAUDE")}
        os.environ["DISPATCH_HOME"] = str(self.tmp / "home")
        os.environ["DISPATCH_CLAUDE"] = "claude"
        self.addCleanup(self.restore, old)
        os.environ["SAZED_WORKER_KEY"] = "worker-secret"
        os.environ["SEAM3_INHERITED"] = "1"
        self.addCleanup(os.environ.pop, "SAZED_WORKER_KEY")
        self.addCleanup(os.environ.pop, "SEAM3_INHERITED")
        registry.install()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        mcp._active = (
            {"context7": {"name": "context7", "transport": "http", "url": "https://c7.test/mcp"}},
            "sazed",
        )
        self.home = self.tmp / "home" / "p1"
        self.ws = UnattendedWorkspace(self.repo, "p1", self.home)

    def restore(self, old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def launch(self, task: Task) -> tuple[list[str], dict, dict, Plan]:
        plan = Plan(id="p1", tasks=[task], check="sh check.sh")
        board = Board(self.home)
        integrate.init(plan, self.ws, board, "main", {"unattended": True})
        specs = []
        outcome = {"code": 0, "stop_reason": "cancelled", "rate_limited": None, "result": None,
                   "evidence": 0, "stderr": ""}  # fmt: skip

        def record(spec, *args):
            specs.append(spec)
            return outcome

        with mock.patch.object(run_agent, "_run", side_effect=record):
            run_task(plan, task, self.ws, board, {"unattended": True})
        argv, env, user = launch_args(specs[0])
        self.assertEqual(Path(specs[0].cwd), self.ws.task_dir(task.id))
        return argv, env, user, plan

    def test_a_writable_task_has_the_edit_tools_and_run_check_only(self):
        grants = {"mcp": {"context7": ["query-docs"]}}
        argv, env, user, plan = self.launch(
            Task(id="w", goal="g", done="d", owns=["src/**"], overrides=grants)
        )
        run_dir = self.home / "runs" / "w"
        self.assertEqual(
            argv,
            [
                "claude", "-p", "--output-format", "stream-json", "--verbose",
                "--model", "sonnet", "--effort", "high",
                "--settings", str(run_dir / "agent-settings.json"),
                "--setting-sources", "project", "--strict-mcp-config",
                "--permission-mode", "dontAsk",
                "--tools", "Read,Grep,Glob,Edit,Write",
                "--max-budget-usd", "2.0", "--json-schema", SCHEMA,
                "--disallowedTools", SECRET_READS,
                "--mcp-config", str(run_dir / "mcp.json"),
                "--allowedTools",
                "Read,Grep,Glob,Edit(./**),Write(./**),mcp__check__run_check",
                "--", WRITABLE_PROMPT,
            ],
        )  # fmt: skip
        self.assertEqual(
            set(env),
            {"HOME", "USER", "PATH", "LANG", "GIT_TERMINAL_PROMPT", "CLAUDE_CODE_OAUTH_TOKEN",
             "DISABLE_AUTOUPDATER", "DISPATCH_TASK", "DISPATCH_OWNS", "DISPATCH_WORKTREE",
             "DISPATCH_ENGINE"},
        )  # fmt: skip
        self.assertNotIn("worker-secret", env.values())
        self.assertEqual(user, as_agent())
        servers = json.loads((run_dir / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(
            servers,
            {"check": {"command": sys.executable,
                       "args": ["-I", str(CHECK_TOOL), "--spec", str(run_dir / "check.json")],
                       "env": {"SAZED_AGENT_USER": os.environ["SAZED_AGENT_USER"]}}},
        )  # fmt: skip
        check = json.loads((run_dir / "check.json").read_text())
        self.assertEqual((check["check"], check["timeout_s"]), ("sh check.sh", 1200))
        settings = json.loads((run_dir / "agent-settings.json").read_text())
        self.assertNotIn("Bash", json.dumps(settings))
        self.assertEqual([h["matcher"] for h in settings["hooks"]["PreToolUse"]], ["Edit|Write"])

    def test_a_read_only_task_has_the_read_tools_and_run_check_only(self):
        argv, _, _, plan = self.launch(Task(id="r", goal="g", done="d", profile="sweep"))
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,mcp__check__run_check"
        )
        self.assertIn(READ_ONLY_RULE, argv[-1])

    def test_the_token_reaches_claude_and_only_claude(self):
        with mock.patch.object(claude, "CLAUDE_TOKEN", "sk-ant-pin"):
            _, env, _, _ = self.launch(Task(id="w", goal="g", done="d", owns=["src/**"]))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-pin")
        check = (self.home / "runs" / "w" / "check.json").read_text()
        self.assertNotIn("sk-ant", check)


if __name__ == "__main__":
    unittest.main()
