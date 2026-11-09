"""Dispatch's engine end to end with the fake agent (plans/17 D3): lands clean, stops on red."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sazed_worker.dispatch import integrate
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.workspace import Workspace, git

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"
CHECK = "sh check.sh"


def _plan(**overrides) -> Plan:
    tasks = [
        Task(id="api", goal="build the api", done="api file", owns=["src/api/**"]),
        Task(id="ui", goal="build the ui", done="ui file", owns=["src/ui/**"], depends_on=["api"]),
        Task(id="docs", goal="write docs", done="docs file", owns=["docs/**"]),
    ]
    for t in tasks:
        if t.id in overrides:
            object.__setattr__(t, "overrides", overrides[t.id])
    return Plan(
        id="p1",
        tasks=tasks,
        check=CHECK,
        interfaces=["src/shared/types.txt"],
        interface_files={"src/shared/types.txt": "shared\n"},
    )


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-test-"))
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
        self.main_sha = git("rev-parse", "main", cwd=self.repo)
        self.env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": f"{sys.executable} {FAKE}",
            "DISPATCH_AGY": f"{sys.executable} {FAKE}",
            "DISPATCH_FAKE_MODES": "{}",
        }
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.home = self.tmp / "home" / "p1"
        self.ws = Workspace(self.repo, "p1", self.home)
        self.board = Board(self.home)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def modes(self, **modes):
        os.environ["DISPATCH_FAKE_MODES"] = json.dumps(modes)

    def integ_files(self) -> set[str]:
        out = git("ls-tree", "-r", "--name-only", "integ/p1", cwd=self.repo)
        return set(out.splitlines())

    def test_three_tasks_with_a_dependency_land_clean_and_main_is_untouched(self):
        states = integrate.go(_plan(), self.ws, self.board, "main")
        self.assertEqual(states, {"api": "landed", "ui": "landed", "docs": "landed"})
        self.assertTrue(
            {"src/api/api.txt", "src/ui/ui.txt", "docs/docs.txt", "src/shared/types.txt"}
            <= self.integ_files()
        )
        self.assertEqual(git("rev-parse", "main", cwd=self.repo), self.main_sha)
        ui_base = git("log", "--format=%s", "integ/p1", cwd=self.repo).splitlines()
        self.assertLess(ui_base.index("ui: work"), ui_base.index("api: work"))

    def test_a_task_that_leaves_its_owns_fails_at_land_and_its_dependents_never_start(self):
        self.modes(api="outside")
        states = integrate.go(_plan(), self.ws, self.board, "main")
        self.assertEqual(states["api"], "failed")
        self.assertIn("left its owns: README.md", self.board.rows("api")[-1]["line"])
        self.assertEqual(states["ui"], "not started")
        self.assertNotIn("README.md strayed", (self.ws.integ_dir / "README.md").read_text())

    def test_a_failing_check_stops_landing_and_rolls_integ_back(self):
        self.modes(docs="failcheck")
        integrate.go(_plan(), self.ws, self.board, "main")
        self.assertEqual(self.board.state("docs"), "failed")
        self.assertIn("check failed", self.board.rows("docs")[-1]["line"])
        self.assertNotIn("docs/docs.txt", self.integ_files())
        self.assertEqual(self.board.state("api"), "landed")

    def test_done_with_no_commits_is_a_failure(self):
        self.modes(docs="nocommit")
        integrate.go(_plan(), self.ws, self.board, "main")
        self.assertIn("made no commits", self.board.rows("docs")[-1]["line"])

    def test_flash_task_runs_with_agy_and_lands_clean(self):
        """Proves a task with the flash profile runs under agy, commits, and lands."""
        task = Task(
            id="api",
            goal="build api",
            done="api file",
            owns=["src/api/**"],
            profile="flash",
        )
        plan = Plan(id="p1", tasks=[task], check=CHECK)
        states = integrate.go(plan, self.ws, self.board, "main")
        self.assertEqual(states, {"api": "landed"})
        self.assertIn("src/api/api.txt", self.integ_files())
        wt_hooks = self.ws.task_dir("api") / ".agents" / "hooks.json"
        self.assertFalse(wt_hooks.exists())

    def test_a_waiting_task_carries_its_question_and_does_not_land(self):
        self.modes(docs="waiting")
        states = integrate.go(_plan(), self.ws, self.board, "main")
        self.assertEqual(states["docs"], "waiting")
        self.assertIn("shared type", self.board.rows("docs")[-1]["line"])

    def test_a_hung_agent_is_killed_at_its_timeout(self):
        self.modes(docs="hang")
        plan = _plan(docs={"timeout_s": 2})
        integrate.init(plan, self.ws, self.board, "main")
        row = run_task(plan, plan.tasks[2], self.ws, self.board)
        self.assertEqual(row["state"], "failed")
        self.assertIn("timed out after 2 s", row["line"])

    def test_a_task_waits_for_its_dependency_to_land(self):
        plan = _plan()
        integrate.init(plan, self.ws, self.board, "main")
        row = run_task(plan, plan.tasks[1], self.ws, self.board)
        self.assertEqual((row["state"], row["line"]), ("blocked", "waiting on api to land"))

    def test_export_pushes_integ_and_opens_one_draft_pr(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.repo)
        plan = _plan()
        with self.assertRaises(integrate.IntegrateError):
            integrate.init(plan, self.ws, self.board, "main")
            integrate.export(plan, self.ws, self.board, "main", "t", "b")
        integrate.go(plan, self.ws, self.board, "main")
        os.environ["DISPATCH_GH"] = f"{sys.executable} -c \"print('https://pr/1')\""
        try:
            url = integrate.export(plan, self.ws, self.board, "main", "Plan p1", "body")
        finally:
            os.environ.pop("DISPATCH_GH")
        self.assertEqual(url, "https://pr/1")
        self.assertTrue(git("rev-parse", "--verify", "-q", "refs/heads/integ/p1", cwd=remote))
        self.assertFalse(
            git("rev-parse", "--verify", "-q", "refs/heads/main", cwd=remote, check=False)
        )

    def test_clean_removes_worktrees_and_refuses_while_running(self):
        plan = _plan()
        integrate.go(plan, self.ws, self.board, "main")
        self.board.write("docs", "running", "pretend")
        with self.assertRaises(integrate.IntegrateError):
            integrate.clean(plan, self.ws, self.board)
        self.board.write("docs", "done", "ok")
        integrate.clean(plan, self.ws, self.board)
        self.assertFalse(self.ws.task_dir("api").exists())
        self.assertEqual(self.board.rows("api")[-1]["state"], "cleaned")

    def _plan_with_check(self, check: str) -> Plan:
        plan = _plan()
        object.__setattr__(plan, "check", check)
        return plan

    def test_init_accepts_a_check_that_finds_no_tests(self):
        integrate.init(self._plan_with_check("exit 5"), self.ws, self.board, "main")
        self.assertIn("src/shared/types.txt", self.integ_files())

    def test_a_refused_init_leaves_no_integ_branch_or_worktree_and_can_retry(self):
        with self.assertRaises(integrate.IntegrateError):
            integrate.init(self._plan_with_check("exit 1"), self.ws, self.board, "main")
        self.assertFalse(self.ws.integ_exists())
        self.assertFalse(self.ws.integ_dir.exists())
        self.assertNotIn("integ", git("worktree", "list", cwd=self.repo))
        integrate.init(_plan(), self.ws, self.board, "main")
        self.assertIn("src/shared/types.txt", self.integ_files())

    def test_clean_removes_the_integ_worktree_and_branch(self):
        plan = _plan()
        integrate.go(plan, self.ws, self.board, "main")
        integrate.clean(plan, self.ws, self.board)
        self.assertFalse(self.ws.integ_exists())
        self.assertFalse(self.ws.integ_dir.exists())
        self.assertNotIn("integ", git("worktree", "list", cwd=self.repo))

    def test_a_cleaned_plan_runs_again_from_scratch(self):
        plan = _plan()
        integrate.go(plan, self.ws, self.board, "main")
        integrate.clean(plan, self.ws, self.board)
        states = integrate.go(plan, self.ws, self.board, "main")
        self.assertEqual(states, {"api": "landed", "ui": "landed", "docs": "landed"})

    def test_the_cli_runs_a_plan_and_shows_the_board(self):
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(
            json.dumps(
                {
                    "id": "p1",
                    "check": CHECK,
                    "tasks": [{"id": "api", "goal": "g", "done": "d", "owns": ["src/api/**"]}],
                }
            )
        )
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["go", str(plan_file), "--repo", str(self.repo)]), 0)
            self.assertEqual(main(["board", str(plan_file), "--repo", str(self.repo)]), 0)
        self.assertIn('"api": "landed"', out.getvalue())
        self.assertRegex(out.getvalue(), r"api\s+landed")


if __name__ == "__main__":
    unittest.main()
