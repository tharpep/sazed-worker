"""The PR diff a review prompt carries, since read-only reviewers can't run git themselves."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sazed_worker import pr_checks
from sazed_worker.agent_process import JobFailed


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _plain_git(*args: str, cwd: Path, token: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise JobFailed(proc.stderr.strip())
    return proc.stdout


class PrDiff(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        origin = tmp / "origin"
        origin.mkdir()
        _git("init", "-q", "-b", "main", cwd=origin)
        _git("config", "user.email", "t@t", cwd=origin)
        _git("config", "user.name", "t", cwd=origin)
        (origin / "app.py").write_text("def f():\n    return 1\n")
        _git("add", "app.py", cwd=origin)
        _git("commit", "-q", "-m", "seed", cwd=origin)
        _git("clone", "-q", str(origin), str(tmp / "repo"), cwd=tmp)
        self.repo = tmp / "repo"
        _git("config", "user.email", "t@t", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        _git("checkout", "-q", "-b", "pr-7", cwd=self.repo)
        (self.repo / "app.py").write_text("def f():\n    return 2\n")
        _git("commit", "-q", "-am", "change", cwd=self.repo)
        self.job = {"id": "j1", "params": {"pr_number": 7}}
        patcher = mock.patch.object(pr_checks, "git", _plain_git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_section_carries_the_stat_and_the_diff(self):
        section = pr_checks.pr_diff(self.job, self.repo)
        self.assertIn("```diff", section)
        self.assertIn("app.py | 2 +-", section)
        self.assertIn("+    return 2", section)
        self.assertTrue(section.endswith("---\n\n"))

    def test_a_long_diff_is_cut_on_a_line_and_says_so(self):
        with mock.patch.object(pr_checks, "_DIFF_CAP", 60):
            section = pr_checks.pr_diff(self.job, self.repo)
        self.assertIn("The diff is cut at 0k characters", section)
        self.assertNotIn("+    return 2", section)

    def test_no_merge_base_gives_no_section(self):
        self.job["params"]["pr_number"] = 99
        self.assertEqual(pr_checks.pr_diff(self.job, self.repo), "")


if __name__ == "__main__":
    unittest.main()
