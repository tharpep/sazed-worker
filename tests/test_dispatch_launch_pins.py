"""Pins how a hand dispatch run starts its agent: argv, environment, user and cwd (S2-9).

Written against the launch code before seam 2 and kept unchanged after it.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from sazed_worker.dispatch import integrate, mcp
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.run_agent import prompt_for, run_task
from sazed_worker.dispatch.workspace import Workspace, git
from tests import registry

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"
SCHEMA = (
    '{"type": "object", "properties": {"state": {"enum": ["done", "waiting"]}, '
    '"summary": {"type": "string"}, "question": {"type": "string"}}, '
    '"required": ["state", "summary"]}'
)
RECORDER = """import json, os, runpy, sys
with open(os.environ["SEAM2_RECORD"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
                        "uid": os.getuid()}) + "\\n")
runpy.run_path(sys.argv[0].replace("recorder.py", "fake.py"), run_name="__main__")
"""


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
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "recorder.py").write_text(RECORDER)
        (bin_dir / "fake.py").write_text(FAKE.read_text())
        self.record = self.tmp / "record.jsonl"
        agent = f"{sys.executable} {bin_dir / 'recorder.py'}"
        env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": agent,
            "DISPATCH_AGY": agent,
            "DISPATCH_FAKE_MODES": "{}",
            "SEAM2_RECORD": str(self.record),
        }
        old = {k: os.environ.get(k) for k in [*env, "DISPATCH_SAZED_URL"]}
        os.environ.pop("DISPATCH_SAZED_URL", None)
        os.environ.update(env)
        self.addCleanup(self.restore, old)
        registry.install()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        mcp._active = (
            {"context7": {"name": "context7", "transport": "http", "url": "https://c7.test/mcp"}},
            "sazed",
        )
        self.home = self.tmp / "home" / "p1"
        self.ws = Workspace(self.repo, "p1", self.home)
        self.board = Board(self.home)

    def restore(self, old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def launch(self, task: Task) -> tuple[dict, Plan]:
        plan = Plan(id="p1", tasks=[task], check="true")
        integrate.init(plan, self.ws, self.board, "main")
        run_task(plan, task, self.ws, self.board)
        lines = self.record.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0]), plan

    def expect_env(self, task: Task, engine: str) -> dict:
        return dict(os.environ) | {
            "DISPATCH_TASK": task.id,
            "DISPATCH_OWNS": json.dumps(task.owns),
            "DISPATCH_WORKTREE": str(self.ws.task_dir(task.id)),
            "DISPATCH_ENGINE": engine,
        }

    def check_process(self, seen: dict, task: Task, engine: str) -> None:
        self.assertEqual(seen["env"], self.expect_env(task, engine))
        self.assertEqual(Path(seen["cwd"]).resolve(), self.ws.task_dir(task.id).resolve())
        self.assertEqual(seen["uid"], os.getuid())

    def test_a_writable_claude_task(self):
        task = Task(id="w", goal="g", done="d", owns=["src/**"], profile="implement")
        seen, plan = self.launch(task)
        settings = str(self.home / "runs" / "w" / "agent-settings.json")
        self.assertEqual(
            seen["argv"],
            [
                "-p", "--output-format", "stream-json", "--verbose",
                "--model", "sonnet", "--effort", "high",
                "--settings", settings, "--setting-sources", "project",
                "--strict-mcp-config", "--permission-mode", "acceptEdits",
                "--max-budget-usd", "2.0", "--json-schema", SCHEMA,
                "--", prompt_for(plan, task, False, "true"),
            ],
        )
        self.check_process(seen, task, "claude")

    def test_a_read_only_claude_task(self):
        task = Task(id="r", goal="g", done="d", profile="sweep")
        seen, plan = self.launch(task)
        settings = str(self.home / "runs" / "r" / "agent-settings.json")
        self.assertEqual(
            seen["argv"],
            [
                "-p", "--output-format", "stream-json", "--verbose",
                "--model", "haiku", "--effort", "max",
                "--settings", settings, "--setting-sources", "project",
                "--strict-mcp-config", "--permission-mode", "acceptEdits",
                "--max-budget-usd", "0.5", "--json-schema", SCHEMA,
                "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,Bash",
                "--", prompt_for(plan, task, True, "true"),
            ],
        )
        self.check_process(seen, task, "claude")

    def test_a_task_with_mcp_grants(self):
        grants = {"mcp": {"context7": ["resolve-library-id", "query-docs"]}}
        task = Task(id="m", goal="g", done="d", profile="sweep", overrides=grants)
        seen, plan = self.launch(task)
        run_dir = self.home / "runs" / "m"
        self.assertEqual(
            seen["argv"],
            [
                "-p", "--output-format", "stream-json", "--verbose",
                "--model", "haiku", "--effort", "max",
                "--settings", str(run_dir / "agent-settings.json"),
                "--setting-sources", "project",
                "--strict-mcp-config", "--permission-mode", "acceptEdits",
                "--max-budget-usd", "0.5", "--json-schema", SCHEMA,
                "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,Bash",
                "--mcp-config", str(run_dir / "mcp.json"),
                "--allowedTools", "mcp__context7__resolve-library-id,mcp__context7__query-docs",
                "--", prompt_for(plan, task, True, "true"),
            ],
        )
        self.check_process(seen, task, "claude")

    def test_an_agy_task(self):
        task = Task(id="a", goal="g", done="d", owns=["src/**"], profile="flash")
        seen, plan = self.launch(task)
        self.assertEqual(
            seen["argv"],
            [
                "--output-format=stream-json",
                "--model=gemini-3.8-flash-high",
                "--effort=high",
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
                "--json-schema",
                SCHEMA,
                f"--prompt={prompt_for(plan, task, False, 'true')}",
            ],
        )
        self.check_process(seen, task, "agy")


if __name__ == "__main__":
    unittest.main()
