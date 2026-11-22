"""Pins what each dispatch command does before and around its work (D7 W-5).

Written against the CLI before D7 and kept unchanged after it.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sazed_worker.dispatch import __main__ as cli
from sazed_worker.dispatch import integrate, mcp, profiles, unattended
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.unattended import ROOT_GIT_ENV, UnattendedWorkspace
from sazed_worker.dispatch.workspace import Workspace
from tests import registry

PLAN = {
    "id": "p1",
    "check": "true",
    "tasks": [
        {"id": "api", "goal": "g", "done": "d", "owns": ["src/api/**"]},
        {"id": "ui", "goal": "g", "done": "d", "owns": ["src/ui/**"]},
    ],
}
LAUNCHES = ("init", "run", "land", "go")
QUIET = ("board", "clean")


class CliPins(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-pins-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.plan = self.tmp / "plan.json"
        self.plan.write_text(json.dumps(PLAN))
        self.home = self.tmp / "home" / "p1"
        env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_SAZED_URL": "http://sazed.test/",
            "DISPATCH_SAZED_KEY": "console",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ROOT_GIT_ENV:
            os.environ.pop(name, None)
        registry.install()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        mcp._active = ({}, "sazed")
        self.addCleanup(os.umask, os.umask(0o077))
        self.events: list = []

    def umask(self) -> int:
        now = os.umask(0o077)
        os.umask(now)
        return now

    def run_cli(self, *argv: str) -> tuple[int, str]:
        events = self.events

        def note(name, answer=None):
            def call(*args, **kwargs):
                events.append((name, args, kwargs))
                return answer

            return call

        def boundary(name):
            return lambda *a: events.append((name, (*a, (self.home / "board").exists()), {}))

        out = io.StringIO()
        with contextlib.ExitStack() as patches:
            for target, name, value in (
                (profiles, "load", note("profiles")),
                (mcp, "load", note("mcp")),
                (unattended, "preflight", boundary("preflight")),
                (UnattendedWorkspace, "seal", boundary("seal")),
                (integrate, "init", note("init", "abc")),
                (integrate, "land", note("land", [{"task": "api"}])),
                (integrate, "go", note("go", {"api": "landed"})),
                (integrate, "export", note("export", "https://pr/1")),
                (integrate, "clean", note("clean")),
                (cli, "run_task", note("run", {"task": "x"})),
            ):
                patches.enter_context(mock.patch.object(target, name, value))
            patches.enter_context(contextlib.redirect_stdout(out))
            code = cli.main([argv[0], str(self.plan), "--repo", str(self.repo), *argv[1:]])
        return code, out.getvalue()

    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]

    def test_a_hand_run_loads_the_rules_and_never_enters_the_boundary(self):
        for command in (*LAUNCHES, "export", *QUIET):
            with self.subTest(command=command):
                self.events.clear()
                code, _ = self.run_cli(command)
                self.assertEqual(code, 0)
                loads = [("profiles", (False,), {}), ("mcp", (False,), {})]
                loads = [] if command in QUIET else loads
                self.assertEqual(self.events[: len(loads)], loads)
                calls = {"run": ["run", "run"], "board": []}.get(command, [command])
                self.assertEqual(self.names()[len(loads) :], calls)
                for _, args, _ in self.events[len(loads) :]:
                    ws = next(a for a in args if isinstance(a, Workspace))
                    board = next(a for a in args if hasattr(a, "mirror"))
                    self.assertIs(type(ws), Workspace)
                    self.assertEqual((ws.repo, ws.home), (self.repo.resolve(), self.home))
                    self.assertEqual(board.dir, self.home / "board")
                    self.assertEqual(
                        (board.mirror.base, board.mirror.key), ("http://sazed.test", "console")
                    )
                self.assertEqual(self.umask(), 0o077)
                self.assertFalse(set(ROOT_GIT_ENV) & set(os.environ))

    def test_an_unattended_launch_is_preflighted_before_the_board_and_sealed_after_it(self):
        for command in LAUNCHES:
            with self.subTest(command=command):
                self.events.clear()
                code, _ = self.run_cli(command, "--unattended")
                self.assertEqual(code, 0)
                calls = ["run", "run"] if command == "run" else [command]
                self.assertEqual(self.names(), ["profiles", "mcp", "preflight", "seal", *calls])
                self.assertEqual([e[1] for e in self.events[:2]], [(True,), (True,)])
                if command == LAUNCHES[0]:
                    self.assertFalse(self.events[2][1][-1])
                self.assertTrue(self.events[3][1][-1])
                args = self.events[4][1]
                ws = next(a for a in args if isinstance(a, Workspace))
                self.assertIs(type(ws), UnattendedWorkspace)
                board = next(a for a in args if hasattr(a, "mirror"))
                self.assertEqual(
                    (board.mirror.base, board.mirror.key), ("http://sazed.test", "console")
                )
                self.assertEqual(self.umask(), 0o022)
                self.assertEqual({k: os.environ.get(k) for k in ROOT_GIT_ENV}, ROOT_GIT_ENV)

    def test_unattended_export_board_and_clean_skip_preflight(self):
        for command in ("export", *QUIET):
            with self.subTest(command=command):
                self.events.clear()
                code, _ = self.run_cli(command, "--unattended")
                self.assertEqual(code, 0)
                self.assertNotIn("preflight", self.names())
                self.assertNotIn("seal", self.names())
                self.assertEqual(self.umask(), 0o077)

    def test_each_command_passes_its_flags_and_defaults(self):
        config = {"unattended": False}
        cases = {
            ("init",): ({}, ("main", config)),
            ("go",): ({}, ("main", config, 3)),
            ("go", "--base", "dev", "--parallel", "1"): ({}, ("dev", config, 1)),
            ("land",): ({}, (config,)),
            ("export",): ({"open_pr": True}, ("main", "p1", None, config)),
            ("export", "--base", "dev", "--title", "t", "--body", "b", "--no-pr"): (
                {"open_pr": False},
                ("dev", "t", "b", config),
            ),
        }
        for argv, (kwargs, tail) in cases.items():
            with self.subTest(argv=argv):
                self.events.clear()
                self.run_cli(*argv)
                _, args, seen = self.events[-1]
                self.assertEqual((args[0].id, args[3:], seen), ("p1", tail, kwargs))
        self.events.clear()
        self.run_cli("run", "--task", "ui")
        self.assertEqual([a[1].id for n, a, _ in self.events if n == "run"], ["ui"])

    def test_what_each_command_prints(self):
        self.assertEqual(self.run_cli("init"), (0, "abc\n"))
        self.assertEqual(self.run_cli("land"), (0, '{"task": "api"}\n'))
        self.assertEqual(self.run_cli("go"), (0, '{\n  "api": "landed"\n}\n'))
        self.assertEqual(self.run_cli("export"), (0, "https://pr/1\n"))
        self.assertEqual(self.run_cli("run"), (0, '{"task": "x"}\n' * 2))

    def test_a_plan_that_does_not_validate_is_refused_after_the_rules_load(self):
        self.plan.write_text(json.dumps({"id": "p1", "tasks": []}))
        code, out = self.run_cli("go", "--unattended")
        self.assertEqual(
            (code, out), (1, "refused: the plan doesn't validate:\nthe plan has no tasks\n")
        )
        self.assertEqual(self.names(), ["profiles", "mcp"])

    def test_the_cli_runs_the_engine_s_run_task(self):
        self.assertIs(cli.run_task, run_task)


if __name__ == "__main__":
    unittest.main()
