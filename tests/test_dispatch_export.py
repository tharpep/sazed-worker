"""Dispatch export with the fake agent: the hand path's git and gh calls stay pinned."""

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sazed_worker.dispatch import integrate
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.workspace import Workspace, git
from tests import registry
from tests.test_dispatch_engine import CHECK, FAKE, _plan

GH = [sys.executable, "-c", "print('https://pr/1')"]
GIT_READS = {"log", "merge-base", "rev-parse", "diff", "cat-file", "remote"}
PLAN_JSON = {
    "id": "p1",
    "check": CHECK,
    "interface_files": {"src/shared/types.txt": "shared\n"},
    "tasks": [
        {"id": "api", "goal": "build the api", "done": "api file", "owns": ["src/api/**"]},
        {
            "id": "ui",
            "goal": "build the ui",
            "done": "ui file",
            "owns": ["src/ui/**"],
            "depends_on": ["api"],
        },
        {"id": "docs", "goal": "write docs", "done": "docs file", "owns": ["docs/**"]},
    ],
}


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-export-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
        ):
            git(*args, cwd=self.repo)
        (self.repo / "check.sh").write_text("! grep -rqs --include=*.txt FAIL .\n")
        (self.repo / "README.md").write_text("# repo\n")
        git("add", "check.sh", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "seed", cwd=self.repo)
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": f"{sys.executable} {FAKE}",
            "DISPATCH_AGY": f"{sys.executable} {FAKE}",
            "DISPATCH_FAKE_MODES": "{}",
            "DISPATCH_GH": shlex.join(GH),
        }
        old = {k: os.environ.get(k) for k in (*env, "DISPATCH_SAZED_URL")}
        self.addCleanup(self._restore, old)
        os.environ.pop("DISPATCH_SAZED_URL", None)
        os.environ.update(env)
        registry.install()
        registry.write_cache(self.tmp / "home")
        self.home = self.tmp / "home" / "p1"
        self.ws = Workspace(self.repo, "p1", self.home)
        self.board = Board(self.home)
        self.plan_file = self.tmp / "plan.json"
        self.plan_file.write_text(json.dumps(PLAN_JSON))

    @staticmethod
    def _restore(old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([*args, str(self.plan_file), "--repo", str(self.repo)])
        return code, out.getvalue()

    def recorded_export(self, *args: str) -> tuple[int, str, list[list[str]]]:
        """Runs export through the CLI and returns every git and gh argv that is not a read."""
        calls: list[list[str]] = []
        real = subprocess.run

        def record(argv, *a, **kw):
            calls.append(list(argv))
            return real(argv, *a, **kw)

        with mock.patch("subprocess.run", side_effect=record):
            code, out = self.cli("export", *args)
        acting = [c for c in calls if not (c[0] == "git" and c[1] in GIT_READS)]
        return code, out, acting


class HandExport(ExportCase):
    def test_a_hand_export_pushes_integ_then_opens_one_draft_pr_with_gh(self):
        """Pins the argv, in order, of every git and gh call a hand export makes that is not a
        read: one push of integ/<plan>, then one `gh pr create --draft`."""
        integrate.go(_plan(), self.ws, self.board, "main")
        code, out, acting = self.recorded_export()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "https://pr/1")
        self.assertEqual(
            acting,
            [
                ["git", "push", "-q", "-u", "origin", "integ/p1"],
                [
                    *GH,
                    "pr",
                    "create",
                    "--draft",
                    "--base",
                    "main",
                    "--head",
                    "integ/p1",
                    "--title",
                    "p1",
                    "--body",
                    "Dispatch plan p1.",
                ],
            ],
        )
        self.assertTrue(git("rev-parse", "--verify", "-q", "refs/heads/integ/p1", cwd=self.remote))


if __name__ == "__main__":
    unittest.main()
